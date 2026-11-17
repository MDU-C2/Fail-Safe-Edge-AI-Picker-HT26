import hashlib
import json
import secrets
from pathlib import Path

from gateway.security.identity import Identity


class ClientRegistry:
    def __init__(self, filename: str) -> None:
        path = Path(filename)

        if not path.is_file():
            raise RuntimeError(
                f"Auth clients file not found: {filename}"
            )

        try:
            data = json.loads(
                path.read_text(encoding="utf-8")
            )

        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Auth clients file is not valid JSON: "
                f"{filename}: {error}"
            ) from error

        if not isinstance(data, dict):
            raise RuntimeError(
                f"Auth clients file must contain a JSON object: "
                f"{filename}"
            )

        for client_id, client in data.items():
            if (
                not isinstance(client, dict)
                or not isinstance(client.get("token_hash"), str)
                or not isinstance(client.get("permissions"), list)
                or not all(
                    isinstance(permission, str)
                    for permission in client["permissions"]
                )
                or not isinstance(client.get("control_priority"), int)
            ):
                raise RuntimeError(
                    f"Auth client {client_id!r} in {filename} must "
                    f"have 'token_hash', 'permissions' and "
                    f"'control_priority'"
                )

        if not data:
            raise RuntimeError(
                f"Auth clients file has no clients: {filename}"
            )

        self._clients = data

    @property
    def default_client_id(self) -> str:
        """The client with the highest control priority."""

        return max(
            self._clients,
            key=lambda client_id: self._clients[client_id][
                "control_priority"
            ],
        )

    def identity(
        self,
        client_id: str,
        authenticated: bool,
    ) -> Identity | None:
        client = self._clients.get(client_id)

        if client is None:
            return None

        return Identity(
            client_id=client_id,
            authenticated=authenticated,
            permissions=frozenset(
                client["permissions"]
            ),
            control_priority=client["control_priority"],
        )

    def authenticate(self, token: str) -> Identity | None:
        token_hash = hashlib.sha256(
            token.encode()
        ).hexdigest()

        for client_id, client in self._clients.items():
            if secrets.compare_digest(
                token_hash,
                client["token_hash"],
            ):
                return self.identity(
                    client_id,
                    authenticated=True,
                )

        return None