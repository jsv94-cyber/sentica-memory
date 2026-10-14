# M1 review summary

PR #2, branch `m1-single-tenant`. Round 3 closes the list below. Do not merge.

Jose's rulings, recorded in `PROGRESS.md`:

- Queue content may sit in the Postgres `queue` table. Host-local stores
  carry ids only. `GOALS.md` ground rule 5 says that.
- The M9 network sentence stays. The M1 proof stack isolates the database
  and Redis now. It does not wait for M9.

## Round 3

| Item | Fixed where | Test |
| --- | --- | --- |
| CI stack job sets `SENTICA_MODE=test` | `.github/workflows/unittest.yml:100` | the job itself |
| Proof image uid is the runner uid, so keys need no root chown | `Dockerfile:47` `SENTICA_UID` | `test_containers_cannot_read_another_tenant_key` |
| Host log grep runs under `sudo -E` and must return 1 | `tests/sentica/test_m1_stack.py:774` | `test_stopped_stack_leaves_no_memory_on_disk` |
| Full-suite job skips the proof file | `.github/workflows/unittest.yml:60` | the job itself |
| `hostssl` scram-sha-256, password only in that proxy's userlist | `docker/sentica/pg_hba.conf:5`, `docker/sentica/pgbouncer-a.ini:2` | `test_pg_hba_rejects_trust_and_cross_tenant` |
| `connect_timeout` on production connections | `src/sentica/db_connect.py:30` | `test_production_connect_args_include_connect_timeout` |
| Provision wrapped in `timeout 120` after the mode and bind checks | `docker/entrypoint.sh:44` then `:48` | `test_entrypoint_checks_mode_and_bind_before_exec`, `test_migration_exits_when_proxy_is_paused`, `test_migration_exits_when_cert_is_revoked` |
| Production access log disabled; names and `DETAIL` stripped | `src/main.py:104`, `src/sentica/log_policy.py:39` | `test_log_filter_hides_names_and_detail`, `test_database_error_canary_is_absent_from_logs` |
| API database calls bound at 8s; deriver and maintenance default to 120s | `src/db.py:88`, `src/db.py:94`, `src/db.py:246` `_BoundedSession` | `test_revocation_rolls_back_and_reports_unavailable`, `test_maintenance_timeout_defaults_to_120`, `test_slow_llm_chat_streams_fully` |
| Certificates mounted per file | `docker-compose.sentica.yml` api and deriver volumes | `test_containers_cannot_read_another_tenant_key` |
| Each PgBouncer lists only its own database | `docker/sentica/pgbouncer-a.ini:2`, `docker/sentica/pgbouncer-b.ini:2` | `test_pg_hba_rejects_trust_and_cross_tenant` |
| Cross-network probes by raw IP, and Redis `save` is empty | `tests/sentica/test_m1_stack.py:831`, `:847`, `:886` | `test_instances_cannot_reach_each_other` |
| Representation rejection is in the work-unit source | `src/deriver/queue_manager.py:665` | `test_representation_rows_for_other_tenant_are_all_errored` |
| No `SENTICA_PRODUCTION_IMAGE_MARKER` override; tests patch the constant | `src/sentica/runtime.py:20` | `test_production_image_refuses_test_mode` |
| Certificate reload drops the old connection | `src/sentica/db_connect.py:85` | `test_install_certificate_reload_drops_the_old_connection` |

## Deferred to M9

- Redis still runs as root (`user: "0:0"` in `docker-compose.sentica.yml`).
- The compose networks are not `internal: true`.

Database isolation is not deferred. `pg_hba` and the three networks are in
force in this proof.

## Test counts

`uv run pytest --ignore=tests/sentica/test_m1_stack.py` on 2026-10-14,
`SENTICA_MODE=test`, local Postgres on port 5433: **3200 passed, 53 skipped,
0 failed** (133.55s).

`uv run pytest tests/sentica/test_m1_stack.py -n0`: **18 passed, 0 skipped,
0 failed** (104.34s).

The 53 skips in the full suite are the same upstream files as M0. The fork
adds no skip.

## Not changed

- Peer-card and summary rows still sit in the Redis cache. The other
  instance cannot open that Redis, and the password lives on tmpfs.
- Refusal counts stay in process until M9.
- `docker-compose.yml` still uses the development password with
  `SENTICA_MODE=test`. The production image refuses that mode.
- `POSTGRES_HOST_AUTH_METHOD=trust` stays because the official image will
  not boot without it. The mounted `hba_file` is the rule Postgres uses.
