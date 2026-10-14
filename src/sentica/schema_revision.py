"""Refuse to serve when the database revision is not this code's head."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from src.sentica.runtime import SenticaStartupError


def head_revision() -> str:
    """The revision this process was built with."""
    root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    head = script.get_current_head()
    if head is None:
        raise SenticaStartupError("Refusing to start: schema revision is behind")
    return head


def assert_revision(connection: Connection, schema: str) -> None:
    """Raise when ``alembic_version`` is missing or is not the code's head."""
    if not schema.replace("_", "").isalnum():
        raise SenticaStartupError("Refusing to start: schema revision is behind")
    head = head_revision()
    try:
        current: object = connection.execute(
            text(f'SELECT version_num FROM "{schema}".alembic_version')
        ).scalar()
    except Exception as exc:
        raise SenticaStartupError(
            "Refusing to start: schema revision is behind"
        ) from exc
    if current != head:
        raise SenticaStartupError("Refusing to start: schema revision is behind")


def assert_schema_at_head() -> None:
    """Open one production connection and compare the stored revision to head."""
    from src.config import settings
    from src.sentica.db_connect import production_connect_args
    from src.sentica.runtime import load_instance

    instance = load_instance()
    if instance is None:
        return
    # Production tables live in memory. src.db sets the same value when the
    # process imports it. This check runs before that import.
    settings.DB.SCHEMA = "memory"
    engine = create_engine(
        instance.connection_uri(),
        connect_args=production_connect_args(),
    )
    try:
        with engine.connect() as connection:
            assert_revision(connection, settings.DB.SCHEMA)
    finally:
        engine.dispose()
