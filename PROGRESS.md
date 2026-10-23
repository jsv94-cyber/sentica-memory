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

Part B on 2026-10-15, same local Postgres:

- `uv run pytest --ignore=tests/sentica/test_m1_stack.py -q --tb=line`:
  **3276 passed, 53 skipped, 0 failed** (115.61s). Four new tests cover
  the workflow grant, the `_ddl` role message, the provider allow-list,
  and row-level security on every memory-schema table.
- The stack suite was not re-run locally. This change does not touch it.
  The last local stack count is 24 passed.
- The runtime-role pin stays 59 tests. The runtime-role job no longer
  grants EXECUTE itself. Upstream triage is `.review/upstream-triage.md`.

## M3. Model gateway

Built:

- `src/sentica/gateway.py` posts chat and embedding calls to an
  OpenAI-compatible URL. Gateway mode is off unless `SENTICA_GATEWAY_MODE`
  is true, so the local stack and the upstream suite keep the provider SDKs.
- Each function has its own model and an optional base URL. Headers are
  tenant, workspace, purpose, and request id. The request id is the
  telemetry span id when that is set, and the same id is reused for retries.
- The daily token budget is a per-tenant total in the instance Redis, shared
  by the API and the deriver. Per-purpose limits are optional. A response
  without usage is charged an estimate, never zero. The authoritative budget
  is enforced at the Sentica gateway. This counter is a backstop and can
  slightly overshoot. A spent budget raises `GatewayBudgetExceeded` (429,
  `token budget exhausted`) before the HTTP call. An outage, after retries
  of the same model, raises `GatewayUnavailable` (503). The deriver leaves
  the queue item unprocessed and backs off before the next claim.
- Provider SDK imports stay on the Part B allow-list because gateway mode
  off still uses them. The M3 proof is that those constructors are not
  called while gateway mode is on.

Decisions:

- The budget backstop is shared across the API and the deriver through the
  instance Redis. It stores token counts, not memory content. The
  authoritative budget is enforced at the Sentica gateway.
- A gateway HTTP 429 is an outage retry, not the local budget. The local
  budget never sends the request.

Test counts on 2026-10-15, against the local Postgres on host port 5433,
with `SENTICA_MODE=test`:

- `uv run pytest --ignore=tests/sentica/test_m1_stack.py -q --tb=line`:
  **3281 passed, 53 skipped, 0 failed** (112.35s). Five new tests cover
  gateway headers, the same model on a tool round, the budget stop, an
  outage that leaves messages in place, and a missing tenant, workspace,
  or model.
- The stack suite was not re-run locally. Gateway mode stays off unless
  `SENTICA_GATEWAY_MODE` is set, so the stack still uses the provider path.
  The last local stack count is 24 passed.
- The runtime-role pin stays 59 tests. No `tests/sentica/test_m2_*` file,
  `tests/sentica/rls_job.py`, or the `test-m2-rls` job changed.

## M4. Safe inference and provenance

Built:

- Deriver, summary, and dream prompts tell the model not to infer health,
  sexual orientation or activity, religion, politics, immigration status,
  criminal history, or personal finances. Leave is dates only.
- `banned_category` drops those conclusions before they are stored and
  counts the drop per tenant. The low-sugar meals plus insulin pumps
  fixture is a health inference.
- An optional second check, `SENTICA_SAFETY_MODEL_CHECK`, asks the summary
  model. A failure drops the conclusion. It is off unless set, so the
  upstream suite does not call a provider.
- Messages can carry `source_trust`. A conclusion built only from
  `external` messages is returned with that flag and is omitted from the
  peer card. A missing value stays unknown. It is not stored as `owner`.
  Requiring the field on every existing production post would break the
  runtime-role job, which does not send it.
- Conclusions return the public ids of the messages they came from.

Decisions:

- The peer card is the instruction field. External-only conclusion text is
  removed there. The conclusion list still returns the row, with the flag,
  so Sentica can see it and not treat it as an instruction.
- The optional model check runs before the write. `create_documents` only
  applies the rules, because it already holds a database session.
- Local unit tests: 3295 passed, 53 skipped. The runtime-role pin stays 59.
  No `tests/sentica/test_m2_*` file, `tests/sentica/rls_job.py`, or the
  `test-m2-rls` job changed.

## M5. Freshness

Built:

- A conclusion can carry `subject_ref`. `POST /subjects/{ref}/changed`
  marks every live conclusion for that subject stale.
- A new conclusion is compared with the nearest live conclusion for the
  same subject, observer, and observed peer. Within cosine distance 0.35
  it is an update, or a contradiction when the text says the old fact no
  longer holds. The old row stays and `superseded_by` points at the new
  one. Retrieval skips superseded rows. Stale rows are still returned and
  sort after current ones: vector search reads them with a second HNSW scan
  on `ix_documents_embedding_hnsw` and appends that page after the fresh page
  from `ix_documents_embedding_hnsw_current`.
- `retrieved_count` and `used_count` record use. The conclusions query
  route counts a retrieval. Decay halves unused weight every
  `SENTICA_DECAY_INTERVAL_DAYS` (30), soft-deletes below
  `SENTICA_DECAY_FLOOR` (0.05), and purges after
  `SENTICA_DECAY_RETENTION_DAYS` (30). The reconciler runs that pass at
  most once an hour.
- `POST /deletions` hard-deletes a peer or a subject, including message
  embeddings and derived summaries. The record is appended to the
  workspace's internal metadata and stores counts only. It is not a new
  table, so the runtime-role table list stays the same.

Decisions:

- Superseding looks at the same `subject_ref` only. Facts with no subject
  are not replaced, so existing writes keep their current behavior.
- A missing `source_trust` is still unknown. It is not treated as owner.
- Local unit tests: 3304 passed, 53 skipped. The runtime-role pin stays 59.
  No `tests/sentica/test_m2_*` file, `tests/sentica/rls_job.py`, or the
  `test-m2-rls` job changed.

## M6. Retrieval

Built:

- Conclusions are retrieved by keyword and by vector, then merged with
  reciprocal rank. Filters cover the workspace, the peer, `subject_ref`,
  and recency. Superseded rows stay out. Stale rows sort after current
  ones. Vector neighbors farther than cosine distance 0.65 are dropped
  from that half. A keyword match is kept.
- `POST /peers/{peer_id}/brief` returns the card, the top conclusions, the
  latest summary, and open stale flags, trimmed to `token_budget`. A
  workspace list is read one workspace at a time.
- On 10,000 conclusions the brief was 191 ms cold and 211 ms warm p95,
  under the 300 ms warm target.

Decisions:

- The vector cutoff is 0.65 so an unrelated neighbor does not fill the
  brief. Exact wording still matches through keyword search.
- Local unit tests: 3312 passed, 53 skipped. The runtime-role pin stays 59.
  No `tests/sentica/test_m2_*` file, `tests/sentica/rls_job.py`, or the
  `test-m2-rls` job changed.

Follow-up on 2026-10-22:

- The parallel suite deselects
  `test_context_latency_on_ten_thousand_conclusions`. The next step runs
  that node with `-n0`. The assertion stays `assert warm_p95 < 0.3`.
- CI run 39882904117 reported context cold 125.3 ms, warm p95 72.0 ms.
- The guard requires that deselect command and one pinned serial script.
  It rejects `continue-on-error` on `test-python` and its steps, an `if`
  on that job or either latency step, and a `shell` or `defaults.run.shell`
  override at workflow, job, or step level. The serial step runs
  `--check-workflow` after the sha256 pin. Collection and pass checks
  read the pytest summary line. The deselect prefix must match one
  collected test. The latency test must keep its assertion and must not
  skip. The two steps share one environment. `test-status` must fail
  when `test-python` does not succeed.
- `uv run pytest -n0 tests/sentica/test_context_latency_ci.py` against
  the local Postgres on host port 55432:
  **27 passed, 0 failed** (24.44s).

## M7. Dreams inside each workspace

Built:

- A daily digest is written from that workspace's conclusions. A weekly
  insight is written from those digests, not from raw messages. Each
  record stores the ids it came from and the workspace name. Banned text
  is dropped by the M4 filter. Tokens for the run are counted per tenant.
- Synthesis is on unless the workspace sets `sentica_synthesis` to false
  through `PUT /workspaces/{id}/synthesis`. Turning it off stops new dreams
  and leaves earlier ones in place. The records live on the workspace, not
  a new table.

Decisions:

- The collection dreamer is unchanged. This pass is a separate schedule.
- Local unit tests: 3316 passed, 53 skipped. The runtime-role pin stays 59.
  No `tests/sentica/test_m2_*` file, `tests/sentica/rls_job.py`, or the
  `test-m2-rls` job changed.

## M8. Evaluation harness

Built:

- Seeded conclusions for two businesses, Harbor Glass and Northline Diesel.
  Questions cover information extraction, multi-session reasoning, temporal
  reasoning, knowledge updates, and abstention. Recall at 5 and at 10 is the
  share of gold conclusions in the top k. Nothing is scored by a model.
- `eval/BASELINE.md` records the first run. A category that falls more than
  2 points fails CI. The isolation red-team searches across the two
  workspaces, refuses a token aimed at the other workspace, and refuses a
  queue task for another tenant. Any leak fails CI.

Decisions:

- The harness calls the same retrieval function the brief uses. It does not
  add a table or a migration. Latency on that corpus was 7.1 ms cold and
  7.0 ms warm p95.
- Local unit tests: 3320 passed, 53 skipped. The runtime-role pin stays 59.
  No `tests/sentica/test_m2_*` file, `tests/sentica/rls_job.py`, or the
  `test-m2-rls` job changed.

## M9. Packaging

Built:

- `/ready` checks the database and is a probe, next to `/health` and
  `/metrics`. Filtered conclusions, decay, and prune each have a counter.
  Request, latency, queue depth, and token series were already present.
- The API and deriver image stays uid 100. The two-instance proof runs
  those containers read-only, with every capability dropped and
  no-new-privileges set. `/tmp` is tmpfs. `docker-compose.gvisor.yml`
  selects the `runsc` runtime. This host has no `runsc`, so the resource
  sample used the default runtime.
- PgBouncer verifies Postgres with `server_tls_sslmode = verify-full`
  against the test CA. Redis replaces its entry process with the `redis`
  user. The password file stays readable by the API user. Networks `db`,
  `net-a`, and `net-b` are internal. Docker does not publish ports from an
  internal network, so `publish-a` and `publish-b` forward `127.0.0.1` to
  the API and the proxy. The image carries the tiktoken encodings, because
  those networks have no route to the internet.
- `docs/DEPLOY.md` has the run flags, the network rules, and the sample:
  0.24% CPU and 249 MiB idle, 0.27% CPU and 251 MiB at 1,000 messages per
  hour. `docs/CONFIG.md` lists every Sentica setting. `SECURITY.md` states
  the isolation model and where to report a vulnerability.
  `docs/UPSTREAM_SYNC.md` says to cherry-pick and not merge upstream main.

Decisions:

- The runtime-role pin stays 59. No `tests/sentica/test_m2_*` file,
  `tests/sentica/rls_job.py`, or the `test-m2-rls` job changed. The
  `.review/M2-summary.md` deferral note stays where it is.
- Local unit tests: 3325 passed, 53 skipped.

## R3. Release verification

Built:

- A plain `docker build .` reuses gid 100 when the base image already has the `users` group. CI job `build-default-image` runs that build with no build args.
- CI job `test-gvisor-smoke` installs `runsc` from `gvisor.tar.bz2` after checking `gvisor.tar.bz2.sha512`, then runs the two-instance smoke subset with `runtime: runsc`. runsc cannot use Docker's embedded DNS at 127.0.0.11, so `docker-compose.gvisor.yml` pins `embeddings` and writes `extra_hosts` for the proxy, Redis, and gateway names.
- `/ready` caches a successful database check for 5 seconds. The body stays `{"status":"ok"}` or `{"status":"unavailable"}`.
- Eval embeddings are hashed character n-grams. Information extraction and knowledge updates score 100 only through the vector channel. `eval/BASELINE.md` states what each score measures. `live-eval.yml` is `workflow_dispatch` only.
- The isolation red-team runs in `test-m2-rls` over HTTP with signed broker tokens. The collected pin is 94 node ids.
- Vector search orders fresh rows by cosine distance on the partial HNSW index `ix_documents_embedding_hnsw_current`, then appends stale rows from `ix_documents_embedding_hnsw`. Superseded and deleted rows stay out. A filtered scan sets `hnsw.iterative_scan` to `relaxed_order` so the page fills.
- Gateway budget reserve and charge are Redis Lua scripts on a pooled client. Invalid gateway environment values refuse startup.

gVisor result, measured on Actions (this host has no Docker daemon):

- Commit `f9df321f6a0066437c7265711fe69eaae56402e1`.
- Workflow run: https://github.com/jsv94-cyber/sentica-memory/actions/runs/39007240804
- `test-gvisor-smoke`: 4 passed, 26 deselected, 80.74s. The four tests are egress denied, read-only root, non-root Redis, and no `NET_ADMIN`.
- Same run, other jobs: `test-python` 3431 passed and 53 skipped; `test-m1-stack` 30 passed; `test-m2-rls` 94 passed; `build-default-image` success; `test-status` success.
- Static analysis run: https://github.com/jsv94-cyber/sentica-memory/actions/runs/39007240678
- Jobs `basedpyright`, `ruff`, `issue-gate`, `openapi-drift`, and `alembic-heads` succeeded.
- 11 checks, 0 failures.

Decisions:

- A local-only `spike-hindsight` branch exists. The bake-off was cancelled 2026-10-17.
- The runtime-role pin is 94 collected node ids in `tests/sentica/rls_job.py`. Four isolation node ids were added. Existing ids stayed.
- `.review/M2-summary.md` deferral note stays where it is.

## M-INT. Integration contract

Built:

- `docs/INTEGRATION.md` is the broker contract. Ingest, conclusions,
  `search_memory`, and brief are specified with request and response
  fields. The token audience is `sentica-memory`. Reads degrade when
  the instance returns `memory unavailable`. Writes stay in the
  broker's durable queue.
- `POST /v3/workspaces/{workspace_id}/peers/{peer_id}/search_memory`
  calls the hybrid retrieval the brief already used. Each conclusion
  in that response, and in the conclusion routes, carries
  `workspace_id`.
- A conclusion created by the broker stores the request's
  `source_trust`. Production refuses a missing label, as ingest does.
- The default image pins `python:3.13-slim-bookworm`, `uv`, and
  BuildKit by digest. Copied files are stamped to `SOURCE_DATE_EPOCH`,
  bytecode is compiled with one worker, and the virtualenv is packed
  in sorted order, so a checkout time, a parallel compile, or
  filesystem order cannot change the digest.
  `docs/RELEASE-v0.2.0-sentica.md` records the built digest and the
  export command.
- CI job `build-default-image` checks out the head commit, sets
  `SOURCE_DATE_EPOCH` from the commit that last changed the
  Dockerfile, builds twice from a new builder, and fails unless both
  digests equal the recorded value. The default pull request checkout
  is a merge commit, and its timestamp is a different epoch.
- The footprint table in `docs/INTEGRATION.md` is a throwaway stack on
  this host. Jose's Redis on port 6379 was not used.

Decisions:

- Role and sensitivity stay off the memory row. The workspace is the
  partition the token allows. Adding those columns, the Restricted
  upgrade classifier, and idempotency keys is listed for Jose in
  `docs/INTEGRATION.md`.
- The runtime-role pin stays 94. `tests/sentica/test_m2_data.py`
  adds `source_trust` on the conclusion bodies the proof creates.
  `tests/sentica/rls_job.py` and the `test-m2-rls` job are unchanged.

Test counts, on this host, against throwaway Postgres on port 5441 and Redis on port 6391:

- Unit suite, optional extras installed, `tests/sentica/test_m1_stack.py` excluded: 3455 passed, 53 skipped, 2 failed. Both failures are `tests/sdk_typescript/test_sdk.py` because `bun` is not installed here. CI installs bun.
- Two-instance stack: 33 passed. The on-disk scan in `test_stopped_stack_leaves_no_memory_on_disk` calls `sudo grep`. This host asks for a sudo password, so that scan did not run to completion here. CI runners that have passwordless sudo still run it.

## Redis host port. 2026-10-22

Jose approved item 11 in chat on 2026-10-22: "1 to 12: yes as recommended."

- Tracked compose files publish Redis on host port 36379. 36379 is
  unused by the common local Redis port 6379, Sentinel 26379, and the
  sandbox Redis port 16379. `SENTICA_REDIS_PORT` still overrides the
  host side. The container listens on 6379.
- Postgres host ports remain 5432. Runtime defaults in `src/config.py`
  and `config.toml.example` are unchanged, so the default image digest
  stays the recorded pin.
- `docker-compose.yml.example` publishes Redis on host port 36379.
  The M0 note above records the example as it was on that date.
- The gVisor smoke job sets `DB_CONNECTION_URI` to the runner Postgres
  on port 5432, so that job does not follow a runtime default.

Follow-up on 2026-10-22, suite command:

- `test_ci_collects_the_firewall_topology_test` allows the pinned base
  command plus one `--deselect` of `context_latency_ci.NODE_ID`, and
  nothing else. A second `--deselect`, a `-k`, or another `--ignore`
  fails that check. Removing the latency `--deselect` fails the latency
  guard and leaves the firewall check green.
- `uv run pytest -n0` of that test, its mutations, and
  `tests/sentica/test_context_latency_ci.py`, against the local Postgres
  on host port 55432: **33 passed, 0 failed** (22.41s).

