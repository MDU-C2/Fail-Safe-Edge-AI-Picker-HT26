from dataclasses import dataclass, field
from multiprocessing import connection

from fastapi import WebSocket


@dataclass
class EventConnection:
    client_id: str
    websocket: WebSocket
    subscriptions: set[str] = field(default_factory=set)


class EventManager:
    def __init__(self) -> None:
        self._connections: list[EventConnection] = []

    async def connect(
        self,
        client_id: str,
        websocket: WebSocket,
    ) -> None:
        await websocket.accept()

        self._connections.append(
            EventConnection(
                client_id=client_id,
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
    ) -> set:
        connection = self.get_connection(websocket)

        if connection is None:
            raise RuntimeError(
                "WebSocket is not registered"
            )

        connection.subscriptions.update(events)

        return connection.subscriptions

    def unsubscribe(
        self,
        websocket: WebSocket,
        events: list[str],
    ) -> set:
        connection = self.get_connection(websocket)

        if connection is None:
            raise RuntimeError(
                "WebSocket is not registered"
            )

        connection.subscriptions.difference_update(events)

        return connection.subscriptions

    async def send_to_client(
        self,
        client_id: str,
        event: dict,
    ) -> None:
        connections = [
            connection
            for connection in self._connections
            if connection.client_id == client_id
        ]

        for connection in connections:
            try:
                await connection.websocket.send_json(event)
            except Exception:
                self.disconnect(connection.websocket)

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

        #NOTE should that nested for be there
        for connection in self._connections:
            if not any(
                self.matches_subscription(
                    event_type,
                    subscription,
                )
                for subscription in connection.subscriptions
            ):
                continue

            try:
                await connection.websocket.send_json(event)
            except Exception:
                disconnected.append(
                    connection.websocket
                )

        for websocket in disconnected:
            self.disconnect(websocket)

    @staticmethod
    def matches_subscription(
        event_type: str,
        subscription: str,
    ) -> bool:
        return (
            event_type == subscription
            or event_type.startswith(
                f"{subscription}."
            )
        )


event_manager = EventManager()