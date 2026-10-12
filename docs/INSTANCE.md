# Single-tenant instance

Each process serves one tenant. It reaches Postgres only through a
certificate-authenticated proxy. It does not hold a database password.

## Private interface

Set `SENTICA_BIND_HOST` to the explicit address of the private interface
Sentica's broker uses. A hostname, `private`, `0.0.0.0`, `::`, and an empty
value refuse to start.

The API must never be published to the internet, and it must never be
reachable from an agent container. The broker is the only client. In the
local proof, published ports bind `127.0.0.1` so a test on this machine can
call the API. That bind is not a production exposure.

`/health` and `/metrics` answer without a token so an orchestrator can probe
the process. They return no memory content. Every other path, including
`/v3` and `/deriver/metrics`, requires a broker token for this tenant.

## Production configuration

`SENTICA_MODE` must be `production` or `test`. Unset, empty, and any other
value refuse to start. `production` is the single-tenant instance. `test`
keeps upstream behavior for the suite. The image defaults to `production`.
The local `docker-compose.yml` stack and the test commands set `test`.

Required settings, with no defaults:

- `SENTICA_TENANT_ID`
- `SENTICA_DB_PROXY_HOST` and `SENTICA_DB_PROXY_PORT`
- `SENTICA_DB_USER` and `SENTICA_DB_NAME`
- `SENTICA_CLIENT_CERT_PATH`, `SENTICA_CLIENT_KEY_PATH`, `SENTICA_PROXY_CA_PATH`
- `SENTICA_TOKEN_PUBLIC_KEY_PATH` and `SENTICA_TOKEN_ISSUER`
- `SENTICA_BIND_HOST`

`DB_CONNECTION_URI`, `DATABASE_URL`, `POSTGRES_PASSWORD`, `DB_PASSWORD`, and
`PGPASSWORD` refuse startup. The connection string is built without a
password and uses `sslmode=verify-full`.

Client certificates are valid for 48 hours and are meant to be replaced
every 24 hours. New connections read the certificate and key files again.
Replace them by writing a temporary file in the same directory and renaming
it onto the destination, certificate and key together, so a reader never
sees a partial pair. A pooled connection is dropped on checkout when either
file changes. Pooled connections are also recycled after at most one hour.

Tokens are EdDSA or RS256, checked with the public key on the instance.
The issuer, an expiry of at most 5 minutes, a request id, and `tenant_id`
equal to this instance are required. An `ad` admin claim is refused.
Expired, forged, wrong-issuer, and wrong-tenant tokens are refused and
counted. The log line is a fixed phrase and does not include the token.

Honcho tables live in the schema `memory`. The database role used by the
instance is granted that schema only.

If the proxy drops a connection, an in-flight transaction rolls back. The
API answers `503` with `{"detail":"memory unavailable"}`. The deriver treats
the disconnect as retryable, backs off, and tries the same queue row again.

## Local proof

`docker-compose.yml` is the upstream-compatible stack from M0. It still has
a development database password so `docker compose up` and the unit suite
keep working.

`docker-compose.sentica.yml` is the production-shaped stack: two instances,
two PgBouncer processes with client certificates, one Postgres whose data
directory is tmpfs, and two memory-only Redis processes. Generate a local
test CA into a tmpfs directory and pass it as `CERT_DIR`:

```bash
CERT_DIR=$(mktemp -d /dev/shm/sentica-m1-XXXX)
uv run python scripts/sentica_test_ca.py --out "$CERT_DIR"
CERT_DIR="$CERT_DIR" docker compose -f docker-compose.sentica.yml up -d --build
```

The acceptance test does this itself:

```bash
SENTICA_MODE=test uv run pytest tests/sentica/test_m1_stack.py -n0
```

Postgres on that compose network accepts TCP only from the two PgBouncer
addresses, and only as the matching tenant role. That allowlist is
test-only. The instance environment does not contain a password. Do not
publish the database port.

Nothing in this stack writes memory content to the host disk. Postgres data,
Redis data, `/tmp`, and the certificate directory are tmpfs. Queue payloads
live in Postgres. See `PROGRESS.md`.
