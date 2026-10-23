# Releases

## v0.2.1-sentica

The image manifest is
`sha256:65bfe6725581aae41a242f7a14f7f9a4b6deeba2d01bf85577d3286765c615e8`,
the same value `docs/image-digest.txt` already records.
`docs/RELEASE-v0.2.0-sentica.md` stays as released. The broker contract
is unchanged. There is no migrate step. The Alembic head stays
`d4a8c1e27b90`.

Sentica swaps the two constants in `pin.mjs` lines 6-9. The note is
`docs/RELEASE-v0.2.1-sentica.md`. `docs/image-digest.txt` is the current
expected digest. CI builds the image twice from clean builders and fails
when those two digests differ or when they differ from that file.

## v0.1.0-sentica

First fork release on the v3.3.0 pin
(`608aec8a928da13c540262ed980c569448fb53ab`).

- One tenant per instance, with certificate-proxy database access and
  broker tokens.
- Workspaces are partitions, with row-level security and a runtime role
  that cannot cross them.
- Model calls can go through one gateway. Sensitive inferences are dropped.
  Conclusions can go stale, be superseded, and be deleted with counts only.
- A call brief is keyword plus vector, bounded by a token budget.
- Daily and weekly dreams stay inside one workspace.
- Retrieval is scored on seeded businesses. An isolation leak or a recall
  drop of more than 2 points fails CI.
- Images run as uid 100, read-only, with capabilities dropped. The database
  proxy verifies Postgres with `verify-full`. Redis runs as the `redis`
  user. The proof networks are internal.

Deploy flags are in `docs/DEPLOY.md`. Settings are in `docs/CONFIG.md`.
Vulnerability reports follow `SECURITY.md`. Upstream sync follows
`docs/UPSTREAM_SYNC.md`.
