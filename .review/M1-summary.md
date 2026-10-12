# M1 review summary

PR #2, branch `m1-single-tenant`. The reviews below are of `987c6f2a`.
Commit `46746649` closed the fail-open mode gap. This round closes the rest.

Jose's rulings, recorded in `PROGRESS.md`:

- Queue content may sit in the Postgres `queue` table. Host-local stores
  carry ids only. `GOALS.md` ground rule 5 says that.
- The M9 network sentence stays. The M1 proof stack isolates the database
  and Redis now. It does not wait for M9.

## Already fixed in 46746649

| Finding | Fixed where | Test |
| --- | --- | --- |
| B1, unset, empty, misspelled, and wrong-case `SENTICA_MODE` | `src/sentica/runtime.py:95` `mode()` | `test_rejected_modes_refuse` |
| Image default `SENTICA_MODE=production` | `Dockerfile:43` | `test_dockerfile_defaults_to_production` |
| CI and Compose set `test` explicitly | `.github/workflows/unittest.yml`, `docker-compose.yml` | `test_rejected_modes_refuse` |
| Queue text stays in Postgres (Jose, this round) | `GOALS.md` ground rule 5 | no code change |
| M9 network sentence | `GOALS.md` M9 | left in place |

## This round

| Finding | Fixed where | Test |
| --- | --- | --- |
| B1, image `CMD` can skip the entrypoint | `Dockerfile:86` `ENTRYPOINT`, no `CMD` | `test_dockerfile_defaults_to_production` |
| B1, production image accepts `SENTICA_MODE=test` | `Dockerfile:76` marker, `src/sentica/runtime.py:20`, `docker/entrypoint.sh` | `test_production_image_refuses_test_mode` |
| B2, stack tests hidden or skipped | `tests/sentica/conftest.py` removed; `unittest.yml` job `test-m1-stack` fails on zero tests | the job itself |
| B2, image publish before M9 | `docker-build.yml` and the other publish workflows `if: false` | workflow files |
| B3, shared network, trust-for-all, open Redis | `docker-compose.sentica.yml` networks `db`, `net-a`, `net-b`; `docker/sentica/pg_hba.conf`; `docker/sentica/redis-entry.sh` | `test_instances_cannot_reach_each_other` |
| Review 1 M3, peer cards in Redis | Redis requires a per-instance password on tmpfs and is not reachable from the other instance. The cache stays on. | `test_instances_cannot_reach_each_other` |
| Major, tenant role owns the database | `docker/sentica/init-tenants.sql` | `test_tokens_and_isolation` (SQLSTATE `42501`) |
| Major, tenant is PgBouncer admin | `docker/sentica/pgbouncer-a.ini` `sentica_operator` | `test_tenant_is_not_pgbouncer_admin` |
| Major, reconciler and webhook inserts unstamped | `src/sentica/queue.py` `install_queue_stamp`, called from `src/models.py` | `test_reconciler_and_webhook_complete`, `test_orm_and_core_inserts_stamp_tenant` |
| Major, representation check after message load, only first row errored | `src/deriver/queue_manager.py` `reject_foreign_queue_rows` | `test_representation_rows_for_other_tenant_are_all_errored` |
| Major, certificate swap does not prove the new cert | `src/sentica/db_connect.py` fingerprint of cert and key | `test_certificate_swap_has_no_failed_requests` |
| Major, pool recycle untested | `src/db.py` `capped_pool_recycle`, compose `DB_POOL_RECYCLE=2` | `test_pool_recycle_is_short`, `test_short_pool_recycle_opens_a_new_connection` |
| Major, cross-tenant auth accepts any error | `docker/sentica/pg_hba.conf` | `test_tokens_and_isolation` |
| Major, token walk uses the wrong key and skips hidden routes | `tests/sentica/test_m1_stack.py` | `test_tokens_and_isolation` |
| Major, revocation is a raw transaction | API batch write during `docker pause` | `test_revocation_rolls_back_and_reports_unavailable` |
| Major, deriver resume untested | queue row processed once after the proxy returns | `test_deriver_resumes_a_pending_row_once` |
| Major, disk check ignores logs | compose logging driver `none` | `test_stopped_stack_leaves_no_memory_on_disk`, `test_database_error_canary_is_absent_from_logs` |
| Major, hang when the proxy is paused | `src/main.py` `_BoundRequest` (8s), `src/sentica/db_connect.py` socket shutdown, TCP keepalives, role `statement_timeout` | `test_revocation_rolls_back_and_reports_unavailable` |
| Major, SQL parameters and exception text in logs | `src/db.py` `hide_parameters=True`, `src/sentica/log_policy.py` | `test_log_filter_hides_parameters_and_work_unit_keys`, `test_database_error_canary_is_absent_from_logs` |
| Major, traces, SQL debug, local metrics, workspace labels | `src/sentica/runtime.py:232` `_refuse_unsafe_runtime` | `test_unsafe_production_settings_refuse` |
| Major, deriver metrics on `0.0.0.0` | `src/deriver/__main__.py` binds `SENTICA_BIND_HOST` | startup refusal covers metrics; bind is the deriver entry |
| Minor, algorithm from the header, no `aud` | `src/sentica/tokens.py` | `test_none_hs256_and_algorithm_mismatch_are_forged` |
| Minor, key parsed per request | `src/sentica/runtime.py:137` caches the instance and the key | `test_malformed_token_key_refuses`, `test_production_without_password_files_loads` |
| Minor, startup tests skip some settings | `REQUIRED_SETTINGS` | `test_each_required_setting_refuses` |
| Minor, bind host `private` | `src/sentica/runtime.py:209` | `test_bind_host_must_be_an_address` |
| Minor, password check is env only | `src/sentica/runtime.py:266`, `.dockerignore` excludes `config.toml` | `test_config_toml_password_refuses`, `test_pgpass_file_refuses`, `test_home_pgpass_refuses` |
| Minor, cert and key rotated apart | `certificate_fingerprint` hashes both; rename-into-place in `docs/INSTANCE.md` | `test_rename_into_place_changes_the_fingerprint` |

## Test counts

`uv run pytest` on 2026-10-12, `SENTICA_MODE=test`, local Postgres on
port 5433: **3207 passed, 53 skipped, 1 failed** (188.28s). The failure
was a stale `docs/MAP.md` line for `create_async_engine`. That citation
now points at `src/db.py:74`, and `test_map_citations_resolve` passed on
its own. The 53 skips are the same upstream files as M0. The stack file
is collected, not skipped. It contributed 14 passes inside that run.

`uv run pytest tests/sentica/test_m1_stack.py -n0`: **14 passed** (102.66s).

## Not changed

- Peer-card and summary rows still sit in the Redis cache. The other
  instance cannot open that Redis, and the password lives on tmpfs.
- Refusal counts stay in process until M9.
- `docker-compose.yml` still uses the development password with
  `SENTICA_MODE=test`. The production image refuses that mode.
