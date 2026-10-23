# Configuration

Sentica settings are environment variables. Upstream Honcho settings are
the groups at the end of this file. A name not listed here is not read.

## Every Sentica setting

| Setting | Role |
| --- | --- |
| `SENTICA_MODE` | `production` or `test`. Anything else refuses to start. |
| `SENTICA_TENANT_ID` | The one tenant this instance serves. |
| `SENTICA_DB_PROXY_HOST` | Certificate proxy hostname. |
| `SENTICA_DB_PROXY_PORT` | Certificate proxy port. |
| `SENTICA_DB_USER` | Database role. No password is set. |
| `SENTICA_DB_NAME` | Database name. |
| `SENTICA_DB_CALL_TIMEOUT_SECONDS` | Statement timeout. Default 120. Maximum 600. |
| `SENTICA_CLIENT_CERT_PATH` | Client certificate file. |
| `SENTICA_CLIENT_KEY_PATH` | Client private key file. |
| `SENTICA_PROXY_CA_PATH` | CA that signed the proxy certificate. |
| `SENTICA_TOKEN_PUBLIC_KEY_PATH` | Broker token verification key. |
| `SENTICA_TOKEN_ISSUER` | Expected token issuer. |
| `SENTICA_TOKEN_SIGNING_KEY` | Test-only signing key path. Production does not use it. |
| `SENTICA_TENANT_HASH_KEY` | HMAC key for deletion identifiers. At least 32 bytes. |
| `SENTICA_TENANT_HASH_KEY_PATH` | Broker key file used when `SENTICA_TENANT_HASH_KEY` is unset. |
| `SENTICA_BIND_HOST` | Private address the API binds. Required in production. |
| `SENTICA_REDIS_HOST` | This instance's Redis hostname. |
| `SENTICA_MIGRATION_TIMEOUT_SECONDS` | How long startup waits for migrations. Default 360. |
| `SENTICA_DECAY_INTERVAL_DAYS` | Unused conclusions halve once per interval. Default 30. |
| `SENTICA_DECAY_FLOOR` | Weight below this is soft-deleted. Default 0.05. |
| `SENTICA_DECAY_RETENTION_DAYS` | Soft-deleted rows are purged after this. Default 30. |
| `SENTICA_SAFETY_MODEL_CHECK` | Optional second filter. Off unless `1`, `true`, `yes`, or `on`. |
| `SENTICA_GATEWAY_MODE` | Gateway mode. Off unless `1`, `true`, `yes`, or `on`. |
| `SENTICA_GATEWAY_BASE_URL` | Default gateway URL when a purpose-specific URL is unset. |
| `SENTICA_GATEWAY_DERIVER_BASE_URL` | Deriver gateway URL. |
| `SENTICA_GATEWAY_SUMMARY_BASE_URL` | Summary gateway URL. |
| `SENTICA_GATEWAY_DIALECTIC_BASE_URL` | Dialectic gateway URL. |
| `SENTICA_GATEWAY_DREAM_BASE_URL` | Dream gateway URL. |
| `SENTICA_GATEWAY_EMBED_BASE_URL` | Embedding gateway URL. |
| `SENTICA_GATEWAY_DERIVER_MODEL` | Required deriver model when gateway mode is on. |
| `SENTICA_GATEWAY_SUMMARY_MODEL` | Required summary model when gateway mode is on. |
| `SENTICA_GATEWAY_DIALECTIC_MODEL` | Required dialectic model when gateway mode is on. |
| `SENTICA_GATEWAY_DREAM_MODEL` | Required dream model when gateway mode is on. |
| `SENTICA_GATEWAY_EMBED_MODEL` | Required embedding model when gateway mode is on. |
| `SENTICA_GATEWAY_DAILY_TOKEN_BUDGET` | Optional daily token cap. Unset means unlimited. `0` is exhausted. |
| `SENTICA_GATEWAY_RETRY_ATTEMPTS` | Gateway retries. Default 3. An invalid value refuses startup. |
| `SENTICA_GATEWAY_RETRY_WAIT_SECONDS` | Wait between gateway retries. Default 0.05. An invalid value refuses startup. |
| `SENTICA_GATEWAY_TOKENIZER` | Tiktoken encoding for unknown gateway model names. Default `cl100k_base`. |
| `SENTICA_GATEWAY_TOKENIZER_ALIASES` | Comma-separated `alias=encoding` pairs, for example `sentica-embed=cl100k_base`. |
| `SENTICA_API_PORT` | Host port for the local API. Default 8000. |
| `SENTICA_POSTGRES_PORT` | Host port for the local database. Default 35432. |
| `SENTICA_REDIS_PORT` | Host port for the local Redis. Default 36379. |
| `SENTICA_API_A_PORT` | Host port for instance A in the two-instance proof. |
| `SENTICA_API_B_PORT` | Host port for instance B in the two-instance proof. |
| `SENTICA_PROXY_A_PORT` | Host port for A's certificate proxy. |
| `SENTICA_PROXY_B_PORT` | Host port for B's certificate proxy. |
| `SENTICA_UID` | Image user id. Default 100. The agent must not use this id. |
| `SENTICA_GID` | Image group id. Default 100. |
| `SENTICA_BAKE_PRODUCTION_MARKER` | Build arg. `true` bakes the production image marker. |
| `SENTICA_RLS_JOB` | Set to `1` only by the runtime-role CI job. |
| `SENTICA_RLS_ADMIN_DSN` | Admin connection string for that CI job only. |

## Upstream setting groups

These classes live in `src/config.py`. Each field is an environment variable
named from the class prefix plus the field name.

| Class | Prefix |
| --- | --- |
| `DBSettings` | `DB_` |
| `AuthSettings` | `AUTH_` |
| `SentrySettings` | `SENTRY_` |
| `LLMSettings` | `LLM_` |
| `EmbeddingSettings` | `EMBEDDING_` |
| `DeriverSettings` | `DERIVER_` |
| `PeerCardSettings` | `PEER_CARD_` |
| `DialecticSettings` | `DIALECTIC_` |
| `SummarySettings` | `SUMMARY_` |
| `WebhookSettings` | `WEBHOOK_` |
| `MetricsSettings` | `METRICS_` |
| `TelemetrySettings` | `TELEMETRY_` |
| `CacheSettings` | `CACHE_` |
| `DreamSettings` | `DREAM_` |
| `VectorStoreSettings` | `VECTOR_STORE_` |
| `TraceViewerSettings` | `TRACE_VIEWER_` |
| `AppSettings` | (none) |

`DialecticLevelSettings` is nested under `DIALECTIC_LEVELS__<level>__`.
`ModelOverrideSettings`, `FallbackModelSettings`, and
`ConfiguredModelSettings` are nested under the model config that uses them.

## SENTICA_MIGRATION_TIMEOUT_SECONDS

How long the container entrypoint waits for database migrations, in seconds.
The default is 360.

This value must be greater than `DB_MIGRATION_LOCK_WAIT_SECONDS`
(the `MIGRATION_LOCK_WAIT_SECONDS` setting, default 300). The entrypoint
compares the two at startup and exits before it runs a migration when the
migration timeout is less than or equal to the lock wait.

## Gateway

Production refuses to start unless `SENTICA_GATEWAY_MODE` is explicitly on.
The accepted on-values are `1`, `true`, `yes`, and `on` (any case, surrounding
space ignored). Unset, empty, and any other value refuse in production. Test
mode may leave it unset and keep using the provider SDKs.

When it is on, every model call needs:

- `SENTICA_TENANT_ID`
- `SENTICA_GATEWAY_BASE_URL`, or a function-specific
  `SENTICA_GATEWAY_DERIVER_BASE_URL`, `SENTICA_GATEWAY_SUMMARY_BASE_URL`,
  `SENTICA_GATEWAY_DIALECTIC_BASE_URL`, `SENTICA_GATEWAY_DREAM_BASE_URL`, or
  `SENTICA_GATEWAY_EMBED_BASE_URL`
- `SENTICA_GATEWAY_DERIVER_MODEL`, `SENTICA_GATEWAY_SUMMARY_MODEL`,
  `SENTICA_GATEWAY_DIALECTIC_MODEL`, `SENTICA_GATEWAY_DREAM_MODEL`, and
  `SENTICA_GATEWAY_EMBED_MODEL`

A missing value is refused. The call does not pick another model.

The authoritative budget is enforced at the Sentica gateway. This process
keeps a backstop in the instance Redis, shared by the API and the deriver.
`SENTICA_GATEWAY_REDIS_URL` wins. Otherwise a `redis://` or `rediss://`
`CACHE_URL` is used. The counter is per tenant, across every purpose, and
resets at UTC midnight. `SENTICA_GATEWAY_DAILY_TOKEN_BUDGET` is that total.
Unset means no total. `0` means the budget is already spent.

An optional per-purpose limit is
`SENTICA_GATEWAY_DERIVER_DAILY_TOKEN_BUDGET`,
`SENTICA_GATEWAY_SUMMARY_DAILY_TOKEN_BUDGET`,
`SENTICA_GATEWAY_DIALECTIC_DAILY_TOKEN_BUDGET`,
`SENTICA_GATEWAY_DREAM_DAILY_TOKEN_BUDGET`, or
`SENTICA_GATEWAY_EMBED_DAILY_TOKEN_BUDGET`. A purpose limit blocks only that
purpose. A response that omits usage is charged an estimate, never zero.
Reserve reads both counters in one Redis script. Charge increments both
counters in one Redis script, on a pooled connection. Two reserves can both
pass before either charge lands, so the backstop can still slightly overshoot.
An invalid gateway environment value, including
`SENTICA_GATEWAY_RETRY_ATTEMPTS` and every other `SENTICA_GATEWAY_*` value
listed above, refuses startup. A blank optional value keeps its default.

When `SENTICA_GATEWAY_TIMEOUT_SECONDS` is unset, each purpose has its own
default: deriver 120 seconds, dream 180 seconds, dialectic 60 seconds,
summary 60 seconds, and embed 30 seconds. A purpose may override that with
`SENTICA_GATEWAY_DERIVER_TIMEOUT_SECONDS`,
`SENTICA_GATEWAY_SUMMARY_TIMEOUT_SECONDS`,
`SENTICA_GATEWAY_DIALECTIC_TIMEOUT_SECONDS`,
`SENTICA_GATEWAY_DREAM_TIMEOUT_SECONDS`, or
`SENTICA_GATEWAY_EMBED_TIMEOUT_SECONDS`. `SENTICA_GATEWAY_TIMEOUT_SECONDS`
overrides every purpose that does not set its own value. A value that is
not a positive number is refused.

`SENTICA_GATEWAY_RETRY_ATTEMPTS` defaults to 3.
`SENTICA_GATEWAY_RETRY_WAIT_SECONDS` defaults to 0.05. A failed attempt
retries the same URL and model. It does not fall back to another provider.

Gateway model names such as `sentica-embed` are not tiktoken model names.
`SENTICA_GATEWAY_TOKENIZER` is the encoding used for those names. The
default is `cl100k_base`. `SENTICA_GATEWAY_TOKENIZER_ALIASES` maps one
alias to one encoding (`sentica-embed=cl100k_base`). A known tiktoken model
still uses `encoding_for_model`. An unknown encoding refuses startup.

Streaming to the gateway is simulated in v1: full completion, then chunked.
The call waits for the full completion, then chunks that text. The request
body does not set `stream: true`.

A token may list more than one workspace. It can create resources only in
its primary workspace (`w`): a workspace, a peer, a session, or a webhook.
Creating one of those in another workspace named on the token is refused.

## Message source trust

A message may set `source_trust` to `owner`, `staff`, `external`, or
`system`. In production a missing value is refused. A conclusion is
`external` when any source message or any premise is `external`. Derived
conclusions, dreams, and digests inherit that taint. Peer cards are built
only from conclusions that are not external.

## Retrieval

`POST /v3/workspaces/{workspace_id}/peers/{peer_id}/brief` takes
`token_budget` on the request. There is no environment setting for it.
`POST /v3/workspaces/{workspace_id}/peers/{peer_id}/search_memory` is the
same keyword-plus-vector search without the card or the summary. Vector
neighbors farther than cosine distance 0.65 are left out of that half of
the search. A keyword match is still returned. The request and response
shapes are in `docs/INTEGRATION.md`.

## Freshness

`SENTICA_DECAY_INTERVAL_DAYS` defaults to 30. An unused conclusion halves
its weight once per interval. `SENTICA_DECAY_FLOOR` defaults to 0.05. A
weight below that is soft-deleted. `SENTICA_DECAY_RETENTION_DAYS` defaults
to 30. Soft-deleted conclusions are purged after that. A use resets the
clock. Decay runs as one queue work unit per workspace. A value that is
not a positive number is refused.

## Workspace synthesis

`PUT /v3/workspaces/{workspace_id}/synthesis` sets `sentica_synthesis` and
`dream.enabled` on the workspace. A missing value means synthesis is on,
including for a restricted workspace. `false` stops new daily and weekly
dreams and the upstream dreamer, and does not delete earlier ones. A daily
digest is written once per UTC date. A weekly insight is written once per
ISO week, from the latest seven daily digests. Digests are read at
`GET /v3/workspaces/{workspace_id}/digests`.

## Safety model check

`SENTICA_SAFETY_MODEL_CHECK` is on in production unless it is `0`, `false`,
`no`, or `off`. Any other value is refused. When it is on, a conclusion,
summary, or digest that passes the rules is checked again by the summary
model. A yes answer, a missing workspace, an open database session, or a
failed call holds the text. It is not stored. The check does not switch
to another model.
