#!/bin/bash
# Set tenant roles from SCRAM verifiers generated outside this container.
# The files are verifiers, not plaintext passwords.
set -euo pipefail

verifier_a=$(tr -d '\n' < /var/lib/postgresql/secrets/tenant-a)
verifier_b=$(tr -d '\n' < /var/lib/postgresql/secrets/tenant-b)
umask 077
cat > /tmp/tenant-passwords.sql <<EOF
ALTER ROLE tenant_a PASSWORD '${verifier_a}';
ALTER ROLE tenant_b PASSWORD '${verifier_b}';
EOF
unset verifier_a verifier_b
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -f /tmp/tenant-passwords.sql
rm -f /tmp/tenant-passwords.sql
