"""
Subscribe to gateway events over the WebSocket and print them.

The WebSocket only works with the gateway in development mode; in
strict mode the connection is refused.

Usage (from the repository root):
    python gateway/scripts/watch_events.py
    python gateway/scripts/watch_events.py --client diagnostics --events control robot.rapid

Needs: websockets
"""

import argparse
import json

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
    parser.add_argument("--client", default="autonomy", help="Development client id")
    parser.add_argument("--events", nargs="+", default=DEFAULT_EVENTS, help="Event names or prefixes")

    args = parser.parse_args()

    with connect(f"{args.url}?client={args.client}") as websocket:
        print(f"Connected as {args.client}", flush=True)

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
