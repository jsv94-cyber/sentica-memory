# Upstream triage

Base is plastic-labs/honcho v3.3.0, commit `608aec8a928da13c540262ed980c569448fb53ab`. Compared with `upstream/main` at `2bdd4587921dc0cbfabfe84692be02a29f3e0179` on 2026-10-15. There is no release tag after v3.3.0. Fourteen commits. R1 took `2bdd4587` and adapted `261357ef` on the gateway path. It did not take `c384783d`. This report does not change runtime code by itself.

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

Recommendation: take later. It is an error-mapping fix for model output, and it conflicts with the dialectic core. It is not an isolation or credential fix.

### `154b6b8` fix(llm): retry tool-loop answers that miss their response_model (#1393)

Builds on `4fc0490`. A backend that returns a plain string instead of raising was treated as a successful tool-loop answer, so the 422 fired before the configured retries and the fallback model. The tool loop now checks a no-tool answer against `response_model` inside the retried call.

Files include `src/llm/tool_loop.py` and `src/dialectic/core.py`.

Touches a path we use: yes. Conflicts: yes. R3 edits `src/llm/tool_loop.py` for the tool-error cap.

Recommendation: take later, after the R3 tool-loop change is on main. Do not take it in this release.

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
