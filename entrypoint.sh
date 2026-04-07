#!/bin/sh
set -e

mkdir -p /app/configs /app/data /data/staging

exec "$@"
