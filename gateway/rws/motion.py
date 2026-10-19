import math
import threading
import time
from typing import Literal

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

Arm = Literal["left", "right"]

#
# RAPID motion task of each arm. The command variables are named
# <variable>_<arm>, e.g. move_request_left (rapid/SharedVars.mod).
#
ARM_TASKS: dict[Arm, str] = {
    "left": "T_ROB_L",
    "right": "T_ROB_R",
}

GRIPPER_COMMANDS = {
    "grip": "GRIP",
    "open": "OPEN",
}


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
    """Commands the arm tasks of the RAPID program over Robot Web
    Services.

    Uses the PERS variables of rapid/SharedVars.mod: the
    job is written first (target, go_home or grip_cmd), then
    move_request_<arm> is set to TRUE. The arm task does the job,
    writes move_ok_<arm> and clears move_request_<arm>, which is
    polled here.

    The arm tasks do not check a safe zone, so targets outside the
    configured box are refused here before anything is written.

    Blocking; call it through asyncio.to_thread. One job per arm at
    a time; the two arms are independent. With wait=False a job
    returns STARTED as soon as it is written, like the team's
    CommServer, and status() reports BUSY / DONE / ERR move failed.
    """

    def __init__(
        self,
        client: RwsClient,
        timeout: float,
        safe_zone_min: tuple[float, float, float],
        safe_zone_max: tuple[float, float, float],
    ) -> None:
        self._client = client
        self._timeout = timeout

        self._safe_zone_min = safe_zone_min
        self._safe_zone_max = safe_zone_max

        self._busy = {
            arm: threading.Lock()
            for arm in ARM_TASKS
        }

    def move(
        self,
        arm: Arm,
        position: tuple[float, float, float],
        rotation: tuple[float, float, float] | None,
        wait: bool = True,
    ) -> CommandResponse:
        """Move the arm's tool0 to position (mm, wobj0).

        rotation is (rx, ry, rz) in degrees, or None to keep the
        current orientation.
        """

        if not self.in_safe_zone(position):
            return CommandResponse(
                accepted=False,
                message="ERR outside safe zone",
            )

        values = {
            "target_pos": rapid_array(position),
            "use_rot": "FALSE" if rotation is None else "TRUE",
            "go_home": "FALSE",
            "grip_cmd": '""',
        }

        if rotation is not None:
            rx, ry, rz = rotation

            values["target_rot"] = rapid_array(orient_zyx(rz, ry, rx))

        return self._run(arm, values, wait)

    def home(
        self,
        arm: Arm,
        wait: bool = True,
    ) -> CommandResponse:
        return self._run(
            arm,
            {
                "go_home": "TRUE",
                "grip_cmd": '""',
            },
            wait,
        )

    def gripper(
        self,
        arm: Arm,
        action: Literal["grip", "open"],
        wait: bool = True,
    ) -> CommandResponse:
        return self._run(
            arm,
            {
                "go_home": "FALSE",
                "grip_cmd": f'"{GRIPPER_COMMANDS[action]}"',
            },
            wait,
        )

    def status(
        self,
        arm: Arm,
    ) -> CommandResponse:
        """The arm's last job, as CommServer answers STATUS.

        BUSY while it runs, then DONE or ERR move failed. Also
        reports jobs started by other clients (CommServer).
        """

        self._check_loaded(arm)

        if self._read(arm, "move_request") == "TRUE":
            return CommandResponse(
                accepted=True,
                message="BUSY",
            )

        return self._result(arm)

    def in_safe_zone(
        self,
        position: tuple[float, float, float],
    ) -> bool:
        return all(
            low <= value <= high
            for value, low, high in zip(
                position,
                self._safe_zone_min,
                self._safe_zone_max,
            )
        )

    def is_moving(self) -> bool:
        """A job sent through the gateway has not finished yet."""

        return any(
            lock.locked()
            for lock in self._busy.values()
        )

    # ============================================================
    # Handshake
    # ============================================================

    def _run(
        self,
        arm: Arm,
        values: dict[str, str],
        wait: bool,
    ) -> CommandResponse:
        if not self._busy[arm].acquire(blocking=False):
            raise MotionConflict(f"A {arm} arm job is already in progress")

        try:
            self._check_ready(arm)

            for name, value in values.items():
                self._write(arm, name, value)

            self._write(arm, "move_request", "TRUE")

            #
            # The arm task now owns the job; a new one is refused
            # until it clears move_request (see _check_ready).
            #
            if not wait:
                return CommandResponse(
                    accepted=True,
                    message="STARTED",
                )

            self._wait_for_job(arm)

            return self._result(arm)

        finally:
            self._busy[arm].release()

    def _result(
        self,
        arm: Arm,
    ) -> CommandResponse:
        if self._read(arm, "move_ok") == "TRUE":
            return CommandResponse(
                accepted=True,
                message="DONE",
            )

        #
        # Same reply as the team's CommServer: the arm tasks only
        # report move_ok (an unreachable target or a home move that
        # stopped early).
        #
        return CommandResponse(
            accepted=False,
            message="ERR move failed",
        )

    def _check_loaded(
        self,
        arm: Arm,
    ) -> None:
        try:
            self._read(arm, "move_request")

        except RwsError as error:
            if error.status_code != 400:
                raise

            raise MotionConflict(
                f"The motion program is not loaded in {ARM_TASKS[arm]} "
                f"(no move_request_{arm} variable)"
            ) from error

    def _check_ready(
        self,
        arm: Arm,
    ) -> None:
        if self._client.execution_state() != "running":
            raise MotionConflict("RAPID is not running")

        self._check_loaded(arm)

        #
        # Still set while an earlier job runs: one started with
        # wait=False, one that timed out here, or one from another
        # client such as the team's CommServer.
        #
        if self._read(arm, "move_request") == "TRUE":
            raise MotionConflict(f"The {arm} arm is busy with an earlier job")

    def _wait_for_job(
        self,
        arm: Arm,
    ) -> None:
        deadline = time.monotonic() + self._timeout

        while self._read(arm, "move_request") == "TRUE":
            if self._client.execution_state() != "running":
                raise MotionConflict("RAPID stopped before the move finished")

            if time.monotonic() > deadline:
                raise MotionTimeout(
                    f"The {arm} arm did not finish within {self._timeout:g} s"
                )

            time.sleep(POLL_INTERVAL_SECONDS)

    def _read(
        self,
        arm: Arm,
        name: str,
    ) -> str:
        return self._client.read_symbol(
            ARM_TASKS[arm],
            f"{name}_{arm}",
        )

    def _write(
        self,
        arm: Arm,
        name: str,
        value: str,
    ) -> None:
        self._client.write_symbol(
            ARM_TASKS[arm],
            f"{name}_{arm}",
            value,
        )


def create_rws_motion(
    settings: Settings,
) -> RwsMotion:
    return RwsMotion(
        client=rws_client,
        timeout=settings.rws_move_timeout,
        safe_zone_min=settings.rws_safe_zone_min,
        safe_zone_max=settings.rws_safe_zone_max,
    )


rws_motion = create_rws_motion(
    get_settings()
)
