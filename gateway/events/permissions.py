EVENT_PERMISSIONS = {
    "control": "robot.read",
    "robot.status": "robot.read",
    "robot.error": "robot.read",
    "camera.status": "camera.read",
}

def get_event_permission(
    event_name: str,
) -> str | None:
    if event_name in EVENT_PERMISSIONS:
        return EVENT_PERMISSIONS[event_name]

    return None