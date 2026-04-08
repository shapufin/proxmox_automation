#!/bin/sh
set -e

mkdir -p /app/configs /app/data /data/staging

# ── Database migrations ────────────────────────────────────────────────────────
echo "[entrypoint] Running database migrations..."
python manage.py migrate --noinput

# ── Optional: auto-register a Proxmox host ────────────────────────────────────
# Set AUTO_REGISTER=true (and the PROXMOX_* env vars) to upsert a host record
# at startup.  Failures are non-fatal so the container starts even when the
# Proxmox host is temporarily unreachable.
if [ "${AUTO_REGISTER:-false}" = "true" ]; then
    echo "[entrypoint] AUTO_REGISTER=true — running register_host..."
    python manage.py register_host || {
        echo "[entrypoint] WARNING: register_host failed (host may be unreachable). Continuing startup."
    }
fi

exec "$@"
