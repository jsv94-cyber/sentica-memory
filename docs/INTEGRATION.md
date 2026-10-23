# Integration contract

This is the contract Sentica's memory broker implements against one
sentica-memory instance. The engine is the Honcho fork named in
BUILD.md Section 35.13. Shapes below are what the running API returns.
A change that breaks a shape fails `test_integration_contract` on the
two-instance stack.

The broker is the only caller. The agent container has no route to the
instance. Tokens are signed by the broker. The instance holds the public
key only.

## Base URL

`http://<SENTICA_BIND_HOST>:8000`. In the deploy plan that address is
`10.8.0.4`. Paths below start at `/v3`. Probes are at the root.

Every `/v3` route requires `Authorization: Bearer <token>`. A missing
token is HTTP 401 `{"detail":"missing token"}`.

## Broker token

Signed with Ed25519 (`alg` EdDSA) or RSA (`alg` RS256). The instance
picks the algorithm from the public key. A token whose header names a
different algorithm is refused.

| Claim | Rule |
| --- | --- |
| `iss` | Equal to `SENTICA_TOKEN_ISSUER`. |
| `aud` | The string `sentica-memory`. |
| `iat` | Numeric date. |
| `exp` | Numeric date. `exp - iat` is at most 300 seconds. |
| `request_id` | Non-empty string. Not logged as content. |
| `tenant_id` | Equal to this instance's `SENTICA_TENANT_ID`. |
| `w` | Primary workspace name. |
| `workspaces` | Array of workspace names. `w` is one of them. Omit the array only when the token allows exactly `w`. |
| `p` | Optional peer name. When set, peer routes for a different peer are refused. |
| `s` | Optional session name. When set, the token can call only that session's message routes. Leave it unset on brief, search_memory, and conclusion calls. |
| `ad` | Omit. `true` is refused. Admin tokens are disabled. |

The conversation audience (the people on the call, or the customer on a
channel) is computed by the broker. The authorization boundary for that
audience is this token: `workspaces` contains only the partitions that
audience may read. The instance does not take a second audience claim.
Database verification of a signed audience value is a later Sentica
phase and is not part of this service.

Lifetime over 5 minutes, a bad signature, a wrong issuer, a wrong
audience, a wrong tenant, a missing request id, and a workspace outside
`workspaces` are HTTP 401. The broker mints a new token for the same
audience. It does not widen `workspaces` to make the call succeed.

## Workspace and peer mapping

One process serves one tenant. Tables live in schema `memory` in that
tenant's database.

The broker chooses workspace names. One workspace is one access
partition: the general partition, one workspace per role (including each
Restricted role), and one workspace per personal space. The instance
does not parse the name. A peer that exists in two workspaces has two
representations.

`w` is the primary workspace. Creating a workspace, a peer, a session,
or a webhook succeeds only when the path workspace is `w`. Message
ingest, conclusion writes, brief, and search_memory succeed for any
name in `workspaces`.

The peer id is the subject the memory is about (a customer, a staff
member, or the entity). Brief and search_memory read that peer's
self-collection: observer and observed are both the path peer. Ingest
sets `peer_id` to the same id. Create the peer and the session in that
workspace before the first ingest.

Suggested names, chosen by the broker and not enforced here:

| Partition | Workspace name | Peer id |
| --- | --- | --- |
| General business memory | `general` | The subject, for example `customer-123` |
| A role | `role-<registry id>` | The subject |
| A personal space | `personal-<user id>` | That user |

`subject_ref` on a conclusion is the record id, for example
`customer:123` or `job:J-2291`. It is not the workspace name.

## Ingest

`POST /v3/workspaces/{workspace_id}/sessions/{session_id}/messages`

Request:

```json
{
  "messages": [
    {
      "peer_id": "customer-123",
      "content": "the customer prefers the window seat",
      "source_trust": "owner"
    }
  ]
}
```

`source_trust` is required in production. The values are `owner`,
`staff`, `external`, and `system`. A missing value is HTTP 422
`{"detail":"source_trust is required"}`. At most 100 messages. Optional
fields on each message are `metadata` (object) and `created_at`
(ISO-8601).

Response `201`, an array:

```json
[
  {
    "id": "msg_public_id",
    "content": "the customer prefers the window seat",
    "peer_id": "customer-123",
    "session_id": "call-1",
    "metadata": {},
    "source_trust": "owner",
    "created_at": "2026-10-18T15:00:00Z",
    "workspace_id": "general",
    "token_count": 7
  }
]
```

The deriver turns messages into conclusions in the background. The
response does not wait for that.

## Conclusions

Create: `POST /v3/workspaces/{workspace_id}/conclusions`

```json
{
  "conclusions": [
    {
      "content": "the customer prefers the window seat",
      "observer_id": "customer-123",
      "observed_id": "customer-123",
      "session_id": "call-1",
      "subject_ref": "customer:123",
      "source_trust": "owner"
    }
  ]
}
```

`observer_id` and `observed_id` are required and must already be peers
in this workspace. For the broker they are the same subject peer.
`session_id` is optional and must already exist when set.
`subject_ref` is optional. `source_trust` uses the same four values as
ingest. Production refuses a missing label, as ingest does. At most
100 conclusions.
A conclusion the safety filter drops is omitted from the response. An
empty kept set returns `201` and `[]`.

Response `201`, an array of conclusion objects:

| Field | Meaning |
| --- | --- |
| `id` | Conclusion id. |
| `content` | Stored text. |
| `observer_id` | Peer who holds the conclusion. |
| `observed_id` | Peer the conclusion is about. |
| `session_id` | Session, or null. |
| `level` | `explicit`, `deductive`, `inductive`, or `contradiction`. A broker create is `explicit`. |
| `source_ids` | Premise conclusion ids. Empty for an explicit conclusion. |
| `times_derived` | Integer. |
| `message_ids` | Public message ids this conclusion came from. |
| `source_trust` | `owner`, `staff`, `external`, `system`, or null. |
| `subject_ref` | Subject, or null. |
| `stale` | True after that subject is marked changed. |
| `superseded_by` | Id of the replacement, or null. |
| `weight` | Decay weight. Starts at 1. |
| `retrieved_count` | Times a read counted this row. |
| `used_count` | Times a read counted this row as used. |
| `created_at` | ISO-8601. |
| `workspace_id` | Workspace the row is stored in. |

List: `POST /v3/workspaces/{workspace_id}/conclusions/list`

```json
{ "filters": {} }
```

Response `200`: `{"items":[ <conclusion> ], "total": 1, "page": 1, "size": 50, "pages": 1}`.

Get: `GET /v3/workspaces/{workspace_id}/conclusions/{conclusion_id}`

Response `200`, one conclusion object. Unknown id is HTTP 404.

`POST /v3/workspaces/{workspace_id}/conclusions/query` is the upstream
vector route. Sentica uses search_memory for hybrid retrieval.

## search_memory

`POST /v3/workspaces/{workspace_id}/peers/{peer_id}/search_memory`

```json
{
  "query": "window seat",
  "subject_ref": "customer:123",
  "since": "2026-10-01T00:00:00Z",
  "top_k": 10,
  "workspaces": ["general", "role-sales"]
}
```

`query` is required. Whitespace only is HTTP 422. `subject_ref`,
`since`, and `workspaces` are optional. `top_k` defaults to 10 and is
at most 50, and it applies per workspace. Every name in `workspaces`
must be in the token. A name outside the token is refused before the
query is embedded. The path workspace is always included. Each
workspace is queried on its own.

Response `200`, an array of conclusion objects in the shape above.
Keyword matches and vector neighbors within cosine distance 0.65 are
merged. Superseded rows are left out. Stale rows sort after current
rows. Each object includes `workspace_id`. A hit increments
`retrieved_count` and `used_count`.

## Brief

`POST /v3/workspaces/{workspace_id}/peers/{peer_id}/brief`

```json
{
  "token_budget": 400,
  "query": "window seat",
  "subject_ref": "customer:123",
  "since": "2026-10-01T00:00:00Z",
  "top_k": 10,
  "workspaces": ["general"]
}
```

`token_budget` is required, from 1 to 8000. `query` is optional. With
a query, the conclusions are the search_memory merge. Without a query,
they are the most recent current conclusions. The card, conclusions,
summary, and stale flags are trimmed to the budget.

One workspace responds `200` with one object:

```json
{
  "peer_id": "customer-123",
  "card": [],
  "conclusions": [],
  "summary": null,
  "stale": [],
  "tokens_used": 0,
  "workspace_id": "general"
}
```

`stale` items are `{"id", "subject_ref", "content"}` and carry no
`session_id`. A request whose `workspaces` array names more than the
path returns a JSON array of those objects, one per workspace, path
workspace first.

Caller-ID preload is this call. The broker still applies the release
check. The instance returns the package to any token that includes the
workspace.

## Companion routes

Mark a subject stale:
`POST /v3/workspaces/{workspace_id}/subjects/{subject_ref}/changed`

Response `200`: `{"subject_ref":"customer:123","stale_count":1}`.

Delete a peer or a subject:
`POST /v3/workspaces/{workspace_id}/deletions`

```json
{ "subject_ref": "customer:123" }
```

or `{"peer_id":"customer-123"}`. Set one of the two. Response `201`:

```json
{
  "id": "deletion_id",
  "peer_name": null,
  "subject_ref": "customer:123",
  "document_count": 1,
  "embedding_count": 1,
  "summary_count": 0
}
```

The body has counts only.

Synthesis, on unless turned off:
`PUT /v3/workspaces/{workspace_id}/synthesis`

```json
{ "enabled": false }
```

Response `200`: `{"enabled": false}`. Earlier dreams stay.

Digests: `GET /v3/workspaces/{workspace_id}/digests`

Response `200`, an array of `{id, kind, period, content, external, created_at}`.

## Environment and secrets

Production refuses to start when `SENTICA_MODE` is not `production`,
when `SENTICA_GATEWAY_MODE` is not on, when the tenant hash key is
missing or shorter than 32 bytes, or when any name from
`SENTICA_TENANT_ID` through `SENTICA_BIND_HOST` is unset. The gateway
URL and the five model names are required before a model call.
`SENTICA_REDIS_HOST` is required when the instance password file is
mounted, which is how the production container starts. Values are not
logged. No database password is set. Provider API keys are not set on
this sidecar. The model gateway holds those.

| Name | Secret | Role |
| --- | --- | --- |
| `SENTICA_MODE` | no | `production` |
| `SENTICA_TENANT_ID` | no | This tenant only |
| `SENTICA_DB_PROXY_HOST` | no | Certificate proxy hostname |
| `SENTICA_DB_PROXY_PORT` | no | Certificate proxy port, `6432` in the deploy plan |
| `SENTICA_DB_USER` | no | Memory role. No password. |
| `SENTICA_DB_NAME` | no | Tenant database name |
| `SENTICA_CLIENT_CERT_PATH` | file | Client certificate, tmpfs, mode 600, uid 100 |
| `SENTICA_CLIENT_KEY_PATH` | file | Client private key, same mount rules |
| `SENTICA_PROXY_CA_PATH` | file | CA that signed the proxy certificate |
| `SENTICA_TOKEN_PUBLIC_KEY_PATH` | file | Broker public key. The private key stays on the broker. |
| `SENTICA_TOKEN_ISSUER` | no | Expected `iss` |
| `SENTICA_BIND_HOST` | no | Private address, `10.8.0.4` in the deploy plan |
| `SENTICA_TENANT_HASH_KEY` or `SENTICA_TENANT_HASH_KEY_PATH` | yes | HMAC key, at least 32 bytes |
| `SENTICA_GATEWAY_MODE` | no | `on` |
| `SENTICA_GATEWAY_BASE_URL` | no | Model gateway, `https://10.8.0.22` in the deploy plan |
| `SENTICA_GATEWAY_DERIVER_MODEL` | no | Required model name |
| `SENTICA_GATEWAY_SUMMARY_MODEL` | no | Required model name |
| `SENTICA_GATEWAY_DIALECTIC_MODEL` | no | Required model name |
| `SENTICA_GATEWAY_DREAM_MODEL` | no | Required model name |
| `SENTICA_GATEWAY_EMBED_MODEL` | no | Required model name |
| `SENTICA_REDIS_HOST` | no | This instance's Redis. The password is generated on tmpfs inside the instance. |

`SENTICA_MIGRATION_TIMEOUT_SECONDS` (default 360) must be greater than
`DB_MIGRATION_LOCK_WAIT_SECONDS` (default 300). The rest of the settings
are in `docs/CONFIG.md`. `docs/DEPLOY.md` has the run flags.

## Network

The bridges and addresses are in `docs/DEPLOY.md`. The broker reaches
the API at `10.8.0.4:8000`. That address is not on a bridge.

| Container | Address |
| --- | --- |
| API | `10.8.0.18` on `sentica-instance`, `10.8.0.82` on `sentica-clients` |
| Deriver | `10.8.0.19` on `sentica-instance`, `10.8.0.83` on `sentica-clients` |
| Migrate | `10.8.0.20` on `sentica-instance`, `10.8.0.84` on `sentica-clients` |
| Certificate proxy | `10.8.0.85:6432` on `sentica-clients`, `10.8.0.67` on `sentica-db` |
| Model gateway | `10.8.0.22:443` on `sentica-instance`, `10.8.0.34` on `sentica-egress` |
| Egress proxy | `10.8.0.35:8443` on `sentica-egress`, `10.8.0.50` on `sentica-wan` |
| Postgres | `10.8.0.66:5432` on `sentica-db` |

`sentica-clients`, `sentica-egress`, and `sentica-db` are internal.
`sentica-instance` is not, because the API publish cannot come from an
internal network. The gateway is not on `sentica-clients`. The
certificate proxy is not on `sentica-instance`. Postgres is only on
`sentica-db`. The API, deriver, and migrate reach Postgres only through
the certificate proxy. The agent container is on none of these networks.

Provider hosts (`api.openai.com`, `api.anthropic.com`,
`generativelanguage.googleapis.com`) are opened by the egress proxy,
from the gateway only. The `SENTICA` chain and the unit that restores
it after `docker.service` are in `docs/DEPLOY.md`. Run the API and the
deriver read-only, uid 100, capabilities dropped, `no-new-privileges`,
tmpfs `/tmp`, runtime `runsc`.

## Health and readiness

`GET /health` is liveness. No token. `200` `{"status":"ok"}` when the
process is up.

`GET /ready` is readiness. No token. `200` `{"status":"ok"}` when the
database accepts `SELECT 1`. A success is cached for 5 seconds. A
failure is not cached. `503` `{"status":"unavailable"}` when the
database is down. Send traffic only when ready is `200`.

`GET /metrics` is the Prometheus scrape. No token. Reachable only from
the broker network. The series are listed in `docs/DEPLOY.md`.

## Migrate order

1. Create the instance network and mount the certificate files on tmpfs.
2. Start the certificate proxy. Postgres is already accepting that proxy.
3. Start Redis with persistence off.
4. Run the migrate container to exit 0. The command is the migrate block
   in `docs/DEPLOY.md`, including
   `--entrypoint /app/docker/migrate-entrypoint.sh`. The migration role
   is the certificate on that container.
5. Start the API and the deriver. Both refuse to start when
   `memory.alembic_version` is missing or is not this image's head.

The API and the deriver do not run migrations.

## Resource footprint

Measured on this host with a throwaway compose project, default runtime
(this host has no `runsc`), `SENTICA_MODE=test`, embeddings off. RSS is
the sum of `VmRSS` for every process in the container. Idle is before
the first request. The small load posted 20 messages and one brief.
Jose's stack on port 6379 was not used.

| Container | Idle RSS | Load RSS |
| --- | --- | --- |
| api | 244.1 MiB | 286.6 MiB |
| deriver | 231.9 MiB | 231.9 MiB |
| postgres | 118.7 MiB | 144.9 MiB |
| redis | 22.6 MiB | 22.7 MiB |

The four idle figures added together are the sidecar's resident set
before traffic. The 4 GB tenant droplet also runs the agent. Size from
these figures plus the agent, with headroom for the deriver's model
responses held in memory during a call.

## When memory is down

`/ready` is `503` `{"status":"unavailable"}`, or a `/v3` call is `503`
`{"detail":"memory unavailable"}`, when the database or the proxy is
down, including a revoked certificate. The body has no memory content.

Reads (brief, search_memory, conclusion list and get) degrade. The
entity keeps working without memory context. Skip caller-ID preload.
Do not retry the read on the phone-call path until ready is `200`.
Do not answer from a workspace that was not on the token.

Writes (ingest, conclusion create, subject changed, deletion) fail
closed. The broker holds the write in its durable control-plane queue
and retries the same workspace after ready is `200`. A successful
response is the only ack. Do not write the payload to the agent
container or to the tenant VM disk.

HTTP 401 is a bad token, not an outage. Mint a new token for the same
audience. Do not queue a 401 as a retry of the same token, and do not
add a workspace to get past it.

HTTP 422 is a bad request. Do not queue it as a transient failure.

HTTP 429 `token budget exhausted` pauses background model work for this
tenant. Do not switch models.

A gateway timeout or HTTP 503 from a model call on a read degrades the
same way as memory unavailable. A model outage during deriver work
leaves the queue item unprocessed. The deriver retries it. The broker
does not also replay that message.

## Gaps for Jose

Item 1 is closed. Item 4 is the read contract. Item 6 is an accepted
residual risk. Items 2, 3, 5, and 7 are Section 35 requirements this
service does not implement yet. The small ones in this release are
search_memory and `workspace_id` on every conclusion. A conclusion
stores the `source_trust` that was sent.

1. Closed by design (D5). The workspace is the label. Role and
   sensitivity stay off the memory row, as recorded in `PROGRESS.md`.
   A conclusion stores `source_trust` and `subject_ref`, and
   `workspace_id` is the partition. Derived rows inherit an external
   taint. They do not store General, Internal, Restricted, or Personal,
   and they do not store a role id.

   Planned contract for R6-6. The Sentica broker sets an allow-listed
   ceiling key in workspace metadata
   (`ALLOWED_WORKSPACE_INTERNAL_METADATA_KEYS` in
   `src/sentica/workspace_metadata.py`) in `ensurePartition`. The engine
   treats a missing key as `general` and fails closed. A key that is
   not on that allow-list is refused.
2. The classifier that upgrades bank numbers, Social Security numbers,
   salaries, medical terms, and disciplinary language to Restricted is
   not in this service. The storage bans in Section 35.7 are in force:
   those inferences are dropped before they are stored.
3. The release check, redaction, and the Restricted-read audit row are
   the broker's. This service returns the rows the token's workspaces
   allow.
4. Dedupe is on read (D9), using `workspace_id`, `subject_ref`,
   `session_id`, and `content`. search_memory, each brief conclusion,
   and each conclusion list item carry those four fields. Stale items
   carry no `session_id`. `Idempotency-Key` is an upstream ask and is
   not built here. The broker must treat a 2xx body as the only ack. A
   dropped connection after the server committed can insert a second
   row on replay.
5. Break-glass reads, owner sharing rules, and holding a caller-ID
   preload until identity is confirmed are Sentica product behavior.
   The brief is returned to any token that includes the workspace.
6. Accepted residual risk. Provider hostnames are not iptables
   destinations. A hostname in `iptables -d` is resolved once, when the
   rule is inserted, and the rule keeps that address. Squid enforces
   hostnames. See the network section of `docs/DEPLOY.md`.
7. Production refuses a conclusion with no `source_trust`, as ingest
   does. The runtime-role proof sends the label on every conclusion
   body.

## Rule 4 evidence (D4)

Sentica rule 4 requires `tenant_id NOT NULL` and forced row-level
security on every tenant table (BUILD.md:151, BUILD.md:302). Sentica
pull request #48 decision D4 leaves the engine tables in schema
`memory` walled off by workspace. The evidence for that exemption is
`scripts/sentica_rls_probe.py`.

Run the probe with the Python selection in `docker/migrate-entrypoint.sh`,
connected as the runtime role (`SENTICA_DB_USER` whose name does not end
in `_ddl`). The role is `NOBYPASSRLS` and owns none of the tables. For
each table in schema `memory` the probe reports whether row-level
security is enabled and forced, whether policy `sentica_workspace` has
the USING and WITH CHECK clauses from
`migrations/versions/a9c1e3f5b7d2_workspace_row_level_security.py` lines
36-47, and whether a count with `sentica.workspace` unset is zero.
Tables exempted by `test_every_memory_table_has_rls_or_a_reason` (pull
request #5) are `workspaces`, `active_queue_sessions`, and
`alembic_version`, with the reasons on that guard. The probe prints
table names and booleans only. Any failure exits non-zero.

`tests/sentica/test_rls_probe.py` runs in the unit job. A passing run
is followed by a dropped `sentica_workspace` policy, by
`NO FORCE ROW LEVEL SECURITY`, and by `BYPASSRLS` on the runtime role.
Each mutation makes the probe exit non-zero, and restoring the wall
makes the next run pass. `tests/sentica/rls_job.py` is unchanged, so
the runtime-role job's node list stays the same.
