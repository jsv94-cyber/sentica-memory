#!/bin/sh
# The image ENTRYPOINT. A CMD cannot skip this script.
# Mode and the bind address are checked before any command, including the deriver.
set -eu

case "${SENTICA_MODE-}" in
    production|test)
        ;;
    *)
        echo "Refusing to start: SENTICA_MODE must be 'production' or 'test'" >&2
        exit 1
        ;;
esac

if [ -f /app/.sentica-production-image ] && [ "${SENTICA_MODE}" = "test" ]; then
    echo "Refusing to start: production image cannot run with SENTICA_MODE=test" >&2
    exit 1
fi

if [ -s /secret/password ]; then
    redis_password=$(tr -d '\n' < /secret/password)
    redis_host=${SENTICA_REDIS_HOST:?SENTICA_REDIS_HOST is required}
    export CACHE_URL="redis://:${redis_password}@${redis_host}:6379/0?suppress=true"
    unset redis_password
fi

if [ "${SENTICA_MODE}" = "production" ]; then
    if ! /app/.venv/bin/python -c 'import ipaddress, os, sys
raw = os.environ.get("SENTICA_BIND_HOST", "").strip()
try:
    address = ipaddress.ip_address(raw)
except ValueError:
    sys.exit(1)
sys.exit(0 if not address.is_unspecified else 1)'; then
        echo "Refusing to start: SENTICA_BIND_HOST must be an explicit address" >&2
        exit 1
    fi
    BIND_HOST="$SENTICA_BIND_HOST"
else
    BIND_HOST="${SENTICA_BIND_HOST:-0.0.0.0}"
fi

if [ "${SENTICA_MODE}" = "production" ]; then
    if [ -x /app/.venv/bin/python ]; then
        python_bin=/app/.venv/bin/python
    else
        python_bin="uv run python"
    fi
    # shellcheck disable=SC2086
    if ! $python_bin -c 'from src.sentica.schema_revision import assert_schema_at_head; assert_schema_at_head()'; then
        echo "Refusing to start: schema revision is behind" >&2
        exit 1
    fi
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "Starting API server..."
exec /app/.venv/bin/fastapi run --host "$BIND_HOST" --workers "${API_WORKERS:-1}" src/main.py
