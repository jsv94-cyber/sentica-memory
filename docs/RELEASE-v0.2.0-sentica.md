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
on the request. Production refuses a missing label.

## Image

The default image is `docker build .`. Base images are pinned:

- `python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641`
- `ghcr.io/astral-sh/uv:0.9.24@sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5`

`SOURCE_DATE_EPOCH` defaults to `1760832000` in the Dockerfile. Layer
clocks follow that epoch when the export rewrites timestamps. The
command is:

```
docker build --provenance=false --sbom=false -o type=docker,dest=image.tar,rewrite-timestamp=true .
```

A second build with `--no-cache` wrote the same manifest and the same
layer blobs. The image manifest digest is:

`sha256:35a6bf1f11b39428d31a9e06b81dc0279ab7ce1d5180879690e28fc59db7f055`

## Footprint

Resident set for the sidecar on a throwaway stack is in
`docs/INTEGRATION.md`. Use it when sizing the 4 GB tenant droplet.

## Gaps left for Sentica

Sensitivity and role labels, the Restricted-upgrade classifier, the
release check, idempotency keys, and the hostname firewall rules are
listed in `docs/INTEGRATION.md`. They are not in this image.
