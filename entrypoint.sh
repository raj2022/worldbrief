#!/bin/sh
# The disk mounts as root, so fix its ownership, then run the server as a
# normal user. It fetches from the internet and serves the public.
set -e
mkdir -p /data/dist
chown -R app:app /data
exec setpriv --reuid=app --regid=app --init-groups \
    python -m worldbrief serve --host 0.0.0.0 --port "${PORT:-8080}" \
    --every "${REFRESH_MINUTES:-10}" --out-dir /data/dist
