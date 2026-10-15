# Fork changes

Every change from plastic-labs/honcho is listed here. The pin is upstream
release v3.3.0 (`608aec8a928da13c540262ed980c569448fb53ab`).

| File | Why | Goal |
| --- | --- | --- |
| `NOTICE` | State that this is a modified AGPL fork and keep upstream attribution. | Ground rule 1 |
| `FORK_CHANGES.md` | Record divergences from upstream. | Ground rule 1 |
| `PROGRESS.md` | Milestone log. | Ground rule 8 |
| `docs/MAP.md` | Map of the pinned runtime: database, auth, storage, queue, model calls, prompts. | M0 |
| `docker-compose.yml` | Tracked local stack so `docker compose up` starts the API, deriver, Postgres with pgvector, and Redis. Redis persistence is off and its data directory is tmpfs. API and deriver `/tmp` are tmpfs. No LanceDB volume. Host ports default to 8000, 5432, and 6379 and can be moved with `SENTICA_API_PORT`, `SENTICA_POSTGRES_PORT`, and `SENTICA_REDIS_PORT`. `SENTICA_MODE=test` because the image defaults to production. Postgres `trust` auth on this file is test-only. | M0, M1, ground rule 5 |
| `.gitignore` | Stop ignoring `/docker-compose.yml` and `/compose.yml` so the local stack can be committed. | M0 |
| `.github/workflows/unittest.yml` | Run the upstream test suite on every pull request and every push to `main`. Removed path filters and the changes gate. Both jobs set `SENTICA_MODE=test`. The suite job ignores the two-instance proof. A second job runs that proof and fails if it collects zero tests. | M0, M1 |
| `.github/workflows/push-gcp-registry-prod.yml` | Manual dispatch only, and both jobs are `if: false`. Do not push to Plastic Labs GCP or call its production webhook. | Ground rule 2 |
| `.github/workflows/push-gcp-registry-staging.yml` | Same as production, for the staging registry and webhook. | Ground rule 2 |
| `.github/workflows/unified-tests.yml` | Manual dispatch only. Fly and AWS jobs are `if: false`. | Ground rule 2 |
| `.github/workflows/live-llm-tests.yml` | Manual dispatch only. The live provider job is `if: false` so CI does not call external models or AWS. | Ground rule 2 |
| `.github/workflows/start-fly-runner.yml` | The Fly runner job is `if: false`. | Ground rule 2 |
| `.github/workflows/docker-build.yml` | The image publish job is `if: false` until M9. | M1, M9 |
| `tests/sentica/test_m0_baseline.py` | Acceptance checks for the pin, the map citations, the compose stack, and CI. | M0, ground rule 6 |
| `src/sentica/` | Production startup, token checks, proxy TLS, queue tenant stamps, and refusal counts. | M1 |
| `src/db.py` | In production, connect through the proxy without a password, use schema `memory`, recycle pooled connections within an hour, and drop a connection when the client certificate file changes. | M1 |
| `src/security.py` | In production, verify the broker token before any upstream auth path. Admin bypass is not used. | M1 |
| `src/main.py` | Require a broker token on every path except `/health` and `/metrics`. Database disconnects return 503 `memory unavailable`. | M1 |
| `src/deriver/enqueue.py` | Stamp `tenant_id` on queue payloads in production. | M1 |
| `src/deriver/consumer.py` | Refuse a queue task whose tenant is missing or not this instance. | M1 |
| `src/deriver/queue_manager.py` | Same tenant check on representation batches. | M1 |
| `migrations/env.py` | Pass the proxy TLS arguments into Alembic's own engine. Treat a privilege error creating `vector` as "already installed" so a non-superuser tenant role can migrate. | M1 |
| `docker/entrypoint.sh` | Image entrypoint. Production requires an explicit bind address. A baked marker refuses `SENTICA_MODE=test`. | M1 |
| `Dockerfile` | The runtime image sets `SENTICA_MODE=production`, bakes a production marker, and uses `ENTRYPOINT` with no `CMD`. | M1 |
| `docker-compose.sentica.yml` | Local two-instance proof. Separate networks, hostssl scram from the PgBouncers only, per-file certificates, per-instance Redis passwords on tmpfs, and a log driver that does not write to disk. | M1 |
| `.github/workflows/staticanalysis.yml` | The OpenAPI drift job sets `SENTICA_MODE=test` because it imports the app. | M1 |
| `docker/sentica/` | PgBouncer config, pg_hba, Redis password entrypoint, and the SQL that confines each tenant role to `memory`. | M1 |
| `scripts/sentica_test_ca.py` | Local test CA, 48-hour client certificates, and Ed25519 token keys. No real certificates. | M1 |
| `docs/INSTANCE.md` | How the private bind, the proxy, and the local proof fit together. | M1 |
| `docs/MAP.md` | Line citations updated after the production engine block. | M1 |
| `tests/sentica/test_m1_startup.py` | Startup refuses missing config, a public bind, and a database password. | M1 |
| `tests/sentica/test_m1_tokens.py` | Tokens: expiry, issuer, tenant, admin claim, lifetime. | M1 |
| `tests/sentica/test_m1_stack.py` | Two-instance compose proof. Collected by the default suite. | M1 |
| `src/sentica/workspace_scope.py` | Bind each database transaction to one workspace. A workspace outside the token set is refused. | M2 |
| `migrations/versions/a9c1e3f5b7d2_workspace_row_level_security.py` | Enable and force row-level security on every table that has `workspace_name`. The app role does not own those tables and cannot bypass the policy. | M2 |
| `src/routers/peers.py`, `src/routers/sessions.py`, `src/routers/workspaces.py` | Search, context, chat, representation, and card reads run one workspace at a time and tag each returned item. | M2 |
| `src/deriver/queue_manager.py` | Claim and run each deriver, summary, and dream task under that task's workspace. A gateway budget or outage leaves the queue item unprocessed. | M2, M3 |
| `src/reconciler/scheduler.py` | Enqueue reconciler work once per workspace so the row is visible under row-level security. | M2 |
| `src/sentica/runtime.py` | Production refuses LanceDB, Qdrant, and Chroma. pgvector is the production vector store. | M2 |
| `tests/sentica/test_m2_partitions.py` | Row-level security proof, canary leak test, cache key test, and the next-request token test. | M2 |
| `docs/CONFIG.md` | `SENTICA_MIGRATION_TIMEOUT_SECONDS` and the rule that it must be greater than the lock wait. Gateway mode, per-function URL and model, and the daily token budget. | M1 carry-over, M3 |
| `src/sentica/gateway.py` | One OpenAI-compatible client for deriver, summary, dialectic, dream, and embed calls when gateway mode is on. | M3 |
| `src/llm/api.py` | When gateway mode is on, return before provider resolution so a call cannot fall back to another model. | M3 |
| `src/embedding_client.py` | When gateway mode is on, embed through the gateway and do not construct a provider client. | M3 |
| `src/exceptions.py` | `GatewayBudgetExceeded` (429) and `GatewayUnavailable` (503). | M3 |
| `tests/sentica/test_m3_gateway.py` | Mock-gateway proof for headers, budget, outage, and fail-closed configuration. | M3 |
| `src/sentica/safety.py` | Rules that drop sensitive inferences, plus an optional second model check. | M4 |
| `migrations/versions/f3c8a1d94e27_message_source_trust.py` | `messages.source_trust` for owner, staff, external, and system. | M4 |
| `src/models.py` | Message `source_trust`, and conclusion public message ids plus the external flag. | M4 |
| `src/schemas/api.py` | Message and conclusion responses expose `source_trust`. Conclusions expose `message_ids`. | M4 |
| `src/crud/document.py` | Drop banned conclusions and record the messages they came from. | M4 |
| `src/crud/representation.py` | Screen deriver observations before they are embedded or stored. | M4 |
| `src/utils/agent_tools.py` | Screen dream observations before they are embedded or stored. | M4 |
| `src/crud/peer_card.py` | Leave banned text and external-only conclusions out of the peer card. | M4 |
| `src/deriver/prompts.py`, `src/utils/summarizer.py`, `src/dreamer/specialists.py` | Tell the model not to infer the banned categories. | M4 |
| `tests/sentica/test_m4_safety.py` | Category fixtures, provenance, the external card rule, and the optional model check. | M4 |
| `src/sentica/freshness.py` | Stale flags, superseding, decay, purge, and hard deletion with counts only. | M5 |
| `migrations/versions/a7d4e2c91b08_conclusion_freshness.py` | Subject, stale, superseded, and weight columns on conclusions. | M5 |
| `src/routers/subjects.py` | `POST /subjects/{ref}/changed` and `POST /deletions`. | M5 |
| `tests/sentica/test_m5_freshness.py` | Changed subjects, supersede, decay, purge, and deletion. | M5 |
| `.review/M2-summary.md` | Review notes for M2, including the M9 deferrals. | M2 |

