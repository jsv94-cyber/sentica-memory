# syntax=docker/dockerfile:1

# https://pythonspeed.com/articles/base-image-python-docker-images/
# https://testdriven.io/blog/docker-best-practices/
FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /bin/uv

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Python optimizations
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy only requirements to cache them in docker layer
COPY uv.lock pyproject.toml /app/

# Optionall include lancedb with:
#   docker build --build-arg INSTALL_LANCEDB=true .
ARG INSTALL_LANCEDB=false
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_LANCEDB" = "true" ]; then \
        uv sync --frozen --no-install-project --no-group dev --extra lancedb; \
    elif [ "$INSTALL_LANCEDB" = "false" ]; then \
        uv sync --frozen --no-install-project --no-group dev; \
    else \
        echo "INSTALL_LANCEDB must be 'true' or 'false'" >&2; \
        exit 2; \
    fi

FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV SENTICA_MODE=production

# The proof build passes the runner uid so certificate files can stay mode 600
# without a root chown on the host. The published image keeps uid 100.
ARG SENTICA_UID=100
ARG SENTICA_GID=100
RUN if [ "$SENTICA_UID" != "0" ]; then \
        addgroup --gid "$SENTICA_GID" app \
        && adduser --uid "$SENTICA_UID" --gid "$SENTICA_GID" \
            --disabled-password --gecos "" --no-create-home app; \
    fi \
    && mkdir -p /app/lancedb_data \
    && chown "${SENTICA_UID}:${SENTICA_GID}" /app /app/lancedb_data

COPY --from=builder --chown=${SENTICA_UID}:${SENTICA_GID} /app/.venv /app/.venv

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"
ENV HOME=/app

COPY --chown=${SENTICA_UID}:${SENTICA_GID} src/ /app/src/
COPY --chown=${SENTICA_UID}:${SENTICA_GID} migrations/ /app/migrations/
COPY --chown=${SENTICA_UID}:${SENTICA_GID} scripts/ /app/scripts/
COPY --chown=${SENTICA_UID}:${SENTICA_GID} docker/ /app/docker/
COPY --chown=${SENTICA_UID}:${SENTICA_GID} alembic.ini /app/alembic.ini
# src/_version.py reads the service version from here at runtime, so this
# is a runtime input as well as a build input.
COPY --chown=${SENTICA_UID}:${SENTICA_GID} pyproject.toml /app/pyproject.toml
# Copy config files - this will copy config.toml if it exists, and config.toml.example
COPY --chown=${SENTICA_UID}:${SENTICA_GID} config.toml* /app/

# The default image is a production image. A test-stack build can opt out.
ARG SENTICA_BAKE_PRODUCTION_MARKER=true
RUN if [ "$SENTICA_BAKE_PRODUCTION_MARKER" = "true" ]; then \
        printf '%s\n' production > /app/.sentica-production-image; \
    fi \
    && chown "${SENTICA_UID}:${SENTICA_GID}" /app/.sentica-production-image 2>/dev/null || true \
    && chmod 755 /app/docker/entrypoint.sh \
    && chmod 755 /app/docker/sentica/postgres-entry.sh /app/docker/sentica/20-passwords.sh

USER ${SENTICA_UID}:${SENTICA_GID}

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
