# Deploy

The image is one artifact. The API and the deriver are two containers
started from it. The image user is `app`, uid 100, gid 100. The agent
container must use a different uid. Do not share a volume between them.

Certificates and the Redis password live on tmpfs. The root filesystem is
read-only. `/tmp` is a tmpfs. Every capability is dropped. The default
seccomp profile stays on. Do not disable seccomp.

## API

```bash
docker run --rm \
  --name sentica-memory-api \
  --user 100:100 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-instance \
  -p 10.8.0.4:8000:8000 \
  --env-file /run/sentica/api.env \
  sentica-memory:0.1.0
```

`SENTICA_BIND_HOST` is that private interface address (`10.8.0.4` in the
example). The broker is not assumed to be on the same host, so the publish
is not `127.0.0.1` and it is not `0.0.0.0`. `127.0.0.1` is only enough when
the broker process is on the API host. Set `SENTICA_BIND_HOST` to the same
address the broker uses.

`/health` is liveness. `/ready` is readiness. It answers only when the
database accepts `SELECT 1`, caches that success for a few seconds, takes
no token, and the body is only `{"status":"ok"}` or
`{"status":"unavailable"}`. `/metrics` is the Prometheus scrape, also a
probe, and it must be reachable only from the broker network.

## Deriver

```bash
docker run --rm \
  --name sentica-memory-deriver \
  --user 100:100 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-instance \
  --env-file /run/sentica/deriver.env \
  sentica-memory:0.1.0 \
  /app/.venv/bin/python -m src.deriver
```

## Migrate

The image entrypoint is the API. The one-shot migration job replaces it.
`SENTICA_MIGRATION_TIMEOUT_SECONDS` must be greater than
`DB_MIGRATION_LOCK_WAIT_SECONDS`.

```bash
docker run --rm \
  --name sentica-memory-migrate \
  --user 100:100 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-instance \
  --env-file /run/sentica/migrate.env \
  --entrypoint /app/docker/migrate-entrypoint.sh \
  sentica-memory:0.1.0
```

## Key files

`SENTICA_UID` and `SENTICA_GID` are image build args. The defaults are
`100` and `100`. The container runs as that numeric uid, so a mode `600`
key is readable only when the file is owned by that uid and gid:

```bash
chown "${SENTICA_UID:-100}:${SENTICA_GID:-100}" \
  /run/sentica/certs/client.key \
  /run/sentica/certs/client.crt \
  /run/sentica/certs/ca.crt \
  /run/sentica/certs/token.pub
chmod 600 /run/sentica/certs/client.key
```

Build the image with the same ids when they are not 100:

```bash
docker build \
  --build-arg SENTICA_UID=100 \
  --build-arg SENTICA_GID=100 \
  .
```

A plain `docker build .` uses those defaults. The runtime image reuses gid
100 when the base image already has it (`users` on
`python:3.13-slim-bookworm`) instead of failing `addgroup`.

The local two-instance proof is `docker-compose.sentica.yml`. It sets
`read_only`, `cap_drop: ALL`, and `no-new-privileges` on the API, deriver,
and migrate containers. gVisor is the extra file `docker-compose.gvisor.yml`:

```bash
docker compose -f docker-compose.sentica.yml -f docker-compose.gvisor.yml up -d --build
```

That override requires the `runsc` runtime on the Docker daemon. CI job
`test-gvisor-smoke` downloads `gvisor.tar.bz2` for the runner architecture,
checks `gvisor.tar.bz2.sha512`, installs `runsc` with its sidecar binaries,
and runs the smoke subset with `SENTICA_GVISOR=1`: egress denied, read-only
root, non-root Redis, and no `NET_ADMIN`. The full two-instance proof stays
on the default runtime in `test-m1-stack`. `publish-a-api`, `publish-a-proxy`,
`publish-b-api`, and `publish-b-proxy` are proof-only. Each runs as uid
65534, read-only, and forwards exactly one port. They are not part of the
instance.

## Gateway timeouts

When the timeout environment variables are unset, the defaults are
deriver 120 seconds, dream 180 seconds, dialectic 60 seconds, summary
60 seconds, and embed 30 seconds. Set `SENTICA_GATEWAY_TIMEOUT_SECONDS`
to override every purpose, or set one of
`SENTICA_GATEWAY_DERIVER_TIMEOUT_SECONDS`,
`SENTICA_GATEWAY_DREAM_TIMEOUT_SECONDS`,
`SENTICA_GATEWAY_DIALECTIC_TIMEOUT_SECONDS`,
`SENTICA_GATEWAY_SUMMARY_TIMEOUT_SECONDS`, and
`SENTICA_GATEWAY_EMBED_TIMEOUT_SECONDS` for a single purpose. A value
that is not a positive number refuses startup and refuses the call.

| Purpose | Default timeout |
| --- | --- |
| deriver | 120 seconds |
| dream | 180 seconds |
| dialectic | 60 seconds |
| summary | 60 seconds |
| embed | 30 seconds |

## Network

Sentica applies these rules. The compose file makes the first three
structural.

- Networks `db`, `net-a`, and `net-b` are `internal: true`. They have no
  route to the internet. Docker does not publish a port from an internal
  network, so the local proof uses four proof-only forwarders,
  `publish-a-api`, `publish-a-proxy`, `publish-b-api`, and
  `publish-b-proxy`. Each forwards exactly one port to `127.0.0.1` on the
  host. Those forwarders are not part of the instance.
- Postgres accepts TCP only from the PgBouncer addresses (`pg_hba.conf`).
  The proxy connects to Postgres with `server_tls_sslmode = verify-full`
  and the test CA. Clients of the instance reach Postgres only through
  that proxy, with a client certificate.
- The API and the deriver sit on the instance network. They are not on
  `db`. Their only database path is the proxy hostname.
- Inbound to the API is the broker, on `SENTICA_BIND_HOST`, which is a
  private address. Do not publish that port on a public interface.
- The agent container is not attached to `db`, `net-a`, `net-b`, or the
  instance network. It has no route to the API or to the proxy.
- Egress from the API and the deriver allows only the certificate proxy
  (`SENTICA_DB_PROXY_HOST`:`SENTICA_DB_PROXY_PORT`) and the model gateway
  (`SENTICA_GATEWAY_BASE_URL`, or a purpose-specific
  `SENTICA_GATEWAY_DERIVER_BASE_URL`, `SENTICA_GATEWAY_SUMMARY_BASE_URL`,
  `SENTICA_GATEWAY_DIALECTIC_BASE_URL`, `SENTICA_GATEWAY_DREAM_BASE_URL`,
  or `SENTICA_GATEWAY_EMBED_BASE_URL`). Deny provider hosts
  (`api.openai.com`, `api.anthropic.com`,
  `generativelanguage.googleapis.com`) and the rest of the public internet.

Redis runs as the `redis` user. The entry script starts as root only long
enough to create the password file, then `su redis` replaces it.

## Metrics

| Series | Meaning |
| --- | --- |
| `api_requests_total` | Requests by method, route, and status. |
| `api_request_duration_seconds` | Request latency. |
| `deriver_queue_items_pending` | Queue depth. |
| `deriver_tokens_processed_total` | Deriver and summary tokens. |
| `dialectic_tokens_processed_total` | Dialectic tokens. |
| `dreamer_tokens_processed_total` | Dream specialist tokens. |
| `conclusions_filtered_total` | Conclusions dropped by the safety filter. |
| `conclusions_decayed_total` | Conclusions whose weight decay updated. |
| `conclusions_pruned_total` | Conclusions removed after retention. |

## Memory and CPU

Sampled with `docker stats` on this host. The API sample is a second
container of the local `sentica-memory-api` image, `SENTICA_MODE=test`,
embeddings off, on the same Docker network as the local database. Idle is
the sample before the first message. The load sample posted 17 messages
in 61 seconds, which is 1,000 messages per hour, and records the peak.
This host does not have `runsc`, so the sample used the default runtime.

| State | CPU | Memory |
| --- | --- | --- |
| Idle | 0.24% | 249 MiB |
| 1,000 messages/hour | 0.27% | 251 MiB |

The deriver container that was already running beside that database, with
no traffic of its own, was 0.00% CPU and 206 MiB. The hosted CI runner
does not publish container stats for this table.
