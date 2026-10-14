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
  set whenever auth is on. `src/sentica/workspace_scope.py:109`
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
  `src/sentica/workspace_scope.py:240` is `sentica_pending_counts()`. It
  takes no arguments, pins `search_path`, and returns the workspace name
  plus pending queue, pending embeddings, pending backfills, soft-deleted
  documents, and dreams due. It returns no message or document text.
- Merge. Search, session context, peer context, peer chat, workspace chat,
  representation, and peer cards loop the allowed workspaces, query each,
  and set `workspace_id` on the returned item. Messages already serialize
  `workspace_name` as `workspace_id`. Scope sessions are resolved inside
  each workspace (`tests/sentica/test_m2_partitions.py:1296`
  `test_search_scope_is_resolved_per_workspace`). Workspace A's scope
  membership would hide B's canary if that membership were reused for B.
- Cache. `src/crud/peer.py:39` includes `workspace_name` in the peer cache
  key. The card is stored on that peer row. Test:
  `test_cached_card_for_r_is_not_served_without_r`.
- Token removal. `test_removing_a_workspace_from_the_token_refuses_the_next_request`
  sends one request with R in the token and the next request without R.
  The second request is 401 `workspace is outside the token set`.
- Vectors. `src/sentica/runtime.py:272` allows only `pgvector` in
  production. Test: `test_production_refuses_external_vector_stores`,
  including `turbopuffer`.
- Leak. `tests/sentica/test_m2_partitions.py:768`
  `test_canary_written_only_to_r_stays_out_of_other_workspaces` writes a
  canary only in R. Each captured path has its own counter, incremented
  only inside that path's mock: deriver, `simple_batch_embed`,
  `batch_embed`, induction, deduction, stream, conclusions, and dialectic.
  `tests/sentica/test_m2_partitions.py:1177` asserts every counter is
  greater than zero. The scripted model issues search tool calls for the
  canary and R's names on the first round of every chat.
  `tests/sentica/test_m2_partitions.py:1173` asserts the tool-round count
  equals the number of chats, and that each tool result reached the next
  model call. Deduction and induction are mocked at
  `src.llm.executor.execute_completion`
  (`tests/sentica/test_m2_partitions.py:1018`), below their tool loops, so
  those tool calls run (`tests/sentica/test_m2_partitions.py:1145` and
  `:1152`). Deriver exceptions are not swallowed. The canary is absent
  from A's responses.
  `test_harness_flags_the_canary_when_the_request_includes_its_workspace`
  is the positive control. `test_membership_noop_lets_the_canary_through`
  shows a no-op membership check returns the canary. Two API proofs:
  `tests/sentica/test_m2_partitions.py:1792`
  `test_crud_filter_removed_rls_still_hides_the_canary` removes the CRUD
  workspace filter on messages while row-level security stays on, and the
  leak test stays clean. `tests/sentica/test_m2_partitions.py:1802`
  `test_crud_filter_removed_disabled_rls_shows_the_canary` does the same
  after the migration role disables that table's row-level security, and
  the harness sees the canary. `tests/sentica/test_m2_partitions.py:1522`
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
(`EXPECTED_M2_NODE_IDS`, 53 ids). `tests/conftest.py:203`
`pytest_sessionfinish` fails the job if any of those tests is skipped.
`tests/conftest.py:432` `app_engine_is_runtime_role` checks `current_user`
on the app engine for the session. `tests/sentica/test_m2_rls.py:75`
`test_rls_job_collects_every_m2_file` and `tests/sentica/test_m2_rls.py:113`
`test_app_engine_is_the_runtime_role` fail if either check regresses.

Local run on 2026-10-14: **53 passed, 0 skipped, 0 failed** (15.89s).
That includes the leak test, the two-layer RLS proofs, the peer-card
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
  (`src/routers/peers.py:693`). `tests/sentica/test_m2_partitions.py:1353`
  `test_cold_cache_returns_each_peer_card` returns both cards from a cold
  cache.
- Due dreams are listed and enqueued per workspace
  (`src/backlog.py:155`, `src/backlog.py:210`).
  `tests/sentica/test_m2_rls.py:484`
  `test_due_dream_is_reported_and_triggered` runs as the runtime role.
- The reconciler gate enqueues one row per workspace that needs the task.
  `tests/sentica/test_m2_rls.py:329`
  `test_reconciler_gate_enqueues_one_row_per_workspace` covers
  `sync_vectors`, a backfill, and cleanup.
- A representation read error on the read-only engine rolls the transaction
  back (`src/crud/representation.py:44`).
  `tests/crud/test_representation_manager.py:1214`
  `test_read_only_transaction_error_does_not_poison_session`.
- Migration `c4e8b1a2d6f0` drops `FORCE` before it deletes queue rows whose
  workspace is null (`migrations/versions/c4e8b1a2d6f0_runtime_role_and_pending_counts.py:73`).
  `tests/sentica/test_m2_fix.py:695`
  `test_null_queue_rows_are_deleted_when_force_rls_is_on` upgrades a
  pre-M2 database as the non-superuser migration role.
- `tests/sentica/test_m1_stack.py:1604`
  `test_posted_message_embedding_leaves_pending` posts a message, lets the
  scheduler gate enqueue `sync_vectors:<workspace>`, and waits until
  `sync_state` leaves pending. It does not call the cycle directly.
- `tests/sentica/test_m1_stack.py:1724`
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

## Deferred to M9

- The proxy's connection to Postgres uses `server_tls_sslmode=require`.
  `verify-full` is deferred to M9.
- Redis still runs as root. A non-root Redis user is deferred to M9.
- `internal: true` on the `db`, `net-a`, and `net-b` networks is deferred to M9.
