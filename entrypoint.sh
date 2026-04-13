#!/bin/sh
set -e

mkdir -p /app/configs /app/data /data/staging

# ── Migration lock mechanism ─────────────────────────────────────────────────────
# Prevent race conditions when multiple containers attempt migrations simultaneously
LOCK_FILE="/app/data/migration.lock"
LOCK_TIMEOUT=300  # 5 minutes maximum wait time
LOCK_ACQUIRED=0

# Try to acquire lock with timeout
echo "[entrypoint] Acquiring migration lock..."
elapsed=0
while [ $elapsed -lt $LOCK_TIMEOUT ]; do
    if ( set -o noclobber; echo $$ > "$LOCK_FILE" ) 2> /dev/null; then
        LOCK_ACQUIRED=1
        echo "[entrypoint] Migration lock acquired (PID: $$)"
        break
    else
        # Check if lock is stale (process no longer running)
        if [ -f "$LOCK_FILE" ]; then
            lock_pid=$(cat "$LOCK_FILE")
            if ! kill -0 "$lock_pid" 2>/dev/null; then
                echo "[entrypoint] Removing stale lock from PID $lock_pid"
                rm -f "$LOCK_FILE"
                continue
            fi
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    fi
done

if [ $LOCK_ACQUIRED -eq 0 ]; then
    echo "[entrypoint] ERROR: Failed to acquire migration lock after ${LOCK_TIMEOUT}s"
    echo "[entrypoint] Another process may be running migrations. Check $LOCK_FILE"
    exit 1
fi

# Cleanup function to release lock on exit
cleanup() {
    if [ $LOCK_ACQUIRED -eq 1 ]; then
        echo "[entrypoint] Releasing migration lock..."
        rm -f "$LOCK_FILE"
    fi
}
trap cleanup EXIT INT TERM

# ── Database migrations ────────────────────────────────────────────────────────
echo "[entrypoint] Running database migrations..."
python manage.py migrate --noinput

# ── Optional: auto-register a Proxmox host ────────────────────────────────────
# Set AUTO_REGISTER=true (and the PROXMOX_* env vars) to upsert a host record
# at startup.  Failures are non-fatal so the container starts even when the
# Proxmox host is temporarily unreachable.
if [ "${AUTO_REGISTER:-false}" = "true" ] || [ -n "${PROXMOX_NODE:-}" ] || [ -n "${PROXMOX_API_HOST:-}" ] || [ -n "${PROXMOX_SSH_HOST:-}" ]; then
    echo "[entrypoint] Running register_host --skip-if-exists..."
    python manage.py register_host --skip-if-exists || {
        echo "[entrypoint] WARNING: register_host failed (host may be unreachable). Continuing startup."
    }
fi

echo "[entrypoint] Initialization complete, releasing lock..."
# Lock will be released by trap cleanup

exec "$@"
