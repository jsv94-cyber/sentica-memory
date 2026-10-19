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
| `src/sentica/vector_query.py` | HNSW neighbor reads. Stale conclusions come back after fresh ones; superseded and deleted rows stay out. | M5 |
| `src/llm/tool_loop.py` | A safety outage stops the tool loop instead of sending another model call. | M4 |
| `src/sentica/ready.py` | Caches a successful readiness check for a few seconds. | M9 |
| `src/sentica/tokenizer.py` | Local tiktoken encoding for gateway model names. | M3 |
| `migrations/versions/d4a8c1e27b90_documents_hnsw_current.py` | Partial HNSW index for current conclusion embeddings. | M6 |
| `migrations/versions/a7d4e2c91b08_conclusion_freshness.py` | Subject, stale, superseded, and weight columns on conclusions. | M5 |
| `src/routers/subjects.py` | `POST /subjects/{ref}/changed` and `POST /deletions`. | M5 |
| `tests/sentica/test_m5_freshness.py` | Changed subjects, supersede, decay, purge, and deletion. | M5 |
| `src/sentica/retrieval.py` | Keyword plus vector retrieval, and the bounded call brief. | M6 |
| `migrations/versions/b8e3c1a94d20_conclusion_keyword_index.py` | Full-text index on conclusion content. | M6 |
| `src/routers/peers.py` | `POST /peers/{peer_id}/brief`. | M6 |
| `tests/sentica/test_m6_retrieval.py` | Synonym retrieval, filters, the token budget, and latency. | M6 |
| `src/sentica/dreams.py` | Daily digests and weekly insights inside one workspace. | M7 |
| `src/routers/workspaces.py` | `PUT /workspaces/{id}/synthesis` turns new dreams off. | M7 |
| `tests/sentica/test_m7_dreams.py` | Weekly cites digests, isolation, the banned filter, and the off switch. | M7 |
| `src/sentica/eval_harness.py` | Deterministic recall for the synthetic-business eval, and the baseline comparison. | M8 |
| `tests/sentica/test_m8_eval.py` | Category scores, latency, and the isolation red-team. | M8 |
| `eval/BASELINE.md` | First-run recall, latency, and the zero-leak line CI guards. | M8 |
| `src/sentica/ops_metrics.py` | Counters for filtered conclusions, decay, and prune. | M9 |
| `src/main.py` | `/ready` checks the database and stays a probe. | M9 |
| `docker-compose.sentica.yml` | Read-only API and deriver, internal networks, proxy `verify-full`, and proof forwarders for the local ports. | M9 |
| `docker/sentica/publish.py` | Forwards the local proof ports onto the internal networks. | M9 |
| `Dockerfile` | Bakes the tiktoken encodings so startup does not use the internet. | M9 |
| `docker/sentica/redis-entry.sh` | Redis server runs as the `redis` user. The API can still read the password file. | M9 |
| `docs/DEPLOY.md` | Run flags, network rules, and the resource sample. | M9 |
| `docs/UPSTREAM_SYNC.md` | Cherry-pick upstream. Do not merge upstream main. | M9 |
| `docs/CONFIG.md` | Every Sentica setting and the upstream setting groups. | M9 |
| `SECURITY.md` | Fork isolation model and where to report a vulnerability. | M9 |
| `docs/RELEASES.md` | Notes for `v0.1.0-sentica`. | M9 |
| `.review/M2-summary.md` | Review notes for M2, including the M9 deferrals. | M2 |
| `src/routers/peers.py` | `POST /peers/{peer_id}/search_memory` runs hybrid retrieval for the broker. | M-INT |
| `src/sentica/retrieval.py` | `prepare_search` counts the conclusions that search returned. | M-INT |
| `src/schemas/api.py` | Conclusion responses include `workspace_id`. Create accepts `source_trust`. | M-INT |
| `src/schemas/__init__.py` | Exports `MemorySearchRequest`. | M-INT |
| `src/crud/document.py` | Production refuses a missing conclusion `source_trust`. The label that was sent is stored. | M-INT |
| `docs/INTEGRATION.md` | Broker contract: routes, tokens, placement, footprint, failure modes. | M-INT |
| `docs/RELEASE-v0.2.0-sentica.md` | Release notes for `v0.2.0-sentica`. | M-INT |
| `Dockerfile` | Base image and uv are pinned by digest. `SOURCE_DATE_EPOCH` has a default. | M-INT |
| `tests/sentica/test_integration_contract.py` | The written contract and the production trust refusal. | M-INT |
| `tests/sentica/test_m1_stack.py` | Signed-token exercise of every contract route. | M-INT |
| `tests/sentica/test_m6_retrieval.py` | search_memory returns the keyword hit and its workspace. | M-INT |
| `tests/routes/test_scope_route_policy.py` | A scope in the search_memory peer position is refused. | M-INT |
| `.github/workflows/unittest.yml` | The default image build pins BuildKit by digest, checks out the head commit, sets `SOURCE_DATE_EPOCH` from that commit, and fails unless two clean builds match the recorded digest. | M-INT |

