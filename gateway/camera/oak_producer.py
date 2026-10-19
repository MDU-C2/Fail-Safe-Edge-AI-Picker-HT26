import asyncio
import time
from datetime import datetime, timezone

import depthai as dai

from gateway.camera.producer import CameraProducer
from gateway.camera.service import CameraService
from gateway.models.camera import CameraIntrinsics


class OakCameraProducer(CameraProducer):
    """
    OAK-D Pro W PoE camera producer.

    Captures:
        RGB:
            BGR888p
            Configurable output size
            Default: 1920x1080

        Depth:
            RAW16
            Configurable output size
            Default: 1920x1080
            Value = depth in millimetres
            0 = invalid / unknown

    Depth is aligned to CAM_A, the RGB camera.

    This means that for a published RGB/depth frame pair:

        rgb[v, u]

    corresponds spatially to:

        depth[v, u]

    The producer does not perform JPEG/PNG encoding and does not
    require OpenCV or NumPy. Raw DepthAI buffers are published to
    CameraService.

    Blocking DepthAI operations are executed outside FastAPI's
    asyncio event loop using asyncio.to_thread().

    If capture fails, or no frame arrives for stall_timeout_seconds,
    the camera is closed and reopened: quick_retries attempts
    quick_retry_seconds apart, then one every slow_retry_seconds
    until it is back or the producer is stopped.
    """

    def __init__(
        self,
        camera_service: CameraService,
        ip: str = "169.254.1.223",
        fps: float = 2.0,
        width: int = 1920,
        height: int = 1080,
        stereo_width: int = 640,
        stereo_height: int = 400,
        warmup_seconds: float = 2.0,
        connect_timeout_seconds: float = 60.0,
        reconnect_delay_seconds: float = 3.0,
        stall_timeout_seconds: float = 10.0,
        quick_retries: int = 5,
        quick_retry_seconds: float = 5.0,
        slow_retry_seconds: float = 60.0,
    ) -> None:
        if fps <= 0:
            raise ValueError(
                "fps must be greater than zero"
            )

        if width <= 0 or height <= 0:
            raise ValueError(
                "Camera width and height must be greater than zero"
            )

        if stereo_width <= 0 or stereo_height <= 0:
            raise ValueError(
                "Stereo width and height must be greater than zero"
            )

        self._camera_service = camera_service

        #
        # Camera configuration
        #

        self._ip = ip
        self._fps = fps

        self._width = width
        self._height = height

        self._stereo_width = stereo_width
        self._stereo_height = stereo_height

        self._warmup_seconds = warmup_seconds

        self._connect_timeout_seconds = (
            connect_timeout_seconds
        )

        self._reconnect_delay_seconds = (
            reconnect_delay_seconds
        )

        #
        # Recovery after the camera fails while running
        #

        self._stall_timeout_seconds = stall_timeout_seconds

        self._quick_retries = quick_retries
        self._quick_retry_seconds = quick_retry_seconds
        self._slow_retry_seconds = slow_retry_seconds

        #
        # Runtime state
        #

        self._running = False

        self._task: asyncio.Task | None = None

        #
        # Set by stop() to cut reconnect and rate-limit waits short.
        #
        self._stop_event = asyncio.Event()

        #
        # DepthAI objects
        #

        self._device = None
        self._pipeline = None

        self._frame_queue = None

        self._intrinsics: CameraIntrinsics | None = None

    # ================================================================
    # Camera pipeline
    # ================================================================

    def _open_camera(
        self,
        connect_timeout_seconds: float,
    ) -> None:
        """
        Connect to the OAK-D Pro PoE and create the RGB + depth
        pipeline.

        The RGB and depth streams are synchronized on the device.

        Depth is aligned to the RGB camera so corresponding pixel
        coordinates refer to the same point in the image.
        """

        #
        # Connect to the configured PoE camera.
        #

        self._device = self._connect_device(
            connect_timeout_seconds
        )

        #
        # Read the RGB intrinsics from the device calibration,
        # converted to the published frame size.
        #

        self._intrinsics = self._read_intrinsics()

        #
        # Create pipeline for this device.
        #

        self._pipeline = dai.Pipeline(
            self._device
        )

        # ------------------------------------------------------------
        # RGB camera
        # ------------------------------------------------------------

        rgb_camera = self._pipeline.create(
            dai.node.Camera
        ).build(
            dai.CameraBoardSocket.CAM_A
        )


        rgb_output = rgb_camera.requestOutput(
            size=(
                self._width,
                self._height,
            ),
            type=dai.ImgFrame.Type.BGR888p,
            fps=self._fps,
        )

        # ------------------------------------------------------------
        # Stereo cameras
        # ------------------------------------------------------------

        left_camera = self._pipeline.create(
            dai.node.Camera
        ).build(
            dai.CameraBoardSocket.CAM_B
        )

        right_camera = self._pipeline.create(
            dai.node.Camera
        ).build(
            dai.CameraBoardSocket.CAM_C
        )

        left_output = left_camera.requestOutput(
            size=(
                self._stereo_width,
                self._stereo_height,
            ),
            type=dai.ImgFrame.Type.GRAY8,
            fps=self._fps,
        )

        right_output = right_camera.requestOutput(
            size=(
                self._stereo_width,
                self._stereo_height,
            ),
            type=dai.ImgFrame.Type.GRAY8,
            fps=self._fps,
        )

        # ------------------------------------------------------------
        # Stereo depth
        # ------------------------------------------------------------

        stereo = self._pipeline.create(
            dai.node.StereoDepth
        )

        stereo.setDefaultProfilePreset(
            dai.node.StereoDepth.PresetMode.ROBOTICS
        )

        stereo.setLeftRightCheck(True)

        left_output.link(
            stereo.left
        )

        right_output.link(
            stereo.right
        )

        #
        # Align depth to the RGB output.
        #
        # On RVC2 this is how the current DepthAI v3 alignment
        # example aligns the StereoDepth output to the RGB stream.
        #

        rgb_output.link(
            stereo.inputAlignTo
        )

        # ------------------------------------------------------------
        # Synchronize RGB + depth
        # ------------------------------------------------------------

        sync = self._pipeline.create(
            dai.node.Sync
        )

        #
        # At 1 FPS this is intentionally generous.
        #

        from datetime import timedelta

        sync.setSyncThreshold(
            timedelta(milliseconds=100)
        )

        rgb_output.link(
            sync.inputs["rgb"]
        )

        stereo.depth.link(
            sync.inputs["depth"]
        )

        # ------------------------------------------------------------
        # Host output
        # ------------------------------------------------------------

        self._frame_queue = (
            sync.out.createOutputQueue(
                maxSize=1,
                blocking=False,
            )
        )

        #
        # Start everything.
        #

        self._pipeline.start()

        #
        # Allow auto exposure, white balance, stereo processing,
        # and the Ethernet pipeline to settle before we start
        # consuming frames.
        #

        if self._warmup_seconds > 0:
            time.sleep(
                self._warmup_seconds
            )

    # ================================================================
    # Public lifecycle
    # ================================================================

    async def start(self) -> None:
        """
        Start the OAK camera producer.

        Camera setup is run in a worker thread because opening an OAK
        device and starting a DepthAI pipeline may block.
        """

        if self._running:
            return

        self._running = True

        self._stop_event.clear()

        try:
            #
            # Opening the camera is blocking.
            #

            await asyncio.to_thread(
                self._open_camera,
                self._connect_timeout_seconds,
            )

            self._camera_service.set_intrinsics(
                self._intrinsics
            )

            await self._camera_service.set_connected(
                True
            )

            #
            # Start asynchronous capture task.
            #

            self._task = asyncio.create_task(
                self._capture_loop(),
                name="oak-camera-producer",
            )

        except Exception:
            self._running = False

            await self._camera_service.set_connected(
                False
            )

            #
            # Make sure a partially created device/pipeline
            # doesn't remain open.
            #

            await asyncio.to_thread(
                self._close_camera
            )

            raise

    async def stop(self) -> None:
        """
        Stop camera acquisition and release the OAK device.
        """

        self._running = False

        self._stop_event.set()

        #
        # Wait for capture task to finish.
        #

        if self._task is not None:
            try:
                await self._task

            except asyncio.CancelledError:
                pass

            finally:
                self._task = None

        #
        # Closing DepthAI can block, so do it outside
        # the asyncio event loop.
        #

        await asyncio.to_thread(
            self._close_camera
        )

        await self._camera_service.set_connected(
            False
        )

    # ================================================================
    # Camera connection
    # ================================================================

    def _connect_device(
        self,
        timeout_seconds: float,
    ):
        """
        Connect to the configured PoE OAK camera.

        OAK PoE cameras may temporarily disappear while rebooting after
        a pipeline/device closes. Connection attempts are therefore
        retried until timeout_seconds has elapsed; with 0 there is a
        single attempt.
        """

        deadline = (
            time.monotonic()
            + timeout_seconds
        )

        attempt = 0

        while True:
            attempt += 1

            try:
                device_info = dai.DeviceInfo(
                    self._ip
                )

                device = dai.Device(
                    device_info
                )

                return device

            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise

                print(
                    f"OAK camera {self._ip} not ready "
                    f"(attempt {attempt}), retrying...",
                    flush=True,
                )

                time.sleep(
                    self._reconnect_delay_seconds
                )

    # ================================================================
    # Camera calibration
    # ================================================================

    def _read_intrinsics(self) -> CameraIntrinsics:
        """
        Read the CAM_A intrinsics from the device calibration.

        The matrix is converted to the published RGB frame size.
        Depth is aligned to CAM_A, so the same intrinsics apply to
        the published depth frames.
        """

        calibration = self._device.readCalibration()

        matrix = calibration.getCameraIntrinsics(
            dai.CameraBoardSocket.CAM_A,
            self._width,
            self._height,
        )

        return CameraIntrinsics(
            width=self._width,
            height=self._height,
            fx=matrix[0][0],
            fy=matrix[1][1],
            cx=matrix[0][2],
            cy=matrix[1][2],
            matrix=matrix,
        )

    # ================================================================
    # Camera shutdown
    # ================================================================

    def _close_camera(self) -> None:
        self._frame_queue = None

        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:
                pass

            self._pipeline = None

        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass

            self._device = None

    # ================================================================
    # Capture loop
    # ================================================================

    async def _capture_loop(self) -> None:
        """
        Read the newest RGB + depth frame pair at the configured rate
        and publish it to CameraService.

        A capture error, or no frame for stall_timeout_seconds, hands
        over to _reconnect(), and capture carries on once the camera
        is back.

        DepthAI queue access is performed in a worker thread so that
        blocking/device operations cannot block FastAPI's asyncio loop.
        """

        interval = 1.0 / self._fps

        loop = asyncio.get_running_loop()

        last_frame = loop.time()

        while self._running:
            started = loop.time()

            try:
                frame_data = await asyncio.to_thread(
                    self._get_frames
                )

                if frame_data is not None:
                    last_frame = started

                    rgb_bytes, depth_bytes = frame_data

                    await self._camera_service.publish_frame_set(
                        rgb_data=rgb_bytes,
                        depth_data=depth_bytes,

                        # RGB and depth have the same dimensions
                        # because depth has been aligned to CAM_A
                        # and resized with stereo.setOutputSize().
                        rgb_width=self._width,
                        rgb_height=self._height,
                        rgb_pixel_format="BGR888p",

                        depth_width=self._width,
                        depth_height=self._height,
                        depth_pixel_format="RAW16",

                        rgb_content_type=(
                            "application/x-oak-bgr888"
                        ),

                        depth_content_type=(
                            "application/x-oak-depth"
                        ),

                        timestamp=datetime.now(
                            timezone.utc
                        ),
                    )

                elif started - last_frame > self._stall_timeout_seconds:
                    raise RuntimeError(
                        f"no frames for {self._stall_timeout_seconds:g} s"
                    )

            except asyncio.CancelledError:
                #
                # Normal application shutdown.
                #
                # Do not treat task cancellation as a camera failure.
                #
                raise

            except Exception as error:
                print(
                    f"OAK camera capture error: {error}",
                    flush=True,
                )

                await self._reconnect(
                    f"Capture failed: {error}"
                )

                last_frame = loop.time()

                continue

            # --------------------------------------------------------
            # Maintain configured publication rate
            # --------------------------------------------------------

            remaining = (
                interval
                - (loop.time() - started)
            )

            if remaining > 0:
                await self._wait(
                    remaining
                )

    async def _wait(
        self,
        seconds: float,
    ) -> None:
        """Sleep, but return at once when stop() is called."""

        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                seconds,
            )

        except asyncio.TimeoutError:
            pass

    async def _reconnect(
        self,
        reason: str,
    ) -> None:
        """
        Close the camera and reopen it until it works or the producer
        is stopped.

        The first quick_retries attempts are quick_retry_seconds
        apart, later ones slow_retry_seconds, so a camera that stays
        away is not hammered. While it is away, CameraService reports
        it disconnected with the reason, and the frame routes give 503.
        """

        await self._camera_service.set_connected(
            False,
            error=reason,
        )

        await asyncio.to_thread(
            self._close_camera
        )

        attempt = 0

        while self._running:
            attempt += 1

            delay = (
                self._quick_retry_seconds
                if attempt <= self._quick_retries
                else self._slow_retry_seconds
            )

            print(
                f"OAK camera {self._ip}: reconnect attempt "
                f"{attempt} in {delay:g} s",
                flush=True,
            )

            await self._wait(
                delay
            )

            if not self._running:
                return

            try:
                await asyncio.to_thread(
                    self._open_camera,
                    0.0,
                )

            except Exception as error:
                await asyncio.to_thread(
                    self._close_camera
                )

                await self._camera_service.set_connected(
                    False,
                    error=(
                        f"{reason}; reconnect attempt "
                        f"{attempt} failed: {error}"
                    ),
                )

                continue

            self._camera_service.set_intrinsics(
                self._intrinsics
            )

            await self._camera_service.set_connected(
                True
            )

            print(
                f"OAK camera {self._ip}: reconnected "
                f"after {attempt} attempt(s)",
                flush=True,
            )

            return

    # ================================================================
    # Frame acquisition
    # ================================================================

    def _get_frames(
        self,
    ) -> tuple[bytes, bytes] | None:
        """
        Read the newest RGB and depth frames currently available.

        Returns:
            tuple[bytes, bytes]:
                RGB raw bytes
                Depth raw bytes

            None:
                Either RGB or depth does not currently have a frame.

        RGB format:
            BGR888p

        Depth format:
            RAW16

        The returned data is passed directly from DepthAI.
        No OpenCV, NumPy, JPEG encoding, or other image
        conversion is performed.
        """

        if self._frame_queue is None:
            return None

        message_group = self._frame_queue.tryGet()

        if message_group is None:
            return None

        rgb_packet = message_group["rgb"] #type: ignore
        depth_packet = message_group["depth"] #type: ignore

        rgb_bytes = bytes(
            rgb_packet.getData()
        )

        depth_bytes = bytes(
            depth_packet.getData()
        )

        return (
            rgb_bytes,
            depth_bytes,
        )