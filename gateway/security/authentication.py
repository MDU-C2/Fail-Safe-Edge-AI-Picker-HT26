from typing import Annotated

from fastapi import (
    Depends,
    Header,
    HTTPException,
    status,
)
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from gateway.config import Settings, get_settings
from gateway.security.client_registry import ClientRegistry
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


bearer_scheme = HTTPBearer(
    auto_error=False
)


def get_identity(
    settings: Annotated[
        Settings,
        Depends(get_settings),
    ],

    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],

    x_dev_client: Annotated[
        str | None,
        Header(),
    ] = None,

) -> Identity:

    # ------------------------------------------------------------
    # Development mode
    # ------------------------------------------------------------

    if settings.security_mode == "development":
        client_id = x_dev_client or "operator"

        identity = DEVELOPMENT_IDENTITIES.get(
            client_id
        )

        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unknown development client",
            )

        return identity

    # ------------------------------------------------------------
    # Strict mode
    # ------------------------------------------------------------

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={
                "WWW-Authenticate": "Bearer",
            },
        )

    registry = ClientRegistry(
        settings.auth_clients_file
    )

    identity = registry.authenticate(
        credentials.credentials
    )

    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Bearer token",
            headers={
                "WWW-Authenticate": "Bearer",
            },
        )

    return identity


def require_permission(
    permission: str,
):
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