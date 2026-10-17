"""Helpers for the runtime-role job. The unit suite never sets SENTICA_RLS_JOB."""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.sentica.runtime import TOKEN_AUDIENCE


def rls_job() -> bool:
    """True when pytest is the test-m2-rls job."""
    return os.environ.get("SENTICA_RLS_JOB") == "1"


# Collected node ids for the runtime-role job, including parameters.
# tests/conftest.py fails the job when the collected set differs.
EXPECTED_M2_NODE_IDS: frozenset[str] = frozenset(
    {
        "tests/sentica/test_m2_rls.py::test_signed_token_names_its_workspaces",
        "tests/sentica/test_m2_rls.py::test_rls_job_collects_every_m2_file",
        "tests/sentica/test_m2_rls.py::test_app_engine_is_the_runtime_role",
        "tests/sentica/test_m2_rls.py::test_runtime_role_lockdown",
        "tests/sentica/test_m2_rls.py::test_reconciler_gate_enqueues_one_row_per_workspace",
        "tests/sentica/test_m2_rls.py::test_due_dream_is_reported_and_triggered",
        "tests/sentica/test_m2_rls.py::test_later_test_has_no_workspace_guard",
        "tests/sentica/test_m2_rls.py::test_sync_vectors_leaves_the_other_workspace",
        "tests/sentica/test_m2_rls.py::test_cleanup_leaves_the_other_workspace",
        "tests/sentica/test_m2_rls.py::test_backfill_leaves_the_other_workspace",
        "tests/sentica/test_m2_fix.py::test_ambiguous_workspace_claims_are_refused",
        "tests/sentica/test_m2_fix.py::test_pending_counts_function_has_no_content_columns",
        "tests/sentica/test_m2_fix.py::test_db_call_timeout_rejects_nan_and_infinity",
        "tests/sentica/test_m2_fix.py::test_workflow_job_builds_schema_as_the_ddl_role",
        "tests/sentica/test_m2_fix.py::test_data_migration_updates_each_workspace",
        "tests/sentica/test_m2_fix.py::test_queue_workspace_name_is_required",
        "tests/sentica/test_m2_fix.py::test_workspace_setting_is_not_inherited",
        "tests/sentica/test_m2_fix.py::test_revision_check_refuses_a_stale_schema",
        "tests/sentica/test_m2_fix.py::test_pagination_cache_key_includes_the_workspace",
        "tests/sentica/test_m2_fix.py::test_deriver_metrics_are_limited_to_the_token_workspaces",
        "tests/sentica/test_m2_fix.py::test_execute_dream_and_delayed_dream_bind_the_workspace",
        "tests/sentica/test_m2_fix.py::test_workspace_change_inside_an_open_transaction_is_refused",
        "tests/sentica/test_m2_fix.py::test_savepoint_rollback_refuses_a_different_workspace",
        "tests/sentica/test_m2_fix.py::test_workspace_setting_is_cleared_on_rollback",
        "tests/sentica/test_m2_fix.py::test_null_queue_rows_are_deleted_when_force_rls_is_on",
        "tests/sentica/test_m2_fix.py::test_claim_loop_uses_the_queue_counts_function",
        "tests/sentica/test_m2_fix.py::test_migration_refuses_a_role_that_does_not_end_in_ddl",
        "tests/sentica/test_m2_partitions.py::test_runtime_role_cannot_cross_workspaces",
        "tests/sentica/test_m2_partitions.py::test_cached_card_for_r_is_not_served_without_r",
        "tests/sentica/test_m2_partitions.py::test_workspace_outside_the_token_set_is_refused",
        "tests/sentica/test_m2_partitions.py::test_removing_a_workspace_from_the_token_refuses_the_next_request",
        "tests/sentica/test_m2_partitions.py::test_production_refuses_external_vector_stores[lancedb]",
        "tests/sentica/test_m2_partitions.py::test_production_refuses_external_vector_stores[qdrant]",
        "tests/sentica/test_m2_partitions.py::test_production_refuses_external_vector_stores[chroma]",
        "tests/sentica/test_m2_partitions.py::test_production_refuses_external_vector_stores[chromadb]",
        "tests/sentica/test_m2_partitions.py::test_production_refuses_external_vector_stores[turbopuffer]",
        "tests/sentica/test_m2_partitions.py::test_production_refuses_external_vector_stores[pinecone]",
        "tests/sentica/test_m2_partitions.py::test_search_tags_each_workspace",
        "tests/sentica/test_m2_partitions.py::test_canary_written_only_to_r_stays_out_of_other_workspaces",
        "tests/sentica/test_m2_partitions.py::test_harness_flags_the_canary_when_the_request_includes_its_workspace",
        "tests/sentica/test_m2_partitions.py::test_membership_noop_lets_the_canary_through",
        "tests/sentica/test_m2_partitions.py::test_search_scope_is_resolved_per_workspace",
        "tests/sentica/test_m2_partitions.py::test_cold_cache_returns_each_peer_card",
        "tests/sentica/test_m2_partitions.py::test_body_workspaces_outside_the_token_are_refused",
        "tests/sentica/test_m2_partitions.py::test_crud_filter_removed_rls_still_hides_the_canary",
        "tests/sentica/test_m2_partitions.py::test_crud_filter_removed_disabled_rls_shows_the_canary",
        "tests/sentica/test_m2_partitions.py::test_brief_stays_inside_the_callers_workspace",
        "tests/sentica/test_m2_partitions.py::test_subject_change_and_deletion_leave_no_canary_text",
        "tests/sentica/test_m2_partitions.py::test_synthesis_stays_inside_the_callers_workspace",
        "tests/sentica/test_m2_partitions.py::test_run_dreams_stays_inside_the_callers_workspace",
        "tests/sentica/test_m2_carryover.py::test_status_check_fails_when_the_runtime_role_job_fails",
        "tests/sentica/test_m2_carryover.py::test_workflows_pin_ubuntu_2404_and_node24_actions",
        "tests/sentica/test_m2_carryover.py::test_bounded_session_method_times_out[rollback]",
        "tests/sentica/test_m2_carryover.py::test_bounded_session_method_times_out[close]",
        "tests/sentica/test_m2_carryover.py::test_bounded_session_method_times_out[delete]",
        "tests/sentica/test_m2_carryover.py::test_bounded_session_method_times_out[refresh]",
        "tests/sentica/test_m2_carryover.py::test_bounded_session_method_times_out[get]",
        "tests/sentica/test_m2_carryover.py::test_bounded_session_method_times_out[merge]",
        "tests/sentica/test_m2_carryover.py::test_bounded_session_method_times_out[connection]",
        "tests/sentica/test_m2_carryover.py::test_bounded_session_method_times_out[run_sync]",
        "tests/sentica/test_m2_carryover.py::test_cancelled_caller_stops_the_session_connection",
        "tests/sentica/test_m2_carryover.py::test_database_receives_scram_verifiers_and_tmpfs",
        "tests/sentica/test_m2_carryover.py::test_bounded_wait_matches_psycopg",
        "tests/sentica/test_m2_carryover.py::test_production_log_strips_messages_except_the_allow_list",
        "tests/sentica/test_m2_data.py::test_unknown_workspace_metadata_is_refused",
        "tests/sentica/test_m2_data.py::test_digests_are_scoped_to_the_workspace",
        "tests/sentica/test_m2_data.py::test_deletion_records_store_hashes",
        "tests/sentica/test_m2_data.py::test_synthesis_off_does_not_enqueue_a_due_dream",
        "tests/sentica/test_m2_data.py::test_external_paraphrase_never_reaches_the_card",
        "tests/sentica/test_m2_data.py::test_brief_omits_a_session_the_peer_left",
        "tests/sentica/test_m2_data.py::test_forget_removes_the_fact_from_card_digest_descendants_summaries_and_search",
        "tests/sentica/test_m2_data.py::test_decay_anchor_moves_when_a_conclusion_is_used",
        "tests/sentica/test_m2_data.py::test_apply_decay_does_not_load_embeddings",
        "tests/sentica/test_m2_data.py::test_brief_calls_note_used",
        "tests/sentica/test_m2_data.py::test_note_locks_in_id_order_and_a_failure_still_returns_the_brief",
        "tests/sentica/test_m2_data.py::test_dreams_and_decay_are_claimed_queue_work",
        "tests/sentica/test_m2_data.py::test_dreams_and_decay_enqueue_once",
        "tests/sentica/test_m2_data.py::test_banned_digest_is_not_retried",
        "tests/sentica/test_m2_data.py::test_weekly_window_and_history_are_bounded",
        "tests/sentica/test_m2_data.py::test_safety_model_check_fails_closed_in_production",
        "tests/sentica/test_m2_data.py::test_safety_model_check_refuses_an_open_session",
        "tests/sentica/test_m2_data.py::test_delete_and_provenance_keep_the_workspace_predicate",
        "tests/sentica/test_m2_data.py::test_malformed_freshness_env_is_refused",
        "tests/sentica/test_m2_data.py::test_source_trust_is_required_in_production",
        "tests/sentica/test_m2_data.py::test_rls_policy_expression_excludes_extra_policies",
        "tests/sentica/test_m2_data.py::test_summary_check_runs_before_the_save_session",
        "tests/sentica/test_m2_data.py::test_forget_route_drops_descendants_digests_summaries_and_the_card",
        "tests/sentica/test_m2_data.py::test_budget_pause_keeps_the_queue_item_pending",
        "tests/sentica/test_m2_data.py::test_dream_budget_leaves_the_dream_unprocessed",
        "tests/sentica/test_m2_data.py::test_forget_drops_later_summaries_in_the_session",
        "tests/sentica/test_m2_isolation.py::test_workspace_filter_and_rls_policy_are_present",
        "tests/sentica/test_m2_isolation.py::test_http_red_team_hides_the_other_workspace",
        "tests/sentica/test_m2_isolation.py::test_removed_filter_and_disabled_rls_shows_the_canary",
        "tests/sentica/test_m2_isolation.py::test_multi_workspace_token_creates_only_in_the_primary",
    }
)


def defined_m2_tests() -> int:
    """How many test functions the M2 files define, before parametrization."""
    root = Path(__file__).resolve().parent
    total = 0
    for path in sorted(root.glob("test_m2_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name.startswith("test_"):
                total += 1
    return total


def runtime_dsn() -> str:
    """Passwordless TLS URI for the runtime role."""
    user = os.environ["SENTICA_DB_USER"]
    host = os.environ["SENTICA_DB_PROXY_HOST"]
    port = os.environ["SENTICA_DB_PROXY_PORT"]
    name = os.environ["SENTICA_DB_NAME"]
    ca = quote(os.environ["SENTICA_PROXY_CA_PATH"], safe="")
    cert = quote(os.environ["SENTICA_CLIENT_CERT_PATH"], safe="")
    key = quote(os.environ["SENTICA_CLIENT_KEY_PATH"], safe="")
    return (
        "postgresql://"
        + user
        + "@"
        + host
        + ":"
        + port
        + "/"
        + name
        + "?sslmode=verify-full&sslrootcert="
        + ca
        + "&sslcert="
        + cert
        + "&sslkey="
        + key
    )


def runtime_sqlalchemy_url() -> str:
    """SQLAlchemy URL for the same runtime connection."""
    return runtime_dsn().replace("postgresql://", "postgresql+psycopg://", 1)


def ddl_dsn() -> str:
    """Trust connection for the migration role. It is not the app engine."""
    host = os.environ["SENTICA_DB_PROXY_HOST"]
    port = os.environ["SENTICA_DB_PROXY_PORT"]
    name = os.environ["SENTICA_DB_NAME"]
    user = os.environ["SENTICA_DB_USER"] + "_ddl"
    return "postgresql://" + user + "@" + host + ":" + port + "/" + name


def sync_dsn(worker_id: str) -> str:
    """Worker database in the unit suite, runtime role in the RLS job."""
    if rls_job():
        return runtime_dsn()
    from sqlalchemy.engine.url import make_url

    run_id = os.environ.get("HONCHO_TEST_RUN_ID", "local")
    suffix = "" if worker_id == "master" else "_" + worker_id
    url = make_url(os.environ["DB_CONNECTION_URI"]).set(
        drivername="postgresql",
        database="test_db_" + run_id + suffix,
    )
    return url.render_as_string(hide_password=False)


def sign_broker_token(
    workspaces: list[str],
    *,
    primary: str | None = None,
    peer: str | None = None,
    session: str | None = None,
) -> str:
    """Sign a broker token with the job's private key."""
    raw = Path(os.environ["SENTICA_TOKEN_SIGNING_KEY"]).read_bytes()
    loaded = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise RuntimeError("broker signing key must be Ed25519")
    workspace = primary if primary is not None else workspaces[0]
    issued = int(time.time())
    claims: dict[str, Any] = {
        "iss": os.environ["SENTICA_TOKEN_ISSUER"],
        "aud": TOKEN_AUDIENCE,
        "iat": issued,
        "exp": issued + 60,
        "request_id": "m2-" + workspace + "-" + str(issued),
        "tenant_id": os.environ["SENTICA_TENANT_ID"],
        "w": workspace,
        "workspaces": workspaces,
    }
    if peer is not None:
        claims["p"] = peer
    if session is not None:
        claims["s"] = session
    return jwt.encode(claims, loaded, algorithm="EdDSA")


def authorize(
    client: object,
    workspaces: list[str],
    *,
    primary: str | None = None,
    peer: str | None = None,
) -> None:
    """Attach a real broker token. The unit suite leaves the client alone."""
    if not rls_job():
        return
    headers = getattr(client, "headers", None)
    if headers is None or not hasattr(headers, "__setitem__"):
        raise RuntimeError("client has no headers")
    token = sign_broker_token(workspaces, primary=primary, peer=peer)
    headers["Authorization"] = "Bearer " + token
