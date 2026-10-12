from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from forgeeval.config import get_settings

_settings = get_settings()
engine = create_engine(_settings.database_url, pool_pre_ping=True)
if _settings.db_secret_arn:
    from forgeeval.aws import client
    from forgeeval.db.credentials import RotatingCredentials, use_rotating_credentials

    use_rotating_credentials(
        engine, RotatingCredentials(client("secretsmanager"), _settings.db_secret_arn)
    )
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
