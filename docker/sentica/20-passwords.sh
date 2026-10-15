#!/bin/bash
# Set tenant roles from SCRAM verifiers generated outside this container.
# The files are verifiers, not plaintext passwords.
set -euo pipefail

verifier_a=$(tr -d '\n' < /var/lib/postgresql/secrets/tenant-a)
verifier_b=$(tr -d '\n' < /var/lib/postgresql/secrets/tenant-b)
verifier_a_ddl=$(tr -d '\n' < /var/lib/postgresql/secrets/tenant-a-ddl)
verifier_b_ddl=$(tr -d '\n' < /var/lib/postgresql/secrets/tenant-b-ddl)
umask 077
cat > /tmp/tenant-passwords.sql <<EOF
ALTER ROLE tenant_a PASSWORD '${verifier_a}';
ALTER ROLE tenant_b PASSWORD '${verifier_b}';
ALTER ROLE tenant_a_ddl PASSWORD '${verifier_a_ddl}';
ALTER ROLE tenant_b_ddl PASSWORD '${verifier_b_ddl}';
EOF
unset verifier_a verifier_b verifier_a_ddl verifier_b_ddl
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -f /tmp/tenant-passwords.sql
rm -f /tmp/tenant-passwords.sql
