from typing import Literal, Self

from pydantic import (
    BaseModel,
    Field,
    FiniteFloat,
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


class RapidModuleUpload(BaseModel):
    source: str = Field(
        min_length=1,
        max_length=1_000_000,
    )


class RapidStartRequest(BaseModel):
    reset_program_pointer: bool = False


class RwsArmRequest(BaseModel):
    arm: Literal["left", "right"] = "left"

    #
    # False: answer STARTED as soon as the job is written and poll
    # GET /robot/rws/status, like the team's CommServer.
    #
    wait: bool = True


class RwsGripperRequest(RwsArmRequest):
    action: Literal["grip", "open"]


class RwsMoveRequest(RwsArmRequest):
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
