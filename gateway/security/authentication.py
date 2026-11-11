from functools import lru_cache
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


bearer_scheme = HTTPBearer(
    auto_error=False
)


@lru_cache
def get_client_registry() -> ClientRegistry:
    return ClientRegistry(
        get_settings().auth_clients_file
    )


def get_development_identity(
    client_id: str | None,
) -> Identity | None:
    """
    Development mode: the client named by id in the auth clients
    file, without a token. No id means the client with the highest
    control priority.
    """

    registry = get_client_registry()

    return registry.identity(
        client_id or registry.default_client_id,
        authenticated=False,
    )


def get_bearer_identity(
    authorization: str | None,
) -> Identity | None:
    """
    Strict mode: the client of the token in an
    "Authorization: Bearer <token>" header value, or None for a
    missing, malformed or unknown token. Parsed like FastAPI's
    HTTPBearer; used by the events WebSocket, whose upgrade request
    carries the same header as the HTTP routes.
    """

    if authorization is None:
        return None

    scheme, _, credentials = authorization.strip().partition(" ")
    credentials = credentials.strip()

    if scheme.lower() != "bearer" or not credentials:
        return None

    return get_client_registry().authenticate(
        credentials
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
        identity = get_development_identity(
            x_dev_client
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

    registry = get_client_registry()

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