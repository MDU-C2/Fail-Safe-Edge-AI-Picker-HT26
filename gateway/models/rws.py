from typing import Literal, Self

from pydantic import (
    BaseModel,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    model_validator,
)


class ArmPose(BaseModel):
    """End-effector pose of one arm, as a RAPID robtarget."""

    arm: str
    mech_unit: str

    tool: str
    wobj: str
    coordinate: str | None = None

    #
    # Position in mm
    #
    x: float
    y: float
    z: float

    #
    # Orientation quaternion
    #
    q1: float
    q2: float
    q3: float
    q4: float

    #
    # Arm configuration
    #
    cf1: int
    cf4: int
    cf6: int
    cfx: int

    #
    # YuMi arm angle in degrees (eax_a)
    #
    arm_angle: float


class ArmJoints(BaseModel):
    arm: str
    mech_unit: str

    #
    # rax_1 .. rax_6 in degrees
    #
    joints: list[float]

    #
    # YuMi arm angle in degrees (eax_a)
    #
    arm_angle: float


class GripperState(BaseModel):
    """Finger position of one arm's SmartGripper."""

    arm: str

    #
    # mm on the SmartGripper's scale, as g_MoveTo / g_GetPos use
    # it: 0 = closed .. 25 = fully open.
    #
    position: float


class RapidTask(BaseModel):
    name: str

    state: str
    execution_state: str
    active: bool

    modules: list[str]


class RapidStatus(BaseModel):
    execution_state: str
    controller_state: str
    operation_mode: str

    tasks: list[RapidTask]
    stop_generation: int | None = None


class RapidModuleUpload(BaseModel):
    source: str = Field(
        min_length=1,
        max_length=1_000_000,
    )


class RapidStartRequest(BaseModel):
    reset_program_pointer: bool = False


class RapidResetRequest(BaseModel):
    start: StrictBool = False
    stop_generation: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def start_requires_generation(self) -> Self:
        if self.start and self.stop_generation is None:
            raise ValueError("Fresh start requires stop_generation from GET /robot/rapid")
        return self


class CollisionStatus(BaseModel):
    collision_state: Literal["INIT", "TRIGGERED", "CONFIRMED", "TRIGGERED_ACK"]


class RwsArmRequest(BaseModel):
    arm: Literal["left", "right"] = "left"

    #
    # False: answer STARTED as soon as the job is written and poll
    # GET /robot/rws/status, like the team's CommServer.
    #
    wait: bool = True


class RwsGripperRequest(RwsArmRequest):
    """One gripper job.

    grip closes until the fingers hold something (or are closed),
    with force N (default 20, the SmartGripper's maximum); open
    opens fully; move goes to position, the opening between the
    fingers in mm (0 closed .. 25 fully open), in either direction
    and without holding force (SmartGripper g_MoveTo).
    """

    action: Literal["grip", "open", "move"]

    position: FiniteFloat | None = Field(
        default=None,
        ge=0,
        le=25,
    )

    force: FiniteFloat | None = Field(
        default=None,
        gt=0,
        le=20,
    )

    @model_validator(mode="after")
    def check_parameters(self) -> Self:
        if (self.action == "move") != (self.position is not None):
            raise ValueError("position is required for move and only for move")

        if self.force is not None and self.action != "grip":
            raise ValueError("force is only for grip")

        return self


class RwsMotionRequest(RwsArmRequest):
    """A job that moves the arm (move or home).

    Left out, each value is the gateway's default
    (GATEWAY_RWS_DEFAULT_SPEED / _ACCELERATION / _RAMP).
    """

    #
    # TCP speed in mm/s, the v_tcp of the RAPID speeddata
    # (v50 = 50).
    #
    speed: FiniteFloat | None = Field(
        default=None,
        ge=1,
        le=1500,
    )

    #
    # RAPID AccSet, in % of the maximum: acceleration, and ramp
    # (how fast the acceleration builds up). The controller treats
    # less than 20 % / 10 % as 20 % / 10 %, so that is refused.
    #
    acceleration: FiniteFloat | None = Field(
        default=None,
        ge=20,
        le=100,
    )

    ramp: FiniteFloat | None = Field(
        default=None,
        ge=10,
        le=100,
    )


class RwsMoveRequest(RwsMotionRequest):
    """Target of one arm's tool0 in wobj0 (default the left arm).

    Position in mm. The rotation, in degrees, is optional and
    means the same as RAPID's OrientZYX(rz, ry, rx); without it
    the arm keeps its current orientation.
    """

    x: FiniteFloat
    y: FiniteFloat
    z: FiniteFloat

    rx: FiniteFloat | None = None
    ry: FiniteFloat | None = None
    rz: FiniteFloat | None = None

    @model_validator(mode="after")
    def check_rotation(self) -> Self:
        given = [
            value is not None
            for value in (self.rx, self.ry, self.rz)
        ]

        if any(given) and not all(given):
            raise ValueError("Give all of rx, ry and rz, or none of them")

        return self
