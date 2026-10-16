from pydantic import BaseModel, Field


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
