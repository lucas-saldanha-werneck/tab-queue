#!/bin/sh
# Start as root only to fix the owner of the data folder (a bind mount such as ./data shows up owned by the
# host user, hiding the chown done at build time), then drop to the unprivileged user.
set -e
if [ "$(id -u)" = 0 ]; then
  chown -R tq /data
  exec su-exec tq "$@"
fi
exec "$@"
