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
    # RAPID task holding the command variables of
    # gateway/rapid/LegacyShared.mod, written by POST
    # /robot/rws/move and /robot/rws/home, and how long to wait
    # for a move to finish.
    #
    rws_motion_task: str = "T_ROB_L"
    rws_move_timeout: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()