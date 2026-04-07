from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import paramiko

from .models import VmwareVmSpec


@dataclass(slots=True)
class GuestRemediationPlan:
    script: str
    notes: list[str]


class GuestRemediator:
    def build_script(
        self,
        vm: VmwareVmSpec,
        rewrite_fstab: bool = True,
        install_qemu_agent: bool = True,
    ) -> GuestRemediationPlan:
        notes = [
            "Run as root inside the Linux guest after the Proxmox VM boots.",
            "Review /etc/fstab changes before rebooting if the system uses custom mount logic.",
        ]
        script = textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail

            log() {{ printf '[migrate] %s\n' "$*"; }}

            detect_pkg_mgr() {{
              if command -v apt-get >/dev/null 2>&1; then echo apt; return 0; fi
              if command -v dnf >/dev/null 2>&1; then echo dnf; return 0; fi
              if command -v yum >/dev/null 2>&1; then echo yum; return 0; fi
              if command -v zypper >/dev/null 2>&1; then echo zypper; return 0; fi
              if command -v pacman >/dev/null 2>&1; then echo pacman; return 0; fi
              echo unknown
            }}

            PKG_MGR="$(detect_pkg_mgr)"
            log "Detected package manager: $PKG_MGR"

            case "$PKG_MGR" in
              apt)
                export DEBIAN_FRONTEND=noninteractive
                apt-get update
                if {str(install_qemu_agent).lower()}; then
                  apt-get install -y qemu-guest-agent
                fi
                if dpkg -l | awk '{{print $2}}' | grep -qx open-vm-tools; then
                  apt-get remove -y open-vm-tools open-vm-tools-desktop || true
                fi
                update-initramfs -u -k all || true
                update-grub || grub-mkconfig -o /boot/grub/grub.cfg || true
                ;;
              dnf)
                if {str(install_qemu_agent).lower()}; then
                  dnf install -y qemu-guest-agent
                fi
                dnf remove -y open-vm-tools || true
                dracut -f || true
                grub2-mkconfig -o /boot/grub2/grub.cfg || true
                ;;
              yum)
                if {str(install_qemu_agent).lower()}; then
                  yum install -y qemu-guest-agent
                fi
                yum remove -y open-vm-tools || true
                dracut -f || true
                grub2-mkconfig -o /boot/grub2/grub.cfg || true
                ;;
              zypper)
                if {str(install_qemu_agent).lower()}; then
                  zypper --non-interactive install qemu-guest-agent
                fi
                zypper --non-interactive rm open-vm-tools || true
                dracut -f || true
                grub2-mkconfig -o /boot/grub2/grub.cfg || true
                ;;
              pacman)
                if {str(install_qemu_agent).lower()}; then
                  pacman -Sy --noconfirm qemu-guest-agent
                fi
                pacman -Rns --noconfirm open-vm-tools || true
                mkinitcpio -P || true
                grub-mkconfig -o /boot/grub/grub.cfg || true
                ;;
              *)
                log "Unsupported package manager; skipping package changes"
                ;;
            esac

            systemctl enable qemu-guest-agent >/dev/null 2>&1 || true
            systemctl restart qemu-guest-agent >/dev/null 2>&1 || true

            if {str(rewrite_fstab).lower()}; then
              if command -v python3 >/dev/null 2>&1; then
                python3 - <<'PY'
from pathlib import Path
import re
import subprocess

fstab = Path('/etc/fstab')
backup = Path('/etc/fstab.vmware-migration.bak')
text = fstab.read_text()
backup.write_text(text)
pattern = re.compile(r'^(?P<src>/dev/[^\\s]+)\\s+(?P<dst>\\S+)\\s+(?P<fstype>\\S+)\\s+(?P<opts>\\S+)\\s+(?P<dump>\\S+)\\s+(?P<passno>\\S+)', re.M)
lines = []
for line in text.splitlines():
    if line.lstrip().startswith('#') or not line.strip():
        lines.append(line)
        continue
    m = pattern.match(line)
    if not m:
        lines.append(line)
        continue
    src = m.group('src')
    try:
        uuid = subprocess.check_output(['blkid', '-s', 'UUID', '-o', 'value', src], text=True).strip()
    except Exception:
        uuid = ''
    if uuid:
        line = line.replace(src, f'UUID={uuid}', 1)
    lines.append(line)
fstab.write_text('\n'.join(lines) + '\n')
PY
              fi
            fi

            log "Remediation completed. Review boot logs and verify networking before production cutover."
            """
        )
        return GuestRemediationPlan(script=script, notes=notes)

    def write_script(self, path: Path, vm: VmwareVmSpec, rewrite_fstab: bool = True, install_qemu_agent: bool = True) -> Path:
        plan = self.build_script(vm, rewrite_fstab=rewrite_fstab, install_qemu_agent=install_qemu_agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.script, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_over_ssh(self, host: str, username: str, script_path: Path, port: int = 22, password: str = "", private_key: str = "") -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, port=port, username=username, password=password or None, key_filename=private_key or None)
        try:
            sftp = client.open_sftp()
            remote_path = f"/tmp/{script_path.name}"
            sftp.put(str(script_path), remote_path)
            sftp.chmod(remote_path, 0o755)
            sftp.close()
            stdin, stdout, stderr = client.exec_command(f"sudo -n bash {remote_path}")
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                raise RuntimeError(f"Guest remediation failed:\nSTDOUT: {stdout.read().decode()}\nSTDERR: {stderr.read().decode()}")
        finally:
            client.close()
