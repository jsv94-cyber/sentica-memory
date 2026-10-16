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
