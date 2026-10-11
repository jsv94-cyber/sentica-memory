# Progress

## Pin

Upstream remote: `https://github.com/plastic-labs/honcho.git` (`upstream`).

Pinned release: **v3.3.0**

Commit: `608aec8a928da13c540262ed980c569448fb53ab`

The fork's `main` was three commits behind that tag (changelog, trigram
index, contributing docs) plus the local goals commit. M0 merged the tag
in. `git describe` on the pin is `v3.3.0`.

## M0. Baseline and map

Built:

- Tracked `docker-compose.yml`: API, deriver, Postgres `pgvector/pgvector:pg15`, Redis 8.2.
- `docs/MAP.md` for the pinned tree.
- `NOTICE` and `FORK_CHANGES.md`.
- GitHub Actions runs `uv run pytest` on every pull request and every push to `main`.
- Acceptance tests in `tests/sentica/test_m0_baseline.py`.

Decisions:

- Local Compose still uses the upstream development database password and
  `AUTH_USE_AUTH=false`. Removing the password and requiring a client
  certificate is M1. Doing it in M0 would leave the pinned stack unable to
  boot, because the certificate proxy does not exist yet.
- Representation queue payloads still include message text
  (`src/utils/queue_payload.py:28`). The pinned deriver reads that field.
  Changing it here would diverge from the release and break the worker.
  Recorded in `docs/MAP.md`. A later milestone removes content from queue rows.
- Redis persistence is off and `/data` is tmpfs. API and deriver `/tmp` are
  tmpfs. Postgres data stays in the `pgdata` volume because the database is
  the store. The default vector store is pgvector, so the compose file does
  not mount a LanceDB volume.
- GCP, Fly, AWS, and live-provider workflows no longer run on push or pull
  request, and their external jobs are `if: false`. The upstream unit-test
  workflow still runs on GitHub-hosted runners with a local Postgres service.
- `docker-compose.yml.example` is unchanged so the upstream example stays
  diffable. `docker compose up` uses the tracked `docker-compose.yml`.
- Host ports default to 8000, 5432, and 6379 and can be moved with
  `SENTICA_API_PORT`, `SENTICA_POSTGRES_PORT`, and `SENTICA_REDIS_PORT`.
  This machine already had an unrelated Postgres container on 5432. The
  local proof used port 5433 and did not connect to that container.

Deviations from GOALS.md: none in the M0 deliverables. The two items above
(database password, queue message text) are left as upstream behavior until
the milestone that replaces them.

Test counts: `uv run pytest` on 2026-10-11, against the local Postgres
on host port 5433: **3140 passed, 53 skipped, 0 failed** (132.83s). The
53 skips are upstream (live provider and optional store tests). M0 adds
no skips. M0 acceptance: `tests/sentica/test_m0_baseline.py`, 7 passed.

Open questions: none for M0.

## M1. Single tenant

Built:

- `src/sentica/`: startup checks, broker tokens, proxy TLS, queue tenant stamps.
- Production database connections have no password, use `sslmode=verify-full`,
  and keep tables in schema `memory`.
- Every non-probe request and every deriver task must carry this instance's
  tenant. Admin tokens are refused.
- A dropped proxy connection rolls the transaction back. The API returns
  503 `memory unavailable`. The deriver backs off and retries.
- `docker-compose.sentica.yml` runs two local instances behind PgBouncer
  with a local test CA. Postgres data and Redis data are tmpfs.
- `docs/INSTANCE.md` says the API listens on the broker's private interface
  and must not be exposed to the internet or an agent container.

Decisions:

- `SENTICA_MODE` unset keeps upstream behavior. The unit suite imports
  `src.db` at startup, and a default of production would refuse to boot
  without certificates. Unknown values still refuse to start. This is not
  a default tenant.
- `AUTH_USE_AUTH` is not required in production. The production check runs
  first and never takes the upstream admin bypass, so the instance does not
  need a symmetric `JWT_SECRET`. Requiring `AUTH_USE_AUTH=true` forced that
  secret through the existing settings validator.
- Refusal counts live in an in-process dict (`src/sentica/refusals.py`).
  They are not Prometheus series yet. Adding a series here would have to
  update the metric zero-init registry. Export is left for M9.
- `docker-compose.yml` still has the development database password. The
  certificate proxy is `docker-compose.sentica.yml`. M0's `docker compose up`
  keeps working.
- On the local proof network, Postgres accepts the proxy with `trust` and
  no password. The instance environment has no password. Blocking a client
  that skips the proxy is network policy, recorded for a later milestone.
- `/health` and `/metrics` stay unauthenticated so a healthcheck can run.
  They return no memory content. Every other path requires a token.
- Representation queue payloads still include message text. Same deferral
  as M0. The tenant stamp is an extra id on the payload and is removed
  before upstream validation.
- The two-instance test is skipped unless `SENTICA_M1_STACK=1`. GitHub
  Actions runs the unit suite. The compose proof was run on this machine.

Deviations from GOALS.md: the unit suite still uses a password database,
because that is the M0 stack. The production-shaped stack does not. Queue
message text is still present, as recorded under M0.

Test counts: `uv run pytest` on 2026-10-11, against the local Postgres
on host port 5433, with `SENTICA_M1_STACK=1`: **3158 passed, 53 skipped,
0 failed** (260.44s). The 53 skips are the same upstream skips as M0.
That run includes `tests/sentica/test_m1_stack.py` (6 passed). Unit checks
for startup and tokens are `tests/sentica/test_m1_startup.py` and
`tests/sentica/test_m1_tokens.py`. `ruff check` and `basedpyright` were
clean on this branch.
