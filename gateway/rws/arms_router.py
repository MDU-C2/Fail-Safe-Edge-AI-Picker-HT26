import asyncio
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    Query,
)

from gateway.models.rws import (
    ArmJoints,
    ArmPose,
)
from gateway.rws.client import RwsError, rws_client
from gateway.rws.errors import to_http_exception
from gateway.security.authentication import require_permission
from gateway.security.identity import Identity

#
# YuMi mechanical units. ROB_R_7 (the right arm's 7th axis) is
# reported as the arm angle of ROB_R instead.
#
ARM_UNITS = {
    "left": "ROB_L",
    "right": "ROB_R",
}

#
# RAPID identifiers: a letter followed by letters, digits or _,
# at most 32 characters.
#
RAPID_NAME = r"^[A-Za-z][A-Za-z0-9_]{0,31}$"

Arm = Literal["left", "right"]

router = APIRouter(
    prefix="/robot/arms",
    tags=["robot"],
)


# ================================================================
# Conversion
# ================================================================

def read_pose(
    arm: str,
    tool: str | None = None,
    wobj: str | None = None,
    coordinate: str | None = None,
) -> ArmPose:
    unit = ARM_UNITS[arm]

    #
    # Without tool or wobj the controller uses the arm's active
    # one, so look up which that is to report it.
    #
    if tool is None or wobj is None:
        mechunit = rws_client.mechunit(unit)

        tool = tool or mechunit["tool-name"]
        wobj = wobj or mechunit["wobj-name"]

    target = rws_client.robtarget(
        unit,
        tool=tool,
        wobj=wobj,
        coordinate=coordinate,
    )

    return ArmPose(
        arm=arm,
        mech_unit=unit,
        tool=tool,
        wobj=wobj,
        coordinate=coordinate,
        x=target["x"],
        y=target["y"],
        z=target["z"],
        q1=target["q1"],
        q2=target["q2"],
        q3=target["q3"],
        q4=target["q4"],
        cf1=target["cf1"],
        cf4=target["cf4"],
        cf6=target["cf6"],
        cfx=target["cfx"],
        arm_angle=target["eaxa"],
    )


def read_joints(
    arm: str,
) -> ArmJoints:
    unit = ARM_UNITS[arm]

    target: dict[str, Any] = rws_client.jointtarget(unit)

    return ArmJoints(
        arm=arm,
        mech_unit=unit,
        joints=[
            float(target[f"rax_{axis}"])
            for axis in range(1, 7)
        ],
        arm_angle=target["eax_a"],
    )


# ================================================================
# Endpoints
# ================================================================

@router.get(
    "",
    response_model=dict[str, ArmPose],
    summary="Get the end-effector pose of both arms",
)
async def get_arm_poses(
    _: Annotated[
        Identity,
        Depends(require_permission("robot.read")),
    ],
) -> dict[str, ArmPose]:
    try:
        return {
            arm: await asyncio.to_thread(
                read_pose,
                arm,
            )
            for arm in ARM_UNITS
        }

    except RwsError as error:
        raise to_http_exception(error) from error


@router.get(
    "/{arm}/pose",
    response_model=ArmPose,
    summary="Get the end-effector pose of one arm",
    description=(
        "Without parameters the arm's active tool and work object "
        "are used. coordinate is the frame the pose is given in."
    ),
)
async def get_arm_pose(
    arm: Arm,
    _: Annotated[
        Identity,
        Depends(require_permission("robot.read")),
    ],
    tool: Annotated[
        str | None,
        Query(pattern=RAPID_NAME),
    ] = None,
    wobj: Annotated[
        str | None,
        Query(pattern=RAPID_NAME),
    ] = None,
    coordinate: Literal[
        "Base",
        "Wobj",
        "Tool",
    ] | None = None,
) -> ArmPose:
    try:
        return await asyncio.to_thread(
            read_pose,
            arm,
            tool,
            wobj,
            coordinate,
        )

    except RwsError as error:
        raise to_http_exception(error) from error


@router.get(
    "/{arm}/joints",
    response_model=ArmJoints,
    summary="Get the joint angles of one arm",
)
async def get_arm_joints(
    arm: Arm,
    _: Annotated[
        Identity,
        Depends(require_permission("robot.read")),
    ],
) -> ArmJoints:
    try:
        return await asyncio.to_thread(
            read_joints,
            arm,
        )

    except RwsError as error:
        raise to_http_exception(error) from error
