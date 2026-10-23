"""composite index on documents (workspace_name, observer, observed)

Conclusion reads filter that triple together. A separate index on each
column cannot satisfy the three equalities in one lookup.

The index is built CONCURRENTLY so document writes are not blocked. When
env.py runs on a caller-supplied connection, that path builds the index with
a plain CREATE INDEX because an autocommit block cannot run there.

Revision ID: e7c2a91b4d08
Revises: d4a8c1e27b90
Create Date: 2026-10-23

"""

import logging
from collections.abc import Generator, Sequence
from contextlib import contextmanager

from alembic import op
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from migrations.utils import get_schema

revision: str = "e7c2a91b4d08"
down_revision: str | None = "d4a8c1e27b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

schema = get_schema()

INDEX_NAME = "ix_documents_workspace_observer_observed"
BUILD_TIMEOUT = "60min"
COLUMNS = ["workspace_name", "observer", "observed"]

logger = logging.getLogger("alembic")


def _index_valid(connection: Connection) -> bool | None:
    """Return the index's validity, or None if it doesn't exist."""
    return connection.scalar(
        text(
            "SELECT i.indisvalid FROM pg_index i"
            + " JOIN pg_class c ON c.oid = i.indexrelid"
            + " JOIN pg_namespace n ON n.oid = c.relnamespace"
            + " WHERE c.relname = :name AND n.nspname = :schema"
        ),
        {"name": INDEX_NAME, "schema": schema},
    )


def _caller_owns_transaction() -> bool:
    """True when env.py is running inside a transaction the caller opened."""
    config = op.get_context().config
    return config is not None and not config.attributes.get("owns_transaction", True)


@contextmanager
def _build_timeouts(connection: Connection) -> Generator[None]:
    """Replace the session's lock/statement timeouts for a concurrent build."""
    lock_timeout = connection.scalar(text("SHOW lock_timeout"))
    statement_timeout = connection.scalar(text("SHOW statement_timeout"))
    connection.execute(text("SET lock_timeout = 0"))
    connection.execute(text(f"SET statement_timeout = '{BUILD_TIMEOUT}'"))
    try:
        yield
    finally:
        try:
            for name, value in (
                ("lock_timeout", lock_timeout),
                ("statement_timeout", statement_timeout),
            ):
                connection.execute(
                    text("SELECT set_config(:name, :value, false)"),
                    {"name": name, "value": value},
                )
        except DBAPIError:
            if not connection.invalidated:
                raise


def _create_index(*, concurrently: bool) -> None:
    op.create_index(
        INDEX_NAME,
        "documents",
        COLUMNS,
        schema=schema,
        postgresql_concurrently=concurrently,
        if_not_exists=True,
    )


def upgrade() -> None:
    connection = op.get_bind()
    if _caller_owns_transaction():
        op.create_index(
            INDEX_NAME,
            "documents",
            COLUMNS,
            schema=schema,
            if_not_exists=True,
        )
        return

    with op.get_context().autocommit_block(), _build_timeouts(connection):
        if _index_valid(connection) is False:
            op.drop_index(
                INDEX_NAME,
                table_name="documents",
                schema=schema,
                postgresql_concurrently=True,
                if_exists=True,
            )
        logger.info(
            "Building %s concurrently with CREATE INDEX CONCURRENTLY, up to %s",
            INDEX_NAME,
            BUILD_TIMEOUT,
        )
        _create_index(concurrently=True)


def downgrade() -> None:
    if _caller_owns_transaction():
        op.drop_index(INDEX_NAME, table_name="documents", schema=schema, if_exists=True)
        return
    connection = op.get_bind()
    with op.get_context().autocommit_block(), _build_timeouts(connection):
        op.drop_index(
            INDEX_NAME,
            table_name="documents",
            schema=schema,
            postgresql_concurrently=True,
            if_exists=True,
        )
