"""HNSW order and the concurrent-index guard for new migrations."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src import models
from src.config import settings
from src.sentica.vector_query import neighbor_statement

# Migrations that were already applied. New files are the ones outside this set.
_FROZEN_MIGRATIONS = {
    "05486ce795d5_make_session_name_required_on_messages.py",
    "066e87ca5b07_align_schema_with_declarative_models.py",
    "08894082221a_replace_collection_name_with_observer_.py",
    "110bdf470272_rename_deriver_disabled_to_deriver_.py",
    "119a52b73c60_support_external_embeddings.py",
    "20f89a421aff_rename_metamessage_type_to_label.py",
    "29ade7350c19_remove_document_level_valid_constraint.py",
    "3e7a1c9d5b20_index_messages_content_trgm.py",
    "556a16564f50_add_user_id_and_app_id_to_tables.py",
    "564ba40505c5_add_session_name_column_to_documents.py",
    "66e63cf2cf77_add_indexes_to_documents_table.py",
    "76ffba56fe8c_add_error_field_to_queueitem.py",
    "7c0d9a4e3b1f_add_unique_index_for_pending_dreams.py",
    "88b0fb10906f_add_webhooks_table.py",
    "917195d9b5e9_add_messageembedding_table.py",
    "a1b2c3d4e5f6_initial_schema.py",
    "a7c3e9f1b2d4_add_document_sources_table.py",
    "a7d4e2c91b08_conclusion_freshness.py",
    "a9c1e3f5b7d2_workspace_row_level_security.py",
    "b765d82110bd_change_metamessages_to_user_level_with_.py",
    "b8183c5ffb48_codify_document_level_and_times_derived.py",
    "b8d2f4a6c9e1_index_session_peers_by_peer.py",
    "b8e3c1a94d20_conclusion_keyword_index.py",
    "baa22cad81e2_standardize_constraint_names.py",
    "bb6fb3a7a643_add_message_seq_in_session_column.py",
    "c3828084f472_add_indexes_for_messages_and_.py",
    "c4e8b1a2d6f0_runtime_role_and_pending_counts.py",
    "c9d4e2a71b58_digests_and_deletion_records.py",
    "d429de0e5338_adopt_peer_paradigm.py",
    "e4eba9cfaa6f_make_document_session_name_nullable.py",
    "e6b2d8f04a17_dreams_due_superset.py",
    "e9b705f9adf9_add_server_defaults_to_timestamp_.py",
    "ec8f94139b02_codify_workspace_name_and_message_id_in_.py",
    "f1a2b3c4d5e6_add_reasoning_tree_columns.py",
    "f3c8a1d94e27_message_source_trust.py",
}


def test_new_index_migrations_use_create_index_concurrently() -> None:
    """Applied migrations stay frozen. A later index build is concurrent."""
    root = Path("migrations/versions")
    seen_new = False
    for path in sorted(root.glob("*.py")):
        if path.name in _FROZEN_MIGRATIONS:
            continue
        seen_new = True
        source = path.read_text(encoding="utf-8")
        creates = "op.create_index" in source or "CREATE INDEX" in source
        if not creates:
            continue
        assert (
            "postgresql_concurrently=True" in source
            or "CREATE INDEX CONCURRENTLY" in source
        ), path.name
    assert seen_new


async def test_vector_query_uses_hnsw_at_10k_rows(
    db_session: AsyncSession,
    sample_data: tuple[models.Workspace, models.Peer],
) -> None:
    """EXPLAIN at 10k current rows is an HNSW index scan."""
    workspace, peer = sample_data
    db_session.add(
        models.Collection(
            workspace_name=workspace.name,
            observer=peer.name,
            observed=peer.name,
        )
    )
    await db_session.commit()
    dimensions = settings.EMBEDDING.VECTOR_DIMENSIONS
    vector = "[" + ",".join(["0"] * (dimensions - 1) + ["1"]) + "]"
    await db_session.execute(
        text(
            "INSERT INTO documents (id, content, observer, observed, "
            + "workspace_name, embedding, stale, level, internal_metadata) "
            + "SELECT substr(md5(g::text || :salt), 1, 21), 'row ' || g, "
            + ":observer, :observed, :workspace, CAST(:vector AS vector), "
            + "false, 'explicit', '{}'::jsonb FROM generate_series(1, 10000) AS g"
        ),
        {
            "salt": workspace.name,
            "observer": peer.name,
            "observed": peer.name,
            "workspace": workspace.name,
            "vector": vector,
        },
    )
    await db_session.execute(text("ANALYZE documents"))
    await db_session.commit()
    embedding = [0.0] * (dimensions - 1) + [1.0]
    stmt = neighbor_statement(
        select(models.Document).where(models.Document.workspace_name == workspace.name),
        embedding,
        10,
    )
    connection = await db_session.connection()
    compiled = stmt.compile(dialect=connection.dialect)
    # exec_driver_sql sends the embedding as float[]. pgvector only defines
    # <=> for the vector type, so the distance bind has to be cast.
    explained_sql = compiled.string.replace(
        " <=> %(",
        " <=> CAST(%(",
        1,
    ).replace(
        ")s \n",
        ")s AS vector)\n",
        1,
    )
    explained = await connection.exec_driver_sql(
        "EXPLAIN " + explained_sql,
        compiled.params,
    )
    plan = "\n".join(str(row[0]) for row in explained.all())
    folded = plan.lower()
    assert "index scan" in folded or "bitmap index scan" in folded
    assert "hnsw" in folded
