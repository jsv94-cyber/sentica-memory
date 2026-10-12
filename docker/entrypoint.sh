#!/bin/sh
# The image ENTRYPOINT. A CMD cannot skip this script.
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

if [ "$#" -gt 0 ]; then
    exec "$@"
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

echo "Running database migrations..."
/app/.venv/bin/python scripts/provision_db.py

echo "Starting API server..."
exec /app/.venv/bin/fastapi run --host "$BIND_HOST" --workers "${API_WORKERS:-1}" src/main.py
