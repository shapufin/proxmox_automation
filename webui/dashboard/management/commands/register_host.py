from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from webui.dashboard.models import ProxmoxHost


class Command(BaseCommand):
    help = (
        "Upsert a ProxmoxHost record from environment variables. "
        "Reads PROXMOX_LABEL, PROXMOX_NODE, PROXMOX_API_HOST, PROXMOX_API_USER, "
        "PROXMOX_API_TOKEN_NAME, PROXMOX_API_TOKEN_VALUE, PROXMOX_SSH_HOST, "
        "PROXMOX_SSH_PORT, PROXMOX_SSH_USERNAME, PROXMOX_SSH_PASSWORD, "
        "PROXMOX_DEFAULT_STORAGE, PROXMOX_DEFAULT_BRIDGE."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--label",
            default=os.environ.get("PROXMOX_LABEL", "Lab Proxmox"),
            help="Unique label for the host (default: PROXMOX_LABEL env var or 'Lab Proxmox')",
        )
        parser.add_argument(
            "--skip-if-exists",
            action="store_true",
            default=False,
            help="Do nothing if the label already exists instead of updating it",
        )

    def handle(self, *args, **options):
        label = options["label"]

        node = os.environ.get("PROXMOX_NODE", "")
        api_host = os.environ.get("PROXMOX_API_HOST", "")
        api_user = os.environ.get("PROXMOX_API_USER", "root@pam")
        api_token_name = os.environ.get("PROXMOX_API_TOKEN_NAME", "")
        api_token_value = os.environ.get("PROXMOX_API_TOKEN_VALUE", "")
        api_verify_ssl = os.environ.get("PROXMOX_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
        ssh_enabled = os.environ.get("PROXMOX_SSH_ENABLED", "true").lower() in ("1", "true", "yes")
        ssh_host = os.environ.get("PROXMOX_SSH_HOST", api_host)
        ssh_port = int(os.environ.get("PROXMOX_SSH_PORT", "22"))
        ssh_username = os.environ.get("PROXMOX_SSH_USERNAME", "root")
        ssh_password = os.environ.get("PROXMOX_SSH_PASSWORD", "")
        ssh_private_key = os.environ.get("PROXMOX_SSH_PRIVATE_KEY", "")
        default_storage = os.environ.get("PROXMOX_DEFAULT_STORAGE", "local-lvm")
        default_bridge = os.environ.get("PROXMOX_DEFAULT_BRIDGE", "vmbr0")
        notes = os.environ.get("PROXMOX_NOTES", "Auto-registered via register_host management command")

        if not node and not api_host and not ssh_host:
            raise CommandError(
                "At minimum one of PROXMOX_NODE, PROXMOX_API_HOST, or PROXMOX_SSH_HOST must be set."
            )

        host, created = ProxmoxHost.objects.get_or_create(label=label)

        if not created and options["skip_if_exists"]:
            self.stdout.write(self.style.WARNING(f"Host '{label}' already exists; skipping."))
            return

        host.node = node or api_host or ssh_host
        host.api_host = api_host
        host.api_user = api_user
        host.api_token_name = api_token_name
        host.api_token_value = api_token_value
        host.api_verify_ssl = api_verify_ssl
        host.ssh_enabled = ssh_enabled
        host.ssh_host = ssh_host
        host.ssh_port = ssh_port
        host.ssh_username = ssh_username
        host.ssh_password = ssh_password
        host.ssh_private_key = ssh_private_key
        host.default_storage = default_storage
        host.default_bridge = default_bridge
        host.notes = notes
        host.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} ProxmoxHost '{label}' (id={host.pk}) — "
                f"api_host={host.api_host or '(none)'}, ssh_host={host.ssh_host or '(none)'}"
            )
        )
