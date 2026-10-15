# M2 review

## Carry-over from M1

Each item landed before the workspace partition code. The test fails if the
behavior is removed.

1. CI pins. `.github/workflows/unittest.yml:17` and the other workflow jobs
   use `runs-on: ubuntu-24.04`. `actions/checkout` is `v7`,
   `actions/setup-python` is `v7`, and `astral-sh/setup-uv` is `v10.3.0`.
   Test: `test_workflows_pin_ubuntu_2404_and_node24_actions`.
2. Production logs. `src/sentica/log_policy.py:17` is the allow list.
   `src/sentica/log_policy.py:30` still drops `uvicorn.access`. Any other
   record is written as logger name, function, line, and level.
   `src/sentica/log_policy.py:41` keeps an allow-listed format string.
   Tests: `test_production_log_strips_messages_except_the_allow_list` and
   `test_named_request_and_dream_log_omit_names`.
3. Call bounds. `src/db.py:270` `_bounded` covers rollback, close, delete,
   refresh, get, merge, connection, and run_sync, plus execute, scalar,
   scalars, commit, and flush. The embedding validator uses the maintenance
   bound and closes the connection on timeout. Test:
   `tests/sentica/test_m2_carryover.py:64`
   `test_bounded_session_method_times_out`, one case for each of the eight
   methods.
4. Cancellation. `src/db.py:286` `_stop_and_wake` stops the session
   connection, wakes the inner wait, and the caller re-raises. Test:
   `test_cancelled_caller_stops_the_session_connection`.
5. Secrets. Both PgBouncer services mount `/tmp` as tmpfs. The database
   container mounts its secrets and certs directories as tmpfs. The fixture
   writes SCRAM verifiers, not plaintext, into the database secrets. Tests:
   `test_database_receives_scram_verifiers_and_tmpfs` and
   `test_api_and_deriver_have_no_tenant_password`.
6. Migration timeout. `docker/migrate-entrypoint.sh:20` reads
   `SENTICA_MIGRATION_TIMEOUT_SECONDS` (default 360) and
   `docker/migrate-entrypoint.sh:34` refuses to start, before any migration,
   when that value is not greater than `DB_MIGRATION_LOCK_WAIT_SECONDS`.
   The API entrypoint does not migrate. `docs/CONFIG.md` states the default
   and the ordering rule. Tests:
   `test_entrypoint_checks_mode_and_bind_before_exec` and
   `test_migration_exits_when_proxy_is_paused`.
7. psycopg wait contract. Test: `test_bounded_wait_matches_psycopg`.
8. Slow chat stream. `test_slow_llm_chat_streams_fully` runs with production
   mode and a broker token. The 15 second stream still completes.
9. Call timeout cap and deleted userlists. `src/sentica/runtime.py:149`
   refuses a value that is not a finite positive number or is above 600.
   `docker/sentica/userlist-a.txt` and `userlist-b.txt` are deleted.
   `docs/INSTANCE.md` says the certificate directory must be bind-mounted as
   a directory, not as individual files. Test:
   `test_db_call_timeout_refuses_unparsable_and_over_cap`.
10. M9 deferrals recorded below. Test:
    `test_m2_summary_defers_proxy_tls_redis_and_internal_networks`.

## Where isolation is enforced

- Database. Migration `a9c1e3f5b7d2` enables and forces row-level security on
  every table in the configured schema that has `workspace_name`. The policy
  `sentica_workspace` is `workspace_name = current_setting('sentica.workspace',
  true)` and the same predicate with `coalesce(..., '') <> ''`, so an unset
  or empty setting matches nothing. `src/sentica/workspace_scope.py:202`
  is the SQL the migration and the unit proof share.
- Role. Each tenant has a runtime role `tenant_<x>` and a migration role
  `tenant_<x>_ddl`. The migration role owns the `memory` schema and the
  tables. The runtime role has `USAGE` and DML, not `CREATE`, is not a
  member of the migration role, and does not bypass row-level security.
  There is no `sentica_app` role. The migrate service is the only process
  that mounts the migration certificate. Test:
  `test_runtime_role_cannot_cross_workspaces`. The stack test
  `test_tenant_a_cannot_escape_its_workspace` checks the same rules through
  the proxy, including `42501` on `ALTER`, `DROP POLICY`, and `CREATE TABLE`.
- Transaction. `src/db.py:358` opens reads with `BEGIN READ ONLY`.
  `src/sentica/workspace_scope.py:159` always uses transaction-local
  `set_config`. A workspace change inside an open transaction is refused
  (`tests/sentica/test_m2_fix.py:619`
  `test_workspace_change_inside_an_open_transaction_is_refused`).
  After `ROLLBACK TO SAVEPOINT` the same workspace is applied again, and a
  different workspace is refused
  (`tests/sentica/test_m2_fix.py:645`
  `test_savepoint_rollback_refuses_a_different_workspace`).
  Rollback clears the setting, not only commit
  (`tests/sentica/test_m2_fix.py:676`
  `test_workspace_setting_is_cleared_on_rollback`).
  `tests/sentica/test_m2_fix.py:227`
  `test_workspace_setting_is_not_inherited` reads the raw driver connection,
  so a session-level setting would fail it.
- Startup. `docker/entrypoint.sh:50` does not migrate. In production it
  compares the stored revision to the code's head and refuses to start when
  the schema is behind. `docker/migrate-entrypoint.sh:41` is the only
  migration command.
- Request. `src/main.py:261` binds the path workspace for the request.
  `src/security.py:266` refuses a path workspace that is not in the token
  set whenever auth is on. `src/sentica/workspace_scope.py:117`
  `enforce_targets` refuses any extra workspace in a search, context, chat,
  representation, or card body before any of those workspaces are queried.
  Each target is checked again, including member-read, inside its own
  transaction.
- One workspace per task. `src/deriver/queue_manager.py:368` claims queue
  rows once per workspace name. `src/deriver/queue_manager.py:696` sets that
  workspace for the whole representation, summary, or dream task.
  `src/reconciler/scheduler.py:89` gates each reconciler task on its own
  count. `src/reconciler/scheduler.py:295` enqueues one row per workspace
  that needs that task and sets the workspace before the insert.
  `src/sentica/workspace_scope.py:293` is `sentica_pending_queue_counts()`.
  The claim loop reads only the workspace name and the pending queue count.
  `src/sentica/workspace_scope.py:319` is `sentica_pending_counts()`. The
  reconciler gates read that function. Both take no arguments, pin
  `search_path`, and return no message or document text. The full function
  also returns pending embeddings, pending backfills, soft-deleted
  documents, and dreams due.
- Merge. Search, session context, peer context, peer chat, workspace chat,
  representation, and peer cards loop the allowed workspaces, query each,
  and set `workspace_id` on the returned item. Messages already serialize
  `workspace_name` as `workspace_id`. Scope sessions are resolved inside
  each workspace (`tests/sentica/test_m2_partitions.py:1325`
  `test_search_scope_is_resolved_per_workspace`). Workspace A's scope
  membership would hide B's canary if that membership were reused for B.
- Cache. `src/crud/peer.py:39` includes `workspace_name` in the peer cache
  key. The card is stored on that peer row. Test:
  `test_cached_card_for_r_is_not_served_without_r`.
- Token removal. `test_removing_a_workspace_from_the_token_refuses_the_next_request`
  sends one request with R in the token and the next request without R.
  The second request is 401 `workspace is outside the token set`.
- Vectors. `src/sentica/runtime.py:290` allows only `pgvector` in
  production. Test: `test_production_refuses_external_vector_stores`,
  including `turbopuffer`.
- Leak. `tests/sentica/test_m2_partitions.py:769`
  `test_canary_written_only_to_r_stays_out_of_other_workspaces` writes a
  canary only in R. `_complete` and `_stream` record `repr(messages)` and
  any prompt before they dispatch (`tests/sentica/test_m2_partitions.py:955`).
  Each captured path has its own counter, incremented only inside that
  path's mock: deriver, `simple_batch_embed`, induction, deduction, stream,
  conclusions, and dialectic. The summarizer is counted by a spy on
  `honcho_llm_call` when the track name is `Short Summary`.
  `batch_embed` must stay at zero.
  `tests/sentica/test_m2_partitions.py:1208` asserts the other counters
  are greater than zero, and `:1209` requires at least two deduction calls
  and two induction calls. The deriver capture contains the posted text
  `local hello` (`:1212`). The scripted model issues search tool calls
  for the canary and R's names on the first round of every chat.
  `tests/sentica/test_m2_partitions.py:1201` asserts the tool-round count
  equals the number of chats, and that each tool result reached the next
  model call. Deduction and induction are mocked at
  `src.llm.executor.execute_completion`
  (`tests/sentica/test_m2_partitions.py:1028`), below their tool loops, so
  those tool calls run (`tests/sentica/test_m2_partitions.py:1173` and
  `:1180`). Deriver exceptions are not swallowed. The canary is absent
  from A's responses.
  `test_harness_flags_the_canary_when_the_request_includes_its_workspace`
  is the positive control. `test_membership_noop_lets_the_canary_through`
  shows a no-op membership check returns the canary. Two API proofs:
  `tests/sentica/test_m2_partitions.py:1822`
  `test_crud_filter_removed_rls_still_hides_the_canary` removes the CRUD
  workspace filter on messages while row-level security stays on, and the
  leak test stays clean. `tests/sentica/test_m2_partitions.py:1832`
  `test_crud_filter_removed_disabled_rls_shows_the_canary` does the same
  after the migration role disables that table's row-level security, and
  the harness sees the canary. `tests/sentica/test_m2_partitions.py:1551`
  `test_body_workspaces_outside_the_token_are_refused` returns 401 for
  search, chat, representation, card, and context when the body names a
  workspace the token does not allow, including a peer-scoped member of
  A refused for B on the session-context fan-out.

## Tables without a workspace column

- `workspaces`. The workspace id is the `name` column. The row holds
  configuration, not peer messages or conclusions. Production list requires
  an admin token, and production rejects admin tokens, so a tenant token
  cannot list every workspace. A path read is checked against the token set
  first. The deriver reads the names so it can set `sentica.workspace`
  before it reads that workspace's queue.
- `active_queue_sessions`. Columns are the work-unit key and a timestamp.
  There is no message or conclusion text.
- `alembic_version`. The only value is the migration revision id.

`test_runtime_role_cannot_cross_workspaces` fails if a table with
`workspace_name` is missing from the policy.

## Runtime-role job

`test-m2-rls` runs every test in `tests/sentica/test_m2_*.py` against the
schema built by alembic. The app engine connects as `tenant_rls`. That
role is not a superuser and has no `BYPASSRLS`. `SENTICA_MODE=production`.
Tokens are signed with the job's Ed25519 key.

`tests/conftest.py:161` `pytest_collection_finish` fails the job unless the
collected node ids equal the pin in `tests/sentica/rls_job.py:26`
(`EXPECTED_M2_NODE_IDS`, 59 ids). `tests/conftest.py:203`
`pytest_sessionfinish` fails the job if any of those tests is skipped.
`tests/conftest.py:432` `app_engine_is_runtime_role` checks `current_user`
on the app engine for the session. `tests/sentica/test_m2_rls.py:75`
`test_rls_job_collects_every_m2_file` and `tests/sentica/test_m2_rls.py:113`
`test_app_engine_is_the_runtime_role` fail if either check regresses.

The pin lists 59 tests. That includes the leak test, the two-layer RLS
proofs, the peer-card
fan-out, the reconciler gate, the due-dream trigger, the positive control,
the membership test, workspace-setting inheritance, `WITH CHECK` inside
`test_runtime_role_lockdown`, multi-workspace auth, metrics scoping, dream
binding, and the eight `test_bounded_session_method_times_out` cases.

The unit suite does not collect `test_runtime_role_lockdown` or
`test_app_engine_is_the_runtime_role` (`tests/conftest.py:131`). They need
the migrated role, so they are omitted from collection rather than skipped.
The unit suite stays at 53 skips: the upstream live-llm tests and two
pagination cases.

`tests/sentica/test_m2_partitions.py:72` calls `pytest.fail` when the
create_all database user is not a superuser. That is not a skip. The job
runs the same proof as `tenant_rls`. `tests/sentica/test_m2_rls.py:235`
restores row-level security on `peers` in `finally` after
`test_runtime_role_lockdown` disables it.

## Round 2

- Peer cards open one read transaction per target workspace
  (`src/routers/peers.py:693`). `tests/sentica/test_m2_partitions.py:1382`
  `test_cold_cache_returns_each_peer_card` returns both cards from a cold
  cache.
- Due dreams are listed and enqueued per workspace
  (`src/backlog.py:155`, `src/backlog.py:210`).
  `tests/sentica/test_m2_rls.py:545`
  `test_due_dream_is_reported_and_triggered` runs as the runtime role.
- The reconciler gate enqueues one row per workspace that needs the task.
  `tests/sentica/test_m2_rls.py:389`
  `test_reconciler_gate_enqueues_one_row_per_workspace` covers
  `sync_vectors`, a backfill, and cleanup.
- A representation read error on the read-only engine rolls the transaction
  back (`src/crud/representation.py:44`).
  `tests/crud/test_representation_manager.py:1214`
  `test_read_only_transaction_error_does_not_poison_session`.
- Migration `c4e8b1a2d6f0` drops `FORCE` before it deletes queue rows whose
  workspace is null (`migrations/versions/c4e8b1a2d6f0_runtime_role_and_pending_counts.py:74`).
  `tests/sentica/test_m2_fix.py:718`
  `test_null_queue_rows_are_deleted_when_force_rls_is_on` upgrades a
  pre-M2 database as the non-superuser migration role.
- `tests/sentica/test_m1_stack.py:1604`
  `test_posted_message_embedding_leaves_pending` posts a message, lets the
  scheduler gate enqueue `sync_vectors:<workspace>`, and waits until
  `sync_state` is `synced` and that queue row completed without error.
  The test calls the gate directly rather than waiting for the timer.
  It does not call the cycle itself.
- `tests/sentica/test_m1_stack.py:1740`
  `test_api_refuses_a_stale_schema_and_a_runtime_cert_as_ddl` refuses to
  start api-a on a stale schema, and the runtime certificate presented as
  `tenant_a_ddl` through the proxy is refused.
- Fan-out list responses use explicit list models
  (`src/schemas/api.py:305` `RepresentationList`,
  `src/schemas/api.py:316` `PeerCardList`,
  `src/schemas/api.py:514` `SessionContextList`,
  `src/schemas/api.py:534` `PeerContextList`).
- `tests/conftest.py:1084` `mock_tracked_db` honours `read_only`.
  `tests/sentica/test_m1_stack.py:148` `_bind_workspace` uses
  transaction-local `set_config`. `src/dependencies.py:81` describes the
  read-only session as `BEGIN READ ONLY`.

## Round 3

- Model calls record `repr(messages)` and any prompt before dispatch
  (`tests/sentica/test_m2_partitions.py:955`). Single-text embeds are
  recorded in `_zero_embed` (`tests/sentica/test_m2_partitions.py:1003`).
  `simple_batch_embed` is spied (`tests/sentica/test_m2_partitions.py:1010`)
  and does not call `batch_embed`. A scrub of a tool argument named `text`
  still leaves a content block visible
  (`tests/sentica/test_m2_partitions.py:1221`).
- Production startup refuses the local dummy key on every provider
  (`src/sentica/runtime.py:301`). The stack embedding service is
  `docker/sentica/fake_embeddings.py`.
  `tests/sentica/test_m1_startup.py:346` and
  `tests/sentica/test_m1_startup.py:359` cover the refusal and the
  deterministic non-zero vectors.
- `src/sentica/workspace_scope.py:203` `remove_workspace_scope` drops the
  per-test listeners. `tests/sentica/test_m2_rls.py:713`
  `test_later_test_has_no_workspace_guard` changes the workspace inside
  one transaction after those listeners are gone.
- One workspace is processed and the other stays pending:
  `tests/sentica/test_m2_rls.py:744`
  `test_sync_vectors_leaves_the_other_workspace`,
  `tests/sentica/test_m2_rls.py:820`
  `test_cleanup_leaves_the_other_workspace`, and
  `tests/sentica/test_m2_rls.py:888`
  `test_backfill_leaves_the_other_workspace`.
- `tests/sentica/test_m2_fix.py:850`
  `test_claim_loop_uses_the_queue_counts_function` reads only the queue
  counts function. `tests/sentica/test_m2_fix.py:875`
  `test_migration_refuses_a_role_that_does_not_end_in_ddl` fails the
  migration when the role name does not end in `_ddl`.
  `tests/sentica/test_m2_rls.py:200` checks `has_function_privilege` for
  both functions. `src/reconciler/backfill.py:167` sets each backfill gauge
  from that backfill's own count. `src/routers/deriver_metrics.py:49`
  counts dreams by calling `list_due_dreams` for each workspace in the
  token. `src/sentica/workspace_scope.py:384` `apply_per_workspace` commits each
  workspace.

## Follow-up

- `src/sentica/workspace_scope.py:267` `_dreams_due_sql` counts collections
  whose explicit document count exceeds `last_dream_document_count`. It
  bakes in no settings and does not parse `work_unit_key`.
  `list_due_dreams` applies the runtime thresholds.
  `tests/sentica/test_m2_rls.py:545`
  `test_due_dream_is_reported_and_triggered` lowers
  `DOCUMENT_THRESHOLD` and enables dreams after the function exists, and
  the poller still enqueues the due dream.
  `tests/sentica/test_m2_fix.py:396`
  `test_deriver_metrics_are_limited_to_the_token_workspaces` asserts a
  scoped dreams count of 1.
- The leak test keeps the real `create_short_summary`
  (`tests/sentica/test_m2_partitions.py:14`) and uses it as the mock side
  effect (`:1095`). A spy on `src.utils.summarizer.honcho_llm_call`
  (`:1099`) counts track name `Short Summary` and calls the real function.
  `_complete` tags that capture (`:969`). The tagged line contains
  `local hello` (`:1216`).
- `tests/sentica/test_m2_partitions.py:1204` asserts `batch_embed` is 0.
  `src/models.py:553` declares `ix_documents_soft_deleted_cleanup`.
  `tests/reconciler/test_backfill.py:403`
  `test_record_backfill_pending_gives_each_gauge_its_own_count` gives
  each gauge its own count. `tests/sentica/test_m2_rls.py:174` checks
  the queue-counts function catalog. `_bind_unit_db` requires `request`
  (`tests/sentica/test_m2_rls.py:301`).

## Review round 1

- `src/llm/executor.py` and `src/llm/request_builder.py` match main. The
  leak test spies on `src.utils.summarizer.honcho_llm_call`
  (`tests/sentica/test_m2_partitions.py:1099`
  `test_canary_written_only_to_r_stays_out_of_other_workspaces`).
- `migrations/versions/e6b2d8f04a17_dreams_due_superset.py:161` replaces
  `sentica_pending_counts()` with the dreams superset and restates the
  grants (`:152`). `a9c1e3f5b7d2` and `c4e8b1a2d6f0` now hold literal SQL.
  `tests/sentica/test_migration_freeze.py:76`
  `test_frozen_sql_matches_main_cdb74439`,
  `tests/sentica/test_migration_freeze.py:52`
  `test_migrations_do_not_import_sql_from_src`, and
  `tests/sentica/test_migration_freeze.py:135`
  `test_upgrade_from_merged_head_ignores_dream_settings`.
- `src/reconciler/backfill.py:164` sets the `document_sources` gauge from
  `pending_backfills`. `tests/reconciler/test_backfill.py:448`
  `test_document_sources_gauge_opens_no_per_workspace_transaction`.
- `tests/dreamer/test_dream_due.py:135`
  `test_soft_deleted_explicit_documents_count`. `src/embedding_client.py`
  matches pre-M2 main.

## Deferred to M9

- The proxy's connection to Postgres uses `server_tls_sslmode=require`.
  `verify-full` is deferred to M9.
- Redis still runs as root. A non-root Redis user is deferred to M9.
- `internal: true` on the `db`, `net-a`, and `net-b` networks is deferred to M9.
