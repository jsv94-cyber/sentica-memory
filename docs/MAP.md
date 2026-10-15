# Runtime map

This map describes the tree pinned to upstream Honcho v3.3.0. Citations are
`path:line` and name the line where the symbol is defined or the call is made.
Public API routes are mounted under `/v3` at `src/main.py:219`.

## Database engine and sessions

`src/db.py:75` builds one async SQLAlchemy engine from
`settings.DB.CONNECTION_URI` (default in `src/config.py`, class `DBSettings`).
The driver is psycopg. Pool size, overflow, recycle, and pre-ping come from
`DBSettings`. `POOL_CLASS=null` selects `NullPool`.

Two session factories share that engine:

- `src/db.py:347` `SessionLocal` is the write session. It is lazy: the pool
  connection is checked out on the first statement, not at construction.
- `src/db.py:358` sets a read engine to `BEGIN READ ONLY`.
  `src/db.py:369` `ReadSessionLocal` is for SELECT-only work. Callers must
  not use it to mutate.

`src/db.py:595` reads `settings.DB.SCHEMA` (default `public`) and
`src/db.py:600` binds it to the declarative `Base`. Every ORM table lives in
that schema.

Request and worker code opens sessions in `src/dependencies.py`:

- `src/dependencies.py:10` `get_db` is the FastAPI write dependency.
- `src/dependencies.py:35` `get_read_db` is the read dependency.
- `src/dependencies.py:55` `tracked_db` is the context manager for background
  work. `read_only=True` binds `ReadSessionLocal`.

Migrations: `docker/migrate-entrypoint.sh:41` runs `scripts/provision_db.py:14`, which
calls `src/migrate.py:1` (`python -m src.migrate`). Alembic's script location
is `alembic.ini:6` (`migrations`). `migrations/env.py:14` imports `Base` so
autogenerate sees the models. `database/init.sql:1` creates the `vector`
extension before migrations run.

## Auth and tokens

Auth is off unless `AUTH_USE_AUTH` is set. `src/config.py:753` `AuthSettings`
defaults `USE_AUTH` to false at `src/config.py:756`. When auth is off and `SENTICA_MODE=test`,
`src/security.py:216` treats every caller as an admin JWT and does not read a
token. `SENTICA_MODE=production` is checked first and does not take that path.
Unset, empty, and any other mode refuse to start.

When auth is on, tokens are HS256 JWTs signed with `AUTH_JWT_SECRET`.
`src/security.py:31` `JWTParams` is the claim set:

| Claim | Meaning |
| --- | --- |
| `t` | Timestamp string. Required by the model, not checked as a clock. |
| `exp` | Optional expiry (standard JWT numeric date). |
| `ad` | Admin. Bypasses workspace, peer, and session checks. |
| `w` | Workspace name. |
| `p` | Peer name. |
| `s` | Session name. |

`src/security.py:74` `create_jwt` encodes the claims.
`src/security.py:67` `create_admin_jwt` sets `ad=True` and an empty timestamp.
`src/security.py:106` `verify_jwt` decodes HS256, rejects expired and malformed
tokens, and rejects a peer or session claim that has no workspace
(`scope_requires_workspace` at `src/security.py:84`).

`src/security.py:144` `require_auth` is the route dependency.
`src/security.py:198` `auth` compares the token's narrowest claim to the route.
An admin token returns immediately. A peer-scoped token cannot act on a
sibling peer. `allow_member_read=True` is an extra grant for read-only session
routes when the token's peer is a member of the session.

`src/routers/keys.py:17` `POST /v3/keys` mints scoped tokens. The route itself
requires an admin token (`src/routers/keys.py:20`). The handler starts at
`src/routers/keys.py:25`.

There is no tenant claim in the pinned token. Workspace is the top of the
hierarchy.

## Workspaces, peers, sessions

Tables are SQLAlchemy models in `src/models.py`. Names are unique inside a
workspace. `workspace_name` is part of the composite foreign keys, so a row
cannot point at another workspace.

- `src/models.py:105` `workspaces`: `name` unique, JSONB `metadata`,
  `internal_metadata`, and `configuration`.
- `src/models.py:138` `peers`: unique `(name, workspace_name)`.
- `src/models.py:175` `sessions`: unique `(name, workspace_name)`, `is_active`.
- `src/models.py:42` `session_peers`: membership. Columns include
  `configuration`, `internal_metadata`, `joined_at`, `left_at`.
- `src/models.py:214` `messages`: integer `id`, public nanoid `public_id`,
  `content`, `token_count`, `seq_in_session`. Composite foreign keys to the
  session and the peer. Full-text GIN index at `src/models.py:277`. Trigram
  GIN index at `src/models.py:286` (added in v3.3.0).

CRUD lives under `src/crud/` (`workspace.py`, `peer.py`, `session.py`,
`message.py`). Routers: `src/routers/workspaces.py`, `src/routers/peers.py`,
`src/routers/sessions.py`, `src/routers/messages.py`.

## Scopes

A scope is not its own table. `src/utils/scopes.py:1` defines it as a peer
whose name starts with `scope.` (`src/utils/scopes.py:33`) and whose
`internal_metadata.kind` is `scope`. The peer observes member sessions and
does not speak.

`src/routers/scopes.py:35` serves
`/v3/workspaces/{workspace_id}/scopes`. Adding or removing a session enqueues
`scope_backfill` or `scope_removal` (`src/utils/queue_payload.py:84` and
`src/utils/queue_payload.py:96`). The deriver copies or drops conclusions for
that observer. See `src/deriver/scope_backfill.py`.

## Conclusions and representations

A conclusion is a `documents` row. The API name is conclusion. The code name
is observation or document.

- `src/models.py:358` `collections`: one row per `(observer, observed,
  workspace_name)`. Both observer and observed are peers in that workspace.
- `src/models.py:406` `documents`: `content`, `level`, `embedding` (pgvector
  HNSW), `session_name`, `deleted_at`, and vector sync columns. `level` is
  `explicit`, `deductive`, `inductive`, or `contradiction`
  (`src/utils/types.py:294`).
- `src/models.py:616` `document_sources`: reasoning-tree edges
  `(derived_id, source_id, position)`.

`src/crud/collection.py:134` `get_or_create_collection` opens the pair's
collection. `src/crud/representation.py:84` `RepresentationManager` writes
derived observations (`save_representation` at `src/crud/representation.py:98`)
and reads them back (`get_working_representation` at
`src/crud/representation.py:268`). Semantic search is
`src/crud/document.py:387` `query_documents`. Superseded conclusions are
left out, and stale ones sort after current ones. Hybrid retrieval is
`src/sentica/retrieval.py:118` `search_conclusions`: keyword search on the
GIN index at `src/models.py:607`, plus vector neighbors within cosine
distance 0.65, merged by reciprocal rank. Stale rows sort after current
ones. `POST /peers/{peer_id}/brief` (`src/routers/peers.py:943`
`get_peer_brief`) returns the card, those conclusions, the latest summary,
and open stale flags inside a token budget.

`src/routers/conclusions.py:22` is `/v3/workspaces/{workspace_id}/conclusions`.
Creating conclusions calls `src/crud/document.py:1155` `create_observations`.
A new conclusion with a `subject_ref` can supersede the nearest older one
for that subject (`src/sentica/freshness.py:104` `supersede_similar`).
`POST /subjects/{ref}/changed` marks them stale
(`src/sentica/freshness.py:87` `mark_subject_changed`). Decay and hard
deletion live in the same module.

`src/models.py:300` `message_embeddings` stores a chunk of message text and its
vector, separate from `messages`, with `sync_state` so embedding can lag
message insert. The reconciler fills `pending` rows
(`src/reconciler/sync_vectors.py`).

## Peer cards

A peer card is a list of strings on the observer peer, not a table.
`src/crud/peer_card.py:22` `get_peer_card` reads `peers.internal_metadata`.
The key is `peer_card` when observer and observed are the same, otherwise
`{observed}_peer_card` (`src/crud/peer_card.py:123`).
`src/crud/peer_card.py:55` `set_peer_card` JSONB-merges that key. A scope may
observe, but a card is never about a scope.

## Summaries

Summaries are not a table. `src/utils/summarizer.py:95` uses the key
`summaries` inside `sessions.internal_metadata`. Each value is a short or long
summary object (content, message id, token count, timestamps).
`src/utils/summarizer.py:769` `_save_summary` merges that JSONB.
`src/utils/summarizer.py:340` `summarize_if_needed` runs from the queue when
the message sequence hits the short or long interval.

## Dreams

Dreams consolidate one `(observer, observed)` collection inside one workspace.
They do not have their own content table. New conclusions are more `documents`
rows at level `deductive` or `inductive`, linked through `document_sources`.
Peer-card updates from a dream go through `set_peer_card`.

`src/dreamer/dream_scheduler.py:33` `DreamScheduler` decides when a collection
is due. `src/dreamer/dream_scheduler.py:248` `check_and_schedule_dream`
enqueues a `dream` queue row. `src/dreamer/orchestrator.py:108` `run_dream`
runs the deduction specialist, then the induction specialist.
`src/dreamer/__init__.py:5` exports `process_dream`, which the queue consumer
calls. Surprisal sampling, when enabled, is `src/dreamer/surprisal.py`.

Workspace synthesis is separate from that collection dream.
`src/sentica/dreams.py:140` `run_dreams` writes a daily digest from that
workspace's conclusions, then a weekly insight from those digests. The
records live on the workspace. `PUT /workspaces/{id}/synthesis`
(`src/routers/workspaces.py:255` `set_workspace_synthesis`) turns new
dreams off and leaves earlier ones in place. The reconciler calls
`src/sentica/dreams.py:230` `dreams_due`.

## Evaluation

Retrieval on seeded businesses is scored without a model judge.
`src/sentica/eval_harness.py:291` `recall_at` is the share of gold conclusions
in the top k. An empty result is full marks only for an abstention question.
`src/sentica/eval_harness.py:342` `parse_baseline` reads `eval/BASELINE.md`.
`src/sentica/eval_harness.py:361` `regressions` fails a category that falls
more than 2 points. The red-team must report zero leaks across workspaces
and tenants.

## Deriver queue

The queue is the Postgres table `queue` (`src/models.py:643` `QueueItem`):
`task_type`, JSONB `payload`, `work_unit_key`, `workspace_name`, optional
`session_id` and `message_id`, `processed`, `error`.
`src/models.py:702` `active_queue_sessions` is the claim lock, one row per
work unit key.

`src/deriver/enqueue.py:27` `enqueue` inserts representation and summary rows
when messages are created. Other producers in that file enqueue dreams,
deletions, and scope tasks. Work unit keys are built by
`src/utils/work_unit.py:19` `construct_work_unit_key`.

Payload shapes are `src/utils/queue_payload.py`. Task types: `representation`,
`summary`, `webhook`, `dream`, `scope_backfill`, `scope_removal`, `deletion`,
`reconciler`.

The representation payload includes the message text
(`src/utils/queue_payload.py:28` `content: str`). Summary, dream, scope, and
deletion payloads do not. Every payload is written only to the Postgres
`queue.payload` column (`src/models.py:654`) by the inserts in
`src/deriver/enqueue.py`. Redis is the cache (`src/cache/client.py`), not a
copy of the queue. The message text in that column is in the database, which
is the store.

The worker process is `python -m src.deriver` (`src/deriver/__main__.py:22`
calls `src/deriver/queue_manager.py:1369` `main`). `src/deriver/queue_manager.py:140`
`QueueManager.polling_loop` (`src/deriver/queue_manager.py:546`) claims work
units (`src/deriver/queue_manager.py:323`) and processes them in order.
`src/deriver/consumer.py:47` `process_item` dispatches on `task_type`.
Representation batches go to `src/deriver/deriver.py:44`
`process_representation_tasks_batch`, which makes one structured model call
per batch.

The same process hosts `DreamScheduler` and `ReconcilerScheduler`. The
reconciler embeds pending vectors and deletes stale queue rows. It does not
run inside the API process unless `DERIVER.SCHEDULER` is set to `api`.

## Where every model call is made

New conclusions pass `src/sentica/safety.py:73` `banned_category` before
they are stored. The deriver, summary, and dream prompts include that rule.

Chat completions go through `src/llm/api.py:147` `honcho_llm_call`. When
`SENTICA_GATEWAY_MODE` is on, `src/llm/api.py:201` returns before any
provider is chosen. `src/sentica/gateway.py:282` `complete_llm` posts to
that function's OpenAI-compatible URL and keeps the same model on every
retry. Gateway mode off resolves a model config, retries, and either calls
`src/llm/executor.py` `honcho_llm_call_inner` or the tool loop in
`src/llm/tool_loop.py`. `src/llm/registry.py:254` `get_backend` picks one
provider client:

| Provider | Adapter | SDK call |
| --- | --- | --- |
| OpenAI | `src/llm/backends/openai.py:150` | `src/llm/backends/openai.py:188` `chat.completions.create` |
| Anthropic | `src/llm/backends/anthropic.py:27` | `src/llm/backends/anthropic.py:112` `messages.create` |
| Gemini | `src/llm/backends/gemini.py:32` | `src/llm/backends/gemini.py:61` `complete` |

Call sites of `honcho_llm_call`:

| Purpose | Call |
| --- | --- |
| Deriver batch | `src/deriver/deriver.py:162` |
| Dialectic answer | `src/dialectic/core.py:589` |
| Dialectic stream | `src/dialectic/core.py:682` |
| Short summary | `src/utils/summarizer.py:288` |
| Long summary | `src/utils/summarizer.py:324` |
| Dream specialists | `src/dreamer/specialists.py:403` |

Dialectic is reached from `POST /v3/.../peers/{peer_id}/chat`
(`src/routers/peers.py:244`), which calls `src/dialectic/chat.py:50`
`agentic_chat`. The agent builds its prompt in `src/dialectic/core.py:122`
and then loops on tools until it can answer.

Embeddings do not use `honcho_llm_call`. `src/embedding_client.py:710`
`EmbeddingClient` wraps `src/embedding_client.py:196` `_EmbeddingClient`.
With gateway mode on, `src/embedding_client.py:776` `embed` calls
`src/sentica/gateway.py:386` `embed_texts` and does not construct a
provider client. With gateway mode off, `src/embedding_client.py:308`
`embed` calls the provider.
The default transport is OpenAI `text-embedding-3-small`
(`src/config.py` `EmbeddingSettings`). The OpenAI request is
`src/embedding_client.py:351` `embeddings.create`. Gemini uses
`embed_content` in the same class when the transport is `gemini`.
Callers are message create (fast path), the reconciler
(`src/reconciler/sync_vectors.py`), and document insert in
`src/crud/document.py`.

## Where every prompt lives

| Prompt | Definition |
| --- | --- |
| Deriver observation extraction | `src/deriver/prompts.py:47` `minimal_deriver_prompt` |
| Dialectic pair recall | `src/dialectic/prompts.py:119` `agent_system_prompt` |
| Dialectic workspace recall | `src/dialectic/prompts.py:350` `workspace_agent_system_prompt` |
| Short session summary | `src/utils/summarizer.py:148` `short_summary_prompt` |
| Long session summary | `src/utils/summarizer.py:188` `long_summary_prompt` |
| Dream peer-card rules | `src/dreamer/specialists.py:79` `PEER_CARD_SYSTEM_SECTION` |
| Deduction specialist | `src/dreamer/specialists.py:622` |
| Induction specialist | `src/dreamer/specialists.py:756` |
| Peer-card maintenance dream | `src/dreamer/specialists.py:905` |

Dream user prompts are the `build_user_prompt` methods on those specialist
classes, next to the system prompts above. Custom instructions from workspace
or peer configuration are appended by the deriver, dialectic, summary, and
dream prompt builders. They are not separate files.

## Process split

`src/main.py` is the API. It serves HTTP, enqueues work, and runs the
dialectic tool loop inside the request. `src/main.py:281` `/health` returns
`{"status": "ok"}`. Metrics are `/metrics`.

`src/deriver/__main__.py` is the worker. It consumes the queue, summarizes,
dreams, and reconciles vectors. Compose runs both against one Postgres and
one Redis. Redis is the cashews cache (`src/cache/client.py`), not the queue.
