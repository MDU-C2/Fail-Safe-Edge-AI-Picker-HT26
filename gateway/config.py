from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Robot & Camera Gateway"
    api_prefix: str = "/api/v1"
    security_mode: Literal["development", "strict"] = "development"

    control_lease_seconds: float = 5.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="GATEWAY_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()