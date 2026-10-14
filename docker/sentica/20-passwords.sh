#!/bin/bash
# Set tenant passwords from files mounted only on this database container.
# The proxy userlists hold the same values. The API and deriver do not.
set -euo pipefail

pass_a=$(tr -d '\n' < /var/lib/postgresql/secrets/tenant-a)
pass_b=$(tr -d '\n' < /var/lib/postgresql/secrets/tenant-b)
umask 077
cat > /tmp/tenant-passwords.sql <<EOF
ALTER ROLE tenant_a PASSWORD '${pass_a}';
ALTER ROLE tenant_b PASSWORD '${pass_b}';
EOF
unset pass_a pass_b
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -f /tmp/tenant-passwords.sql
rm -f /tmp/tenant-passwords.sql
