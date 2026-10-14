## Scope

Reviewed PR #2 at HEAD `987c6f2a` (`m1-single-tenant` vs `main`). Note: the working tree has **uncommitted edits** (`src/sentica/runtime.py`, `Dockerfile`, `docker/entrypoint.sh`, `docker-compose.yml`, CI yml, docs, tests, new `tests/sentica/conftest.py`) that are not in the PR. They address finding B1 below; I flag where they help and where they do not. Findings are against the committed branch.

## Findings

### Blocker

**B1. Unset or empty `SENTICA_MODE` starts a fully open instance; the image does not set production.**
`src/sentica/runtime.py:66-69` returns `None` for unset/empty, and `load_instance()` (`:84-86`) treats that as "upstream mode". In that mode `src/security.py:215-216` makes every request admin (`AUTH_USE_AUTH` defaults false, `src/config.py:756`), the DB URI falls back to the password default at `src/config.py:723-725`, nothing stamps queue rows, and `Dockerfile:78` binds `0.0.0.0`. `Dockerfile` has no `ENV SENTICA_MODE`. Scenario: a compose file with `SENTICA_MODE: ${SENTICA_MODE}` and the variable unset, or an operator omitting it, yields a server with no auth on all interfaces and no refusal anywhere. `"Production"`, `"prod"`, `"test"` do refuse (`:87-92`), so the only fail-open values are the two most likely ones. The committed `docs/INSTANCE.md:24-25` documents this fail-open as intended.
Smallest fix: `mode()` raises on unset/empty; accept exactly `production|test`; `ENV SENTICA_MODE=production` in the image; `SENTICA_MODE=test` in CI, `docker-compose.yml`, and test conftest. The uncommitted working-tree diff does exactly this (plus parametrized tests for `None, "", "Production", ...`); it needs to be committed and the stack re-run.

### Major

**M1. Two queue insert sites are not tenant-stamped, so in production the reconciler and webhooks never run.**
`src/reconciler/scheduler.py:289-297` and `src/webhooks/events.py:62-75` build `QueueItem(...)` directly; only the four `insert(QueueItem)` sites in `src/deriver/enqueue.py` call `stamp_queue_records`. `src/deriver/consumer.py:57` then raises `SenticaTenantRefused("missing_tenant")`, the row is marked errored (`queue_manager.py:1225`), and the scheduler re-enqueues next interval. Result: `sync_vectors` and `cleanup_queue` never execute (embeddings never sync, stale rows never pruned), `queue.empty` webhooks never deliver, and the `missing_tenant` counter is permanently polluted so a real forged row is indistinguishable. The summary's "the other three insert sites" (`.review/M1-summary.md:38`) is incomplete. Hidden in the stack test because `EMBED_MESSAGES=false`.
Fix: stamp both sites, or make the stamp structural with a `before_insert` listener on `QueueItem` that adds the tenant in production (and asserts it). Add a test that enqueues via `ReconcilerScheduler._enqueue_task` and `publish_webhook_event` in production mode and asserts `tenant_id` is present.

**M2. SQL parameters (message content) reach logs; Docker logs are host disk.**
`src/db.py:66-71` does not set `hide_parameters=True`. SQLAlchemy's `StatementError.__str__` appends `[parameters: {...}]`. Those strings are logged at `src/main.py:302` (`str(exc)`, non-Operational errors), `src/deriver/queue_manager.py:614,629-632`, `src/deriver/enqueue.py:79` (`logger.exception`). Scenario: a `DataError`/`IntegrityError` during a message or queue insert writes the message text into stdout, which the json-file log driver persists under `/var/lib/docker/containers` (rule 4 and rule 5). `test_stopped_stack_leaves_no_memory_on_disk` (`test_m1_stack.py:547`) does not look at container logs. Related: `src/telemetry/reasoning_traces.py:100` writes full prompts to a file when `REASONING_TRACES_FILE` is set; `src/db.py:69` echoes SQL when `SQL_DEBUG` is set; neither is refused in production.
Fix: `hide_parameters=True` in `create_async_engine` and in `migrations/env.py`'s engine when production; refuse `REASONING_TRACES_FILE`, `COLLECT_METRICS_LOCAL`, `DB_SQL_DEBUG` in `load_instance`. Test: force an IntegrityError with a canary parameter in production mode and assert the canary is absent from caplog.

**M3. Memory content is cached in Redis.**
`src/crud/peer.py:419-427` caches `internal_metadata`, which holds peer cards (`src/crud/peer_card.py:47-52,101-107`). `src/crud/session.py:111-120` caches `internal_metadata`, which holds summaries (`src/utils/summarizer.py:479,801`). Production compose turns the cache on (`docker-compose.sentica.yml:26`) against an unauthenticated Redis (`:138`). Persistence is off and `/data` is tmpfs (`:98-100`), so nothing is at rest, but peer cards and summaries sit pickled in a second process reachable by any container on the network.
Fix: `CACHE_ENABLED=false` in production (falls back to `mem://`, `src/cache/client.py:319`), or drop `internal_metadata` from the cached dicts. Test: after `set_peer_card`, `redis-cli --scan` + `DUMP` contains no canary.

**M4. The tenant role is the database owner, so it is not confined to `memory`.**
`docker/sentica/init-tenants.sql:7-8` `CREATE DATABASE tenant_a OWNER tenant_a`. On PG15 (`pgvector/pgvector:pg15`) `public` is owned by `pg_database_owner`, so `tenant_a` owns `public`; `REVOKE ALL ON SCHEMA public FROM PUBLIC` (`:19`) does not bind an owner, and a DB owner may `CREATE SCHEMA`. The test at `test_m1_stack.py:390-396` only checks `SELECT public.outside_memory` (never granted) and accepts any `psycopg.Error`, so it passes even if the table does not exist.
Fix: own the DB with `postgres` (or a dedicated owner), `REVOKE CREATE ON DATABASE tenant_a FROM tenant_a`, keep `memory AUTHORIZATION tenant_a`; test `CREATE TABLE public.x` and `CREATE SCHEMA x` raise `InsufficientPrivilege` specifically.

**M5. The stack acceptance tests are gated by a fork-added skip and never run in CI.**
`tests/sentica/test_m1_stack.py:30-33` `skipif(SENTICA_M1_STACK != "1")`; `.github/workflows/unittest.yml` never sets it. So "53 skipped" holds only locally with the flag; in CI 6 more skip and none of the two-instance acceptance items (`GOALS.md:48`) is proven by CI. The uncommitted `tests/sentica/conftest.py` replaces the marker with `pytest_ignore_collect`, which hides the skip from the count without running the tests; that is worse for rule 6, not better.
Fix: a CI job running `SENTICA_M1_STACK=1 uv run pytest tests/sentica/test_m1_stack.py` (ubuntu runners ship docker compose), and fail rather than skip when the flag is absent in CI.

**M6. Vacuous or partial acceptance tests.**
- Certificate swap (`test_m1_stack.py:402-428`) passes with `install_certificate_reload` deleted: the old cert is valid 48 h and pooled connections keep working. It does not show the new file was used. Fix: after reissue, make pgbouncer stop trusting the old cert (rotate the CA and `RELOAD`), or assert `db_connections_established` increments after the swap. The 1 h recycle cap (`src/db.py:58`) has no test.
- Revocation rollback (`:431-446`) uses the test's own psycopg transaction, so it proves Postgres semantics, not the API write path. "Deriver backs off and resumes idempotently" (`GOALS.md:40`) has no test at all. Fix: `PAUSE` pgbouncer during an API batch write, assert 503 and zero rows; stop the proxy with a pending queue row, restart, assert processed exactly once.
- `:398-399` passes on any `psycopg.Error` and never tries the real impersonation (A's cert with `user=tenant_b`).

### Minor

- **m1 Pool timeout is a 500, not "memory unavailable".** `src/main.py:285` omits `sqlalchemy.exc.TimeoutError` (`pool_timeout=5`, `src/config.py:731`). A stalled proxy exhausts the pool and returns 500 with `str(exc)` logged. Also no TCP keepalives in `connect_args` (`src/db.py:27-32`), so a blackholed proxy (no RST) hangs in-flight statements; the test only covers SIGKILL. `pgbouncer-a.ini:17` ignores the `options` startup parameter, so the migration's `statement_timeout` is dropped silently. Fix: add `TimeoutError`; `keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3`; `ALTER ROLE tenant_a SET statement_timeout`.
- **m2 Unauthenticated deriver listener and workspace names in metrics.** `src/deriver/__main__.py:29` `start_http_server(9090)` binds `0.0.0.0` whenever `METRICS.ENABLED`, outside the token middleware. Metrics carry `workspace_name` labels (`src/telemetry/prometheus/metrics.py:120,144,150`), exposed on the tokenless `/metrics` (`src/main.py:227`) and on 9090. Rule 4 forbids names in metrics. Fix: `start_http_server(9090, addr=SENTICA_BIND_HOST)`; drop or hash the label in production.
- **m3 Image default CMD ignores the private bind.** `Dockerfile:78` `--host 0.0.0.0`; only `docker/entrypoint.sh:9-18` enforces `SENTICA_BIND_HOST`, nothing in-process checks the real listen address. Fix: make `entrypoint.sh` the image `ENTRYPOINT`.
- **m4 Rotation is not atomic and only the cert is watched.** `src/sentica/db_connect.py:65-72` fingerprints `cert_path` only; key and CA changes are invisible. The test's `_reissue_client` (`:143-152`) writes key then cert separately; a connect between the writes uses a mismatched pair. Document rename-into-place, or fingerprint cert+key together.
- **m5 Queue payloads carry message text** (`src/utils/queue_payload.py:28`), against rule 5's "ids not content". Recorded as a deferral in `PROGRESS.md` but with no owning milestone. Assign one.
- **m6 OpenAPI walk misses hidden routes** (`src/routers/messages.py:110`); the middleware is path-agnostic so this is coverage, not a hole.
- **m7 No `aud`, no replay check on `request_id`** (`src/sentica/tokens.py:98-100`). A broker token for a different Sentica service under the same issuer/key is accepted here. Not required by GOALS; suggest `aud`.

### Nit

- n1 Token verified twice per request (`src/main.py:246`, `src/security.py:214`), each re-reading env, four `stat()`s and the PEM. Cache the instance at startup.
- n2 Unreadable public key is a 401 (`tokens.py:33-34`); 503 is more honest.
- n3 `honcho_exception_handler` logs `exc.detail` (`src/main.py:265`), which includes peer and workspace names (e.g. `peer_card.py:113`).
- n4 `queue_manager.py:672-674` checks copies and passes the original payload (still with `tenant_id`) downstream; harmless since only `.get("observers")` is read, but worth a comment.
- n5 Representation batches load messages (`queue_manager.py:654-659`) before the tenant check at `:672`. Own DB, so harmless; "check before touching data" is not literally true.
- n6 `.review/M1-summary.md:38` "the other three insert sites" is wrong (five exist).

## Answers to the nine checks

1. **Gate.** Mapping exists in the summary table, but: cert swap and revocation tests are vacuous for the behaviours claimed (M6); deriver back-off/resume and the 1 h recycle are untested; stack tests skip in CI (M5). The fork touches no upstream test file (diff is `tests/sentica/` only), so the 53 upstream skips (`tests/conftest.py:133` live-LLM marker, `importorskip("chromadb")`, `test_pagination_shim.py:213`) are untouched. The fork **did** add one `skipif` (`test_m1_stack.py:30`). No xfail; weak asserts at `test_m1_stack.py:384-388,394,399`.
2. **Fail closed.** Unset/empty: runs fully open (B1). Misspelled or `"Production"`: refuses at import of `src.db`. `"test"`: also refuses on the committed branch. Image does not set production (committed `Dockerfile`). Working-tree edits fix all four if committed.
3. **Auth.** With `AUTH_USE_AUTH` off, every HTTP path except `/health` and `/metrics` passes `sentica_production_token` (`src/main.py:230-252`, outermost of the user middlewares), and `auth()` re-verifies (`security.py:209-214`) before the admin bypass. No websocket or mount exists. Pinning is sound: `alg` must be `EdDSA|RS256` (`tokens.py:21,67-68`), passed as `algorithms=[alg]` against a loaded public-key object, so `none`/HS256 confusion is impossible. Expiry: PyJWT 2.12.1 verifies `exp`, rejects future `iat`, and `exp-iat ≤ 300` is enforced (`:95-96`). Issuer required (`:76`), tenant equality (`:104-105`), request id presence only (`:98-100`). Admin claim refused (`:88-89`). Outside the middleware: the deriver's Prometheus listener on 9090 (m2).
4. **Deriver.** Checks at `consumer.py:57` and `queue_manager.py:672-674` cover every task type; a task without `tenant_id` cannot run in production (`queue.py:55-56`). But two producers never stamp (M1), so the check breaks the reconciler and webhooks rather than only blocking forged rows.
5. **Database.** No password in instance env (checked at `runtime.py:28-34,94-104`; `test_m1_stack.py:369-377`). Password still exists as a code default (`config.py:723`) and in `config.toml.example:24`, reachable only in test mode. TLS `verify-full` + client cert (`db_connect.py:23-27`), repeated for Alembic (`migrations/env.py:245-247`; relies on `from src.db import Base` at `:14` running first). Role confinement is not real (M4). Rotation pickup and the 1 h cap exist in code (`db_connect.py:77-83`, `db.py:58`) but are unproven (M6). Revocation: `connect_timeout=2` and the 503 mapping are right (`main.py:273-300`); pool timeout and blackhole cases are gaps (m1).
6. **Queue and host.** Queue is the Postgres table `memory.queue` only (`src/models.py:583`; inserts at `src/deriver/enqueue.py:74,559,685,814`, `scheduler.py:289`, `events.py:62`). Redis holds the cashews cache, including peer cards and summaries (M3). Redis persistence off: `--save "" --appendonly no`, `/data` tmpfs (`docker-compose.sentica.yml:98-100`). No tempfile use in `src/`; `metrics_collector.py:125,221` and `reasoning_traces.py:100` write files only when enabled, and nothing refuses them in production (M2).
7. **Dev leftovers.** `docker-compose.yml:40,66` carry `postgres:postgres`; setting `SENTICA_MODE=production` there refuses via `_PASSWORD_ENV` (`runtime.py:28-34`). `.env` is in `.dockerignore`. The default URI in `config.py:723` is dead in production because `db.py:60-64` overwrites it, but only after module import order holds (n/a to Alembic thanks to `env.py:14`).
8. **/health, /metrics.** `/health` returns `{"status":"ok"}` (`main.py:255-258`). `/metrics` carries `workspace_name` labels (m2); no message text or conclusions.
9. **Divergence.** `FORK_CHANGES.md:21-37` lists every file in the diff except `.review/M1-summary.md`. Upstream edits are thin hooks (`db.py` +23, `security.py` +21, `main.py` +54, `consumer.py` +3, `enqueue.py` +12, `queue_manager.py` +4, `env.py` +28, `entrypoint.sh` +15). Minimal, yes; add the two files from M1 when stamped.

## Could not verify read-only

The 3158/53 counts and the "6 passed" stack run; that PgBouncer `auth_type=cert` requires the user in `auth_file`; PG15 `pg_database_owner` behaviour on the actual image (inferred from release notes); `private` bind resolution on a real network; actual container log contents; whether the uncommitted edits pass the suite.

## Verdict

**Fix, then reverify.** B1 must land (the working-tree version is the right shape). M1 to M4 are code or fixture changes with small tests; M5/M6 need the stack proof in CI and three tests rewritten so they fail when the behaviour regresses. After that, a fresh stack run plus a check that `docker logs` and `redis-cli` contain no canary.