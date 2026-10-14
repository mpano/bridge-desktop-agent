#!/bin/sh
# The volume is mounted at /data as root; the service runs as its own user.
set -e
mkdir -p /data
chown bridge:bridge /data
exec su bridge -c "uvicorn --factory bridge_cloud.main:create_app --host 0.0.0.0 --port 8080 \
  --proxy-headers --forwarded-allow-ips='*' --no-server-header"
