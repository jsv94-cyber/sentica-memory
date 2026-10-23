# v0.2.1-sentica

Upgrade from `v0.2.0-sentica` for Sentica's memory broker. The git tag is applied
to the merge commit of this change. This file is the release note.

## Contract

The broker contract is unchanged. `docs/INTEGRATION.md` still specifies
ingest, conclusions, search_memory, and brief, plus the companion stale,
deletion, and synthesis routes. Tokens use audience `sentica-memory`,
expire within 5 minutes, and name the tenant and the allowed workspaces.

The image on this tag includes behavior that does not change those
shapes, at `8857fb6bfd4e1d1b3d180240d8a914279573e615`:

- `src/sentica/gateway.py` lines 772-781 count tool-call arguments, and
  lines 744-753 count object keys, in the token budget. That landed in
  pull request 16.
- `src/sentica/vector_query.py` lines 1-6 read stale rows through the
  HNSW index.
- Deploy files hardened in pull requests 16 and 24 are in the image.
- The egress-proxy bypass reported in issue 18 is fixed.

## Image

The image manifest digest is the value already recorded in
`docs/image-digest.txt`. `.dockerignore` excludes `*.md` and `docs/**`,
so this note does not enter the image. `SOURCE_DATE_EPOCH` stays the
timestamp of the commit that last changed the Dockerfile. The export
command, the BuildKit pin, and the base-image digests stay in
`docs/RELEASE-v0.2.0-sentica.md`.

The image manifest digest is:

`sha256:65bfe6725581aae41a242f7a14f7f9a4b6deeba2d01bf85577d3286765c615e8`

## Upgrade

Sentica swaps the two constants in
`packages/api/src/memory-broker/pin.mjs` lines 6-9 and leaves the rest
of that file alone. Those lines are at Sentica commit
`9bb2d1429266a1401c45255516db6d58025307e0`:

`SENTICA_MEMORY_TAG` is `v0.2.0-sentica`. Set it to `v0.2.1-sentica`.

`SENTICA_MEMORY_DIGEST` is
`sha256:522b48cbd15d6ee59b99f0df376fc1e4cdaffd635e8aba66fd45114c6c35ae6d`.
Set it to
`sha256:65bfe6725581aae41a242f7a14f7f9a4b6deeba2d01bf85577d3286765c615e8`.

There is no migrate step. The Alembic head stays `d4a8c1e27b90`.
