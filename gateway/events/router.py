from fastapi import (
    APIRouter,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from gateway.config import get_settings
from gateway.events.manager import event_manager
from gateway.security.authentication import (
    DEVELOPMENT_IDENTITIES,
)

router = APIRouter(
    tags=["events"],
)


@router.websocket("/events")
async def events(
    websocket: WebSocket,
) -> None:
    settings = get_settings()

    if settings.security_mode == "development":
        client_id = websocket.query_params.get(
            "client",
            "operator",
        )

        identity = DEVELOPMENT_IDENTITIES.get(
            client_id
        )

        if identity is None:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION
            )
            return

    else:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION
        )
        return

    await event_manager.connect(
        client_id=identity.client_id,
        websocket=websocket,
    )

    try:
        while True:
            message = await websocket.receive_json()

            action = message.get("action")
            events = message.get("events", [])

            if not isinstance(events, list):
                await websocket.send_json(
                    {
                        "type": "error",
                        "data": {
                            "message": (
                                "'events' must be a list"
                            )
                        },
                    }
                )
                continue

            if action == "subscribe":
                subscriptions = event_manager.subscribe(
                    websocket,
                    events,
                )

                await websocket.send_json(
                    {
                        "type": "subscription.updated",
                        "data": {
                            "events": sorted(
                                subscriptions
                            )
                        },
                    }
                )

            elif action == "unsubscribe":
                subscriptions = event_manager.unsubscribe(
                    websocket,
                    events,
                )

                await websocket.send_json(
                    {
                        "type": "subscription.updated",
                        "data": {
                            "events": sorted(
                                subscriptions
                            )
                        },
                    }
                )

            else:
                await websocket.send_json(
                    {
                        "type": "error",
                        "data": {
                            "message": (
                                f"Unknown action: {action}"
                            )
                        },
                    }
                )

    except WebSocketDisconnect:
        event_manager.disconnect(websocket)