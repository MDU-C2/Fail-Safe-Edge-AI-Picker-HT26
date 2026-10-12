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
                or not isinstance(client.get("control_priority"), int)
            ):
                raise RuntimeError(
                    f"Auth client {client_id!r} in {filename} must "
                    f"have 'token_hash', 'permissions' and "
                    f"'control_priority'"
                )

        self._clients = data

    def authenticate(self, token: str) -> Identity | None:
        token_hash = hashlib.sha256(
            token.encode()
        ).hexdigest()

        for client_id, client in self._clients.items():
            if secrets.compare_digest(
                token_hash,
                client["token_hash"],
            ):
                return Identity(
                    client_id=client_id,
                    authenticated=True,
                    permissions=frozenset(
                        client["permissions"]
                    ),
                    control_priority=client["control_priority"],
                )

        return None