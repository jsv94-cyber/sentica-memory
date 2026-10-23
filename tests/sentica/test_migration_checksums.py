"""Pin the sha256 of every migration that is on main.

Editing, adding, or removing a migration file fails this test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_EXPECTED: dict[str, str] = {
    "05486ce795d5_make_session_name_required_on_messages.py": "7de69d563ec3abdc3a67f3dcf5f44648cd58bbf5a1e9b00938dad6eb49c91a93",
    "066e87ca5b07_align_schema_with_declarative_models.py": "fc7f1915b8f4be2cb4c0090c30ef1d8e88dbabbdc5a5b50d7fd999d4b5852e05",
    "08894082221a_replace_collection_name_with_observer_.py": "e1a4e0f09c801f3fd19ab266691b21284e6e7be6a149a9f6cbb0b8f3a4618d04",
    "110bdf470272_rename_deriver_disabled_to_deriver_.py": "995c9928c2d872ad42b6621f2f53c0ff41a76391f12fd957e6dca3bd682aa991",
    "119a52b73c60_support_external_embeddings.py": "a911bd039ec2e4aa8235598e7fbe736e03ea1145050fdf1a47e04e623807f404",
    "20f89a421aff_rename_metamessage_type_to_label.py": "5e17b0f44b23d11421564973ae45b561052fe4528c563c75d5dc171947884586",
    "29ade7350c19_remove_document_level_valid_constraint.py": "d2fa6467835164b0374a1c0967cc8c4f97a762cebb8519164995f643f7a29105",
    "3e7a1c9d5b20_index_messages_content_trgm.py": "47f5ce88c0e7cc403a0f405c476b1183b8e1e78f2d4e527018836c2663263c46",
    "556a16564f50_add_user_id_and_app_id_to_tables.py": "5f67790a13d420d4623ef104f7a9b62d6a51e8e82ec148c15e86752225d9fc4e",
    "564ba40505c5_add_session_name_column_to_documents.py": "c261563fbb1b93efc06850aec26e6a2459b7c082b9b87d5d0d9fd54d9fae5b35",
    "66e63cf2cf77_add_indexes_to_documents_table.py": "407101bda16b63bed0a46339f9687b5dc141a0e7baf941f208adc3e8aa73f1cb",
    "76ffba56fe8c_add_error_field_to_queueitem.py": "214cc839c1db0ab07ea5406fe24f8b31b4a78ca1e6fa9f01b3cd92e0c2f38d02",
    "7c0d9a4e3b1f_add_unique_index_for_pending_dreams.py": "1508add05eca71a75eb89e63a784782505c0f7822c4e27d4330c49baa6e0520c",
    "88b0fb10906f_add_webhooks_table.py": "e52fa9b114688218d7932d81d18b3b4029e9d7a3f50d635455871e2d8646cd0e",
    "917195d9b5e9_add_messageembedding_table.py": "b5a3962a7bd7e913b1893bbe30ad90bc2805352f1b9acbded9c2a794135112cb",
    "a1b2c3d4e5f6_initial_schema.py": "4dbf115db6b738d5416a7b76b30345bf239dd360bc8cda5a4b3786f0a49cb102",
    "a7c3e9f1b2d4_add_document_sources_table.py": "1ac4d7d02e22495fb189d6a918a57ba3830c95ee39d185972139fe76fb8e38aa",
    "a7d4e2c91b08_conclusion_freshness.py": "bf8d0226e1435ebf9ea0d86259f423f6010a2eb6e66dfe2b3fb1836cf6ef0a74",
    "a9c1e3f5b7d2_workspace_row_level_security.py": "1ac71cc442753c69337c217e671640577e3aebcdd4e57c09d2657ef146119149",
    "b765d82110bd_change_metamessages_to_user_level_with_.py": "fb2a5943eccb1cbc01a90fa543e72cd2c3e21efd5f09e2e0f434192812244b7b",
    "b8183c5ffb48_codify_document_level_and_times_derived.py": "935e93b4d0848a807226f13a86d4c8de1cb0c2c72a78e54d502fad8679f3a542",
    "b8d2f4a6c9e1_index_session_peers_by_peer.py": "d6bd1cccd9e5676e10970c085cbe996572c11613ad3af01780c00d52a3a4ccdd",
    "b8e3c1a94d20_conclusion_keyword_index.py": "c237445a7683ce12e6f89dc8250e6d6ec793354e533f2376bc65b662aaf9cc14",
    "baa22cad81e2_standardize_constraint_names.py": "feb92a3bbf82a2928c810923347d59f86649df8a672fe016947c6f6a164d7b21",
    "bb6fb3a7a643_add_message_seq_in_session_column.py": "f763e0c2750aee0066b655c95538004703499df3ee823d1c99d6f22c9b2de65b",
    "c3828084f472_add_indexes_for_messages_and_.py": "15751708deaaaf8ac8bcd23425704ae85c852f9f05d018be94f43469377c10f7",
    "c4e8b1a2d6f0_runtime_role_and_pending_counts.py": "7125256ec3427c95a3d41d064ec6fcbb84e823ecbf849858a2d232e3a143933a",
    "c9d4e2a71b58_digests_and_deletion_records.py": "0656360f7d13252417bc87e3d4c2b8a25c8b4a4d6edcdf16676279cb82be3e6a",
    "d4a8c1e27b90_documents_hnsw_current.py": "fd54e72e022866035f3289a35313d0ee51f8ec0be6e174cdae953cc9b3fc50b9",
    "d429de0e5338_adopt_peer_paradigm.py": "a2d224d9cdb03ab5eb0044f043709b2d27a592fc6db9446ebef3cab2ee681943",
    "e4eba9cfaa6f_make_document_session_name_nullable.py": "c66c166914a75c8b737eb6408b4eedaaf658f790877124de69dcfc2df0bdde57",
    "e6b2d8f04a17_dreams_due_superset.py": "537c1c9377c04b03a13f2ff7359f8b5dc9f6c86fc71586ed0f8eb552f3a6fc31",
    "e7c2a91b4d08_documents_workspace_observer_observed.py": "5026913f5738a749b4c94906d6fdb2ba04520f45f99f7a16e51082b2a1cf383c",
    "e9b705f9adf9_add_server_defaults_to_timestamp_.py": "33302e21eebe2881fb9b0b47a396ad37aaf1bd36c24a475adb33e74a8e0f2af6",
    "ec8f94139b02_codify_workspace_name_and_message_id_in_.py": "e3351c3421681355e52ce44fe29eb84d206072baef4762289d223987f96dc6aa",
    "f1a2b3c4d5e6_add_reasoning_tree_columns.py": "946544475617cca6899566e77e9a4094854493cf85dbe9b254ff8a8d077aa9cc",
    "f3c8a1d94e27_message_source_trust.py": "4649723cfe1588681713b55826de1ae56ab47363cd360b9665a8a71a6ecf1eb1",
}


# The version pin stays equal to the versions directory. These two files
# are not migrations, and a change to either one still fails the pin.
_EXTRA: dict[str, str] = {
    "migrations/env.py": "d8db768dbfdc98238636bef8a75bbeac0536682126a751691db07a8c9abea5f4",
    "src/migrate.py": "52fa2ca612b493d1d6f6176de48b490d69467f9c993ea71dfeecd9fd13196739",
}


def test_migration_files_match_the_pinned_sha256() -> None:
    root = Path("migrations/versions")
    found = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("*.py"))
    }
    assert found == _EXPECTED
    for relative, digest in _EXTRA.items():
        payload = Path(relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == digest, relative
