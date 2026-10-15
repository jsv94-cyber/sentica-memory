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
  -p 127.0.0.1:8000:8000 \
  --env-file /run/sentica/api.env \
  sentica-memory:0.1.0
```

`/health` is liveness. `/ready` is readiness and answers only when the
database accepts `SELECT 1`. Both are probes and do not require a token.
`/metrics` is the Prometheus scrape, also a probe, and it must be reachable
only from the broker network.

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

The local two-instance proof is `docker-compose.sentica.yml`. It sets
`read_only`, `cap_drop: ALL`, and `no-new-privileges` on the API, deriver,
and migrate containers. gVisor is the extra file `docker-compose.gvisor.yml`:

```bash
docker compose -f docker-compose.sentica.yml -f docker-compose.gvisor.yml up -d --build
```

That override requires the `runsc` runtime on the Docker daemon. The proof
job in CI uses the default runtime because the hosted runner does not ship
`runsc`.

## Network

Sentica applies these rules. The compose file makes the first three
structural.

- Networks `db`, `net-a`, and `net-b` are `internal: true`. They have no
  route to the internet.
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
- Outbound from the instance is the proxy and, when gateway mode is on,
  the model gateway URL. Provider hosts are not used in that mode.

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
