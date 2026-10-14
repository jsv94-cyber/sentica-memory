#!/bin/bash
# Start Postgres with TLS. The server key is generated here and stays in
# the container. Tenant passwords are applied by the init script.
set -euo pipefail

install -d -o postgres -g postgres -m 700 /var/lib/postgresql/certs
install -d -o postgres -g postgres -m 700 /var/lib/postgresql/secrets
if [ -s /secrets/tenant-a ] && [ -s /secrets/tenant-b ] \
    && [ -s /secrets/tenant-a-ddl ] && [ -s /secrets/tenant-b-ddl ]; then
    install -o postgres -g postgres -m 600 \
        /secrets/tenant-a /var/lib/postgresql/secrets/tenant-a
    install -o postgres -g postgres -m 600 \
        /secrets/tenant-b /var/lib/postgresql/secrets/tenant-b
    install -o postgres -g postgres -m 600 \
        /secrets/tenant-a-ddl /var/lib/postgresql/secrets/tenant-a-ddl
    install -o postgres -g postgres -m 600 \
        /secrets/tenant-b-ddl /var/lib/postgresql/secrets/tenant-b-ddl
fi
if [ ! -s /var/lib/postgresql/certs/server.key ]; then
    openssl req -new -x509 -days 1 -nodes \
        -subj "/CN=database" \
        -keyout /var/lib/postgresql/certs/server.key \
        -out /var/lib/postgresql/certs/server.crt
    chown postgres:postgres \
        /var/lib/postgresql/certs/server.key \
        /var/lib/postgresql/certs/server.crt
    chmod 600 /var/lib/postgresql/certs/server.key
    chmod 644 /var/lib/postgresql/certs/server.crt
fi

exec docker-entrypoint.sh postgres \
    -c max_connections=200 \
    -c hba_file=/etc/sentica/pg_hba.conf \
    -c ssl=on \
    -c ssl_cert_file=/var/lib/postgresql/certs/server.crt \
    -c ssl_key_file=/var/lib/postgresql/certs/server.key
