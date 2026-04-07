from __future__ import annotations

import json
import shlex
import subprocess
import shutil
from pathlib import Path
from typing import Any, Optional

import paramiko

from .models import DiskFormat, ProxmoxBridgeSpec, ProxmoxStorageSpec


class ProxmoxClientError(RuntimeError):
    pass


class ProxmoxClient:
    def __init__(
        self,
        node: str,
        ssh_enabled: bool = False,
        ssh_host: str = "",
        ssh_port: int = 22,
        ssh_username: str = "root",
        ssh_private_key: str = "",
        ssh_password: str = "",
    ) -> None:
        self.node = node
        self.ssh_enabled = ssh_enabled
        self.ssh_host = ssh_host or node
        self.ssh_port = ssh_port
        self.ssh_username = ssh_username
        self.ssh_private_key = ssh_private_key
        self.ssh_password = ssh_password

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        if self.ssh_enabled:
            return self._run_remote(args, check=check)
        proc = subprocess.run(args, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise ProxmoxClientError(
                f"Command failed: {' '.join(shlex.quote(x) for x in args)}\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
            )
        return proc

    def _run_remote(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        command = " ".join(shlex.quote(x) for x in args)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs: dict[str, Any] = {
                "hostname": self.ssh_host,
                "port": self.ssh_port,
                "username": self.ssh_username,
            }
            if self.ssh_private_key:
                connect_kwargs["key_filename"] = self.ssh_private_key
            elif self.ssh_password:
                connect_kwargs["password"] = self.ssh_password
            client.connect(**connect_kwargs)
            stdin, stdout, stderr = client.exec_command(command)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            proc = subprocess.CompletedProcess(args=args, returncode=exit_code, stdout=out, stderr=err)
            if check and exit_code != 0:
                raise ProxmoxClientError(
                    f"Command failed: {command}\nSTDOUT: {out}\nSTDERR: {err}"
                )
            return proc
        finally:
            client.close()

    def ensure_prerequisites(self) -> None:
        required = ["qm", "pvesh", "pvesm", "qemu-img"]
        missing = [cmd for cmd in required if shutil.which(cmd) is None]
        if missing:
            if self.ssh_enabled and "qemu-img" not in missing:
                return
            raise ProxmoxClientError(f"Missing required Proxmox host commands: {', '.join(missing)}")

    def list_storages(self) -> list[ProxmoxStorageSpec]:
        proc = self._run(["pvesm", "status", "--output-format", "json"])
        data = json.loads(proc.stdout or "[]")
        storages: list[ProxmoxStorageSpec] = []
        for row in data:
            storages.append(
                ProxmoxStorageSpec(
                    storage=str(row.get("storage", "")),
                    content=str(row.get("content", "")),
                    storage_type=str(row.get("type", "")),
                    total=int(row.get("total", 0) or 0),
                    used=int(row.get("used", 0) or 0),
                    available=int(row.get("avail", row.get("available", 0)) or 0),
                    shared=bool(row.get("shared", False)),
                    active=str(row.get("status", "active")) == "active",
                )
            )
        return storages

    def list_bridges(self) -> list[ProxmoxBridgeSpec]:
        proc = self._run(["pvesh", "get", f"/nodes/{self.node}/network", "--output-format", "json"])
        data = json.loads(proc.stdout or "[]")
        bridges: list[ProxmoxBridgeSpec] = []
        for row in data:
            if row.get("type") != "bridge":
                continue
            bridges.append(
                ProxmoxBridgeSpec(
                    name=str(row.get("iface", "")),
                    active=bool(row.get("active", False)),
                    vlan_aware=str(row.get("vlan_aware", "0")) in {"1", "true", "True"},
                    bridge_ports=str(row.get("bridge_ports", "")),
                    comments=str(row.get("comments", "")),
                )
            )
        return bridges

    def next_vmid(self) -> int:
        proc = self._run(["pvesh", "get", "/cluster/nextid", "--output-format", "text"])
        return int((proc.stdout or "").strip())

    def storage_by_name(self, name: str) -> ProxmoxStorageSpec:
        for storage in self.list_storages():
            if storage.storage == name:
                return storage
        raise ProxmoxClientError(f"Storage '{name}' not found")

    def bridge_exists(self, bridge: str) -> bool:
        return any(item.name == bridge for item in self.list_bridges())

    def choose_storage(self, preferred: Optional[str] = None) -> ProxmoxStorageSpec:
        storages = [s for s in self.list_storages() if s.active]
        if not storages:
            raise ProxmoxClientError("No active storages available on Proxmox")
        if preferred:
            return self.storage_by_name(preferred)
        disk_storages = [s for s in storages if "images" in s.content or s.content == "" or "rootdir" not in s.content]
        ordered = sorted(disk_storages or storages, key=lambda x: x.free, reverse=True)
        return ordered[0]

    def create_vm(
        self,
        vmid: int,
        name: str,
        memory_mb: int,
        cores: int,
        sockets: int,
        ostype: str = "l26",
        machine: str = "q35",
        bios: str = "seabios",
        scsihw: str = "virtio-scsi-single",
        agent: bool = True,
        onboot: bool = False,
    ) -> None:
        args = [
            "qm",
            "create",
            str(vmid),
            "--name",
            name,
            "--memory",
            str(memory_mb),
            "--cores",
            str(cores),
            "--sockets",
            str(sockets),
            "--ostype",
            ostype,
            "--machine",
            machine,
            "--scsihw",
            scsihw,
            "--onboot",
            "1" if onboot else "0",
        ]
        if bios == "ovmf":
            args.extend(["--bios", "ovmf"])
        if agent:
            args.extend(["--agent", "enabled=1"])
        self._run(args)

    def set_vm_options(self, vmid: int, options: dict[str, Any]) -> None:
        args = ["qm", "set", str(vmid)]
        for key, value in options.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "1" if value else "0"
            args.extend([f"--{key}", str(value)])
        self._run(args)

    def import_disk(self, vmid: int, image_path: Path, storage: str, disk_format: DiskFormat) -> str:
        proc = self._run([
            "qm",
            "importdisk",
            str(vmid),
            str(image_path),
            storage,
            "--format",
            disk_format.value,
        ])
        output = (proc.stdout or "") + (proc.stderr or "")
        for line in output.splitlines()[::-1]:
            if "Successfully imported disk as" in line:
                return line.split("as", 1)[1].strip().strip("'\"")
        raise ProxmoxClientError(f"Could not determine imported volume from output:\n{output}")

    def attach_disk(self, vmid: int, volume_id: str, slot: str = "scsi0", cache: str = "writeback") -> None:
        self._run(["qm", "set", str(vmid), f"--{slot}", f"{volume_id},cache={cache}"])

    def add_network(self, vmid: int, index: int, bridge: str, macaddr: str = "", model: str = "virtio", vlan: Optional[int] = None) -> None:
        value = f"{model},bridge={bridge}"
        if macaddr:
            value += f",macaddr={macaddr}"
        if vlan is not None:
            value += f",tag={vlan}"
        self._run(["qm", "set", str(vmid), f"--net{index}", value])

    def add_efi_disk(self, vmid: int, storage: str, disk_format: DiskFormat) -> None:
        value = f"{storage}:1,format=raw,efitype=4m"
        self._run(["qm", "set", str(vmid), "--bios", "ovmf", "--efidisk0", value])

    def set_boot_order(self, vmid: int, order: str = "scsi0") -> None:
        self._run(["qm", "set", str(vmid), "--boot", f"order={order}"])

    def start_vm(self, vmid: int) -> None:
        self._run(["qm", "start", str(vmid)])

    def stop_vm(self, vmid: int) -> None:
        self._run(["qm", "stop", str(vmid)])

    def status(self, vmid: int) -> dict[str, Any]:
        proc = self._run(["qm", "status", str(vmid), "--verbose"])
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {"raw": proc.stdout}

    def destroy_vm(self, vmid: int) -> None:
        self._run(["qm", "destroy", str(vmid), "--purge", "1"])

    def config_dump(self, vmid: int) -> str:
        proc = self._run(["qm", "config", str(vmid)])
        return proc.stdout
