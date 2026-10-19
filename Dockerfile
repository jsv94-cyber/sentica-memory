# syntax=docker/dockerfile:1

# https://pythonspeed.com/articles/base-image-python-docker-images/
# https://testdriven.io/blog/docker-best-practices/
# Digests are the linux/amd64 manifests recorded for a reproducible default
# image. python:3.13-slim-bookworm already has the users group at gid 100.
ARG SOURCE_DATE_EPOCH=1760832000
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.24@sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5 /uv /bin/uv

WORKDIR /app

# uv's parallel bytecode compiler does not write the same files on every
# run. Compile with one worker after the install instead.
ARG SOURCE_DATE_EPOCH=1760832000
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}
ENV UV_COMPILE_BYTECODE=0

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Python optimizations
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy only requirements to cache them in docker layer.
# --chmod fixes the mode so the host umask cannot change the layer.
COPY --chmod=644 uv.lock pyproject.toml /app/

# Optionall include lancedb with:
#   docker build --build-arg INSTALL_LANCEDB=true .
ARG INSTALL_LANCEDB=false
RUN --mount=type=cache,target=/root/.cache/uv \
    umask 0022 \
    && if [ "$INSTALL_LANCEDB" = "true" ]; then \
        uv sync --frozen --no-install-project --no-group dev --extra lancedb; \
    elif [ "$INSTALL_LANCEDB" = "false" ]; then \
        uv sync --frozen --no-install-project --no-group dev; \
    else \
        echo "INSTALL_LANCEDB must be 'true' or 'false'" >&2; \
        exit 2; \
    fi \
    && PYTHONDONTWRITEBYTECODE= PYTHONHASHSEED=0 \
        python -m compileall -q -j 1 -f /app/.venv \
    && find /app/.venv -type d -exec chmod 755 {} + \
    && find /app/.venv -type f ! -perm /111 -exec chmod 644 {} + \
    && find /app/.venv -type f -perm /111 -exec chmod 755 {} + \
    && mkdir /tmp/venv-sorted \
    && LC_ALL=C tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" -cf - -C /app .venv \
        | tar -C /tmp/venv-sorted -xf - \
    && rm -rf /app/.venv \
    && mv /tmp/venv-sorted/.venv /app/.venv

# Host checkout times survive into a COPY layer. BuildKit only clamps a
# timestamp that is newer than SOURCE_DATE_EPOCH, so a file written before
# that commit would otherwise change the layer. Stamp them here, then copy
# the stamped tree into the runtime image.
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS sources

ARG SOURCE_DATE_EPOCH=1760832000
WORKDIR /out
COPY --chmod=755 src/ /out/src/
COPY --chmod=755 migrations/ /out/migrations/
COPY --chmod=755 scripts/ /out/scripts/
COPY --chmod=755 docker/ /out/docker/
COPY --chmod=644 alembic.ini /out/alembic.ini
COPY --chmod=644 pyproject.toml /out/pyproject.toml
COPY --chmod=644 config.toml* /out/
RUN find /out -exec touch -d "@${SOURCE_DATE_EPOCH}" {} + \
    && find /out -type d -exec chmod 755 {} + \
    && find /out -type f -exec chmod 644 {} + \
    && chmod 755 /out/docker/entrypoint.sh /out/docker/migrate-entrypoint.sh \
        /out/docker/sentica/postgres-entry.sh /out/docker/sentica/20-passwords.sh

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV SENTICA_MODE=production
ARG SOURCE_DATE_EPOCH=1760832000
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

# The proof build passes the runner uid so certificate files can stay mode 600
# without a root chown on the host. The published image keeps uid 100.
ARG SENTICA_UID=100
ARG SENTICA_GID=100
# python:3.13-slim-bookworm already has the users group at gid 100.
# Reuse that gid. A second group at the same gid fails the default build.
RUN if [ "$SENTICA_UID" != "0" ]; then \
        if getent group "$SENTICA_GID" >/dev/null; then \
            true; \
        else \
            addgroup --gid "$SENTICA_GID" app; \
        fi; \
        if ! getent passwd "$SENTICA_UID" >/dev/null; then \
            adduser --uid "$SENTICA_UID" --gid "$SENTICA_GID" \
                --disabled-password --gecos "" --no-create-home app; \
        fi; \
    fi \
    && mkdir -p -m 755 /app/lancedb_data \
    && chmod 755 /app /app/lancedb_data \
    && chown "${SENTICA_UID}:${SENTICA_GID}" /app /app/lancedb_data \
    && if [ -f /etc/shadow ]; then \
        day="$((SOURCE_DATE_EPOCH / 86400))"; \
        sed -i "s/^app:!:[0-9][0-9]*:/app:!:${day}:/" /etc/shadow; \
    fi

COPY --from=builder --chown=${SENTICA_UID}:${SENTICA_GID} /app/.venv /app/.venv

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"
ENV HOME=/app

# Stamped in the sources stage, so the host clock and umask cannot change
# these layers.
COPY --from=sources --chown=${SENTICA_UID}:${SENTICA_GID} /out/src/ /app/src/
COPY --from=sources --chown=${SENTICA_UID}:${SENTICA_GID} /out/migrations/ /app/migrations/
COPY --from=sources --chown=${SENTICA_UID}:${SENTICA_GID} /out/scripts/ /app/scripts/
COPY --from=sources --chown=${SENTICA_UID}:${SENTICA_GID} /out/docker/ /app/docker/
COPY --from=sources --chown=${SENTICA_UID}:${SENTICA_GID} /out/alembic.ini /app/alembic.ini
# src/_version.py reads the service version from here at runtime, so this
# is a runtime input as well as a build input.
COPY --from=sources --chown=${SENTICA_UID}:${SENTICA_GID} /out/pyproject.toml /app/pyproject.toml
# Copy config files - this will copy config.toml if it exists, and config.toml.example
COPY --from=sources --chown=${SENTICA_UID}:${SENTICA_GID} /out/config.toml* /app/

# The default image is a production image. A test-stack build can opt out.
# The eval harness and its seeded fixture text are not part of the image.
ARG SENTICA_BAKE_PRODUCTION_MARKER=true
RUN rm -f /app/src/sentica/eval_harness.py \
    && if grep -R -F -q "northline violetkey" /app/src; then \
        echo "seeded fixture text is in the image" >&2; \
        exit 1; \
    fi \
    && if [ "$SENTICA_BAKE_PRODUCTION_MARKER" = "true" ]; then \
        printf '%s\n' production > /app/.sentica-production-image; \
    fi \
    && chown "${SENTICA_UID}:${SENTICA_GID}" /app/.sentica-production-image 2>/dev/null || true \
    && find /app/src /app/migrations /app/scripts /app/docker -type d -exec chmod 755 {} + \
    && find /app/src /app/migrations /app/scripts /app/docker -type f -exec chmod 644 {} + \
    && chmod 755 /app/docker/entrypoint.sh /app/docker/migrate-entrypoint.sh \
        /app/docker/sentica/postgres-entry.sh /app/docker/sentica/20-passwords.sh \
    && chmod 644 /app/alembic.ini /app/pyproject.toml \
    && for cfg in /app/config.toml /app/config.toml.example; do \
        if [ -f "$cfg" ]; then chmod 644 "$cfg"; fi; \
    done

# The instance networks have no route to the public internet. tiktoken would
# otherwise download these encodings the first time the API imports them.
# The URLs and SHA-256 values are the ones tiktoken checks.
ENV TIKTOKEN_CACHE_DIR=/app/tiktoken-cache
RUN python - <<'PY'
import hashlib
import urllib.request
from pathlib import Path

import tiktoken

files = {
    "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken":
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken":
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
}
cache = Path("/app/tiktoken-cache")
cache.mkdir(parents=True, exist_ok=True)
for url, expected in files.items():
    data = urllib.request.urlopen(url, timeout=60).read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit("tiktoken hash mismatch for " + url)
    (cache / hashlib.sha1(url.encode()).hexdigest()).write_bytes(data)
names = sorted(path.name for path in cache.iterdir())
if len(names) != len(files):
    raise SystemExit("tiktoken cache file count")
tiktoken.get_encoding("o200k_base")
tiktoken.get_encoding("cl100k_base")
if sorted(path.name for path in cache.iterdir()) != names:
    raise SystemExit("tiktoken cache changed while loading")
PY
RUN chmod 755 /app/tiktoken-cache \
    && chmod 644 /app/tiktoken-cache/* \
    && touch -d "@${SOURCE_DATE_EPOCH}" /app/tiktoken-cache /app/tiktoken-cache/* \
    && chown -R "${SENTICA_UID}:${SENTICA_GID}" /app/tiktoken-cache

USER ${SENTICA_UID}:${SENTICA_GID}

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
