import asyncio
import json

import websockets


async def main():
    uri = (
        "ws://127.0.0.1:8000"
        "/api/v1/events"
        "?client=autonomy"
    )

    async with websockets.connect(uri) as websocket:
        print("Connected")

        await websocket.send(
            json.dumps(
                {
                    "action": "subscribe",
                    "events": [
                        "control",
                        "robot.status",
                    ],
                }
            )
        )

        while True:
            message = await websocket.recv()

            event = json.loads(message)

            print(
                json.dumps(
                    event,
                    indent=2,
                )
            )


asyncio.run(main())