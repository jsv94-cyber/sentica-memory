# Fork changes

Every change from plastic-labs/honcho is listed here. The pin is upstream
release v3.3.0 (`608aec8a928da13c540262ed980c569448fb53ab`).

| File | Why | Goal |
| --- | --- | --- |
| `NOTICE` | State that this is a modified AGPL fork and keep upstream attribution. | Ground rule 1 |
| `FORK_CHANGES.md` | Record divergences from upstream. | Ground rule 1 |
| `PROGRESS.md` | Milestone log. | Ground rule 8 |
| `docs/MAP.md` | Map of the pinned runtime: database, auth, storage, queue, model calls, prompts. | M0 |
| `docker-compose.yml` | Tracked local stack so `docker compose up` starts the API, deriver, Postgres with pgvector, and Redis. Redis persistence is off and its data directory is tmpfs. API and deriver `/tmp` are tmpfs. No LanceDB volume. Host ports default to 8000, 5432, and 6379 and can be moved with `SENTICA_API_PORT`, `SENTICA_POSTGRES_PORT`, and `SENTICA_REDIS_PORT`. | M0, ground rule 5 |
| `.gitignore` | Stop ignoring `/docker-compose.yml` and `/compose.yml` so the local stack can be committed. | M0 |
| `.github/workflows/unittest.yml` | Run the upstream test suite on every pull request and every push to `main`. Removed path filters and the changes gate. | M0 |
| `.github/workflows/push-gcp-registry-prod.yml` | Manual dispatch only, and both jobs are `if: false`. Do not push to Plastic Labs GCP or call its production webhook. | Ground rule 2 |
| `.github/workflows/push-gcp-registry-staging.yml` | Same as production, for the staging registry and webhook. | Ground rule 2 |
| `.github/workflows/unified-tests.yml` | Manual dispatch only. Fly and AWS jobs are `if: false`. | Ground rule 2 |
| `.github/workflows/live-llm-tests.yml` | Manual dispatch only. The live provider job is `if: false` so CI does not call external models or AWS. | Ground rule 2 |
| `.github/workflows/start-fly-runner.yml` | The Fly runner job is `if: false`. | Ground rule 2 |
| `tests/sentica/test_m0_baseline.py` | Acceptance checks for the pin, the map citations, the compose stack, and CI. | M0, ground rule 6 |
| `src/sentica/` | Production startup, token checks, proxy TLS, queue tenant stamps, and refusal counts. | M1 |
| `src/db.py` | In production, connect through the proxy without a password, use schema `memory`, recycle pooled connections within an hour, and drop a connection when the client certificate file changes. | M1 |
| `src/security.py` | In production, verify the broker token before any upstream auth path. Admin bypass is not used. | M1 |
| `src/main.py` | Require a broker token on every path except `/health` and `/metrics`. Database disconnects return 503 `memory unavailable`. | M1 |
| `src/deriver/enqueue.py` | Stamp `tenant_id` on queue payloads in production. | M1 |
| `src/deriver/consumer.py` | Refuse a queue task whose tenant is missing or not this instance. | M1 |
| `src/deriver/queue_manager.py` | Same tenant check on representation batches. | M1 |
| `migrations/env.py` | Pass the proxy TLS arguments into Alembic's own engine. Treat a privilege error creating `vector` as "already installed" so a non-superuser tenant role can migrate. | M1 |
| `docker/entrypoint.sh` | In production, bind the private interface. `private` resolves to the container's outbound address. | M1 |
| `docker-compose.sentica.yml` | Local two-instance proof: PgBouncer client certificates, schema `memory`, tmpfs Postgres and Redis, no database password in the instance environment. | M1 |
| `docker/sentica/` | PgBouncer config and the SQL that creates one role and one database per tenant. | M1 |
| `scripts/sentica_test_ca.py` | Local test CA, 48-hour client certificates, and Ed25519 token keys. No real certificates. | M1 |
| `docs/INSTANCE.md` | How the private bind, the proxy, and the local proof fit together. | M1 |
| `docs/MAP.md` | Line citations updated after the production engine block. | M1 |
| `tests/sentica/test_m1_startup.py` | Startup refuses missing config, a public bind, and a database password. | M1 |
| `tests/sentica/test_m1_tokens.py` | Tokens: expiry, issuer, tenant, admin claim, lifetime. | M1 |
| `tests/sentica/test_m1_stack.py` | Two-instance compose proof. Skipped unless `SENTICA_M1_STACK=1`. | M1 |
