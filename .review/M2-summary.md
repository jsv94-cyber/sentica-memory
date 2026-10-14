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
  or empty setting matches nothing. `src/sentica/workspace_scope.py:188`
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
  `src/sentica/workspace_scope.py:147` always uses transaction-local
  `set_config`. A workspace change inside an open transaction is refused
  (`tests/sentica/test_m2_fix.py:557`
  `test_workspace_change_inside_an_open_transaction_is_refused`).
  Savepoint rollback clears the cache
  (`tests/sentica/test_m2_fix.py:583`
  `test_savepoint_rollback_clears_the_workspace_cache`).
  Rollback clears the setting, not only commit
  (`tests/sentica/test_m2_fix.py:610`
  `test_workspace_setting_is_cleared_on_rollback`).
  `test_workspace_setting_is_not_inherited` covers the committed case.
- Startup. `docker/entrypoint.sh:50` does not migrate. In production it
  compares the stored revision to the code's head and refuses to start when
  the schema is behind. `docker/migrate-entrypoint.sh:41` is the only
  migration command.
- Request. `src/main.py:261` binds the path workspace for the request.
  `src/security.py:266` refuses a path workspace that is not in the token
  set whenever auth is on. `src/sentica/workspace_scope.py:98`
  `enforce_targets` refuses any extra workspace in a search, context, chat,
  representation, or card body before any of those workspaces are queried.
  Each target is checked again, including member-read, inside its own
  transaction.
- One workspace per task. `src/deriver/queue_manager.py:368` claims queue
  rows once per workspace name. `src/deriver/queue_manager.py:696` sets that
  workspace for the whole representation, summary, or dream task.
  Reconciler rows are enqueued with `workspace_name` set
  (`src/reconciler/scheduler.py:334`) so they are visible under the policy.
  `src/sentica/workspace_scope.py:226` is the pending-counts function. It
  returns the workspace name and two counts, and nothing else.
- Merge. Search, session context, peer context, peer chat, workspace chat,
  representation, and peer cards loop the allowed workspaces, query each,
  and set `workspace_id` on the returned item. Messages already serialize
  `workspace_name` as `workspace_id`. Scope sessions are resolved inside
  each workspace (`tests/sentica/test_m2_partitions.py:1227`
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
- Leak. `tests/sentica/test_m2_partitions.py:742`
  `test_canary_written_only_to_r_stays_out_of_other_workspaces` writes a
  canary only in R. The harness records `execute_completion`,
  `execute_stream` (`capture:stream:`), the deriver's `honcho_llm_call`
  (`capture:deriver:`), `simple_batch_embed`
  (`capture:simple_batch_embed:`), `batch_embed` (`capture:batch_embed:`),
  and the induction specialist (`capture:induction:`). The conclusions list
  is posted and recorded (`capture:conclusions:`). The assertion is
  `tests/sentica/test_m2_partitions.py:1102`. The canary is absent from A's
  responses. `test_harness_flags_the_canary_when_the_request_includes_its_workspace`
  is the positive control. `test_membership_noop_lets_the_canary_through`
  shows a no-op membership check returns the canary.
  `tests/sentica/test_m2_partitions.py:1339`
  `test_disabled_rls_reaches_the_leak_harness` has the migration role
  disable row-level security on `documents`. The harness then selects
  document text with the token's workspace set and no workspace predicate,
  sees the canary, and restores the policy. The token does not include the
  canary's workspace. `test_runtime_role_lockdown` does not cover this.

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

`tests/conftest.py:157` `pytest_collection_finish` fails the job when the
collected count is below the number of `test_` functions in those files.
`tests/conftest.py:395` `app_engine_is_runtime_role` checks `current_user`
on the app engine for the session. `tests/sentica/test_m2_rls.py:68`
`test_rls_job_collects_every_m2_file` and `tests/sentica/test_m2_rls.py:94`
`test_app_engine_is_the_runtime_role` fail if either check regresses.

Local run on 2026-10-14: **47 passed, 0 skipped, 0 failed** (11.20s).
That includes the leak test, the positive control, the membership test,
workspace-setting inheritance, `WITH CHECK` inside
`test_runtime_role_lockdown`, multi-workspace auth, metrics scoping, dream
binding, and the eight `test_bounded_session_method_times_out` cases.

The unit suite does not collect `test_runtime_role_lockdown` or
`test_app_engine_is_the_runtime_role` (`tests/conftest.py:130`). They need
the migrated role, so they are omitted from collection rather than skipped.
On `d2f22e8e`, `tests/sentica/test_m2_rls.py:80` was
`pytest.skip("runs in the test-m2-rls job")` inside
`test_runtime_role_lockdown`. That skip is removed. The unit suite is back
to 53 skips: the upstream live-llm tests and two pagination cases.

No isolation test requires a superuser. The create_all branch of
`test_runtime_role_cannot_cross_workspaces`
(`tests/sentica/test_m2_partitions.py:51`) skips only when that database's
`current_user` is not a superuser, and only after the runtime-role branch
has returned. CI's unit database user is the postgres superuser, so that
skip does not fire. The job runs the same proof as `tenant_rls`.

## Deferred to M9

- The proxy's connection to Postgres uses `server_tls_sslmode=require`.
  `verify-full` is deferred to M9.
- Redis still runs as root. A non-root Redis user is deferred to M9.
- `internal: true` on the `db`, `net-a`, and `net-b` networks is deferred to M9.
