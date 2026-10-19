from fastapi import HTTPException, status

from gateway.rws.client import RwsError


def to_http_exception(
    error: RwsError,
) -> HTTPException:
    """Map a failed controller call to a gateway response.

    The controller's own 400/404 are passed on, a refusal because
    of the controller's state (403/409, e.g. RAPID running or
    mastership held elsewhere) becomes 409, and anything else is
    a 502. An unreachable controller is 503.
    """

    if error.status_code is None:
        #
        # The message already says the controller is unreachable.
        #
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error.message,
        )

    if error.status_code in (400, 404):
        code = error.status_code

    elif error.status_code in (403, 409):
        code = status.HTTP_409_CONFLICT

    else:
        code = status.HTTP_502_BAD_GATEWAY

    return HTTPException(
        status_code=code,
        detail=f"Robot controller: {error.message}",
    )
