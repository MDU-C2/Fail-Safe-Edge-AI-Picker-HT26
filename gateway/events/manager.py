from dataclasses import dataclass, field

from fastapi import WebSocket

from gateway.security.identity import Identity


@dataclass
class EventConnection:
    identity: Identity
    websocket: WebSocket
    subscriptions: set[str] = field(default_factory=set)


class EventManager:
    def __init__(self) -> None:
        self._connections: list[EventConnection] = []

    async def connect(
        self,
        identity: Identity,
        websocket: WebSocket,
    ) -> None:
        await websocket.accept()

        self._connections.append(
            EventConnection(
                identity=identity,
                websocket=websocket,
            )
        )

    def disconnect(
        self,
        websocket: WebSocket,
    ) -> None:
        self._connections = [
            connection
            for connection in self._connections
            if connection.websocket is not websocket
        ]

    def get_connection(
        self,
        websocket: WebSocket,
    ) -> EventConnection | None:
        for connection in self._connections:
            if connection.websocket is websocket:
                return connection

        return None

    def subscribe(
        self,
        websocket: WebSocket,
        events: list[str],
    ) -> set[str]:
        connection = self.get_connection(websocket)

        if connection is None:
            raise RuntimeError("WebSocket is not registered")

        connection.subscriptions.update(events)

        return connection.subscriptions

    def unsubscribe(
        self,
        websocket: WebSocket,
        events: list[str],
    ) -> set[str]:
        connection = self.get_connection(websocket)

        if connection is None:
            raise RuntimeError("WebSocket is not registered")

        connection.subscriptions.difference_update(events)

        return connection.subscriptions

    @staticmethod
    def matches_subscription(
        event_type: str,
        subscription: str,
    ) -> bool:
        return (
            event_type == subscription
            or event_type.startswith(f"{subscription}.")
        )

    async def send_to_client(
        self,
        client_id: str,
        event: dict,
    ) -> None:
        disconnected: list[WebSocket] = []

        for connection in self._connections:
            if connection.identity.client_id != client_id:
                continue

            try:
                await connection.websocket.send_json(event)
            except Exception:
                disconnected.append(connection.websocket)

        for websocket in disconnected:
            self.disconnect(websocket)

    async def publish(
        self,
        event_type: str,
        data: dict,
    ) -> None:
        event = {
            "type": event_type,
            "data": data,
        }

        disconnected: list[WebSocket] = []

        for connection in self._connections:
            matches = any(
                self.matches_subscription(
                    event_type,
                    subscription,
                )
                for subscription in connection.subscriptions
            )

            if not matches:
                continue

            try:
                await connection.websocket.send_json(event)
            except Exception:
                disconnected.append(connection.websocket)

        for websocket in disconnected:
            self.disconnect(websocket)


event_manager = EventManager()