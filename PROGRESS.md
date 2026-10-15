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
- Representation queue payloads include message text
  (`src/utils/queue_payload.py:28`). M1 checked where those payloads live
  and closed this note. See M1.
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

Deviations from GOALS.md: none in the M0 deliverables. The development
database password stayed until M1's certificate proxy. Queue message text
was checked in M1 and stays in Postgres only.

Test counts: `uv run pytest` on 2026-10-11, against the local Postgres
on host port 5433: **3140 passed, 53 skipped, 0 failed** (132.83s). The
53 skips are upstream (live provider tests and two pagination cases). M0 adds
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

- `SENTICA_MODE` must be exactly `production` or `test`. Unset, empty,
  misspelled, and wrong-case values refuse to start. The runtime image
  sets `production`. The unit suite and `docker-compose.yml` set `test`
  explicitly. Test mode does not load a tenant and does not apply the
  production checks.
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
- Jose accepted queue content in the Postgres `queue` table. Ground rule 5
  now says host-local stores (Redis, files) carry ids only, and the Postgres
  queue table may hold content because the database is the store. The M9
  sentence stays: network rules must make the database reachable only
  through the certificate proxy. The M1 proof stack already isolates it.
- The proof stack uses three networks. Postgres `pg_hba.conf` accepts TLS
  only from the two PgBouncer addresses, as the matching tenant role, with
  scram-sha-256. Each role's password exists only in that proxy's userlist.
  The instance holds no password. `local` trust remains for init and
  `pg_isready`. The image variable `POSTGRES_HOST_AUTH_METHOD=trust` is only
  there because the official image refuses to boot without it. The mounted
  `hba_file` is what Postgres uses. Redis generates a password at start and
  keeps it on a tmpfs volume. That password is not in the compose file.
- Two items stay for M9: Redis still runs as root, and the compose networks
  are not `internal: true`. Network isolation of the database is not one of
  them. The proof already rejects cross-network connections by name and by
  raw IP.
- `/health` and `/metrics` stay unauthenticated so a healthcheck can run.
  They return no memory content. Every other path requires a token.
- Queue payloads live only in Postgres. The table is `queue`
  (`src/models.py:583`). The JSONB column is `payload` (`src/models.py:594`).
  Inserts are `src/deriver/enqueue.py:74`, `:559`, `:685`, and `:814`.
  Representation text is `src/utils/queue_payload.py:28`. Redis
  (`src/cache/client.py`) caches workspace, session, and collection rows. It
  does not receive queue payloads, and the deriver does not write them to a
  file. That satisfies ground rule 5: the database is the store. The M0
  deferral is closed. The tenant stamp is an extra id and is removed before
  upstream validation.
- The two-instance proof is `tests/sentica/test_m1_stack.py`. CI job
  `test-python` ignores that file. Job `test-m1-stack` sets
  `SENTICA_MODE=test` and runs it, and fails if it collects zero tests.
  Image publish workflows stay `if: false` until M9.

Deviations from GOALS.md: the unit suite and `docker-compose.yml` still use
a password database, with `SENTICA_MODE=test`. The production image and
`docker-compose.sentica.yml` do not.

Test counts: `uv run pytest --ignore=tests/sentica/test_m1_stack.py` on
2026-10-14, against the local Postgres on host port 5433, with
`SENTICA_MODE=test`: **3200 passed, 53 skipped, 0 failed** (133.55s).
The 53 skips are upstream. The proof file is not in that run. A separate
`uv run pytest tests/sentica/test_m1_stack.py -n0` was **18 passed, 0
skipped, 0 failed** (104.34s). The fork adds no skip. Map citations for
the engine are `src/db.py:75`, `src/db.py:347`, and `src/db.py:367`.

## M2. Workspace partitions

Built:

- Postgres row-level security is enabled and forced on every table that has
  `workspace_name`. The policy keeps a row only when `sentica.workspace` is
  set and equals that column. An unset or empty setting returns no rows.
- Each tenant has a runtime role and a migration role. The migration role
  owns the schema. The runtime role can read and write rows and cannot
  create objects, change policy, or bypass row-level security. The API and
  the deriver do not migrate. They refuse to start when the schema revision
  is behind the code. A one-shot migrate service owns the migration
  certificate.
- Each transaction sets `sentica.workspace` to one workspace. Reads run in
  `BEGIN READ ONLY`. The setting is transaction-local, so session pooling
  and transaction pooling are both safe. Search, context, chat,
  representation, and cards query one workspace at a time and re-check the
  token for each one.
- Queue rows require a workspace. The claim loop reads
  `sentica_pending_queue_counts()`, which returns only the workspace name
  and the pending queue count. The reconciler reads
  `sentica_pending_counts()`, which adds pending embeddings, pending
  backfills, soft-deleted documents, and dreams due. Neither function
  returns message or document text. The deriver and the reconciler do
  that work one workspace per transaction.
- A workspace that is not in the token set is refused. An ambiguous token
  is refused. Production allows only the pgvector store.
- Peer cache keys include the workspace name.

Tables without `workspace_name` are `workspaces`, `active_queue_sessions`,
and `alembic_version`. See `.review/M2-summary.md`.

Test counts on 2026-10-15, against the local Postgres on host port 5433,
with `SENTICA_MODE=test`:

- `uv run pytest --ignore=tests/sentica/test_m1_stack.py -q --tb=line`:
  **3263 passed, 53 skipped, 0 failed** (104.58s). The 53 skips are the
  upstream live-llm tests and two pagination cases.
  `test_runtime_role_lockdown` is not collected here.
- `uv run pytest tests/sentica/test_m1_stack.py -n0 -q --tb=line`:
  **24 passed, 0 skipped, 0 failed** (171.22s).
- Runtime-role job (`SENTICA_MODE=production`, app engine as `tenant_rls`):
  the pin in `tests/sentica/rls_job.py` lists 59 tests. See
  `.review/M2-summary.md`.

Follow-up on 2026-10-15, same local Postgres:

- `uv run pytest --ignore=tests/sentica/test_m1_stack.py -q --tb=line`:
  **3266 passed, 53 skipped, 0 failed** (108.96s). The three extra passes
  are the Anthropic, Gemini, and unknown-provider dummy-key cases.
- `uv run pytest tests/sentica/test_m1_stack.py -n0 -q --tb=line`:
  **24 passed, 0 skipped, 0 failed** (182.93s).
- The runtime-role pin stays 59 tests.

Review round 1 on 2026-10-15, same local Postgres:

- `uv run pytest --ignore=tests/sentica/test_m1_stack.py -q --tb=line`:
  **3272 passed, 53 skipped, 0 failed** (118.17s). Six new tests cover the
  frozen migration SQL, the import guard, the dreams-function match, the
  upgrade from the merged head, the document_sources gauge, and the
  soft-deleted explicit document count.
- `uv run pytest tests/sentica/test_m1_stack.py -n0 -q --tb=line`:
  **24 passed, 0 skipped, 0 failed** (174.62s).
- The runtime-role pin stays 59 tests. The new tests are not in
  `tests/sentica/test_m2_*.py`.

