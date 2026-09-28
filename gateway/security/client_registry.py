import hashlib
import json
import secrets
from pathlib import Path

from gateway.security.identity import Identity


class ClientRegistry:
    def __init__(self, filename: str) -> None:
        data = json.loads(
            Path(filename).read_text(encoding="utf-8")
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