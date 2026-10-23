# Upstream triage

Base is plastic-labs/honcho v3.3.0, commit `608aec8a928da13c540262ed980c569448fb53ab`. Compared with `upstream/main` at `2bdd4587921dc0cbfabfe84692be02a29f3e0179` on 2026-10-15. There was no release tag after v3.3.0 on that day. The 2026-10-23 appendix records v3.4.0. Fourteen commits. R1 took `2bdd4587` and adapted `261357ef` on the gateway path. It did not take `c384783d`. This report does not change runtime code by itself.

A path counts as one we use when it is on the API, the deriver, the migrations, or the CI job that runs those. A conflict means the commit edits a file this fork has also edited since v3.3.0.

## Security first

### `c384783d` feat: add named-scope API keys for recall and ingestion (#1341)

A named-scope key could reopen an existing session with `POST /sessions` and overwrite metadata, merge configuration, and add peers, which grants member read. The routes that key is not allowed to edit were bypassed. The follow-up rejects those fields with 401.

Files: `src/security.py`, `src/routers/keys.py`, `src/routers/sessions.py`, `src/routers/peers.py`, `src/routers/messages.py`, `src/routers/scopes.py`, `src/crud/session.py`, `src/crud/message.py`, `src/utils/agent_tools.py`, `src/utils/scopes.py`, `docs/v3/openapi.json`, `scripts/generate_jwt.py`, `CLAUDE.md`, and the named-scope tests.

Touches a path we use: yes (`src/security.py` and the session and peer routers). Conflicts: yes, those three plus `docs/v3/openapi.json`.

Recommendation: do not take. The commit adds named-scope API keys. The hole it closes exists only inside that new key type. Sentica production uses broker tokens, with the tenant and the workspace set on the token, and does not use Honcho API keys. Taking the commit would add a second credential system and conflict with `src/security.py`, the session and peer routers, and `docs/v3/openapi.json`.

### `2bdd4587` fix: return 422, not 500, for request bodies with lone surrogates (#1387)

A JSON body with a lone surrogate makes Pydantic return a validation error whose echo is not valid UTF-8. Starlette then raises, and the client gets a 500. The new handler keeps the validation body and renders it with `ensure_ascii`.

Files: `src/main.py`, `tests/routes/test_validation_api.py`.

Touches a path we use: yes (`src/main.py`). Conflicts: yes.

Recommendation: taken. R1 cherry-picked it as a validation 422, without the upstream trailer. A crafted body should not turn into a 500.

## Other commits

### `261357ef` fix(deriver): fail loudly on malformed or degraded structured output (#1373)

Truncated, filtered, or refused empty representations and summaries were consumed as if they had succeeded. They now retry, then fail the queue item. Sentry records one issue per failure class.

Files include `src/deriver/deriver.py`, `src/utils/summarizer.py`, `src/llm/api.py`, the three provider backends, `src/llm/structured_output.py`, `src/exceptions.py`, and `src/telemetry/sentry.py`.

Touches a path we use: yes (the deriver and the summarizer). Conflicts: no overlapping files.

Recommendation: adapted, not cherry-picked. R1 fails the gateway path on malformed or refused structured output and retries, then raises `GatewayStructuredOutputError`. The message is a failure class plus a hash and a length. There is no Sentry policy and no provider-backend change. The Sentry fingerprints and the provider `validate_response` path stay upstream.

### `b05dca46` fix(deriver): normalize PromptRepresentation.explicit string shapes (#538)

Some backends return `explicit` as a string or a list of strings. The batch used to fail validation and drop the observations. Strings are now wrapped as `{"content": ...}`.

Files: `src/utils/representation.py`, `tests/deriver/test_representation_crud.py`, `tests/llm/test_backends/test_openai.py`.

Touches a path we use: yes (representation parsing). Conflicts: no.

Recommendation: take later. The cherry-pick does not touch `src/db.py`, auth, the deriver queue, migrations, `src/sentica/`, or the leak harness.

### `fc2f6d26` fix(config): warn about unused provider credentials (#1344)

Startup now warns, by setting name only, when a provider key is set for a transport the configured models do not use. Embeddings and fallbacks are included.

Files: `src/config.py`, `tests/test_config.py`, `tests/conftest.py`, `docs/v3/contributing/configuration.mdx`.

Touches a path we use: yes (`src/config.py` and the test fixture). Conflicts: yes, `tests/conftest.py`.

Recommendation: take later. It is credential hygiene, not a vulnerability, and the fixture conflict has to be resolved by hand.

### `8c292a04` feat(telemetry): CloudEvents for workspace, peer, session and scope creation (#1360)

Emits creation events from the get-or-create functions after commit, and runs a nested workspace post-commit hook that was previously discarded.

Files: `src/crud/workspace.py`, `src/crud/peer.py`, `src/crud/session.py`, `src/crud/scope.py`, `src/telemetry/events/resource.py`, `src/telemetry/events/__init__.py`, `tests/routes/test_resource_created_events.py`.

Touches a path we use: yes (peer, session, and workspace creation). Conflicts: no.

Recommendation: take later. It changes creation side effects. It is not a security fix.

### `0ffc7f1f` fix(sdk): build the async HTTP client per event loop (#1357)

The Python SDK reused one `httpx.AsyncClient` across event loops, so a later loop failed with "Event loop is closed".

Files: `sdks/python/src/honcho/client.py`, `tests/sdk/test_client.py`.

Touches a path we use: no. The memory service does not run the SDK.

Recommendation: take later, and only if the SDK is shipped. No conflict with the service.

### `ed170426` test(hermes-plugin): add unit tests and a CI job for hermes-plugin-honcho (#1343)

Adds tests and a workflow for the Hermes plugin. No production change.

Touches a path we use: the plugin directory is in the tree, and it is not on the API or deriver path. Conflicts: no.

Recommendation: take later. It does not change memory behavior.

### `a6063aaf` ci: run the gate workflows on feat/shared-tenants and allow manual dispatch

Turns on the same workflows for `feat/shared-tenants` and adds manual dispatch. The merge commit `a2efd5ff` only records that pull request and changes no files.

Files: `.github/workflows/unittest.yml`, `.github/workflows/staticanalysis.yml`.

Touches a path we use: yes (both workflows). Conflicts: yes. Our runtime-role job lives in `unittest.yml`.

Recommendation: skip. This fork has no `feat/shared-tenants` branch, and copying the workflow would fight the runtime-role job.

### `3922af85` feat(kilo-honcho): Honcho memory plugin for Kilo Code (#1319)

Adds a Kilo Code plugin, its docs, and its tests. Forty-two files, all under the new plugin and the integration docs.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not run Kilo.

### `046eba82` docs: add Honcho Cloud callout to self-hosting guide (#1363)

A documentation callout for Honcho Cloud. One file, `docs/v3/contributing/self-hosting.mdx`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not use Honcho Cloud.

### `8e1d477a` docs(contributing): add migration rules for contributors (#1377)

Documents upstream's migration rules in `CONTRIBUTING.md`.

Touches a path we use: the file exists, and it is not runtime. Conflicts: no.

Recommendation: skip. This fork's rule is stricter: a migration on main is never edited, and migrations contain literal SQL. Taking their page would describe a different rule.

### `29e20d5e` feat(mcp): forward a caller-set X-Honcho-Plugin and name the client in the docs (#1384)

The MCP server forwards `X-Honcho-Plugin` and the docs name the client. Files are under `mcp/` and the integration guides.

Touches a path we use: no. Sentica calls the HTTP API, not the MCP server. Conflicts: no.

Recommendation: skip.

## Appendix, 2026-10-17

Compared `upstream/main` at `ed9fe6c122446a30a74bbba1364003349bf0acb5` with the previous triage tip `2bdd4587921dc0cbfabfe84692be02a29f3e0179`. There is still no release tag after v3.3.0. Twelve commits. R3 takes one security adaptation and leaves the rest.

### `edfc6ba` fix(mcp): return 404 for expired or unknown session IDs (#1375)

An unknown or expired MCP session id now returns 404 instead of being treated as a new session.

Files: `mcp/src/http.ts`, `mcp/src/http.test.ts`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not serve the MCP transport.

### `5df80a0` fix(dialectic): keep oversized tool output from overflowing the prompt (#1390)

Three changes. Message search no longer binds every session name as a parameter once a peer is in more than 65,535 sessions; it uses a correlated EXISTS. Unexpected tool failures no longer return the full exception text to the model, because SQLAlchemy errors embed the statement and its parameters. `truncate_messages_to_fit` now caps a tool result that is larger than the remaining budget instead of dropping the user query and sending the oversized result.

Files: `src/crud/message.py`, `src/llm/conversation.py`, `src/utils/agent_tools.py`, `tests/crud/test_session_scope_clauses.py`.

Touches a path we use: yes (`src/crud/message.py`, `src/utils/agent_tools.py`). Conflicts: yes, both of those. This fork's tool loop also returns unexpected tool exceptions to the model from `src/llm/tool_loop.py`, which the upstream commit does not edit.

Recommendation: adapt the tool-error cap only, in its own commit. Exception text handed back to the model is capped at `LLM.MAX_TOOL_OUTPUT_CHARS` in `src/utils/agent_tools.py` and on the tool-loop path in `src/llm/tool_loop.py`. The session-name rewrite and the truncation rewrite stay upstream. They are availability fixes, and the session-name rewrite conflicts with `src/crud/message.py`.

### `e7953b2` fix(hermes-plugin): wait for recall on the first turn of a resumed process (#1342)

A Hermes process that resumes a session past turn 1 now waits for context and dialectic before the first turn.

Files: under `hermes-plugin-honcho/`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not run the Hermes plugin.

### `35a0e79` docs(config): note that Langfuse requires TELEMETRY_ENABLED (#1388)

Documents that Langfuse keys alone produce no traces.

Files: `docs/v3/contributing/configuration.mdx`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip.

### `9579ca2` fix(cli): emit nested SDK models as JSON objects, not repr strings (#1324)

`honcho session` JSON output printed pydantic models with `default=str`, so summaries and messages came out as Python reprs.

Files: `honcho-cli/`.

Touches a path we use: no. The memory service does not run the CLI. Conflicts: no.

Recommendation: take later, and only if the CLI is shipped.

### `4fc0490` fix(dialectic): return 422 when a chat answer fails its response_format (#1379)

A final dialectic answer that fails the client's `response_format` raised `ValidationError`, which the global handler turned into a 500. The SDKs retry 500s. Upstream maps that failure to 422.

Files: `src/dialectic/core.py`, `src/exceptions.py`, `src/llm/backends/openai.py`, `docs/v3/contributing/troubleshooting.mdx`, and the structured-output tests.

Touches a path we use: yes (`src/dialectic/core.py`, `src/exceptions.py`). Conflicts: yes, those two.

Recommendation: superseded by the 2026-10-23 appendix.

### `154b6b8` fix(llm): retry tool-loop answers that miss their response_model (#1393)

Builds on `4fc0490`. A backend that returns a plain string instead of raising was treated as a successful tool-loop answer, so the 422 fired before the configured retries and the fallback model. The tool loop now checks a no-tool answer against `response_model` inside the retried call.

Files include `src/llm/tool_loop.py` and `src/dialectic/core.py`.

Touches a path we use: yes. Conflicts: yes. R3 edits `src/llm/tool_loop.py` for the tool-error cap.

Recommendation: superseded by the 2026-10-23 appendix. R3 merged in #15.

### `9d69692` docs: add a real non-Docker install path and fix stale LLM key defaults (#1392)

Rewrites the README install section around native Postgres and one LLM key.

Files: `README.md`, `docs/v3/contributing/self-hosting.mdx`, `docs/v3/contributing/troubleshooting.mdx`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica's install path is `docs/DEPLOY.md`.

### `08dce3d` feat(release): send a migration manifest with each release (#1395)

The release workflows post a migration manifest computed by `scripts/release_manifest.py`.

Files: `.github/workflows/push-gcp-registry-prod.yml`, `.github/workflows/push-gcp-registry-staging.yml`, `CONTRIBUTING.md`, `migrations/compat.toml`, `scripts/release_manifest.py`, `tests/scripts/test_release_manifest.py`.

Touches a path we use: the two publish workflows exist here and are `if: false`. Conflicts: yes, those workflows.

Recommendation: skip. This fork does not publish release images from those workflows.

### `df1f508` ci: run the previous release and the candidate on the candidate's schema (#1397)

A compatibility-matrix workflow boots the previous release image and the candidate against one database.

Files: `.github/workflows/compat-matrix.yml` and the workflow's scripts and fixtures.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. It assumes upstream image tags and a shared-tenant lineage this fork does not publish.

### `0f4dc36` feat(api): expose a stable uid on workspaces, peers and sessions (#1394)

Workspace, peer, and session responses gain `uid`, the server primary key, beside the client-chosen `id`. The Python and TypeScript SDKs surface it.

Files include `src/schemas/api.py`, `docs/v3/openapi.json`, and both SDKs.

Touches a path we use: yes (`src/schemas/api.py`, `docs/v3/openapi.json`). Conflicts: yes, both.

Recommendation: take later. It is additive API surface, not a security fix, and the schema files conflict.

### `ed9fe6c` docs(scopes): drop internal service name from the managed-key note (#1396)

Removes an internal service name from the scopes doc.

Files: `docs/v3/documentation/features/advanced/scopes.mdx`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not use named-scope API keys.

## Appendix, 2026-10-23

Compared `upstream/main` at `f9e39711729052245a623b809fa1797703d6cd99` with v3.3.0 `608aec8a928da13c540262ed980c569448fb53ab`, measured against this fork at `8857fb6bfd4e1d1b3d180240d8a914279573e615`. Forty-one commits, plus the merge `a2efd5ff`, which changes no files and is already noted under `a6063aaf`. Upstream tagged v3.4.0 at `819e66c86e4b8c11d5adad3ec751f47c69c64079` (2026-10-20). Five commits are on main after that tag. The v3.4.0 changelog was read with the diffs. A conflict still means the commit edits a file this fork has also edited since v3.3.0. Where this appendix names a commit that already has a section above, this appendix is the recommendation.

`47d3eac2` is taken by R6-3. `63b61cbc` and `c384783d` stay skipped. This note does not cherry-pick anything.

### Security and isolation

#### `47d3eac2` fix(filter): treat wildcards and `{}` as TRUE inside AND/OR/NOT (#1433)

A filter dict that constrains nothing (`{}`, or only wildcards) built to `None`, and AND, OR, and NOT dropped `None` operands. An AND kept its other operands. An OR of a wildcard and a real predicate kept only the real predicate. A NOT of a wildcard or of `{}` added no predicate, so the query returned every row in scope. A `source_ids` list containing `*` returned `None` for the whole list and dropped the linkage check. The fix renders an unconstrained dict as `true()`, so NOT of it matches nothing, and a `*` inside a `source_ids` list is dropped from that list while the other ids stay.

Files: `src/utils/filter.py`, `tests/test_advanced_filters.py`, `tests/utils/test_filter.py`, `docs/v3/documentation/features/advanced/using-filters.mdx`.

Touches a path we use: yes. Message, conclusion, and session search build SQL from this helper. Conflicts: no. `src/utils/filter.py` is unchanged since v3.3.0. The commit does not touch `src/db.py`, auth, the deriver queue, migrations, `src/sentica/`, or the leak harness.

Recommendation: take now. R6-3 cherry-picks it.

#### `d93af3bc` fix(mcp): answer a rejected OAuth token with HTTP 401 so clients refresh (#1389)

An expired OAuth access token still passed MCP initialize, and the API's 401 came back as a tool error inside HTTP 200. MCP clients refresh on HTTP 401. The worker now answers 401 with `WWW-Authenticate: Bearer` for that refusal. A 401 on an API key stays a tool error.

Files: under `mcp/` (`src/http.ts`, the tool modules, and the HTTP tests).

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not run the MCP worker. A bad broker token is already HTTP 401 from `src/sentica/tokens.py`.

#### `63b61cbc` feat(migrations): queue pending-work indexes and deletion generations (#1409)

Adds partial indexes so unprocessed queue rows can be found by peer or session, a read-only helper in `src/crud/pending_work.py`, and `deletion_generation INTEGER NOT NULL DEFAULT 0` on workspaces, sessions, and collections. Nothing reads or bumps the counter. The revision is `a5f8da8f6d23` with down_revision `3e7a1c9d5b20`. `migrations/env.py` grows a `btree_gin` hook.

Files: `migrations/env.py`, the new revision, `src/models.py`, `src/crud/pending_work.py`, `tests/conftest.py`, `database/init.sql`, `pyproject.toml`, `uv.lock`, and the alembic tests.

Touches a path we use: yes (migrations, models, and `migrations/env.py`). Conflicts: yes, `migrations/env.py`, `src/models.py`, and `tests/conftest.py`.

Recommendation: skip. The Alembic history forked at `3e7a1c9d5b20`. This fork's child of that revision is `a9c1e3f5b7d2` (row-level security), and the head is `d4a8c1e27b90`. `migrations/env.py` passes the proxy TLS arguments into Alembic's engine. Cherry-picking the revision would open a second head and edit that engine. The new counter is unread.

#### `c384783d` feat: add named-scope API keys for recall and ingestion (#1341)

Stays skipped. The 2026-10-15 section has the file list and the hole. Broker tokens use audience `sentica-memory` (`TOKEN_AUDIENCE` in `src/sentica/runtime.py`, checked in `src/sentica/tokens.py`). Named-scope keys are a second credential system with their own JWT audience. The hole exists only inside that key type.

### Revisits

R3 merged in #15 (`672ed996bb4ab74cc217c4af28f9d5e38d22fa32`). That was the hold on the next two commits.

#### `4fc04907` fix(dialectic): return 422 when a chat answer fails its response_format (#1379)

The 2026-10-17 section describes the bug: a final answer that misses `response_format` became a 500, and upstream maps it to 422.

Files: `src/dialectic/core.py`, `src/exceptions.py`, `src/llm/backends/openai.py`, `docs/v3/contributing/troubleshooting.mdx`, and the structured-output tests. Conflicts: yes, `src/dialectic/core.py` and `src/exceptions.py`. The OpenAI backend file does not conflict.

Recommendation: skip. Production chat goes through `honcho_llm_call`, and `gateway_enabled()` returns from `complete_llm` in `src/sentica/gateway.py` before the OpenAI backend parses the answer. A schema miss retries inside the gateway and then raises `GatewayStructuredOutputError` (502). This commit does not edit the gateway. Taking it would install a 422 on the non-gateway path beside that 502, in `src/exceptions.py`, where the gateway error is defined.

#### `154b6b84` fix(llm): retry tool-loop answers that miss their response_model (#1393)

The 2026-10-17 section describes the bug: a plain-string answer counted as a successful tool-loop result, so the failure fired before retries and the fallback model. The check moves inside the retried call.

Files: `src/llm/tool_loop.py`, `src/dialectic/core.py`, `tests/llm/test_tool_loop_structured_answer.py`. Conflicts: yes, both source files. The R3 lines in `src/llm/tool_loop.py` are the tool-error cap and the safety-outage re-raise.

Recommendation: skip, together with `4fc04907`. The production loop is `complete_llm`, which already retries a structured-output miss and then raises 502. This commit does not edit that function. A cherry-pick has to be rewritten around the R3 lines, and the rewritten check does not run while the gateway is on.

### Other v3.4.0 commits

#### `43210f38` fix(llm): honor thinking_tool_choice_conflict on the OpenAI transport (#1402)

`provider_params.thinking_tool_choice_conflict` was read only by the Anthropic backend. Anthropic models behind an OpenAI-compatible proxy ignored it. The OpenAI backend now applies `override_thinking`, `override_tool`, or `throw` when thinking and a forced tool or a structured-output request meet. An unknown mode fails config load. Unset leaves the request unchanged.

Files: `src/config.py`, `src/llm/backends/openai.py`, `src/llm/backends/anthropic.py`, `src/llm/request_builder.py`, the backend tests, `tests/test_config.py`, and `docs/v3/contributing/configuration.mdx`.

Touches a path we use: the config loader does. The production call does not. `gateway_enabled()` returns before provider resolution, and `src/sentica/gateway.py` builds the body itself. Conflicts: no.

Recommendation: take later. Trigger: a deployed model config sets `thinking_tool_choice_conflict` and the request is built by `src/llm/backends/openai.py`. Until that happens the gateway never reads the key. No fork file in this commit has been edited since v3.3.0, so the cherry-pick is clean when the trigger fires.

#### `bd1bd197` fix(mcp): error on chat include_evidence when the SDK ignores it (#1406)

The MCP chat tool now errors when `include_evidence` is set and the SDK ignores it, instead of dropping the flag.

Files: `mcp/src/tools/peers.ts` and `mcp/src/tools/chat-evidence.test.ts`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica calls the HTTP API.

#### `58b679b9` feat(dreamer): working memory — budget, write window, brief, and notes (#1378)

Opt-in dreamer and dialectic working memory: a remaining tool budget, a memory brief, a reserved write window, and capped notes carried from one dream to the next. Off by default. New module `src/dreamer/working_memory.py`.

Files: the dreamer, `src/dialectic/core.py`, `src/llm/tool_loop.py`, `src/crud/document.py`, `src/config.py`, telemetry, and the new tests. About 4,300 lines added.

Touches a path we use: yes (dreamer, dialectic, documents). Conflicts: yes, including `src/dialectic/core.py`, `src/dreamer/orchestrator.py`, `src/dreamer/specialists.py`, `src/dreamer/dream_scheduler.py`, `src/llm/tool_loop.py`, `src/crud/document.py`, `src/utils/agent_tools.py`, and `tests/conftest.py`.

Recommendation: skip. Dreams in this fork are the workspace digests in `src/sentica/dreams.py`. The upstream notes, brief, and write window land in files already edited to keep a dream inside one workspace. The feature is off by default and is not a fix.

#### `a56b2636` fix(mcp): reinstall and typecheck before deploying the Worker (#1407)

The MCP Worker deploy reinstalls dependencies and typechecks first.

Files: `mcp/package.json` and MCP tests.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not deploy that Worker.

#### `ce56c3e8` docs: sharpen Overview and Quickstart titles, descriptions; add pricing line (#1404)

Title and description edits, plus a pricing line, on the Honcho docs intro pages.

Files: `docs/v3/documentation/introduction/overview.mdx`, `docs/v3/documentation/introduction/quickstart.mdx`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica's operator docs are `docs/DEPLOY.md` and `docs/INSTANCE.md`.

#### `d1f2004a` feat(cursor-honcho): Honcho plugin for Cursor and Grok Bot (#1386)

Adds the Cursor and Grok Bot plugin, its hooks, a built `integrations/cursor/dist/hook.js`, and the integration guides. README and CONTRIBUTING gain a catalog line.

Files: under `integrations/cursor/`, plus `README.md`, `CONTRIBUTING.md`, and the docs catalog.

Touches a path we use: no. Conflicts: no. README and CONTRIBUTING match v3.3.0, so the overlap rule does not fire. The built hook is a generated artifact.

Recommendation: skip. Sentica does not install that plugin.

#### `fc84e425` docs: move Paperclip to the built-in connector, mark SillyTavern unmaintained (#1411)

Integration-guide edits for Paperclip and SillyTavern.

Files: `docs/v3/guides/integrations/paperclip.mdx`, `docs/v3/guides/integrations/sillytavern.mdx`, `docs/v3/guides/integrations/mcp.mdx`, `docs/snippets/integrations-catalog.jsx`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not run those integrations.

#### `bf2e2a87` chore: add changelog and version updates (#1412)

Bumps the package version from 3.3.0 to 3.4.0 in `pyproject.toml`, the OpenAPI document, the SDKs, the CLI, the MCP package, and `migrations/compat.toml`, and extends the changelog.

Files: those version files and `CHANGELOG.md`. Conflicts: yes, `docs/v3/openapi.json`.

Touches a path we use: the OpenAPI document is generated and checked here. The version number is upstream's.

Recommendation: skip. This fork's release line is its own. Taking the bump would rename the pin and conflict with `docs/v3/openapi.json`.

#### `819e66c8` fix(ci): recompute release manifest in the GCP POST job (#1414)

The tag commit. The GCP production and staging POST jobs recompute the migration manifest before they publish it.

Files: `.github/workflows/push-gcp-registry-prod.yml`, `.github/workflows/push-gcp-registry-staging.yml`.

Touches a path we use: the files exist. Conflicts: yes, both. Both jobs are `if: false` on this fork.

Recommendation: skip. This fork does not publish from those workflows. Same disposition as `08dce3dc`.

### Commits on main after v3.4.0

#### `e8a2783d` docs: point the Pi guide at the official pi-honcho plugin (#1415)

Replaces the community Pi page with a guide to the official plugin.

Files: `docs/v3/guides/integrations/pi.mdx`, the removed community page, and the docs catalog.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not run Pi.

#### `607438d2` docs: set up Grok Bot from the plugin's GitHub link until it is in the Cursor Marketplace (#1416)

Install instructions for the Grok Bot plugin while it is absent from the marketplace.

Files: `docs/v3/guides/integrations/grok-bot.mdx`.

Touches a path we use: no. Conflicts: no.

Recommendation: skip. Sentica does not install that plugin.

#### `b94a416c` test: cut suite time and run the TypeScript SDK tests outside pytest (#1422)

Drops a cached OpenAPI schema between test clients, shortens LLM retry waits in tests, and moves the TypeScript SDK suite from pytest to `scripts/run_ts_sdk_tests.py` with its own CI job. The workflow gains path filters again.

Files: `.github/workflows/unittest.yml`, `tests/conftest.py`, `CLAUDE.md`, `scripts/run_ts_sdk_tests.py`, and the removed `tests/sdk_typescript/` package.

Touches a path we use: yes (`unittest.yml`). Conflicts: yes, `unittest.yml`, `tests/conftest.py`, and `CLAUDE.md`. This fork's workflow has no path filter, sets `SENTICA_MODE=test`, and runs the two-instance proof in its own job.

Recommendation: skip. Copying the workflow would fight that job. The memory service does not run the TypeScript SDK.

#### `f9e39711` fix(llm): stop retrying provider-rejected requests when no fallback is configured (#1435)

`honcho_llm_call` retried every exception, so a provider 400 or 401 was sent again with backoff. A shared tenacity condition stops after the first attempt on a provider 4xx other than 408, 409, 425, and 429 when no fallback is configured. Calls with a fallback, and response-validation retries, keep their current sequence.

Files: `src/llm/api.py`, `src/llm/errors.py`, `src/llm/tool_loop.py`, and the retry tests.

Touches a path we use: yes (`src/llm/api.py`, `src/llm/tool_loop.py`). Conflicts: yes, both. Production returns from `complete_llm` before those retry decorators. The gateway retries 401, 403, 404, 408, and 429, and a 400 fails on the first response.

Recommendation: skip. The commit does not edit `src/sentica/gateway.py`, which is where production retries live. Applying the upstream condition would fight the gateway early-return and the R3 tool loop, and it would not change the gateway's status set.

### Every commit from v3.3.0 to `upstream/main`

The pin commit `608aec8a` is the base and is not a row. `a2efd5ff` is the only merge in the range. "Taken" and "adapted" are closed: do not cherry-pick them again. A take-later row names the trigger. The reason for a commit that already has a section above is that section, unless this appendix replaced it.

| Commit | Recommendation | Trigger or standing reason |
| --- | --- | --- |
| `a6063aaf` | skip | No `feat/shared-tenants` branch. The workflow fights the runtime-role job. |
| `3922af85` | skip | Sentica does not run Kilo. |
| `a2efd5ff` | skip | Merge commit. No files. |
| `046eba82` | skip | Sentica does not use Honcho Cloud. |
| `fc2f6d26` | take later | Trigger: the next edit to `tests/conftest.py`. Credential hygiene. The fixture conflict is resolved by hand. |
| `8c292a04` | take later | Trigger: `TELEMETRY_ENABLED` is turned on for a deployment. Creation side effects. |
| `0ffc7f1f` | take later | Trigger: the Python SDK is shipped. |
| `c384783d` | skip | Broker tokens, audience `sentica-memory`. Stays skipped. |
| `b05dca46` | take later | Trigger: the next change to representation parsing. No file conflict. |
| `261357ef` | adapted | R1, on the gateway path. Do not cherry-pick. |
| `ed170426` | take later | Trigger: this repo's CI starts running the Hermes plugin. |
| `8e1d477a` | skip | Upstream's migration page describes a different rule. |
| `29e20d5e` | skip | Sentica does not run the MCP server. |
| `2bdd4587` | taken | R1. Do not cherry-pick again. |
| `edfc6bad` | skip | Sentica does not serve the MCP transport. |
| `5df80a09` | adapted in part | The tool-error cap landed in R3 (#15). Remainder take later. Trigger: `src/crud/message.py` or the prompt-truncation helper is already being edited. |
| `e7953b22` | skip | Sentica does not run the Hermes plugin. |
| `35a0e798` | skip | Langfuse docs. Telemetry here is off unless configured. |
| `9579ca2e` | take later | Trigger: the CLI is shipped. |
| `4fc04907` | skip | This appendix. Gateway schema misses are 502. |
| `9d696928` | skip | Sentica's install path is `docs/DEPLOY.md`. |
| `154b6b84` | skip | This appendix. The production loop is `complete_llm`. |
| `08dce3dc` | skip | Release-image workflows are `if: false`. |
| `df1f5088` | skip | Assumes upstream image tags this fork does not publish. |
| `0f4dc36c` | take later | Trigger: a client stores `uid` beside the client-chosen id. Schema files conflict. |
| `ed9fe6c1` | skip | Named-scope docs. Sentica does not use those keys. |
| `43210f38` | take later | Trigger: a deployed config sets `thinking_tool_choice_conflict` and `src/llm/backends/openai.py` builds the request. |
| `bd1bd197` | skip | MCP evidence flag. Sentica calls the HTTP API. |
| `58b679b9` | skip | Workspace digests stay in `src/sentica/dreams.py`. |
| `a56b2636` | skip | MCP Worker deploy. |
| `d93af3bc` | skip | MCP OAuth 401. Broker tokens are already 401. |
| `ce56c3e8` | skip | Honcho docs titles and a pricing line. |
| `63b61cbc` | skip | Alembic fork. Stays skipped. |
| `d1f2004a` | skip | Cursor and Grok Bot plugin. |
| `fc84e425` | skip | Paperclip and SillyTavern docs. |
| `bf2e2a87` | skip | Upstream version bump to 3.4.0. |
| `819e66c8` | skip | GCP manifest recompute. Workflows are `if: false`. |
| `e8a2783d` | skip | Pi plugin docs. |
| `607438d2` | skip | Grok Bot install docs. |
| `b94a416c` | skip | Fights `unittest.yml`. |
| `47d3eac2` | take now | R6-3. Filter wildcards and `{}` fail open inside NOT. |
| `f9e39711` | skip | Production retries live in `src/sentica/gateway.py`. |
