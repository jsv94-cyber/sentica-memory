#!/bin/sh
# Generate this instance's Redis password on a tmpfs mount. The file is the
# only copy. It is not written to the image or the compose file.
set -eu
mkdir -p /secret /data
if [ ! -s /secret/password ]; then
    dd if=/dev/urandom bs=24 count=1 status=none | od -An -tx1 | tr -d ' \n' > /secret/password
fi
chown redis:redis /secret /data /secret/password
# The API runs as a different user and reads this file. 711 lets it open the
# known path without listing the directory. Redis data stays private.
chmod 711 /secret
chmod 700 /data
chmod 644 /secret/password
password=$(tr -d '\n' < /secret/password)
# setpriv execs redis-server, so pid 1 is the server and its uid is redis.
# su would stay pid 1 as root and only fork the server.
exec setpriv --reuid=redis --regid=redis --init-groups --inh-caps=-all \
    redis-server --save '' --appendonly no --requirepass "$password"
