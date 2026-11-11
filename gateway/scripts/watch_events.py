"""
Subscribe to gateway events over the WebSocket and print them.

Strict mode: the Bearer token (--token, default the GATEWAY_TOKEN
environment variable) goes in the upgrade request, as on the HTTP
routes. Development mode: the client is ?client=<--client>.

Usage (from the repository root):
    python gateway/scripts/watch_events.py
    python gateway/scripts/watch_events.py --client diagnostics --events control robot.rapid
    python gateway/scripts/watch_events.py --url ws://169.254.219.127:8000/api/v1/events --token <diagnostics token>

Needs: websockets
"""

import argparse
import json
import os

from websockets.sync.client import connect


DEFAULT_URL = "ws://127.0.0.1:8000/api/v1/events"

#
# Every event family in gateway/events/permissions.py.
#
DEFAULT_EVENTS = [
    "control",
    "robot.status",
    "robot.error",
    "robot.rapid",
    "camera.status",
    "calibration",
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print gateway events",
    )

    parser.add_argument("--url", default=DEFAULT_URL, help="Events WebSocket URL")
    parser.add_argument("--token", default=os.environ.get("GATEWAY_TOKEN"), help="Bearer token (strict mode; default GATEWAY_TOKEN)")
    parser.add_argument("--client", default="primary", help="Development client id")
    parser.add_argument("--events", nargs="+", default=DEFAULT_EVENTS, help="Event names or prefixes")

    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else None

    with connect(f"{args.url}?client={args.client}", additional_headers=headers) as websocket:
        print("Connected" + ("" if args.token else f" as {args.client}"), flush=True)

        websocket.send(
            json.dumps(
                {
                    "action": "subscribe",
                    "events": args.events,
                }
            )
        )

        try:
            for message in websocket:
                print(json.dumps(json.loads(message), indent=2), flush=True)

        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
