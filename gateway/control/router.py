from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from gateway.control.manager import ControlLease, ControlManager
from gateway.models.control import (
    ControlActionResponse,
    ControlLeaseResponse,
)
from gateway.security.authentication import require_permission
from gateway.security.identity import Identity
from gateway.events.manager import event_manager

router = APIRouter(
    prefix="/control",
    tags=["control"],
)

control_manager: ControlManager | None = None


def configure_control_manager(
    lease_seconds: float,
) -> None:
    global control_manager

    control_manager = ControlManager(
        lease_seconds=lease_seconds,
    )


def get_control_manager() -> ControlManager:
    if control_manager is None:
        raise RuntimeError(
            "Control manager has not been configured"
        )

    return control_manager


def serialize_lease(
    lease: ControlLease | None,
) -> ControlLeaseResponse:
    if lease is None:
        return ControlLeaseResponse(
            active=False,
        )

    return ControlLeaseResponse(
        active=True,
        client_id=lease.client_id,
        priority=lease.priority,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
    )


@router.get(
    "",
    response_model=ControlLeaseResponse,
    summary="Get the current control owner",
)
def get_control(
    _: Annotated[
        Identity,
        Depends(require_permission("robot.read")),
    ],
) -> ControlLeaseResponse:
    manager = get_control_manager()

    return serialize_lease(
        manager.get()
    )


@router.post(
    "/acquire",
    response_model=ControlActionResponse,
    summary="Acquire robot control",
)
async def acquire_control(
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
) -> ControlActionResponse:
    manager = get_control_manager()

    result = await manager.acquire(
        client_id=identity.client_id,
        priority=identity.control_priority,
    )

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.message,
        )

    if result.previous_client_id is not None:
        await event_manager.send_to_client(
            result.previous_client_id,
            {
                "type": "control.revoked",
                "data": {
                    "reason": "preempted",
                    "new_controller": identity.client_id,
                },
            },
        )

    await event_manager.publish(
        "control.acquired",
        {
            "client_id": identity.client_id,
            "priority": identity.control_priority,
        },
    )

    return ControlActionResponse(
        success=True,
        message=result.message,
        lease=serialize_lease(result.lease),
    )


@router.post(
    "/renew",
    response_model=ControlActionResponse,
    summary="Renew robot control",
)
async def renew_control(
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
) -> ControlActionResponse:
    manager = get_control_manager()

    success, message, lease = manager.renew(
        client_id=identity.client_id,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        )

    await event_manager.publish(
        "control.renewed",
        {   
            "client_id": identity.client_id,
        },
    )

    return ControlActionResponse(
        success=True,
        message=message,
        lease=serialize_lease(lease),
    )