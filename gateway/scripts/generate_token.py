"""
Generate a random client token and its auth_clients.json entry.

The token is given to the client; only its SHA-256 hash goes into
gateway/auth_clients.json. Restart the gateway after editing it.

Usage (from the repository root):
    python gateway/scripts/generate_token.py autonomy
    python gateway/scripts/generate_token.py autonomy --priority 50 --permissions robot.read camera.read
"""

import argparse
import hashlib
import json
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a client token for strict mode",
    )

    parser.add_argument("client_id", help="Key of the client in auth_clients.json")
    parser.add_argument("--priority", type=int, default=50, help="Control priority (higher preempts)")
    parser.add_argument(
        "--permissions",
        nargs="+",
        default=["robot.read", "robot.control", "camera.read", "camera.control"],
        help="Permissions to grant",
    )

    args = parser.parse_args()

    token = secrets.token_urlsafe(32)

    entry = {
        args.client_id: {
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "permissions": args.permissions,
            "control_priority": args.priority,
        }
    }

    print("Token (give this to the client, it is not stored anywhere):")
    print(token)
    print()
    print("Entry for gateway/auth_clients.json:")
    print(json.dumps(entry, indent=4))


if __name__ == "__main__":
    main()
