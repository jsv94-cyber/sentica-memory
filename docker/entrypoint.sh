#!/bin/sh
set -e

case "${SENTICA_MODE-}" in
    production|test)
        ;;
    *)
        echo "Refusing to start: SENTICA_MODE must be 'production' or 'test'" >&2
        exit 1
        ;;
esac

echo "Running database migrations..."
/app/.venv/bin/python scripts/provision_db.py

echo "Starting API server..."
BIND_HOST="${SENTICA_BIND_HOST:-0.0.0.0}"
if [ "${SENTICA_MODE:-}" = "production" ]; then
    if [ "${SENTICA_BIND_HOST:-}" = "private" ]; then
        # The address on the container's outbound interface. Not 0.0.0.0.
        BIND_HOST="$(/app/.venv/bin/python -c 'import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("192.0.2.1", 9)); print(s.getsockname()[0]); s.close()')"
        export SENTICA_BIND_HOST="$BIND_HOST"
    fi
    if [ -z "${SENTICA_BIND_HOST:-}" ] || [ "$SENTICA_BIND_HOST" = "0.0.0.0" ] || [ "$SENTICA_BIND_HOST" = "::" ] || [ "$SENTICA_BIND_HOST" = "private" ]; then
        echo "Refusing to start: SENTICA_BIND_HOST must be a private interface" >&2
        exit 1
    fi
    BIND_HOST="$SENTICA_BIND_HOST"
fi
exec /app/.venv/bin/fastapi run --host "$BIND_HOST" --workers "${API_WORKERS:-1}" src/main.py
