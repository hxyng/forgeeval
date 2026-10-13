from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    database_url: str = "postgresql+psycopg://forgeeval:forgeeval@localhost:5433/forgeeval"
    # In ECS the RDS-managed secret is injected as separate parts; when DB_HOST is set they
    # take precedence over DATABASE_URL.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "forgeeval"
    db_username: str | None = None
    db_password: str | None = None
    # Preferred in ECS: read credentials from the RDS-managed secret at connect time so password
    # rotation never strands a running task (see forgeeval/db/credentials.py).
    db_secret_arn: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    queue_stream: str = "forgeeval:eval-jobs"

    # AWS (ECS task role supplies credentials; AWS_ENDPOINT_URL targets local emulators)
    queue_backend: Literal["redis", "sqs"] = "redis"
    sqs_queue_url: str | None = None
    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None  # unset: trace payloads stay inline in Postgres
    payload_offload_bytes: int = 64 * 1024
    api_key_pepper: str = "local-dev-pepper-change-me"

    # Model under test / LLM judge. Any OpenAI-compatible Chat Completions endpoint.
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_strip_provider: bool = False  # "openai/gpt-5-mini" -> "gpt-5-mini" for direct APIs
    model_timeout_seconds: float = 60.0

    # Model-call cache (generations and judge verdicts); see forgeeval/model_cache.py
    cache_enabled: bool = True
    cache_max_age_days: int = 30
    cache_sampled_outputs: bool = False  # cache temperature > 0 calls too (a draw, not an answer)

    # Worker
    max_attempts: int = 5
    lease_seconds: float = 120.0
    requeue_stale_after_seconds: float = 600.0

    @model_validator(mode="after")
    def _database_url_from_parts(self) -> "Settings":
        if self.db_host:
            where = f"{self.db_host}:{self.db_port}/{self.db_name}"
            if self.db_secret_arn:
                # user and password are supplied per connection from the secret
                self.database_url = f"postgresql+psycopg://{where}"
            elif self.db_username and self.db_password:
                user, password = quote(self.db_username, safe=""), quote(self.db_password, safe="")
                self.database_url = f"postgresql+psycopg://{user}:{password}@{where}"
            else:
                raise ValueError(
                    "DB_HOST is set, so DB_SECRET_ARN or DB_USERNAME and DB_PASSWORD are required"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
