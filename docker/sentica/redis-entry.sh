#!/bin/sh
# Generate this instance's Redis password on a tmpfs mount. The file is the
# only copy. It is not written to the image or the compose file.
set -eu
mkdir -p /secret
if [ ! -s /secret/password ]; then
    dd if=/dev/urandom bs=24 count=1 status=none | od -An -tx1 | tr -d ' \n' > /secret/password
    chmod 644 /secret/password
fi
exec redis-server --save "" --appendonly no --requirepass "$(tr -d '\n' < /secret/password)"
