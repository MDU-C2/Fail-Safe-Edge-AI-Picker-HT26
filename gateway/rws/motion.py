import math
import threading
import time

from gateway.config import Settings, get_settings
from gateway.models.commands import CommandResponse
from gateway.rws.client import (
    RwsClient,
    RwsError,
    rws_client,
)


#
# How often the move_request flag is read while a move runs.
#
POLL_INTERVAL_SECONDS = 0.05


class MotionConflict(Exception):
    """The motion task cannot take a command right now."""


class MotionTimeout(Exception):
    """The motion task did not finish the move in time."""


def orient_zyx(
    rz: float,
    ry: float,
    rx: float,
) -> tuple[float, float, float, float]:
    """Quaternion of RAPID's OrientZYX(rz, ry, rx), in degrees.

    Rotation about z, then the new y, then the new x; checked
    against the controller's own OrientZYX.
    """

    def axis_rotation(
        axis: int,
        degrees: float,
    ) -> tuple[float, float, float, float]:
        half = math.radians(degrees) / 2

        vector = [0.0, 0.0, 0.0]
        vector[axis] = math.sin(half)

        return (math.cos(half), *vector)

    def multiply(
        a: tuple[float, float, float, float],
        b: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        w1, x1, y1, z1 = a
        w2, x2, y2, z2 = b

        return (
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        )

    return multiply(
        multiply(
            axis_rotation(2, rz),
            axis_rotation(1, ry),
        ),
        axis_rotation(0, rx),
    )


def rapid_array(
    values: tuple[float, ...],
) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in values) + "]"


class RwsMotion:
    """Commands the RAPID motion task over Robot Web Services.

    Uses the PERS variables of gateway/rapid/LegacyShared.mod:
    the target is written first, then move_request is set to
    TRUE. LegacyMotion checks the safe zone, moves, writes
    move_ok / move_error and clears move_request, which is
    polled here.

    Blocking; call it through asyncio.to_thread. One move at a
    time: a second call while a move runs is refused.
    """

    def __init__(
        self,
        client: RwsClient,
        task: str,
        timeout: float,
    ) -> None:
        self._client = client
        self._task = task
        self._timeout = timeout

        self._busy = threading.Lock()

    def move(
        self,
        position: tuple[float, float, float],
        rotation: tuple[float, float, float] | None,
    ) -> CommandResponse:
        """Move tool0 to position (mm, wobj0).

        rotation is (rx, ry, rz) in degrees, or None to keep the
        current orientation.
        """

        values = {
            "target_pos": rapid_array(position),
            "use_rot": "FALSE" if rotation is None else "TRUE",
            "go_home": "FALSE",
        }

        if rotation is not None:
            rx, ry, rz = rotation

            values["target_rot"] = rapid_array(orient_zyx(rz, ry, rx))

        return self._run(values)

    def is_moving(self) -> bool:
        """A move sent through the gateway has not finished yet."""

        return self._busy.locked()

    def home(self) -> CommandResponse:
        return self._run(
            {
                "go_home": "TRUE",
            }
        )

    # ============================================================
    # Handshake
    # ============================================================

    def _run(
        self,
        values: dict[str, str],
    ) -> CommandResponse:
        if not self._busy.acquire(blocking=False):
            raise MotionConflict("A move is already in progress")

        try:
            self._check_ready()

            for name, value in values.items():
                self._client.write_symbol(self._task, name, value)

            self._client.write_symbol(self._task, "move_request", "TRUE")

            self._wait_for_move()

            if self._read("move_ok") == "TRUE":
                return CommandResponse(
                    accepted=True,
                    message="DONE",
                )

            #
            # move_error is optional, so the gateway also works with
            # programs that only report move_ok.
            #
            try:
                error = self._read("move_error").strip('"')

            except RwsError as read_error:
                if read_error.status_code != 400:
                    raise

                error = ""

            return CommandResponse(
                accepted=False,
                message=f"ERR {error or 'move failed'}",
            )

        finally:
            self._busy.release()

    def _check_ready(self) -> None:
        if self._client.execution_state() != "running":
            raise MotionConflict("RAPID is not running")

        try:
            requested = self._read("move_request")

        except RwsError as error:
            if error.status_code != 400:
                raise

            raise MotionConflict(
                f"The motion program is not loaded in {self._task} "
                f"(no move_request variable)"
            ) from error

        #
        # Still set after an earlier move timed out here, or by
        # another RWS client.
        #
        if requested == "TRUE":
            raise MotionConflict("The previous move has not finished")

    def _wait_for_move(self) -> None:
        deadline = time.monotonic() + self._timeout

        while self._read("move_request") == "TRUE":
            if self._client.execution_state() != "running":
                raise MotionConflict("RAPID stopped before the move finished")

            if time.monotonic() > deadline:
                raise MotionTimeout(
                    f"The move did not finish within {self._timeout:g} s"
                )

            time.sleep(POLL_INTERVAL_SECONDS)

    def _read(
        self,
        name: str,
    ) -> str:
        return self._client.read_symbol(self._task, name)


def create_rws_motion(
    settings: Settings,
) -> RwsMotion:
    return RwsMotion(
        client=rws_client,
        task=settings.rws_motion_task,
        timeout=settings.rws_move_timeout,
    )


rws_motion = create_rws_motion(
    get_settings()
)
