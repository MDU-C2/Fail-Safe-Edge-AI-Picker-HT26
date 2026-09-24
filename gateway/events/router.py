from fastapi import (
    APIRouter,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from gateway.config import get_settings
from gateway.events.manager import event_manager
from gateway.events.permissions import get_event_permission
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
        identity=identity,
        websocket=websocket,
    )

    try:
        while True:
            message = await websocket.receive_json()

            action = message.get("action")
            requested_events = message.get(
                "events",
                [],
            )

            if not isinstance(requested_events, list):
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

            if not all(
                isinstance(event, str)
                for event in requested_events
            ):
                await websocket.send_json(
                    {
                        "type": "error",
                        "data": {
                            "message": (
                                "Every event name "
                                "must be a string"
                            )
                        },
                    }
                )
                continue

            if action == "subscribe":
                rejected_events = []

                for event in requested_events:
                    permission = get_event_permission(
                        event
                    )

                    if permission is None:
                        rejected_events.append(
                            {
                                "event": event,
                                "reason": "unknown_event",
                            }
                        )
                        continue

                    if not identity.can(permission):
                        rejected_events.append(
                            {
                                "event": event,
                                "reason": "forbidden",
                            }
                        )

                if rejected_events:
                    await websocket.send_json(
                        {
                            "type": "subscription.rejected",
                            "data": {
                                "events": rejected_events,
                            },
                        }
                    )
                    continue

                subscriptions = event_manager.subscribe(
                    websocket,
                    requested_events,
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
                    requested_events,
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