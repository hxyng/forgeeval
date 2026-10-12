import pytest
from pydantic import ValidationError

from forgeeval.config import Settings


def test_database_url_is_built_from_rds_secret_parts() -> None:
    s = Settings(
        db_host="db.abc.us-east-1.rds.amazonaws.com",
        db_username="forgeeval",
        db_password="p@ss:w/rd#1",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert s.database_url == (
        "postgresql+psycopg://forgeeval:p%40ss%3Aw%2Frd%231"
        "@db.abc.us-east-1.rds.amazonaws.com:5432/forgeeval"
    )


def test_parts_require_credentials() -> None:
    with pytest.raises(ValidationError, match="DB_USERNAME and DB_PASSWORD"):
        Settings(db_host="db", _env_file=None)  # type: ignore[call-arg]


def test_explicit_url_is_kept_without_parts() -> None:
    url = "postgresql+psycopg://u:p@localhost:5433/x"
    assert Settings(database_url=url, _env_file=None).database_url == url  # type: ignore[call-arg]
