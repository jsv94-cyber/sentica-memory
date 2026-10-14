#!/bin/sh
# Copy this proxy's TLS key into the container and run PgBouncer as uid 70.
# The host file stays owned by the runner. No container sees a tenant key.
set -eu

cp /in/server.crt /tmp/proxy.crt
cp /in/server.key /tmp/proxy.key
cp /in/userlist /tmp/userlist.txt
chown 70:70 /tmp/proxy.crt /tmp/proxy.key /tmp/userlist.txt
chmod 644 /tmp/proxy.crt
chmod 600 /tmp/proxy.key /tmp/userlist.txt
exec su postgres -c 'exec /usr/bin/pgbouncer /etc/pgbouncer/pgbouncer.ini'
