from typing import Annotated
from dataclasses import replace
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Header, HTTPException, status

from gateway.control.manager import ControlConflict, ControlLease, ControlManager
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
    require_session: bool = False,
) -> None:
    global control_manager

    control_manager = ControlManager(
        lease_seconds=lease_seconds,
        require_session=require_session,
    )


def get_control_manager() -> ControlManager:
    if control_manager is None:
        raise RuntimeError(
            "Control manager has not been configured"
        )

    return control_manager


def require_control(
    permission: str,
):
    """Dependency: the permission and the active control lease.

    Used by every route that moves the robot or changes its
    program or calibration. Gives 403 without the permission and
    409 when the caller does not hold the lease.
    """

    def dependency(
        identity: Annotated[
            Identity,
            Depends(require_permission(permission)),
        ],
        x_control_session: Annotated[str | None, Header()] = None,
    ) -> Identity:
        try:
            authorization = get_control_manager().authorize(identity.client_id, x_control_session)
        except ControlConflict as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        return replace(identity, control_session=authorization.session_id)

    return dependency


def robot_authorization(identity, require_clear=False, manager=None):
    """Bind to the dependency's session before the worker thread can be delayed."""
    from gateway.rws.client import RwsError
    manager = get_control_manager() if manager is None else manager
    try:
        bound = manager.authorize(identity.client_id, getattr(identity, "control_session", None), require_clear)
    except ControlConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    class Guard:
        session_id = bound.session_id

        def __call__(self):
            try:
                bound()
            except ControlConflict as error:
                raise RwsError(409, str(error)) from error

        @contextmanager
        def dispatch(self):
            try:
                with bound.dispatch():
                    yield
            except ControlConflict as error:
                raise RwsError(409, str(error)) from error

        @contextmanager
        def operation(self):
            try:
                with bound.operation():
                    yield
            except ControlConflict as error:
                raise RwsError(409, str(error)) from error

    return Guard()


def serialize_lease(
    lease: ControlLease | None,
) -> ControlLeaseResponse:
    if lease is None:
        return ControlLeaseResponse(
            active=False,
            **get_control_manager().capabilities(),
        )

    return ControlLeaseResponse(
        active=True,
        client_id=lease.client_id,
        priority=lease.priority,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        session_id=lease.session_id,
        **get_control_manager().capabilities(),
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
    x_control_session: Annotated[str | None, Header()] = None,
) -> ControlActionResponse:
    manager = get_control_manager()

    success, message, lease = manager.renew(
        client_id=identity.client_id,
        session_id=x_control_session,
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

@router.post(
    "/release",
    response_model=ControlActionResponse,
    summary="Release robot control",
)
async def release_control(
    identity: Annotated[
        Identity,
        Depends(require_permission("robot.control")),
    ],
    x_control_session: Annotated[str | None, Header()] = None,
) -> ControlActionResponse:
    manager = get_control_manager()

    success, message = manager.release(
        client_id=identity.client_id,
        session_id=x_control_session,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        )

    await event_manager.publish(
        "control.released",
        {
            "client_id": identity.client_id,
        },
    )

    return ControlActionResponse(
        success=True,
        message=message,
        lease=serialize_lease(None),
    )
