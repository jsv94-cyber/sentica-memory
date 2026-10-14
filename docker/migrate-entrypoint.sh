#!/bin/sh
# One-shot migration. The API and the deriver do not run this script.
set -eu

case "${SENTICA_MODE-}" in
    production|test)
        ;;
    *)
        echo "Refusing to start: SENTICA_MODE must be 'production' or 'test'" >&2
        exit 1
        ;;
esac

if [ -x /app/.venv/bin/python ]; then
    python_bin=/app/.venv/bin/python
else
    python_bin="uv run python"
fi

migration_timeout="${SENTICA_MIGRATION_TIMEOUT_SECONDS:-360}"
lock_wait=$($python_bin -c 'from src.config import settings; print(settings.DB.MIGRATION_LOCK_WAIT_SECONDS)')
case "$migration_timeout" in
    ''|*[!0-9]*)
        echo "Refusing to start: SENTICA_MIGRATION_TIMEOUT_SECONDS must be an integer" >&2
        exit 1
        ;;
esac
case "$lock_wait" in
    ''|*[!0-9]*)
        echo "Refusing to start: DB_MIGRATION_LOCK_WAIT_SECONDS must be an integer" >&2
        exit 1
        ;;
esac
if [ "$migration_timeout" -le "$lock_wait" ]; then
    echo "Refusing to start: SENTICA_MIGRATION_TIMEOUT_SECONDS must be greater than DB_MIGRATION_LOCK_WAIT_SECONDS" >&2
    exit 1
fi

echo "Running database migrations..."
# shellcheck disable=SC2086
timeout "$migration_timeout" $python_bin scripts/provision_db.py
echo "Database initialized successfully"
