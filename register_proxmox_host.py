#!/usr/bin/env python3
"""
Script to pre-register the Proxmox host with credentials from environment
variables.  Never hard-code secrets — set them in .env or export them in
your shell before running this script.

Required env vars (at least one host identifier):
    PROXMOX_NODE, PROXMOX_API_HOST, or PROXMOX_SSH_HOST

Optional env vars:
    PROXMOX_LABEL            (default: "Lab Proxmox")
    PROXMOX_API_USER         (default: "root@pam")
    PROXMOX_API_TOKEN_NAME
    PROXMOX_API_TOKEN_VALUE
    PROXMOX_API_VERIFY_SSL   (default: "false")
    PROXMOX_SSH_ENABLED      (default: "true")
    PROXMOX_SSH_PORT         (default: "22")
    PROXMOX_SSH_USERNAME     (default: "root")
    PROXMOX_SSH_PASSWORD
    PROXMOX_SSH_PRIVATE_KEY
    PROXMOX_DEFAULT_STORAGE  (default: "local-lvm")
    PROXMOX_DEFAULT_BRIDGE   (default: "vmbr0")
"""

import os
import sys

import django

# Add the project directory to Python path
sys.path.insert(0, os.path.dirname(__file__))

# Set up Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'webui.settings_production')
django.setup()

from webui.dashboard.models import ProxmoxHost


def register_proxmox_host():
    """Register / update a Proxmox host using environment variables."""

    label = os.environ.get("PROXMOX_LABEL", "Lab Proxmox")
    node = os.environ.get("PROXMOX_NODE", "")
    api_host = os.environ.get("PROXMOX_API_HOST", "")
    ssh_host = os.environ.get("PROXMOX_SSH_HOST", api_host)

    if not node and not api_host and not ssh_host:
        print(
            "ERROR: At minimum one of PROXMOX_NODE, PROXMOX_API_HOST, or "
            "PROXMOX_SSH_HOST must be set as an environment variable."
        )
        sys.exit(1)

    # Check if host already exists (idempotent upsert)
    existing_host = ProxmoxHost.objects.filter(label=label).first()
    if existing_host:
        print(f"Host '{existing_host.label}' already exists. Updating...")
        host = existing_host
    else:
        print("Creating new Proxmox host...")
        host = ProxmoxHost()
        host.label = label

    # Credentials from environment — no hardcoded values
    host.node = node or api_host or ssh_host
    host.api_host = api_host
    host.api_user = os.environ.get("PROXMOX_API_USER", "root@pam")
    host.api_token_name = os.environ.get("PROXMOX_API_TOKEN_NAME", "")
    host.api_token_value = os.environ.get("PROXMOX_API_TOKEN_VALUE", "")
    host.api_verify_ssl = os.environ.get("PROXMOX_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

    # SSH settings
    host.ssh_enabled = os.environ.get("PROXMOX_SSH_ENABLED", "true").lower() in ("1", "true", "yes")
    host.ssh_host = ssh_host
    host.ssh_port = int(os.environ.get("PROXMOX_SSH_PORT", "22"))
    host.ssh_username = os.environ.get("PROXMOX_SSH_USERNAME", "root")
    host.ssh_password = os.environ.get("PROXMOX_SSH_PASSWORD", "")
    host.ssh_private_key = os.environ.get("PROXMOX_SSH_PRIVATE_KEY", "")

    # Default settings
    host.default_storage = os.environ.get("PROXMOX_DEFAULT_STORAGE", "local-lvm")
    host.default_bridge = os.environ.get("PROXMOX_DEFAULT_BRIDGE", "vmbr0")
    host.notes = os.environ.get(
        "PROXMOX_NOTES",
        "Auto-registered via register_proxmox_host.py",
    )

    # Save the host
    try:
        host.save()
        print(f"Successfully registered/updated Proxmox host: {host.label}")
        print(f"   ID: {host.id}")
        print(f"   API Host: {host.api_host or '(none)'}")
        print(f"   SSH Host: {host.ssh_host or '(none)'}")
        print(f"   Node: {host.node}")
        print(f"   SSH Enabled: {host.ssh_enabled}")
        print("\nYou can now test this host in the UI at /dashboard/hosts/")
    except Exception as e:
        print(f"Failed to save host: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    register_proxmox_host()
