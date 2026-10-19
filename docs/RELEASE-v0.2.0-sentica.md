# v0.2.0-sentica

Integration contract for Sentica's memory broker. The git tag is applied
to the merge commit of this change. This file is the release note.

## Contract

`docs/INTEGRATION.md` is the broker contract: ingest, conclusions,
search_memory, and brief, plus the companion stale, deletion, and
synthesis routes. Tokens use audience `sentica-memory`, expire within
5 minutes, and name the tenant and the allowed workspaces. Reads
degrade when memory is down. Writes stay in the broker's durable queue.

`POST /v3/workspaces/{workspace_id}/peers/{peer_id}/search_memory` is
the hybrid search the brief already used. Conclusion responses include
`workspace_id`. A broker-created conclusion stores the `source_trust`
on the request. Production refuses a missing label, as ingest does.

## Image

The image manifest digest below is `containerimage.digest` from this
export. `SOURCE_DATE_EPOCH` is the timestamp of the commit that last
changed the Dockerfile. A later commit that only records the digest
does not move it. CI checks out the pull request head with full
history, because the default checkout is a merge commit with a
different timestamp. CI runs the export twice, each time from a new
BuildKit builder, and fails unless both digests equal the recorded
value.

```
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct -- Dockerfile)
docker buildx create --name repro --driver docker-container --driver-opt image=moby/buildkit@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea --use
docker buildx build --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" --provenance=false --sbom=false --no-cache --metadata-file image-metadata.json -o type=docker,dest=image.tar,rewrite-timestamp=true .
```

Base images are pinned:

- `python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641`
- `ghcr.io/astral-sh/uv:0.9.24@sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5`

The Dockerfile still has a default `SOURCE_DATE_EPOCH` of `1760832000`
for a build that does not pass the argument. The recorded digest uses
the commit timestamp from the command above. Tiktoken encodings are
downloaded by URL during the build and checked against their SHA-256.
Copied files use `COPY --chmod`, so the host umask cannot change a layer.

The image manifest digest is:

`sha256:8f604726eddf4315f8f1ae19cb4ae10155b417cdbc09a7ca9e1994ea8ccd557f`

## Footprint

Resident set for the sidecar on a throwaway stack is in
`docs/INTEGRATION.md`. Use it when sizing the 4 GB tenant droplet.

## Gaps left for Sentica

Sensitivity and role labels, the Restricted-upgrade classifier, the
release check, idempotency keys, and the hostname firewall rules are
listed in `docs/INTEGRATION.md`. They are not in this image.
