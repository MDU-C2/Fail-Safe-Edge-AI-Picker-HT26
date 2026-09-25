from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from gateway.config import Settings, get_settings
from gateway.security.identity import Identity


DEVELOPMENT_IDENTITIES = {
    "operator": Identity(
        client_id="operator",
        authenticated=False,
        permissions=frozenset({
            "robot.read",
            "robot.control",
            "camera.read",
            "camera.control",
        }),
        control_priority=100,
    ),
    "autonomy": Identity(
        client_id="autonomy",
        authenticated=False,
        permissions=frozenset({
            "robot.read",
            "robot.control",
            "camera.read",
            "camera.control",
        }),
        control_priority=50,
    ),
    "diagnostics": Identity(
        client_id="diagnostics",
        authenticated=False,
        permissions=frozenset({
            "robot.read",
            "camera.read",
        }),
        control_priority=10,
    ),
}


def get_identity(
    settings: Annotated[Settings, Depends(get_settings)],
    x_dev_client: Annotated[str | None, Header()] = None,
) -> Identity:
    if settings.security_mode == "development":
        client_id = x_dev_client or "operator"

        identity = DEVELOPMENT_IDENTITIES.get(client_id)

        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Unknown development client: {client_id}",
            )

        return identity

    # Fail closed until production authentication is implemented.
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Strict security mode is not configured yet",
    )


def require_permission(permission: str):
    def dependency(
        identity: Annotated[
            Identity,
            Depends(get_identity),
        ],
    ) -> Identity:
        if not identity.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission}",
            )

        return identity

    return dependency