"""Headless Raspberry Pi picker, armed only for explicit coordinator batches.

The local model is warmed before readiness. Waiting or completed batches do not
capture frames, acquire robot control or begin another batch on their own.
"""

import argparse
from contextlib import ExitStack
from pathlib import Path
import signal
import sys
import threading
from types import SimpleNamespace

from device_service import read_gateway_token
from handover_plan import right_camera_park_pose
from queued_robot_client import QueueRobotClient
from terminal_commands import positive


def output(*parts, file=None):
    try:
        print(*parts, file=file, flush=True)
    except (OSError, ValueError):
        pass


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--token-file", type=Path,
                        help="Private gateway secondary credential; defaults to state-dir/secondary-token")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--coordinator-token-file", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--conf", type=positive, default=0.5)
    parser.add_argument("--speed", type=positive)
    parser.add_argument("--threads", type=int, choices=range(1, 33), default=3)
    parser.add_argument("--depth-frames", type=int, choices=range(1, 10), default=5)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required to arm the automatic failover worker")
    if args.conf >= 1 or (args.speed is not None and not 1 <= args.speed <= 1500):
        parser.error("confidence must be below 1; speed must be 1..1500 mm/s")
    return args


def build_session(args, robot, resources):
    """Reuse the UI's local perception and finite smooth cup workflow."""
    # These dependencies are deliberately absent from the native Lynx image.
    # Importing the worker module itself must remain lightweight there.
    from camera_client import CameraClient
    from cup_commands import CupSession
    from inference_integration import create_detector
    from picker_workflow import Calibration, CaptureService, CaptureStore
    from placing_grid import PlacingGrid
    from recovery_journal import RecoveryJournal

    state = args.state_dir
    calibration = Calibration(state / "calibration.json")
    grid = PlacingGrid(state / "placing-grid.json")
    store = CaptureStore(state / "captures")
    camera = CameraClient(robot.api, robot.headers)
    options = SimpleNamespace(conf=args.conf, mac_inference_url=None, isolated_inference=True,
        inference_model=str(args.model), inference_image_size=640, inference_threads=args.threads,
        inference_startup_timeout=90.0, inference_events=None, output=str(store.folder),
        mac_timeout=5.0, pi_timeout=30.0, mac_probe_interval=3.0, mac_recovery_successes=3)
    detector, weights = create_detector(options, calibration, camera.intrinsics(),
                                       register_resource=lambda value: resources.callback(value.close))
    captures = CaptureService(camera, detector, store, weights, placing_grid=grid,
                              grid_table_z=0.0, grid_cell_size=90.0,
                              depth_frames=args.depth_frames, scan_observations=1)

    def capture(purpose):
        executing = getattr(getattr(session, "failover", None), "executing", False)
        if executing:
            robot.check_lease()
        result = captures.capture(purpose)
        if executing:
            robot.check_lease()
        return result

    recovery = RecoveryJournal(store.folder / "robot_recovery.json",
                               legacy_inventory_path=store.folder / "cups.json")
    session = CupSession(capture, store.save, robot, calibration.digest,
                      tag_offset=calibration.offset, speed=args.speed,
                      recovery=recovery, placing_grid=grid, verification_frames=1,
                      strict_single_verification=True, camera_z=False,
                      between_cup_check=True, between_cup_grid_check=True,
                      continuous_batch=False, right_park_pose=right_camera_park_pose())
    return session


def main(argv=None):
    args = arguments(argv)
    # Also support `python capture_and_detect/failover_worker.py` from a
    # checkout; the native lock lives in the sibling lightweight package.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from failover_coordinator import CoordinatorClient
    from failover_workflow import FailoverRunner
    from lynx.runtime_lock import NativeRuntimeLock

    stop_event = threading.Event()
    robot = None
    previous = {}

    def shutdown(_signum, _frame):
        stop_event.set()
        if robot is not None:
            robot.cancel_wait()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, shutdown)
        args.state_dir = args.state_dir.resolve(strict=True)
        gateway_token = read_gateway_token(args.token_file or args.state_dir / "secondary-token")
        coordinator = CoordinatorClient(args.coordinator_url,
                                         read_gateway_token(args.coordinator_token_file), "secondary")
        with ExitStack() as resources:
            lock = NativeRuntimeLock(args.state_dir)
            lock.acquire()
            resources.callback(lock.close)
            api = args.gateway.rstrip("/")
            if not api.endswith("/api/v1"):
                api += "/api/v1"
            robot = QueueRobotClient(api, {"Authorization": "Bearer " + gateway_token}, dry_run=False)
            # Release only this worker's lease. Do not cancel an accepted queue
            # when its process receives SIGTERM or its terminal disconnects.
            def release():
                robot.cancel_wait()
                robot.end_standby()
            resources.callback(release)
            session = build_session(args, robot, resources)
            if not stop_event.is_set():
                FailoverRunner(session, coordinator, role="secondary", state_dir=args.state_dir,
                               output=output).serve(stop_event=stop_event)
        return 0
    except (KeyboardInterrupt, EOFError):
        output("Worker disarmed; an accepted gateway cup may finish. No robot Stop was sent.")
        return 0
    except Exception as error:
        if stop_event.is_set():
            return 0
        output(f"Failover worker stopped: {error}", file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
