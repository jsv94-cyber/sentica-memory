# M1 review summary

One process, one tenant. The database is reached only through PgBouncer
with a client certificate. The instance has no database password.

`SENTICA_MODE` unset leaves the upstream suite unchanged. `production` is
the only accepted mode. Anything else refuses to start.

## Where isolation is enforced

Startup (`src/sentica/runtime.py:77`). Missing tenant, proxy address, client
certificate, key, proxy CA, token public key, issuer, or bind host refuses
to start. A database password in `DB_CONNECTION_URI`, `DATABASE_URL`,
`POSTGRES_PASSWORD`, `DB_PASSWORD`, or `PGPASSWORD` refuses to start
(`src/sentica/runtime.py:28`). Bind addresses `0.0.0.0`, `::`, and empty
refuse (`src/sentica/runtime.py:107`). Importing `src.db` in production
without a tenant exits before the server listens
(`tests/sentica/test_m1_startup.py:110`).

Database (`src/db.py:62`). The URI is passwordless and points at the proxy.
`settings.DB.SCHEMA` is `memory`. Pool recycle is capped at 3600 seconds
(`src/db.py:58`). TLS is `sslmode=verify-full` with the client certificate,
key, and proxy CA (`src/sentica/db_connect.py:23`). Checkout drops a pooled
connection when the certificate file changes
(`src/sentica/db_connect.py:46`). Alembic builds a second engine, so the
same TLS arguments are applied in `migrations/env.py` before
`engine_from_config`.

API tokens (`src/main.py:231` and `src/security.py:209`). Every path except
`/health` and `/metrics` (`src/main.py:227`) requires a broker token.
Production verification runs before the upstream `USE_AUTH=false` admin
bypass, so that bypass is not reachable. Tokens are EdDSA or RS256, checked
with the public key only (`src/sentica/tokens.py:40`). Issuer, expiry of at
most 5 minutes, request id, and matching `tenant_id` are required. An `ad`
claim is refused (`src/sentica/tokens.py:89`). Refusal details are fixed
phrases. The token is not logged.

Deriver (`src/deriver/enqueue.py:73` and the other three insert sites,
`src/deriver/consumer.py:57`, `src/deriver/queue_manager.py:674`). Production
payloads gain `tenant_id`. A missing or different tenant raises
`SenticaTenantRefused`, which is not a retryable error, so the row is marked
errored. A database disconnect still uses the existing retryable path, so
the worker backs off and resumes.

Proxy drop (`src/main.py:273`). In production, `OperationalError`,
`InterfaceError`, and `DisconnectionError` become HTTP 503
`{"detail":"memory unavailable"}`. The log line is the exception class name.

Schema (`docker/sentica/init-tenants.sql`). Each tenant has its own database
and role. The role can use schema `memory` only. `public.outside_memory` is
revoked. Extensions `vector` and `pg_trgm` are installed in `memory` by the
init script because the role is not a superuser.

## Tests that prove it

| Acceptance | Test |
| --- | --- |
| Missing tenant, certificate, key, password, or public bind refuses startup | `tests/sentica/test_m1_startup.py:61` through `:110`, and `tests/sentica/test_m1_stack.py:234` |
| Missing, expired, forged, wrong-issuer, over-lifetime, and admin tokens refused | `tests/sentica/test_m1_tokens.py:97` and `tests/sentica/test_m1_stack.py:255` |
| Token for B refused by A's API on every non-probe endpoint | `tests/sentica/test_m1_stack.py:255` walks the OpenAPI paths. B's key fails closed as forged. A's key with `tenant_id` of B fails as wrong tenant. `/deriver/metrics` is included. |
| Admin list route unreachable | same test, `POST /v3/workspaces/list` returns 401 `Resource requires admin privileges` |
| Deriver task tagged B refused by A's worker, and the canary string is absent from the worker log | `tests/sentica/test_m1_tokens.py:119` and `tests/sentica/test_m1_stack.py:498` |
| A's certificate cannot open B's database through B's proxy | `tests/sentica/test_m1_stack.py:255` |
| A's role cannot read `public.outside_memory` | `tests/sentica/test_m1_stack.py:255` |
| Certificate swap during requests causes zero failures | `tests/sentica/test_m1_stack.py:402` |
| Killing the proxy mid-transaction leaves no inserted rows, and a later API write returns 503 `memory unavailable` | `tests/sentica/test_m1_stack.py:431` |
| After stop, Postgres and Redis data are tmpfs and no project volume remains | `tests/sentica/test_m1_stack.py:547` and the fixture teardown |

Local run on 2026-10-11: `uv run pytest` with `SENTICA_M1_STACK=1` against
Postgres on 127.0.0.1:5433. 3158 passed, 53 skipped, 0 failed. The stack
file itself was 6 passed. `ruff check` and `basedpyright` were clean.

## Decisions a reviewer should see

- Unset `SENTICA_MODE` is the upstream suite, not a default tenant.
- Production does not set `AUTH_USE_AUTH`. Doing so required `JWT_SECRET`,
  and the instance must not hold a symmetric signing key.
- Refusal counts are in-process (`src/sentica/refusals.py`), not Prometheus.
  M9 exports them. A new series here would have to join the zero-init registry.
- `docker-compose.yml` still has the development password so M0's
  `docker compose up` keeps working. The production-shaped stack is
  `docker-compose.sentica.yml`.
- On that compose network, Postgres trusts the proxy and has no password.
  The instance environment does not contain one. A client that skips the
  proxy is a network-policy item for a later milestone.
- `/health` and `/metrics` have no token. They return no memory content.
- Representation payloads still contain message text. That is the M0
  deferral. The tenant stamp is an id and is removed before upstream
  payload models validate.
- The local CA and certificate files are generated into `/dev/shm` by the
  test. They are not in git.
