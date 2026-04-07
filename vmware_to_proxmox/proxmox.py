from __future__ import annotations

import json
import logging
import shlex
import stat as stat_mod
import subprocess
import shutil
from pathlib import Path
from typing import Any, Optional

import paramiko

from .models import DiskFormat, ProxmoxBridgeSpec, ProxmoxStorageSpec

log = logging.getLogger(__name__)


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
        api_host: str = "",
        api_user: str = "root@pam",
        api_token_name: str = "",
        api_token_value: str = "",
        api_verify_ssl: bool = False,
    ) -> None:
        self.node = node
        self.ssh_enabled = ssh_enabled
        self.ssh_host = ssh_host or node
        self.ssh_port = ssh_port
        self.ssh_username = ssh_username
        self.ssh_private_key = ssh_private_key
        self.ssh_password = ssh_password
        self.api_host = api_host
        self.api_user = api_user
        self.api_token_name = api_token_name
        self.api_token_value = api_token_value
        self.api_verify_ssl = api_verify_ssl
        self._proxmox_api: Any = None

    _API_UNAVAILABLE = object()  # sentinel: tried and failed, don't retry

    def _api_client(self) -> Any:
        """Return a proxmoxer ProxmoxAPI instance, or None if unavailable.
        Uses a sentinel to avoid retrying failed connections on every call."""
        if self._proxmox_api is self._API_UNAVAILABLE:
            return None
        if self._proxmox_api is not None:
            return self._proxmox_api
        host = self.api_host or self.ssh_host or self.node
        if not host:
            return None
        try:
            from proxmoxer import ProxmoxAPI  # type: ignore[import-untyped]
            if self.api_token_name and self.api_token_value:
                api = ProxmoxAPI(
                    host,
                    user=self.api_user,
                    token_name=self.api_token_name,
                    token_value=self.api_token_value,
                    verify_ssl=self.api_verify_ssl,
                )
            elif self.ssh_password:
                api = ProxmoxAPI(
                    host,
                    user=self.api_user,
                    password=self.ssh_password,
                    verify_ssl=self.api_verify_ssl,
                )
            else:
                return None
            api.version.get()  # eagerly test connectivity
            self._proxmox_api = api
            return self._proxmox_api
        except Exception as exc:  # noqa: BLE001
            log.warning("Proxmox API unavailable (%s): will use SSH CLI fallback", exc)
            self._proxmox_api = self._API_UNAVAILABLE  # type: ignore[assignment]
            return None

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
        """When SSH is enabled the binaries live on the remote host, not locally.
        When the API client is available we skip CLI checks for read-only operations.
        Only check locally if both SSH and API are absent."""
        if self.ssh_enabled:
            return
        if self._api_client() is not None:
            return
        required = ["qm", "pvesh", "pvesm", "qemu-img"]
        missing = [cmd for cmd in required if shutil.which(cmd) is None]
        if missing:
            raise ProxmoxClientError(
                f"Missing required Proxmox host commands: {', '.join(missing)}. "
                "Set proxmox.ssh_enabled=true and proxmox.ssh_host in config.yaml, "
                "or configure proxmox.api_host + api_token_name + api_token_value."
            )

    def list_storages(self) -> list[ProxmoxStorageSpec]:
        api = self._api_client()
        if api is not None:
            try:
                data = api.nodes(self.node).storage.get()
                return self._parse_storages(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("API storage query failed, falling back to SSH CLI: %s", exc)
        proc = self._run(["pvesm", "status", "--output-format", "json"])
        return self._parse_storages(json.loads(proc.stdout or "[]"))

    @staticmethod
    def _parse_storages(data: list[dict[str, Any]]) -> list[ProxmoxStorageSpec]:
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
        api = self._api_client()
        if api is not None:
            try:
                data = api.nodes(self.node).network.get()
                return self._parse_bridges(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("API network query failed, falling back to SSH CLI: %s", exc)
        proc = self._run(["pvesh", "get", f"/nodes/{self.node}/network", "--output-format", "json"])
        return self._parse_bridges(json.loads(proc.stdout or "[]"))

    @staticmethod
    def _parse_bridges(data: list[dict[str, Any]]) -> list[ProxmoxBridgeSpec]:
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
        api = self._api_client()
        if api is not None:
            try:
                return int(api.cluster.nextid.get())
            except Exception as exc:  # noqa: BLE001
                log.warning("API nextid query failed, falling back to SSH CLI: %s", exc)
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

    def list_remote_dir(self, remote_path: str) -> dict[str, Any]:
        """List a directory on the Proxmox HOST via SFTP.
        Returns {path, folders: [str], files: [{path, name, size}]}.
        Falls back to local filesystem when SSH is not configured."""
        if not self.ssh_enabled:
            p = Path(remote_path)
            if not p.exists():
                return {"path": str(p), "folders": [], "files": []}
            entries = sorted(p.iterdir())
            return {
                "path": str(p),
                "folders": [str(e) for e in entries if e.is_dir()],
                "files": [{"path": str(e), "name": e.name, "size": e.stat().st_size} for e in entries if e.is_file()],
            }
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict[str, Any] = {
            "hostname": self.ssh_host,
            "port": self.ssh_port,
            "username": self.ssh_username,
        }
        if self.ssh_private_key:
            connect_kwargs["key_filename"] = self.ssh_private_key
        elif self.ssh_password:
            connect_kwargs["password"] = self.ssh_password
        try:
            client.connect(**connect_kwargs)
            sftp = client.open_sftp()
            try:
                attrs = sftp.listdir_attr(remote_path)
            except FileNotFoundError:
                return {"path": remote_path, "folders": [], "files": []}
            folders = []
            files = []
            for a in sorted(attrs, key=lambda x: x.filename):
                full = remote_path.rstrip("/") + "/" + a.filename
                if stat_mod.S_ISDIR(a.st_mode or 0):
                    folders.append(full)
                else:
                    files.append({"path": full, "name": a.filename, "size": a.st_size or 0})
            return {"path": remote_path, "folders": folders, "files": files}
        finally:
            client.close()

    def read_remote_file(self, remote_path: str) -> str:
        """Read a text file from the Proxmox HOST via SFTP. Returns empty string on error."""
        if not self.ssh_enabled:
            try:
                return Path(remote_path).read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return ""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict[str, Any] = {
            "hostname": self.ssh_host,
            "port": self.ssh_port,
            "username": self.ssh_username,
        }
        if self.ssh_private_key:
            connect_kwargs["key_filename"] = self.ssh_private_key
        elif self.ssh_password:
            connect_kwargs["password"] = self.ssh_password
        try:
            client.connect(**connect_kwargs)
            sftp = client.open_sftp()
            with sftp.open(remote_path, "r") as fh:
                return fh.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
        finally:
            client.close()
