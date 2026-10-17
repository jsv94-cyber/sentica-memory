# R3 review

Each item has a test that fails if the behavior is removed. Isolation tests
run in `test-m2-rls` as the runtime role. The collected pin is 94 node ids
in `tests/sentica/rls_job.py:26`. Draft pull request 15 is not merged.

The gVisor smoke and the default image build were verified on Actions at
`f9df321f6a0066437c7265711fe69eaae56402e1`.
Unittest run:
https://github.com/jsv94-cyber/sentica-memory/actions/runs/39007240804
Static analysis run:
https://github.com/jsv94-cyber/sentica-memory/actions/runs/39007240678
11 checks, 0 failures. `test-gvisor-smoke` passed 4 tests (26 deselected).

## Fix round 1

Each row is one commit. The test fails if that commit is reverted.

| Finding | Commit | Test |
| --- | --- | --- |
| Filtered HNSW returned a short page | `a2c12405` | `test_filtered_hnsw_returns_a_full_page_at_10k_rows` |
| Stale conclusions were dropped instead of ranked last | `973a4866` | `test_stale_ranks_after_a_fresh_conclusion` |
| Read-only root check wrote a path the app user cannot own | `ebc9bc3a` | `test_root_filesystem_is_read_only` |
| Eval scores did not depend on vector search | `583922f6` | `test_blank_vector_channel_misses_the_baseline` |
| Budget check did not hold tokens for an in-flight call | `160e387b` | `test_concurrent_holds_stay_inside_a_100_token_budget` |
| NET_ADMIN check did not require an unprivileged container | `48417d9e` | `test_no_container_has_net_admin` |
| gVisor install followed `release/latest` | `a1f84b68`, `9b0c89e2` | `test_gvisor_services_use_runsc` |
| Search red-team and RLS policy check were too weak | `b19094db` | `test_http_red_team_hides_the_other_workspace`, `test_workspace_filter_and_rls_policy_are_present` |
| Deploy commands omitted the network, cert mounts, and egress rules | `d4ea63d8` | `test_deploy_flags_and_internal_networks` |
| Fork changelog omitted four R3 files | `5d67d186` | `test_fork_changes_lists_the_r3_files` |
| Status check no longer listed every required job | `d2064607` | `test_workflow_job_builds_schema_as_the_ddl_role` |
| Concurrent-index guard accepted one plain index beside a concurrent one | `5ed9c9e3` | `test_index_guard_rejects_a_plain_index_beside_a_concurrent_one` |
| Publisher bind address was not checked | `9e6196e0` | `test_publishers_are_single_port_and_non_root` |
| Unknown tokenizer startup error omitted the rejected name | `b7111428` | `test_unknown_tokenizer_names_the_rejected_encoding` |
| Replacing the Redis URL left the old pool open | `febbf626` | `test_a_new_redis_url_closes_the_old_pool` |
| Eval search helper took an unused slot | `8737d190` | `test_search_helper_takes_no_slot` |
| Progress note said no Hindsight branch existed | `1775d146` | `test_progress_records_the_local_hindsight_branch` |
| Deleted HNSW entries hid the next test's rows | `ff5fc489` | `test_neighbor_scan_after_a_large_delete_returns_a_full_page` |
| Eval bytecode was copied into the image | `7496055c` | `test_dockerignore_excludes_bytecode` |

Local Python suite, ignoring `tests/sentica/test_m1_stack.py`: 3440 passed, 53 skipped, 2 failed, 259.95s. Both failures are `tests/sdk_typescript/test_sdk.py` because `bun` is not installed on this machine.

Local stack suite: `test_stopped_stack_leaves_no_memory_on_disk` runs `sudo grep` over `/var/lib/docker/containers`, and sudo waited for a password, so that test was not finished. The other 30 tests were run with that one deselected: 29 passed, then `test_gvisor_services_use_runsc` failed until the download URL contained `release/20261012/`. After `9b0c89e2` that test passed (1 passed, 30 deselected, 60.27s). `runsc` is not installed here, so `HostConfig.Runtime` was not read. The static overlay, checksum, and URL checks did run.

## Packaging

1. Default image. `Dockerfile:52` reuses an existing group before
   `addgroup`, so gid 100 (`users` on `python:3.13-slim-bookworm`) does not
   fail the build. Defaults are `Dockerfile:47` `SENTICA_UID=100`. CI job
   `.github/workflows/unittest.yml:298` `build-default-image` runs
   `docker build .` with no build args. Tests:
   `test_deploy_flags_and_internal_networks` and
   `test_workflow_job_builds_schema_as_the_ddl_role`.
2. gVisor smoke. `.github/workflows/unittest.yml:309` installs `runsc` from
   `gvisor.tar.bz2` and checks `gvisor.tar.bz2.sha512`
   (`.github/workflows/unittest.yml:354`).
   `.github/workflows/unittest.yml:362` runs the four-test subset with
   `SENTICA_GVISOR=1`. `docker-compose.gvisor.yml:19` sets `runtime: runsc`.
   `docker-compose.gvisor.yml:14` pins `embeddings`, and
   `docker-compose.gvisor.yml:20` writes `extra_hosts`, because runsc cannot
   reach Docker DNS at 127.0.0.11. Result recorded in `PROGRESS.md`. Tests:
   `test_workflow_job_builds_schema_as_the_ddl_role` and
   `test_deploy_flags_and_internal_networks`.
3. Stack proofs. `tests/sentica/test_m1_stack.py:1896`
   `test_api_and_deriver_cannot_reach_the_internet` (egress denied).
   `tests/sentica/test_m1_stack.py:1917`
   `test_root_filesystem_is_read_only`.
   `tests/sentica/test_m1_stack.py:1928`
   `test_redis_runs_as_a_non_root_user` (`docker/sentica/redis-entry.sh:18`
   `setpriv --reuid=redis` so pid 1 is the redis user).
   `tests/sentica/test_m1_stack.py:1942`
   `test_no_container_has_net_admin`. The same four tests passed under
   `runsc` in `test-gvisor-smoke`.
4. Deploy notes. `docs/DEPLOY.md:23` publishes the API on `10.8.0.4:8000`
   for the broker. `docs/DEPLOY.md:64` is the one-shot migrate command,
   including `--entrypoint /app/docker/migrate-entrypoint.sh`.
   `docs/DEPLOY.md:80` sets ownership from `SENTICA_UID` and `SENTICA_GID`.
   `docs/DEPLOY.md:166` allows egress only to the certificate proxy and the
   model gateway, and names the provider hosts to deny.
   `docs/DEPLOY.md:129` lists the per-purpose timeouts. Test:
   `test_deploy_flags_and_internal_networks`.
5. Proof-only forwarders. `docker-compose.sentica.yml` services
   `publish-a-api`, `publish-a-proxy`, `publish-b-api`, and `publish-b-proxy`
   each run as uid 65534, read-only, and forward one port.
   `docker/sentica/publish.py:61` exits unless the argument list is exactly
   one `listen=host:port`. `docs/DEPLOY.md:125` calls them proof-only.
   Tests: `test_publish_forwards_exactly_one_port` and
   `test_publishers_are_single_port_and_non_root`.
6. Eval text stays out of the image. `.dockerignore:11` excludes
   `src/sentica/eval_harness.py`. `Dockerfile:85` removes that file and
   fails the build if `northline violetkey` remains under `/app/src`.
   Test: `test_deploy_flags_and_internal_networks`.
7. Readiness cache. `src/sentica/ready.py:22` `database_ready` returns the
   cached success until `src/sentica/ready.py:7` `_READY_TTL_SECONDS` (5
   seconds). A failure is not cached. The route stays tokenless and the
   body is only the status. Tests:
   `test_ready_reuses_a_cached_database_check` and
   `test_ready_answers_when_the_database_does`.

## Eval honesty

8. Embeddings. `src/sentica/eval_harness.py:63` `text_embedding` is hashed
   signed character n-grams (widths 3, 4, and 5), L2-normalized.
   `eval/BASELINE.md:24` states what each score measures. Measured recall
   is 100 for every category. A vector stub that returns nothing drops
   information extraction and knowledge updates to 0.
   `.github/workflows/live-eval.yml:5` is `workflow_dispatch` only and
   reads `SENTICA_LIVE_EVAL_GATEWAY_URL`. It has no `pull_request` trigger.
   Tests: `test_embeddings_are_not_one_hot_copies`,
   `test_vector_only_recall_is_not_perfect`,
   `test_retrieval_eval_matches_baseline`, and
   `test_live_eval_workflow_is_dispatch_only`.
9. Isolation red-team. `tests/sentica/test_m2_isolation.py:58`
   `test_http_red_team_hides_the_other_workspace` uses signed broker tokens
   over HTTP as the runtime role.
   `tests/sentica/test_m2_isolation.py:29`
   `test_workspace_filter_and_rls_policy_are_present` fails if the filter
   or the `sentica_workspace` policy is gone.
   `tests/sentica/test_m2_isolation.py:75`
   `test_removed_filter_and_disabled_rls_shows_the_canary` fails if removing
   both still hides the canary. Node ids are pinned at
   `tests/sentica/rls_job.py:118`. `test-m2-rls` passed 94 tests.

## Retrieval

10. HNSW order. `src/sentica/vector_query.py:34` `neighbor_statement` orders
    by cosine distance, applies the current-row predicates, and limits to
    `limit * CANDIDATE_FACTOR` (`src/sentica/vector_query.py:17`, factor 4).
    `src/sentica/vector_query.py:59` `within_distance` applies the cutoff
    after that candidate set. The partial index is
    `src/models.py:575` `ix_documents_embedding_hnsw_current` and migration
    `migrations/versions/d4a8c1e27b90_documents_hnsw_current.py:34`.
    Test: `test_vector_query_uses_hnsw_at_10k_rows` (EXPLAIN at 10k rows
    must be an index scan).
11. Concurrent indexes. `migrations/versions/d4a8c1e27b90_documents_hnsw_current.py:132`
    builds with `CREATE INDEX CONCURRENTLY` when the caller does not own
    the transaction. Applied migrations are listed as frozen and are not
    edited. Test: `test_new_index_migrations_use_create_index_concurrently`.

## Gateway

12. Forced tools. `src/sentica/gateway.py:1090` accepts `force_tools_until`.
    `src/sentica/gateway.py:1257` starts from the caller's `tool_choice` and
    `src/sentica/gateway.py:1321` relaxes `required` or `any` to `auto` once
    the gate is met or the forced-round cap is reached, matching the
    upstream tool loop. Test: `test_force_tools_until_relaxes_tool_choice`.
13. Atomic budget. Reserve holds the estimate while the call is in flight.
    Settle replaces that hold with the actual usage, and a failed call
    releases the hold. Test: `test_concurrent_holds_stay_inside_a_100_token_budget`.
14. Jitter once. `src/deriver/queue_manager.py:538`
    `_advance_poll_interval` applies `_jitter` to the interval it returns.
    `src/deriver/queue_manager.py:557` sleeps that value once on a gateway
    outage. Test: `test_gateway_outage_jitter_is_applied_once`.
15. Gateway environment. `src/sentica/gateway.py:477`
    `refuse_invalid_gateway_env` rejects an invalid
    `SENTICA_GATEWAY_RETRY_ATTEMPTS` and every other gateway environment
    value. `src/sentica/runtime.py:235` calls it while loading the instance,
    before the test-mode return. Test:
    `test_invalid_gateway_env_refuses_startup`.
16. Tokenizer. `src/sentica/tokenizer.py:9` defaults to `cl100k_base`.
    `src/sentica/tokenizer.py:38` reads `SENTICA_GATEWAY_TOKENIZER`.
    `src/sentica/tokenizer.py:12` reads
    `SENTICA_GATEWAY_TOKENIZER_ALIASES`. `docs/CONFIG.md:47` documents both.
    Test: `test_gateway_alias_uses_the_configured_encoding`.
17. Simulated stream and primary workspace. `docs/CONFIG.md:163` states that
    streaming to the gateway is simulated in v1: full completion, then
    chunked. `docs/CONFIG.md:168` states that a multi-workspace token
    creates a workspace, peer, session, or webhook only in its primary
    workspace (`w`). Tests:
    `test_config_documents_simulated_streaming_and_primary_workspace` and
    `test_multi_workspace_token_creates_only_in_the_primary`.

## Upstream

Triage against `plastic-labs/honcho` `main` is appended in
`.review/upstream-triage.md:131` (appendix dated 2026-10-17). The only
security fix taken is `5df80a0`: `src/utils/agent_tools.py:243`
`_describe_tool_error` caps tool-error text at
`LLM.MAX_TOOL_OUTPUT_CHARS`, and `src/llm/tool_loop.py:629` uses that
helper for unexpected tool exceptions. Its own commit. Test:
`test_tool_error_text_is_capped_before_it_reaches_the_model`.
