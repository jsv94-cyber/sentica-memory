# Deploy

The image is one artifact. The API and the deriver are two containers
started from it. The image user is `app`, uid 100, gid 100. The agent
container must use a different uid. Do not share a volume between them.

Certificates and the Redis password live on tmpfs. The root filesystem is
read-only. `/tmp` is a tmpfs. Every capability is dropped. The default
seccomp profile stays on. Do not disable seccomp.

## API

Create the instance network once. The API, deriver, migrate, certificate
proxy, and model gateway join it. The egress proxy does not. `10.8.0.4`
is the host address the broker uses. It is not an address on this bridge.
The subnet is `10.8.0.16/28` (`10.8.0.16` through `10.8.0.31`). The
bridge gateway is `10.8.0.17`. The containers use `10.8.0.18` through
`10.8.0.22`. `10.8.0.23` through `10.8.0.30` are spare. Each container
below has a fixed `--ip` on this bridge. The gateway and the certificate
proxy each take a second address on another bridge, in the sections below.

`sentica-egress` is an internal bridge. Only the gateway and the egress
proxy join it. `sentica-wan` is the egress proxy's path to the internet.
Only the egress proxy joins it. `sentica-db` is an internal bridge. Only
Postgres and the certificate proxy join it.

```bash
docker network create \
  --driver bridge \
  --subnet 10.8.0.16/28 \
  --gateway 10.8.0.17 \
  sentica-instance

docker network create \
  --driver bridge \
  --internal \
  --subnet 10.8.0.32/28 \
  --gateway 10.8.0.33 \
  sentica-egress

docker network create \
  --driver bridge \
  --subnet 10.8.0.48/28 \
  --gateway 10.8.0.49 \
  sentica-wan

docker network create \
  --driver bridge \
  --internal \
  --subnet 10.8.0.64/28 \
  --gateway 10.8.0.65 \
  sentica-db
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
  --ip 10.8.0.18 \
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
  --ip 10.8.0.19 \
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
  --ip 10.8.0.20 \
  -v /run/sentica/certs/client.crt:/certs/client.crt:ro \
  -v /run/sentica/certs/client.key:/certs/client.key:ro \
  -v /run/sentica/certs/ca.crt:/certs/ca.crt:ro \
  -v /run/sentica/certs/token.pub:/certs/token.pub:ro \
  --env-file /run/sentica/migrate.env \
  --entrypoint /app/docker/migrate-entrypoint.sh \
  sentica-memory:0.1.0
```

## Certificate proxy

The proxy listens on `10.8.0.21:6432` for the API, the deriver, and
migrate. It is not published on the host. Postgres listens on
`10.8.0.66:5432` on `sentica-db`. That address is not on the host and not
on `10.8.0.16/28`. The certificate proxy joins `sentica-db` at
`10.8.0.67`. Those two containers are the only members, so the proxy
opens Postgres on that bridge. Docker 29 does not filter same-bridge
packets, and a `SENTICA` rule for a database address the proxy is not
attached to matches nothing. Do not add one.

```bash
docker run --rm \
  --name sentica-memory-proxy \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-instance \
  --ip 10.8.0.21 \
  -v /run/sentica/certs/proxy.crt:/certs/proxy.crt:ro \
  -v /run/sentica/certs/proxy.key:/certs/proxy.key:ro \
  -v /run/sentica/certs/ca.crt:/certs/ca.crt:ro \
  --env-file /run/sentica/proxy.env \
  edoburu/pgbouncer:v1.24.1-p1

docker network connect \
  --ip 10.8.0.67 \
  sentica-db \
  sentica-memory-proxy

docker run --rm \
  --name sentica-memory-postgres \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-db \
  --ip 10.8.0.66 \
  pgvector/pgvector:pg15
```

## Model gateway

The gateway listens on `10.8.0.22:443` for the API and the deriver. It
also joins `sentica-egress` at `10.8.0.34`. That address is the only
source the egress proxy accepts. No other container joins
`sentica-egress`, so no other container has a route to the proxy.

```bash
docker run --rm \
  --name sentica-memory-gateway \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-instance \
  --ip 10.8.0.22 \
  --env-file /run/sentica/gateway.env \
  sentica-gateway:0.1.0

docker network connect \
  --ip 10.8.0.34 \
  sentica-egress \
  sentica-memory-gateway
```

`docker/sentica/gateway.env` is installed as `/run/sentica/gateway.env`.
It sets `HTTPS_PROXY` to `http://10.8.0.35:8443` and `NO_PROXY` to the
instance, database, and egress addresses, so provider calls leave through
the egress proxy. The API, deriver, migrate, and certificate proxy env
files do not set `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`.

## Egress proxy

The egress proxy listens on `10.8.0.35:8443` on `sentica-egress`. It is
not on `sentica-instance`, it is not published on the host, and it is
not the broker at `10.8.0.4`. The image is Ubuntu 24.04 LTS, pinned by
digest. The config is `docker/sentica/egress-proxy.conf`. `CONNECT` is
allowed only to port 443 on the three provider hostnames below. Plain
HTTP is denied. Every other destination is denied. `dstdom_regex -n`
matches an IP-literal `CONNECT` as an address, so a reverse name cannot
satisfy the hostname rule. A new connection is accepted only from the
gateway at `10.8.0.34`.

The proxy also joins `sentica-wan` at `10.8.0.50`. That bridge is not
internal, and the proxy is its only member. Name resolution and provider
TCP 443 leave through it.

The egress proxy allowlist is:

```
api.openai.com
api.anthropic.com
generativelanguage.googleapis.com
```

A hostname that is not on that list is refused. runsc does not use
Docker's embedded DNS at `127.0.0.11`. The proxy resolves provider names
through `1.1.1.1`. The same service is `docker/sentica/egress-proxy.yml`.

```bash
docker run --rm \
  --name sentica-memory-egress \
  --user 13:13 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --runtime runsc \
  --network sentica-egress \
  --ip 10.8.0.35 \
  --dns 1.1.1.1 \
  --tmpfs /var/spool/squid:rw,noexec,nosuid,size=16m,uid=13,gid=13 \
  --tmpfs /var/log/squid:rw,noexec,nosuid,size=16m,uid=13,gid=13 \
  --tmpfs /var/run/squid:rw,noexec,nosuid,size=4m,uid=13,gid=13 \
  -v /run/sentica/egress-proxy.conf:/etc/squid/squid.conf:ro \
  --entrypoint /usr/sbin/squid \
  ubuntu/squid:6.6-24.04_edge@sha256:8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c \
  -f /etc/squid/squid.conf -NYC

docker network connect \
  --ip 10.8.0.50 \
  sentica-wan \
  sentica-memory-egress
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
`python:3.13-slim-bookworm`) instead of failing `addgroup`. Both stages
pin that image by digest.

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
  `db` or `sentica-db`. Their only database path is the certificate
  proxy (`SENTICA_DB_PROXY_HOST`:`SENTICA_DB_PROXY_PORT`). The
  certificate proxy is the only instance container on `sentica-db`.
- Inbound to the API is the broker, on `SENTICA_BIND_HOST`, which is a
  private address. Do not publish that port on a public interface.
- The agent container is not attached to `db`, `net-a`, `net-b`,
  `sentica-db`, `sentica-egress`, `sentica-wan`, or the instance network.
  It has no route to the API, the certificate proxy, or the egress proxy.
- Provider calls from the API and the deriver go to the model gateway
  (`SENTICA_GATEWAY_BASE_URL`, or a purpose-specific
  `SENTICA_GATEWAY_DERIVER_BASE_URL`, `SENTICA_GATEWAY_SUMMARY_BASE_URL`,
  `SENTICA_GATEWAY_DIALECTIC_BASE_URL`, `SENTICA_GATEWAY_DREAM_BASE_URL`,
  or `SENTICA_GATEWAY_EMBED_BASE_URL`). The gateway opens the egress
  proxy. The proxy checks the destination hostname on each connection.
  A hostname in `iptables -d` is resolved once, when the rule is
  inserted, and the rule keeps that address, so provider hosts are not
  iptables destinations.

  On `sentica-instance` (`10.8.0.16/28`) the API is `10.8.0.18`, the
  deriver is `10.8.0.19`, migrate is `10.8.0.20`, the certificate proxy
  is `10.8.0.21:6432`, and the gateway is `10.8.0.22:443`. Containers
  that share this bridge can open each other's ports. Docker 29 does not
  load `br_netfilter`, so `DOCKER-USER` does not see those packets. They
  are switched, not forwarded. A `RETURN` or `DROP` between two addresses
  on one bridge counts no packets.

  The egress proxy is not on this bridge. It listens at `10.8.0.35:8443`
  on `sentica-egress`, which is internal and has no member except the
  gateway (`10.8.0.34`) and the proxy. No other container has a connected
  route to that listener, and Docker drops packets the host would
  forward onto an internal bridge. The proxy process also accepts a new
  connection only from `10.8.0.34`. The proxy's other address,
  `10.8.0.50` on `sentica-wan`, is how it reaches the internet. No other
  container joins `sentica-wan`.

  Postgres is `10.8.0.66:5432` on `sentica-db`. The certificate proxy's
  address on that bridge is `10.8.0.67`. No other container joins
  `sentica-db`.

  `DOCKER-USER` is the host chain for packets that cross `FORWARD`.
  Docker 29 creates that chain empty. It does not add a RETURN. A packet
  that matches no rule returns to `FORWARD`, and Docker accepts the
  forwarded traffic. Packets addressed to an address on the host take
  `INPUT`. `DOCKER-USER` does not see them, so a proxy listening on the
  host is open to every container.

  The rules below apply to routed packets. They do not separate
  containers that share a bridge. The block creates `SENTICA`, flushes
  it, and appends every rule. It inserts the jump from `DOCKER-USER`
  only when `iptables -C` says the jump is absent. Running the block
  again rebuilds the same rules. Established replies return. The egress
  proxy's `sentica-wan` address may resolve names at `1.1.1.1` and may
  open TCP 443. Every container address is then dropped, so a routed
  packet from an address Docker assigned cannot leave the host.

```bash
iptables -N SENTICA 2>/dev/null || true
iptables -F SENTICA
iptables -A SENTICA -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A SENTICA -s 10.8.0.50/32 -d 1.1.1.1/32 -p udp --dport 53 -j RETURN
iptables -A SENTICA -s 10.8.0.50/32 -d 1.1.1.1/32 -p tcp --dport 53 -j RETURN
iptables -A SENTICA -s 10.8.0.50/32 -p tcp --dport 443 -j RETURN
iptables -A SENTICA -s 10.8.0.18/32 -j DROP
iptables -A SENTICA -s 10.8.0.19/32 -j DROP
iptables -A SENTICA -s 10.8.0.20/32 -j DROP
iptables -A SENTICA -s 10.8.0.21/32 -j DROP
iptables -A SENTICA -s 10.8.0.22/32 -j DROP
iptables -A SENTICA -s 10.8.0.34/32 -j DROP
iptables -A SENTICA -s 10.8.0.35/32 -j DROP
iptables -A SENTICA -s 10.8.0.50/32 -j DROP
iptables -A SENTICA -s 10.8.0.66/32 -j DROP
iptables -A SENTICA -s 10.8.0.67/32 -j DROP
iptables -C DOCKER-USER -j SENTICA 2>/dev/null || iptables -I DOCKER-USER 1 -j SENTICA
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
