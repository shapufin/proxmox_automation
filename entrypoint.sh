#!/bin/sh
set -e

mkdir -p /app/configs /app/data /data/staging

# Only the first container that wins the migrate race actually runs migrations;
# the others wait. With SQLite this is file-lock safe.
python manage.py migrate --noinput
python manage.py collectstatic --noinput --clear

exec "$@"
