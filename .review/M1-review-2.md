**Verdict: fix, then reverify.** I'd block merging PR #2 at commit `987c6f2a` for three reasons:

- **The instance can start wide open.** With `SENTICA_MODE` unset or empty, it runs upstream's no-auth, admin-for-everyone mode.
- **CI does not run the isolation proof.** All six two-instance acceptance tests are skipped there.
- **The local proof stack lets one instance reach the other's Redis and the shared Postgres.** No certificate is needed.

Several other acceptance tests would still pass if the behavior they guard were removed.

**The working tree changed while I was reviewing.** `HEAD` still equals `origin/m1-single-tenant` (`987c6f2a`), but 17 files now have uncommitted edits, plus a new `tests/sentica/conftest.py`. I reviewed the committed PR and re-read changed files with `git show HEAD:`. Three of the pending edits matter:
- **They partly fix the fail-closed gap.** `runtime.mode()` now refuses unset, and `Dockerfile` sets `ENV SENTICA_MODE=production`.
- **They hide the skipped stack tests instead of running them.** The `skipif` is replaced with a `pytest_ignore_collect` hook, so the tests vanish from the skip count.
- **They edit `GOALS.md`.** M9 gains "Network rules must make the database reachable only through the certificate proxy". That moves an M1 isolation gap to a later milestone by changing the source of truth. That decision is yours, not the builder's.

## Blockers

**B1. The instance fails open when `SENTICA_MODE` is unset or empty, and the production image does not set it.**
- **Code:**
  - `src/sentica/runtime.py:66-69` turns unset or empty into `None`.
  - `:84-86` then returns no instance, which is upstream mode.
  - `src/security.py:215-216` then grants admin: `JWTParams(t="", ad=True)`.
  - `src/main.py:240` skips token checks.
  - The committed `Dockerfile:37-77` has no `SENTICA_MODE`, and its `CMD` at `:77` runs `fastapi run --host 0.0.0.0` without the entrypoint.
  - `docker/entrypoint.sh:8` also defaults to `0.0.0.0`.
  - The upstream workflow `.github/workflows/docker-build.yml` publishes that image as `latest` on every push to `main`.
- **Your four cases:**

| Value | Result |
| --- | --- |
| Unset | Starts open |
| Empty | Starts open |
| Misspelled | Refuses to start |
| `"Production"` | Refuses (the comparison is case-sensitive) |

- **Failure:** an operator forgets the variable. The container listens on all interfaces and treats every caller as admin. It connects with the default URI `postgres:postgres@localhost` (`src/config.py:723-725`).
- **Test that locks in the bug:** `tests/sentica/test_m1_startup.py:50-52` asserts this open behavior.
- **Smallest fix:**
  - Land the pending `mode()` change and the `Dockerfile` `ENV`.
  - Point the image `CMD` at `docker/entrypoint.sh`.
  - Make the production image refuse `test` mode, for example with a baked marker that `mode()` checks. Otherwise one `-e SENTICA_MODE=test` turns a production container into upstream's open mode.

**B2. The fork added a skip, and CI never runs the M1 acceptance proof.**
- **Code:** `tests/sentica/test_m1_stack.py:30-33` skips the module unless `SENTICA_M1_STACK=1`. `.github/workflows/unittest.yml:59-62` does not set it.
- **Skip count:** there are 8 skip or xfail markers under `tests/` on `main` and 9 on the branch, so this is the only one the fork added. The reported 53 skips were measured with `SENTICA_M1_STACK=1`, so CI shows 59.
- **Failure:** any regression in tenant, proxy, rotation or revocation behavior merges green.
- **Pending edit:** the `conftest.py` hook makes it worse, because the tests disappear from the count entirely.
- **Smallest fix:** add a CI job that runs `SENTICA_M1_STACK=1 uv run pytest tests/sentica/test_m1_stack.py` (GitHub-hosted runners have Docker). Have it fail if that file collects zero tests.

**B3. In the proof stack, Redis and Postgres are reachable across instances.**
- **Code:** `docker-compose.sentica.yml` defines no networks, so every container shares one default network. `redis-a` and `redis-b` have no auth (`:96-116`). Postgres trusts every host (`POSTGRES_HOST_AUTH_METHOD: trust`, `:42`).
- **Failure:**
  - From `api-b`, `redis-cli -h redis-a` reads A's cache, including peer rows with `internal_metadata`. That field holds peer cards (`src/crud/peer.py:421-429`, `src/crud/peer_card.py:47-52`).
  - From `api-a`, `psql -h database -U postgres` gives superuser access to `tenant_b` with no certificate.
- **Why it blocks:** M1 says "Redis ... reachable only by that instance", and `.review/M1-summary.md:3` says "The database is reached only through PgBouncer." No test checks either.
- **Smallest fix:**
  - Use three networks: an internal `db` network with only `database`, `pgbouncer-a` and `pgbouncer-b`; `net-a` with `api-a`, `deriver-a`, `redis-a` and `pgbouncer-a`; and `net-b` mirroring it.
  - Add tests asserting that `api-b` cannot open `redis-a:6379` and `api-a` cannot open `database:5432`.

## Major

1. **The tenant role is not confined to the `memory` schema** (`docker/sentica/init-tenants.sql:7-8`, `:19`).
   - `CREATE DATABASE tenant_a OWNER tenant_a` lets the role create new schemas.
   - On PG15, schema `public` is owned by `pg_database_owner`, which is the database owner, i.e. `tenant_a`. `REVOKE ... FROM PUBLIC` does not apply to an owner.
   - **Failure:** `CREATE SCHEMA x`, `CREATE TABLE public.t`, or `DROP TABLE public.outside_memory` all work, since a schema owner can drop tables in it.
   - The test (`test_m1_stack.py:390-396`) only tries one `SELECT` and accepts any error, including "table does not exist".
   - **Fix:** have an admin role own the database. Then `REVOKE CREATE ON DATABASE`, `ALTER SCHEMA public OWNER TO postgres` (or drop `public`), and assert SQLSTATE `42501` for `SELECT`, `CREATE SCHEMA` and `CREATE TABLE public.t`.

2. **The tenant role is PgBouncer's admin and stats user** (`docker/sentica/pgbouncer-a.ini:18-19`, same in `-b`).
   - **Failure:** A connects to the `pgbouncer` admin database with its own certificate and runs `SHUTDOWN`, `PAUSE`, `KILL` or `SET`. If the production proxy is shared, that hits other tenants.
   - **Fix:** remove both lines, or use a separate operator identity.

3. **Two enqueue sites never stamp a tenant, so production refuses their tasks.**
   - The sites are `src/reconciler/scheduler.py:289-296` and `src/webhooks/events.py:62-74`.
   - The refusal at `src/deriver/consumer.py:55-57` runs before the reconciler branch at `:61`.
   - **Failure:** every reconciler cycle is marked errored. Message embeddings stay pending forever, stale-queue cleanup never runs, and webhooks are never delivered.
   - The stack hides this by setting `EMBED_MESSAGES: "false"`.
   - **Fix:** call `stamp_queue_records` at both sites. Add a stack test that a reconciler task completes.

4. **The representation batch reads data before the tenant check, and that check is untested.**
   - `src/deriver/queue_manager.py:654-659` calls `get_queue_item_batch`, which loads message rows and configuration, before the check at `:671-674`.
   - Deleting lines 671-674 fails no test: the unit test calls `pop_and_check_tenant` directly, and the stack test uses a deletion task through `consumer.py`.
   - An untagged row also poisons legitimate rows, because only `items[0]` is errored per round (`:619-620`).
   - **Fix:** check the tenant on the claimed queue rows before loading messages. Add a stack test with a representation row tagged for B.

5. **Several acceptance tests would pass if the behavior they guard regressed:**
   - **Certificate swap** (`test_m1_stack.py:402-428`). Requests are sequential, not continuous, and the old certificate stays valid for 48 hours. The test passes with `install_certificate_reload` (`src/sentica/db_connect.py:56-85`) deleted. The 1-hour pool recycle (`src/db.py:57-58`) has no test.
   - **A's certificate against B's proxy** (`:398-399`). It accepts any `psycopg.Error`, including connection refused or a CA error. There is no positive control that proxy B accepts B's certificate. It never tries A's certificate with user `tenant_b`, or proxy A with database `tenant_b`.
   - **Every endpoint refuses B's token** (`:280-293`). The token is signed with B's key, so it fails as "forged", not "wrong tenant". The real cross-tenant token is only tried on `POST /v3/workspaces` (`:303-309`). The loop also misses routes hidden from OpenAPI, such as `src/routers/messages.py:110`.
   - **Revocation mid-write** (`:431-463`). This proves Postgres atomicity on a raw connection, not the app's write path. The 503 check (`:465-483`) only covers a stopped proxy, which refuses connections immediately.
   - **Nothing on disk after stop** (`:547-570`). It checks tmpfs mounts and volumes. It does not check container logs or scan for a canary.

6. **A request can hang when the proxy silently drops packets.**
   - The only bound is `connect_timeout=2` (`src/db.py:28-32`). There is no statement timeout, and PgBouncer drops the `options` startup parameter (`pgbouncer-a.ini:17`). There are no TCP keepalive or `tcp_user_timeout` settings.
   - **Failure:** a dropped-packet partition mid-query blocks until the kernel gives up retransmitting, roughly 15 minutes.
   - **Fix:** add `tcp_user_timeout`, `keepalives_*` and `ALTER ROLE ... SET statement_timeout`. Test with `docker pause pgbouncer-a`.

7. **Memory content and names reach logs, and Docker writes logs to the host disk.**
   - `src/main.py:302` logs `str(exc)` with a traceback for every database error other than a disconnect. The engine (`src/db.py:66-71`) lacks `hide_parameters=True`, so SQLAlchemy errors print bound parameters, including message text.
   - `src/deriver/consumer.py:69-72` and similar lines log whole payloads, which include session and peer names.
   - `queue_manager.py:630` logs the work-unit key, which contains workspace and peer names.
   - Neither compose file sets a `logging:` driver.
   - **Fix:**
     - Set `hide_parameters=True` in production.
     - Log the exception class name only in production.
     - Use a log driver that does not write to disk.
     - Add a canary-in-logs test for an error path.

8. **`/metrics` would expose workspace names, and the deriver's metrics port bypasses broker auth.**
   - `src/telemetry/prometheus/metrics.py:117-151` labels series by `workspace_name`. `/metrics` is exempt from the token check (`src/main.py:227`).
   - `src/deriver/__main__.py:29` and `:100-101` start an unauthenticated listener on `0.0.0.0:9090`.
   - It is safe today only because `METRICS.ENABLED` defaults to false (`src/config.py:1256`).
   - **Fix:** in production, refuse to start with metrics enabled until those labels are removed. Bind the deriver listener to `SENTICA_BIND_HOST`.

9. **Queue payloads carry message text** (`src/utils/queue_payload.py:28`, `content: str`).
   - Ground rule 5 says queue entries carry ids, not content.
   - The text is only in Postgres, so the host disk is unaffected. But this is a deviation from the goals, and the pending edits relabel it "closed" rather than fixing it. It needs your explicit decision.

## Minor

- **The signature algorithm comes from the token header** (`src/sentica/tokens.py:66-77`).
  - `none` and `HS256` are excluded (`:67`), so there is no algorithm confusion.
  - PyJWT 2.12.1 rejects an `iat` in the future, so the expiry really is at most 5 minutes away.
  - But an `RS256` header against an Ed25519 key raises `TypeError` inside PyJWT. That escapes the `PyJWTError` handler, so the request returns 500 with a logged traceback and no refusal count.
  - **Fix:** derive the algorithm from the key type, and treat any decode exception as forged.
  - **Missing tests:** no test covers `none`, `HS256`, or an algorithm/key mismatch.
- **Key and file problems surface per request instead of at startup.**
  - The token key file is checked only for being non-empty, so a malformed key starts the instance and every request gets a 401.
  - Each request re-runs `load_instance()` twice (middleware and `auth`). A certificate file briefly missing during rotation raises `SenticaStartupError`. The middleware (`main.py:245-251`) only catches `AuthenticationException`, so that becomes a 500.
  - **Fix:** parse the key at startup and cache the loaded instance.
- **Startup tests cover only some required settings.** `test_m1_startup.py:61-86` covers the tenant and the certificate. A missing key, CA, token key or issuer has no individual test, though the acceptance item names the key. Parametrize over `_REQUIRED`.
- **`SENTICA_BIND_HOST=private` picks the default-route interface** (`docker/entrypoint.sh:10-13`). That is not necessarily the broker's interface. Require an explicit address.
- **The password check covers environment variables only** (`src/sentica/runtime.py:28-34`). It ignores `config.toml`, `PGPASSFILE` and `~/.pgpass`.
- **Reasoning traces can write prompts and outputs to disk.** `REASONING_TRACES_FILE` (`src/config.py:1603`) has no production guard.

## Nits

- Refusal counts are an in-process dict (`src/sentica/refusals.py`) and are not exported until M9.
- Imports inside the per-request hot path (`src/main.py:236-238`).
- The Redis `save` assertion is fragile (`test_m1_stack.py:383-388`).

## Your nine questions

1. **Acceptance-to-test mapping.**
   - **Solid:** token expiry, issuer, admin claim and lifetime (`test_m1_tokens.py:97-116`); the missing tenant at import (`test_m1_startup.py:110`).
   - **Weak, would pass after a regression:** the items in Major 5 and Major 4.
   - **No test:** pool recycle, Redis reachability, deriver back-off and resume.
   - **Skips:** the fork added one skip and no xfail or disabled assertion. `test_m1_startup.py:50` asserts the open behavior.
2. **Fail closed.** See B1.
3. **Authentication.**
   - Every route in the FastAPI app passes the broker check. There are no websocket routes, and the middleware covers hidden routes.
   - The upstream admin bypass is unreachable in production.
   - The exceptions are `/health`, `/metrics` and the deriver's `:9090` listener.
   - Issuer, expiry of at most 5 minutes, tenant and request id checks are correct. Algorithm handling is covered in Minor.
4. **Deriver.** No task enqueued without a tenant can run in production. The representation batch checks after reading data (Major 4). Reconciler and webhook tasks are always refused (Major 3).
5. **Database.**
   - No password exists in the instance environment or the production path.
   - TLS `verify-full` with the client certificate is applied to both the app engine and Alembic's engine (`src/sentica/db_connect.py:23-28`, `migrations/env.py`).
   - Confinement to `memory`, rotation and hang behavior are covered in Major 1, 5 and 6.
6. **Queue and host.**
   - The queue is a Postgres table only: `class QueueItem(Base)` at `src/models.py:583`, a JSONB `payload` at `:594`, inserted via `stmt = insert(QueueItem).returning(QueueItem)` at `src/deriver/enqueue.py:74` and three more sites, plus the two ORM sites in Major 3.
   - Redis is only a cache. It holds workspace, session, collection and peer rows, including peer cards, plus pagination positions.
   - Persistence is off: `--save "" --appendonly no` with `/data` on tmpfs (`docker-compose.sentica.yml:98-100`).
   - Content on host disk is through logs (Major 7).
7. **Development password.**
   - `docker-compose.yml` itself is not copied into the image, and production refuses `DB_CONNECTION_URI`.
   - But in the committed state, the image with `SENTICA_MODE` unset uses the identical default `postgres:postgres` URI (B1).
   - `config.toml.example:24` ships in the image via `Dockerfile:70`.
   - A developer's gitignored `config.toml` would also be baked in, because `.dockerignore` does not exclude it.
8. **Probes.** `/health` returns `{"status":"ok"}` (`src/main.py:255-258`). `/metrics` is covered in Major 8.
9. **Upstream divergence.** `FORK_CHANGES.md` lists every committed change. The hooks into upstream files are small: `src/db.py` +23 lines, `src/main.py` +54, `src/security.py` +21, deriver files +19.

## What I could not verify read-only

- The 3158 passed / 53 skipped counts and whether the 53 are all upstream's own; I could only confirm statically that the fork added one marker.
- PG15 ownership behavior and PgBouncer certificate common-name handling in this stack.
- How long a hung query actually blocks.
- Whether message create and enqueue run as separate transactions, which would be an app-level partial write.
- The Docker log driver on the target VM.
- How refused reconciler rows behave over time.

**Verdict: fix B1-B3 and Majors 1-5, then reverify.** Land the pending `SENTICA_MODE` change, but not the hidden-collection hook. Run the stack in CI, so the tests are counted. Get your sign-off on the `GOALS.md` edit and the queue-content deviation.