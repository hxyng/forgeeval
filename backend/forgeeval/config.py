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

    # Model under test / LLM judge. Any OpenAI-compatible Chat Completions endpoint.
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_strip_provider: bool = False  # "openai/gpt-5-mini" -> "gpt-5-mini" for direct APIs
    model_timeout_seconds: float = 60.0

    # Worker
    max_attempts: int = 5
    lease_seconds: float = 120.0
    requeue_stale_after_seconds: float = 600.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
