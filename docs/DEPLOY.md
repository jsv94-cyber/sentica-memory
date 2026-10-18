# Deploy

The image is one artifact. The API and the deriver are two containers
started from it. The image user is `app`, uid 100, gid 100. The agent
container must use a different uid. Do not share a volume between them.

Certificates and the Redis password live on tmpfs. The root filesystem is
read-only. `/tmp` is a tmpfs. Every capability is dropped. The default
seccomp profile stays on. Do not disable seccomp.

## API

Create the instance network once. The API, deriver, migrate, certificate
proxy, and model gateway containers join it. `10.8.0.4` is the host
address the broker uses. It is not an address on this bridge. The subnet
is `10.8.0.8/29` (`10.8.0.8` through `10.8.0.15`). The bridge gateway is
`10.8.0.9`. Each container below has a fixed `--ip` in that range.

```bash
docker network create \
  --driver bridge \
  --subnet 10.8.0.8/29 \
  --gateway 10.8.0.9 \
  sentica-instance
```

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
  --ip 10.8.0.10 \
  -p 10.8.0.4:8000:8000 \
  -v /run/sentica/certs/client.crt:/certs/client.crt:ro \
  -v /run/sentica/certs/client.key:/certs/client.key:ro \
  -v /run/sentica/certs/ca.crt:/certs/ca.crt:ro \
  -v /run/sentica/certs/token.pub:/certs/token.pub:ro \
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
  --ip 10.8.0.11 \
  -v /run/sentica/certs/client.crt:/certs/client.crt:ro \
  -v /run/sentica/certs/client.key:/certs/client.key:ro \
  -v /run/sentica/certs/ca.crt:/certs/ca.crt:ro \
  -v /run/sentica/certs/token.pub:/certs/token.pub:ro \
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
  --ip 10.8.0.12 \
  -v /run/sentica/certs/client.crt:/certs/client.crt:ro \
  -v /run/sentica/certs/client.key:/certs/client.key:ro \
  -v /run/sentica/certs/ca.crt:/certs/ca.crt:ro \
  -v /run/sentica/certs/token.pub:/certs/token.pub:ro \
  --env-file /run/sentica/migrate.env \
  --entrypoint /app/docker/migrate-entrypoint.sh \
  sentica-memory:0.1.0
```

## Certificate proxy

The proxy listens on `10.8.0.13:6432` for the API, the deriver, and
migrate. It is not published on the host. Postgres is on the internal
`db` network, not on `10.8.0.4` and not on this bridge. A `RETURN` for
that Postgres address has to be inserted before the proxy `DROP` below.
Do not return the public internet.

```bash
docker run --rm \
  --name sentica-memory-proxy \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-instance \
  --ip 10.8.0.13 \
  -v /run/sentica/certs/proxy.crt:/certs/proxy.crt:ro \
  -v /run/sentica/certs/proxy.key:/certs/proxy.key:ro \
  -v /run/sentica/certs/ca.crt:/certs/ca.crt:ro \
  --env-file /run/sentica/proxy.env \
  edoburu/pgbouncer:v1.24.1-p1
```

## Model gateway

The gateway listens on `10.8.0.14:443` for the API and the deriver. It is
the only container that may open a new connection to the egress proxy.
Provider hosts stay denied for every other container.

```bash
docker run --rm \
  --name sentica-memory-gateway \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-instance \
  --ip 10.8.0.14 \
  --env-file /run/sentica/gateway.env \
  sentica-gateway:0.1.0
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

The env files point the process at those mounts:

```bash
SENTICA_CLIENT_CERT_PATH=/certs/client.crt
SENTICA_CLIENT_KEY_PATH=/certs/client.key
SENTICA_PROXY_CA_PATH=/certs/ca.crt
SENTICA_TOKEN_PUBLIC_KEY_PATH=/certs/token.pub
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

That override requires the `runsc` runtime on the Docker daemon. runsc
keeps its own network stack and cannot use Docker's embedded DNS at
127.0.0.11, so the file pins `embeddings` at `172.28.10.20` and
`172.28.11.20` and writes `extra_hosts` for the proxy, Redis, and gateway
names. Those names stay: the proxy certificate is issued for `pgbouncer-a`
and `pgbouncer-b`. CI job `test-gvisor-smoke` downloads `gvisor.tar.bz2`
for the runner architecture, checks `gvisor.tar.bz2.sha512`, installs
`runsc` with its sidecar binaries, and runs the smoke subset with
`SENTICA_GVISOR=1`: egress denied, read-only root, non-root Redis, and no
`NET_ADMIN`. The full two-instance proof stays on the default runtime in
`test-m1-stack`. `publish-a-api`, `publish-a-proxy`,
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
  or `SENTICA_GATEWAY_EMBED_BASE_URL`). Provider egress goes through the
  egress proxy. The proxy checks the destination hostname on each
  connection. A hostname in `iptables -d` is resolved once, when the rule
  is inserted, and the rule keeps that address, so provider hosts are not
  iptables destinations.

  The egress proxy listens on `10.8.0.5:8443`. That address is on the host.
  It is not on the instance bridge, and it is not the broker at `10.8.0.4`.
  The egress proxy allowlist is:

```
api.openai.com
api.anthropic.com
generativelanguage.googleapis.com
```

  A hostname that is not on that list is refused.

  On the `10.8.0.8/29` instance network the API container is `10.8.0.10`,
  the deriver is `10.8.0.11`, migrate is `10.8.0.12`, the certificate
  proxy is `10.8.0.13:6432`, and the model gateway is `10.8.0.14:443`.
  Those are the `--ip` values on the run commands above. `DOCKER-USER` is
  the host firewall for traffic that leaves those containers. Docker creates
  that chain with a RETURN that matches every packet. `-A` appends after
  that RETURN, so the new rule never runs. Insert each rule with `-I` at
  the position below. The positions keep this order and stay in front of
  the default RETURN. Established replies return. The API, the deriver,
  and migrate may open the proxy. The API and the deriver may open the
  gateway. The gateway may open the egress proxy on port 8443. Every
  container address is then dropped, so an address Docker would otherwise
  assign cannot leave the host.

```bash
iptables -I DOCKER-USER 1 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -I DOCKER-USER 2 -s 10.8.0.10/32 -d 10.8.0.13/32 -p tcp --dport 6432 -j RETURN
iptables -I DOCKER-USER 3 -s 10.8.0.11/32 -d 10.8.0.13/32 -p tcp --dport 6432 -j RETURN
iptables -I DOCKER-USER 4 -s 10.8.0.12/32 -d 10.8.0.13/32 -p tcp --dport 6432 -j RETURN
iptables -I DOCKER-USER 5 -s 10.8.0.10/32 -d 10.8.0.14/32 -p tcp --dport 443 -j RETURN
iptables -I DOCKER-USER 6 -s 10.8.0.11/32 -d 10.8.0.14/32 -p tcp --dport 443 -j RETURN
iptables -I DOCKER-USER 7 -s 10.8.0.14/32 -d 10.8.0.5/32 -p tcp --dport 8443 -j RETURN
iptables -I DOCKER-USER 8 -s 10.8.0.10/32 -j DROP
iptables -I DOCKER-USER 9 -s 10.8.0.11/32 -j DROP
iptables -I DOCKER-USER 10 -s 10.8.0.12/32 -j DROP
iptables -I DOCKER-USER 11 -s 10.8.0.13/32 -j DROP
iptables -I DOCKER-USER 12 -s 10.8.0.14/32 -j DROP
```

Redis runs as the `redis` user. The entry script starts as root only long
enough to create the password file, then `setpriv` execs `redis-server` so
pid 1 is that user.

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
