"""ClientRegistry: auth_clients.json is the one list of clients for both security modes."""

import hashlib
import json
from pathlib import Path

import pytest

from gateway.security.client_registry import ClientRegistry


def write(
    path: Path,
    clients: object,
) -> str:
    path.write_text(
        json.dumps(clients),
        encoding="utf-8",
    )

    return str(path)


def entry(
    token: str,
    permissions: list[str],
    priority: int,
) -> dict:
    return {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "permissions": permissions,
        "control_priority": priority,
    }


def test_identities_come_from_the_file(
    tmp_path: Path,
) -> None:
    registry = ClientRegistry(
        write(
            tmp_path / "clients.json",
            {
                "low": entry("low-token", ["robot.read"], 5),
                "custom": entry("custom-token", ["robot.read", "robot.control"], 42),
            },
        )
    )

    custom = registry.identity("custom", authenticated=False)

    assert custom is not None
    assert custom.permissions == frozenset({"robot.read", "robot.control"})
    assert custom.control_priority == 42
    assert not custom.authenticated

    assert registry.identity("admin", authenticated=False) is None
    assert registry.default_client_id == "custom"

    authenticated = registry.authenticate("low-token")

    assert authenticated is not None
    assert authenticated.client_id == "low"
    assert authenticated.authenticated

    assert registry.authenticate("wrong") is None


@pytest.mark.parametrize(
    "clients",
    [
        [],
        {},
        {"x": {"permissions": [], "control_priority": 1}},
        {"x": {"token_hash": "h", "permissions": "robot.read", "control_priority": 1}},
        {"x": {"token_hash": "h", "permissions": [1], "control_priority": 1}},
        {"x": {"token_hash": "h", "permissions": [], "control_priority": "high"}},
    ],
)
def test_invalid_files_are_rejected(
    tmp_path: Path,
    clients: object,
) -> None:
    with pytest.raises(RuntimeError):
        ClientRegistry(write(tmp_path / "clients.json", clients))


def test_repository_clients_file_is_valid() -> None:
    registry = ClientRegistry("gateway/auth_clients.json")

    assert registry.default_client_id == "admin"

    for client_id in ("admin", "primary", "secondary", "tertiary", "operator", "diagnostics"):
        assert registry.identity(client_id, authenticated=False) is not None

    admin = registry.identity("admin", authenticated=False)

    assert admin is not None and admin.can("robot.program")

    for client_id in ("primary", "secondary", "tertiary", "operator", "diagnostics"):
        identity = registry.identity(client_id, authenticated=False)

        assert identity is not None and not identity.can("robot.program")
