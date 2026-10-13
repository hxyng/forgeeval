from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    database_url: str = "postgresql+psycopg://forgeeval:forgeeval@localhost:5433/forgeeval"
    redis_url: str = "redis://localhost:6379/0"
    queue_stream: str = "forgeeval:eval-jobs"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "forgeeval-local"
    api_key_pepper: str = "local-dev-pepper-change-me"


@lru_cache
def get_settings() -> Settings:
    return Settings()
