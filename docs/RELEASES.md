# Releases

## Unreleased

The default image manifest is no longer the one recorded for
`v0.2.0-sentica`. `docs/RELEASE-v0.2.0-sentica.md` stays as released.
This branch's image includes the digest-pinned Python stages and the
other files that image copies, so its manifest is
`sha256:84187e5ddf7131b30e20bc073d388b0ab07fc4b6f50030e1ebe03c71bc81ad7d`.
`docs/image-digest.txt` is the current expected digest. CI builds the
image twice from clean builders and fails when those two digests differ
or when they differ from that file.

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
