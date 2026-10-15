#!/bin/sh
# Generate this instance's Redis password on a tmpfs mount. The file is the
# only copy. It is not written to the image or the compose file.
set -eu
mkdir -p /secret /data
if [ ! -s /secret/password ]; then
    dd if=/dev/urandom bs=24 count=1 status=none | od -An -tx1 | tr -d ' \n' > /secret/password
fi
chown redis:redis /secret /data /secret/password
chmod 700 /secret /data
chmod 600 /secret/password
password=$(tr -d '\n' < /secret/password)
exec su redis -s /bin/sh -c \
    "exec redis-server --save '' --appendonly no --requirepass '$password'"
