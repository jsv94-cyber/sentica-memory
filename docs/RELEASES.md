# Releases

## Unreleased

The default image manifest is no longer the one recorded for
`v0.2.1-sentica`. `docs/RELEASE-v0.2.1-sentica.md` stays as released.
Pull request #34 records
`sha256:2c8bbf29429766bcf1e502d75d03f347b57ad050e48594ca18cd4b9d6895fa97`.
Pull request #36 records
`sha256:df87aff25b1da10125bfa9fe4ad12aa3a26531a9b30806c9912926b0bc6c890f`.
The next release migrates from Alembic head `d4a8c1e27b90`
to `e7c2a91b4d08`. That revision adds
`ix_documents_workspace_observer_observed` on documents
`(workspace_name, observer, observed)`.
The migrate container stops at 360 seconds. This revision sets
`lock_timeout` to 30 seconds and `statement_timeout` to 240 seconds,
both below that limit. If either fires, migrate refuses to finish and
drops an invalid index. If a stuck lock still leaves
`ix_documents_workspace_observer_observed` invalid, connect as the ddl
role and run `DROP INDEX CONCURRENTLY IF EXISTS
memory.ix_documents_workspace_observer_observed`, then run migrate again.
`docs/image-digest.txt` holds the pull request #36 manifest. CI builds the
image twice from clean builders and fails when those two digests differ
or when they differ from that file.

## v0.2.1-sentica

The image manifest is
`sha256:65bfe6725581aae41a242f7a14f7f9a4b6deeba2d01bf85577d3286765c615e8`,
the value `docs/image-digest.txt` recorded at tag time.
`docs/RELEASE-v0.2.0-sentica.md` stays as released. The broker contract
is unchanged. There is no migrate step. The Alembic head stays
`d4a8c1e27b90`.

Sentica swaps the two constants in `pin.mjs` lines 6-9 and repoints
the comment on line 2. The note is `docs/RELEASE-v0.2.1-sentica.md`.
CI builds the image twice from clean builders and fails when those two
digests differ or when they differ from `docs/image-digest.txt`.

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
