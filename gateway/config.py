from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Robot & Camera Gateway"
    api_prefix: str = "/api/v1"
    security_mode: Literal["development", "strict"] = "strict"
    auth_clients_file: str = "gateway/auth_clients.json"

    control_lease_seconds: float = 5.0

    calibration_file: str = "gateway/calibration.json"

    model_config = SettingsConfigDict(
        env_file="gateway/.env",
        env_prefix="GATEWAY_",
        extra="ignore",
    )

    camera_provider: Literal[
        "mock",
        "oak",
    ] = "oak"

    camera_ip: str = "169.254.1.223"

    camera_fps: float = 4.0

    camera_rgb_width: int = 1920
    camera_rgb_height: int = 1080

    #
    # /camera/rgb and /camera/depth answer 503 instead of serving
    # a frame older than this, so a client never acts on an old
    # scene after the camera has stalled or disconnected.
    #
    camera_max_frame_age_seconds: float = 5.0

    robot_provider: Literal[
        "mock",
        "tcp",
    ] = "tcp"

    #
    # Address of the robot controller, used for both the legacy
    # RAPID socket server (robot_port, tcp provider) and Robot
    # Web Services (rws_port).
    #
    robot_host: str = "192.168.125.1"
    robot_port: int = 5001

    #
    # Robot Web Services (RWS 1.0, RobotWare 6) on the robot
    # controller, used for arm poses, RAPID program control and
    # /robot/rws moves.
    #
    rws_port: int = 80
    rws_user: str = "Default User"
    rws_password: str = "robotics"
    rws_timeout: float = 10.0

    #
    # How long POST /robot/rws/* waits for the arm to finish.
    #
    rws_move_timeout: float = 60.0

    #
    # Box that /robot/rws/move targets must lie in, tool0 (the
    # flange) in wobj0, mm, for both arms. The arm tasks do not
    # check it themselves, so the gateway does. Holds the picking
    # poses (gripper down: flange 159 mm above the TCP) and the
    # camera calibration poses (gripper along +x: flange 159 mm
    # behind the TCP in x, at its height).
    #
    rws_safe_zone_min: tuple[float, float, float] = (180.0, -400.0, 50.0)
    rws_safe_zone_max: tuple[float, float, float] = (800.0, 500.0, 600.0)

    #
    # Speed of /robot/rws/move and /home when the request gives
    # none: TCP speed in mm/s (v50), and acceleration and ramp in %
    # of the maximum (RAPID AccSet).
    #
    rws_default_speed: float = 50.0
    rws_default_acceleration: float = 100.0
    rws_default_ramp: float = 100.0


@lru_cache
def get_settings() -> Settings:
    return Settings()