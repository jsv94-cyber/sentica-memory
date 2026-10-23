"""composite index on documents (workspace_name, observer, observed)

Conclusion reads filter that triple together. A separate index on each
column cannot satisfy the three equalities in one lookup.

The index is built CONCURRENTLY so document writes are not blocked. When
env.py runs on a caller-supplied connection, that path builds the index with
a plain CREATE INDEX because an autocommit block cannot run there.

The migrate container stops at 360 seconds. ``lock_timeout`` and
``statement_timeout`` stay below that limit. A build that stops is refused,
and an invalid index is dropped before the revision returns.

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
# docker/migrate-entrypoint.sh stops the container at this many seconds.
_CONTAINER_LIMIT_SECONDS = 360
LOCK_TIMEOUT = "30s"
STATEMENT_TIMEOUT = "240s"
COLUMNS = ["workspace_name", "observer", "observed"]

logger = logging.getLogger("alembic")


def _timeout_seconds(value: str) -> int:
    if value.endswith("min") and value.removesuffix("min").isdigit():
        return int(value.removesuffix("min")) * 60
    if value.endswith("s") and value.removesuffix("s").isdigit():
        return int(value.removesuffix("s"))
    raise RuntimeError(
        "Refusing to migrate: timeout " + value + " is not a seconds or minutes value"
    )


def _require_under_container_limit() -> None:
    for label, value in (
        ("lock_timeout", LOCK_TIMEOUT),
        ("statement_timeout", STATEMENT_TIMEOUT),
    ):
        seconds = _timeout_seconds(value)
        if seconds <= 0 or seconds >= _CONTAINER_LIMIT_SECONDS:
            raise RuntimeError(
                "Refusing to migrate: "
                + label
                + " "
                + value
                + " is not below the migrate container limit of "
                + str(_CONTAINER_LIMIT_SECONDS)
                + "s"
            )


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
    """Keep the build inside the migrate container limit."""
    lock_timeout = connection.scalar(text("SHOW lock_timeout"))
    statement_timeout = connection.scalar(text("SHOW statement_timeout"))
    connection.execute(
        text("SELECT set_config('lock_timeout', :value, false)"),
        {"value": LOCK_TIMEOUT},
    )
    connection.execute(
        text("SELECT set_config('statement_timeout', :value, false)"),
        {"value": STATEMENT_TIMEOUT},
    )
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


def _drop_invalid(connection: Connection) -> None:
    if _index_valid(connection) is False:
        op.drop_index(
            INDEX_NAME,
            table_name="documents",
            schema=schema,
            postgresql_concurrently=True,
            if_exists=True,
        )


def _refuse(detail: str) -> RuntimeError:
    return RuntimeError(
        "Refusing to finish migrate: "
        + INDEX_NAME
        + " "
        + detail
        + " lock_timeout is "
        + LOCK_TIMEOUT
        + " and statement_timeout is "
        + STATEMENT_TIMEOUT
        + ", both below the migrate container limit of "
        + str(_CONTAINER_LIMIT_SECONDS)
        + "s. If a stuck lock leaves the index invalid, drop it concurrently"
        + " and migrate again."
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

    _require_under_container_limit()
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
            "Building %s concurrently with CREATE INDEX CONCURRENTLY,"
            + " lock_timeout %s, statement_timeout %s",
            INDEX_NAME,
            LOCK_TIMEOUT,
            STATEMENT_TIMEOUT,
        )
        try:
            _create_index(concurrently=True)
        except DBAPIError as exc:
            connection.rollback()
            try:
                _drop_invalid(connection)
            except DBAPIError:
                raise _refuse(
                    "stopped and the invalid index could not be dropped."
                ) from exc
            raise _refuse("stopped. The invalid index was dropped.") from exc
        if _index_valid(connection) is not True:
            try:
                _drop_invalid(connection)
            except DBAPIError as exc:
                raise _refuse(
                    "is not valid and the invalid index could not be dropped."
                ) from exc
            raise _refuse("is not valid. The invalid index was dropped.")


def downgrade() -> None:
    if _caller_owns_transaction():
        op.drop_index(INDEX_NAME, table_name="documents", schema=schema, if_exists=True)
        return
    connection = op.get_bind()
    _require_under_container_limit()
    with op.get_context().autocommit_block(), _build_timeouts(connection):
        try:
            op.drop_index(
                INDEX_NAME,
                table_name="documents",
                schema=schema,
                postgresql_concurrently=True,
                if_exists=True,
            )
        except DBAPIError as exc:
            raise _refuse(
                "could not be dropped. Drop it concurrently, then migrate again."
            ) from exc
