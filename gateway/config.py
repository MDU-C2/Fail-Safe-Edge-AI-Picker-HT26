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

    robot_host: str = "192.168.125.1"
    robot_port: int = 5001

    #
    # Robot Web Services (RWS 1.0, RobotWare 6) on the robot
    # controller, used for arm poses and RAPID program control.
    #
    rws_host: str = "192.168.125.1"
    rws_port: int = 80
    rws_user: str = "Default User"
    rws_password: str = "robotics"
    rws_timeout: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()