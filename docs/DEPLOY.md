# Deploy

The image is one artifact. The API and the deriver are two containers
started from it. The image user is `app`, uid 100, gid 100. The agent
container must use a different uid. Do not share a volume between them.

Certificates and the Redis password live on tmpfs. The root filesystem is
read-only. `/tmp` is a tmpfs. Every capability is dropped. The default
seccomp profile stays on. Do not disable seccomp.

## API

Create the instance network once. The API, deriver, migrate, certificate
proxy, model gateway, and egress proxy containers join it. `10.8.0.4` is
the host address the broker uses. It is not an address on this bridge.
The subnet is `10.8.0.16/28` (`10.8.0.16` through `10.8.0.31`). The
bridge gateway is `10.8.0.17`. The containers use `10.8.0.18` through
`10.8.0.23`. `10.8.0.24` through `10.8.0.30` are spare. Each container
below has a fixed `--ip` in that range.

```bash
docker network create \
  --driver bridge \
  --subnet 10.8.0.16/28 \
  --gateway 10.8.0.17 \
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
`10.8.0.6:5432` on the internal `db` network. That address is not on the
host and not on `10.8.0.16/28`. Rule 8 of `SENTICA` returns that flow
before the certificate proxy is dropped:

`iptables -A SENTICA -s 10.8.0.21/32 -d 10.8.0.6/32 -p tcp --dport 5432 -j RETURN`

Do not return the public internet. A destination other than `10.8.0.6`
stays under the certificate proxy `DROP`.

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
```

## Model gateway

The gateway listens on `10.8.0.22:443` for the API and the deriver. It is
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
  --ip 10.8.0.22 \
  --env-file /run/sentica/gateway.env \
  sentica-gateway:0.1.0
```

`docker/sentica/gateway.env` is installed as `/run/sentica/gateway.env`.
It sets `HTTPS_PROXY` to `http://10.8.0.23:8443` and `NO_PROXY` to the
instance addresses, so provider calls leave through the egress proxy.
The API, deriver, migrate, and certificate proxy env files do not set
`HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`.

## Egress proxy

The egress proxy listens on `10.8.0.23:8443`. It is a container on the
instance bridge. It is not published on the host, and it is not the
broker at `10.8.0.4`. The image is pinned by digest. The config is
`docker/sentica/egress-proxy.conf`. `CONNECT` is allowed only to port
443 on the three provider hostnames below. Plain HTTP is denied. Every
other destination is denied. A new connection is accepted only from the
gateway at `10.8.0.22`.

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
  --network sentica-instance \
  --ip 10.8.0.23 \
  --dns 1.1.1.1 \
  --tmpfs /var/spool/squid:rw,noexec,nosuid,size=16m,uid=13,gid=13 \
  --tmpfs /var/log/squid:rw,noexec,nosuid,size=16m,uid=13,gid=13 \
  --tmpfs /var/run/squid:rw,noexec,nosuid,size=4m,uid=13,gid=13 \
  -v /run/sentica/egress-proxy.conf:/etc/squid/squid.conf:ro \
  --entrypoint /usr/sbin/squid \
  ubuntu/squid:6.13-25.04_beta@sha256:3de2e64f0ca6efdac3e98557607dc0f23050037f3885016d5d5bfcf9950501b8 \
  -f /etc/squid/squid.conf -NYC
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

  On the `10.8.0.16/28` instance network the API container is `10.8.0.18`,
  the deriver is `10.8.0.19`, migrate is `10.8.0.20`, the certificate
  proxy is `10.8.0.21:6432`, the model gateway is `10.8.0.22:443`, and
  the egress proxy is `10.8.0.23:8443`. Those are the `--ip` values on
  the run commands above. `DOCKER-USER` is the host chain for packets
  that cross `FORWARD`. Docker 29 creates that chain empty. It does not
  add a RETURN. A packet that matches no rule returns to `FORWARD`, and
  Docker accepts the forwarded traffic. Packets addressed to an address
  on the host take `INPUT`. `DOCKER-USER` does not see them, so a proxy
  listening on the host is open to every container.

  The egress proxy is a container on this bridge, so traffic from the
  gateway to it is forwarded and these rules apply. The proxy process
  also accepts a new connection only from `10.8.0.22`.

  The rules live in `SENTICA`. The block creates that chain, flushes it,
  and appends every rule. It inserts the jump from `DOCKER-USER` only
  when `iptables -C` says the jump is absent. Running the block again
  rebuilds the same rules. Established replies return. The API, the
  deriver, and migrate may open the certificate proxy. The API and the
  deriver may open the gateway. The gateway may open the egress proxy on
  port 8443. The certificate proxy may open Postgres at `10.8.0.6:5432`.
  The egress proxy may resolve names at `1.1.1.1` and may open TCP 443.
  Every container address is then dropped, so an address Docker would
  otherwise assign cannot leave the host.

```bash
iptables -N SENTICA 2>/dev/null || true
iptables -F SENTICA
iptables -A SENTICA -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A SENTICA -s 10.8.0.18/32 -d 10.8.0.21/32 -p tcp --dport 6432 -j RETURN
iptables -A SENTICA -s 10.8.0.19/32 -d 10.8.0.21/32 -p tcp --dport 6432 -j RETURN
iptables -A SENTICA -s 10.8.0.20/32 -d 10.8.0.21/32 -p tcp --dport 6432 -j RETURN
iptables -A SENTICA -s 10.8.0.18/32 -d 10.8.0.22/32 -p tcp --dport 443 -j RETURN
iptables -A SENTICA -s 10.8.0.19/32 -d 10.8.0.22/32 -p tcp --dport 443 -j RETURN
iptables -A SENTICA -s 10.8.0.22/32 -d 10.8.0.23/32 -p tcp --dport 8443 -j RETURN
iptables -A SENTICA -s 10.8.0.21/32 -d 10.8.0.6/32 -p tcp --dport 5432 -j RETURN
iptables -A SENTICA -s 10.8.0.23/32 -d 1.1.1.1/32 -p udp --dport 53 -j RETURN
iptables -A SENTICA -s 10.8.0.23/32 -d 1.1.1.1/32 -p tcp --dport 53 -j RETURN
iptables -A SENTICA -s 10.8.0.23/32 -p tcp --dport 443 -j RETURN
iptables -A SENTICA -s 10.8.0.18/32 -j DROP
iptables -A SENTICA -s 10.8.0.19/32 -j DROP
iptables -A SENTICA -s 10.8.0.20/32 -j DROP
iptables -A SENTICA -s 10.8.0.21/32 -j DROP
iptables -A SENTICA -s 10.8.0.22/32 -j DROP
iptables -A SENTICA -s 10.8.0.23/32 -j DROP
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
