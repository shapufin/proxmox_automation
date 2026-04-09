from __future__ import annotations

import json
import logging
import re
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


def _sanitize_vm_name(name: str, fallback: str = "vm") -> str:
    """Return a Proxmox-safe VM name.

    Proxmox rejects names that are not DNS-like. We normalize to lowercase,
    replace unsafe characters with hyphens, collapse repeats, and trim any
    leading/trailing punctuation. If the result would be empty, return *fallback*.
    """
    raw = (name or "").strip().lower()
    sanitized = re.sub(r"[^a-z0-9.-]+", "-", raw)
    sanitized = re.sub(r"[-.]{2,}", "-", sanitized).strip("-.")
    if not sanitized:
        sanitized = fallback
    if not re.match(r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$|^[a-z0-9]$", sanitized):
        sanitized = re.sub(r"[^a-z0-9]+", "-", sanitized).strip("-") or fallback
    return sanitized[:63]


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
        self._ssh_client: Optional[paramiko.SSHClient] = None

    _API_UNAVAILABLE = object()  # sentinel: tried and failed, don't retry

    def reset(self) -> None:
        """Reset all cached connections so the next call re-establishes them.
        Call this before a user-triggered refresh to force a clean reconnect."""
        self._proxmox_api = None
        self._close_ssh()

    def _close_ssh(self) -> None:
        if self._ssh_client is not None:
            try:
                self._ssh_client.close()
            except Exception:  # noqa: BLE001
                pass
            self._ssh_client = None

    def _test_host_connectivity(self, host: str) -> bool:
        """Test basic TCP connectivity to a host (IP or hostname)."""
        try:
            import socket
            # Try to connect to port 22 (SSH) and 8006 (HTTPS API)
            for port in [22, 8006]:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(5)
                    result = sock.connect_ex((host, port))
                    sock.close()
                    if result == 0:
                        log.info(f"Successfully connected to {host}:{port}")
                        return True
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def _get_actual_node_name(self, api) -> str:
        """Get the actual node name from the Proxmox cluster in case the configured one is wrong."""
        try:
            # Get all nodes in the cluster
            nodes = api.nodes.get()
            if nodes:
                # Use the first available node
                actual_node = nodes[0].get('node', self.node)
                if actual_node != self.node:
                    log.info(f"Using actual node name '{actual_node}' instead of configured '{self.node}'")
                return actual_node
        except Exception as exc:
            log.warning(f"Failed to get actual node name: {exc}")
        return self.node

    def _get_ssh_client(self) -> paramiko.SSHClient:
        """Return a connected paramiko SSHClient, reusing an existing one if still active."""
        if self._ssh_client is not None:
            transport = self._ssh_client.get_transport()
            if transport is not None and transport.is_active():
                return self._ssh_client
            self._close_ssh()
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
        client.connect(**connect_kwargs)
        self._ssh_client = client
        return self._ssh_client

    def _api_client(self) -> Any:
        """Return a proxmoxer ProxmoxAPI instance, or None if unavailable.
        Uses a sentinel to avoid retrying failed connections on every call.
        Call reset() to clear the sentinel and allow a fresh attempt."""
        if self._proxmox_api is self._API_UNAVAILABLE:
            return None
        if self._proxmox_api is not None:
            return self._proxmox_api
        host = self.api_host or self.ssh_host or self.node
        if not host:
            return None
        
        # Log the exact host we're trying to connect to
        log.info(f"Attempting to connect to Proxmox API at host: {host}")
        
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
            log.info(f"Successfully connected to Proxmox API at {host}")
            return self._proxmox_api
        except Exception as exc:  # noqa: BLE001
            log.warning("Proxmox API unavailable (%s): will use SSH CLI fallback", exc)
            # Try IP fallback if hostname resolution failed
            if "Name or service not known" in str(exc) or "nodename nor servname" in str(exc):
                log.warning("Hostname resolution failed, trying IP connectivity test...")
                try:
                    import socket
                    # Try to resolve if it's an IP, or try basic connectivity
                    if self._test_host_connectivity(host):
                        log.info(f"Host {host} is reachable via IP but API/SSH connection failed")
                    else:
                        log.error(f"Host {host} is not reachable at all")
                except Exception as conn_exc:
                    log.error(f"Connectivity test failed: {conn_exc}")
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
        try:
            client = self._get_ssh_client()
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
        except ProxmoxClientError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._close_ssh()  # drop broken connection so next call reconnects
            raise ProxmoxClientError(f"SSH command failed: {exc}") from exc

    def ensure_prerequisites(self) -> None:
        """When SSH is enabled the binaries live on the remote host, not locally.
        When the API client is available we skip CLI checks for read-only operations.
        Only check locally if both SSH and API are absent."""
        if self.ssh_enabled:
            log.info("SSH enabled - skipping local binary checks, will use remote execution")
            return
        if self._api_client() is not None:
            log.info("API client available - skipping local binary checks")
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
                # First get the actual node name in case the configured one is wrong
                actual_node = self._get_actual_node_name(api)
                data = api.nodes(actual_node).storage.get()
                return self._parse_storages(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("API storage query failed, falling back to SSH CLI: %s", exc)
        
        # CLI fallback: probe formats from newest to oldest.
        # IMPORTANT: Never use "-format=json" — Proxmox VE 7/8 parses "-format" as a
        # boolean flag and rejects "=json" with "400 unable to parse boolean option".
        try:
            # PVE 7+ uses --output-format as a separate token
            proc = self._run(["pvesm", "status", "--output-format", "json"])
            return self._parse_storages(json.loads(proc.stdout or "[]"))
        except ProxmoxClientError as exc:
            _exc_str = str(exc)
            if "Unknown option" in _exc_str or "unable to parse" in _exc_str or "400" in _exc_str:
                log.warning("pvesm --output-format not supported, falling back to plain text: %s", exc)
                # Plain text works on all PVE versions
                return self._parse_storages_text(self._run(["pvesm", "status"]))
            raise

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

    @staticmethod
    def _parse_storages_text(proc: subprocess.CompletedProcess[str]) -> list[ProxmoxStorageSpec]:
        """Parse plain text output from pvesm status for very old Proxmox versions"""
        storages: list[ProxmoxStorageSpec] = []
        lines = proc.stdout.strip().split('\n')
        
        # Skip header line if present
        start_line = 1 if lines and 'storage' in lines[0].lower() else 0
        
        for line in lines[start_line:]:
            if not line.strip():
                continue
            
            # Parse tab/space separated columns
            parts = line.split()
            if len(parts) >= 3:
                storage_name = parts[0]
                storage_type = parts[1] if len(parts) > 1 else ""
                status = parts[2] if len(parts) > 2 else "active"
                
                storages.append(
                    ProxmoxStorageSpec(
                        storage=storage_name,
                        content="",  # Not available in text format
                        storage_type=storage_type,
                        total=0,  # Not available in text format
                        used=0,
                        available=0,
                        shared=False,
                        active=status.lower() == "active",
                    )
                )
        
        return storages

    def list_bridges(self) -> list[ProxmoxBridgeSpec]:
        """List bridges including SDN VNets and standard bridges."""
        api = self._api_client()
        if api is not None:
            try:
                # First try to get SDN VNets
                bridges = []
                try:
                    sdn_data = api.cluster.sdn.vnets.get()
                    log.info(f"Found {len(sdn_data)} SDN VNets")
                    for vnet in sdn_data:
                        if isinstance(vnet, dict):
                            bridges.append(
                                ProxmoxBridgeSpec(
                                    name=str(vnet.get("vnet", "")),
                                    active=bool(vnet.get("active", False)),
                                    vlan_aware=False,  # VNets handle VLANs differently
                                    bridge_ports=str(vnet.get("tag", "")),  # Store VLAN tag here
                                    comments=f"SDN VNet - Type: {vnet.get('type', 'unknown')}",
                                )
                            )
                except Exception as sdn_exc:
                    log.debug(f"SDN VNets not available (likely not enabled): {sdn_exc}")
                
                # Then get standard network interfaces using actual node name
                actual_node = self._get_actual_node_name(api)
                net_data = api.nodes(actual_node).network.get()
                standard_bridges = self._parse_bridges(self._normalise_network_data(net_data))
                bridges.extend(standard_bridges)
                
                log.info(f"Total bridges found: {len(bridges)} (SDN + standard)")
                return bridges
            except Exception as exc:  # noqa: BLE001
                log.warning("API network query failed, falling back to SSH CLI: %s", exc)
        
        # CLI fallback: same rule — never use "-format=json" (triggers PVE 7/8 boolean parse error).
        actual_node = self._get_actual_node_name(api) if api else self.node
        try:
            # PVE 7+ --output-format as a separate token
            proc = self._run(["pvesh", "get", f"/nodes/{actual_node}/network", "--output-format", "json"])
            raw = json.loads(proc.stdout or "[]")
            return self._parse_bridges(self._normalise_network_data(raw))
        except ProxmoxClientError as exc:
            _exc_str = str(exc)
            if "Unknown option" in _exc_str or "unable to parse" in _exc_str or "400" in _exc_str:
                log.warning("pvesh --output-format not supported for network, falling back to ip link show: %s", exc)
                return self._parse_bridges_text(self._run(["ip", "link", "show"]))
            raise

    @staticmethod
    def _normalise_network_data(data: Any) -> list[dict[str, Any]]:
        """pvesh returns a dict keyed by iface name; the REST API returns a list.
        Normalise both into a list[dict] with an 'iface' key present."""
        if isinstance(data, dict):
            result = []
            for iface_name, row in data.items():
                if isinstance(row, dict):
                    entry = dict(row)
                    entry.setdefault("iface", iface_name)
                    result.append(entry)
            return result
        if isinstance(data, list):
            return data
        return []

    _BRIDGE_TYPES = {"bridge", "OVSBridge", "vnet"}  # include SDN vnets and OVS bridges

    @classmethod
    def _parse_bridges(cls, data: list[dict[str, Any]]) -> list[ProxmoxBridgeSpec]:
        bridges: list[ProxmoxBridgeSpec] = []
        for row in data:
            iface = str(row.get("iface", "") or row.get("name", "")).strip()
            if not iface:
                continue
            row_type = str(row.get("type", ""))
            if row_type not in cls._BRIDGE_TYPES:
                continue
            bridges.append(
                ProxmoxBridgeSpec(
                    name=iface,
                    active=bool(row.get("active", False)),
                    vlan_aware=bool(int(row.get("vlan_aware", 0) or 0)),
                    bridge_ports=str(row.get("bridge_ports", "") or ""),
                    comments=str(row.get("comments", "") or ""),
                )
            )
        return bridges

    @staticmethod
    def _parse_bridges_text(proc: subprocess.CompletedProcess[str]) -> list[ProxmoxBridgeSpec]:
        """Parse plain text output from ip link show for very old Proxmox versions"""
        bridges: list[ProxmoxBridgeSpec] = []
        lines = proc.stdout.strip().split('\n')
        
        for line in lines:
            if not line.strip():
                continue
            
            # Look for bridge interfaces
            if 'bridge' in line.lower() or line.strip().startswith('vmbr'):
                parts = line.split()
                if parts:
                    iface = parts[1].rstrip(':') if len(parts) > 1 else parts[0]
                    if iface.startswith('vmbr') or 'bridge' in iface.lower():
                        bridges.append(
                            ProxmoxBridgeSpec(
                                name=iface,
                                active='UP' in line.upper(),
                                vlan_aware=False,  # Cannot determine from ip link output
                                bridge_ports="",
                                comments="Detected via ip link show",
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
        
        # pvesh get /cluster/nextid returns a plain integer on all PVE versions.
        # Do NOT use "-format=text" — PVE 7/8 treats -format as a boolean flag
        # and rejects the value with "400 unable to parse boolean option".
        proc = self._run(["pvesh", "get", "/cluster/nextid"])
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
    ) -> str:
        safe_name = _sanitize_vm_name(name, fallback=f"vm-{vmid}")
        args = [
            "qm",
            "create",
            str(vmid),
            "--name",
            safe_name,
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
        return safe_name

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
        # Try the English success message first (any capitalisation)
        for line in output.splitlines()[::-1]:
            lower = line.lower()
            if "successfully imported disk as" in lower or "imported disk as" in lower:
                return line.split("as", 1)[1].strip().strip("'\"")
        # Fallback: grep for a volume-id pattern like "local-lvm:vm-100-disk-0"
        import re as _re
        for line in output.splitlines()[::-1]:
            m = _re.search(r"(\S+:\S+-\d+-disk-\d+)", line)
            if m:
                return m.group(1)
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
        try:
            client = self._get_ssh_client()
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
        except Exception as exc:  # noqa: BLE001
            self._close_ssh()
            raise ProxmoxClientError(f"SFTP listing failed: {exc}") from exc

    def read_remote_file(self, remote_path: str) -> str:
        """Read a text file from the Proxmox HOST via SFTP. Returns empty string on error."""
        if not self.ssh_enabled:
            try:
                return Path(remote_path).read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return ""
        try:
            client = self._get_ssh_client()
            sftp = client.open_sftp()
            with sftp.open(remote_path, "r") as fh:
                return fh.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            self._close_ssh()
            return ""

    # ------------------------------------------------------------------
    # Archive extraction (host-side via SSH to avoid pulling GB into LXC)
    # ------------------------------------------------------------------

    _EXTRACT_CMDS: dict[str, list[str]] = {
        "zip":      ["unzip", "-o", "{archive}", "-d", "{dest}"],
        "7z":       ["7z", "x", "-y", "-o{dest}", "{archive}"],
        "tar":      ["tar", "--no-same-owner", "-xf",  "{archive}", "-C", "{dest}"],
        "tar.gz":   ["tar", "--no-same-owner", "-xzf", "{archive}", "-C", "{dest}"],
        "tar.bz2":  ["tar", "--no-same-owner", "-xjf", "{archive}", "-C", "{dest}"],
        "tar.xz":   ["tar", "--no-same-owner", "-xJf", "{archive}", "-C", "{dest}"],
        "tar.zst":  ["tar", "--no-same-owner", "--use-compress-program=zstd", "-xf", "{archive}", "-C", "{dest}"],
    }

    def extract_archive(self, remote_archive: str, dest_dir: str) -> str:
        """Extract an archive on the Proxmox HOST (via SSH) into *dest_dir*.

        Args:
            remote_archive: Host-absolute path to the archive file.
            dest_dir:        Host-absolute path to the destination directory
                             (will be created if it does not exist).

        Returns:
            The *dest_dir* path so callers can chain directly.

        Raises:
            ProxmoxClientError: if the archive type is unrecognised or extraction fails.
        """
        from .disk import detect_archive_type  # local import to avoid circular
        archive_type = detect_archive_type(remote_archive)
        if archive_type is None:
            raise ProxmoxClientError(
                f"Cannot extract '{remote_archive}': unrecognised archive type. "
                "Supported: .zip, .7z, .tar, .tar.gz, .tar.bz2, .tar.xz, .tar.zst"
            )
        template = self._EXTRACT_CMDS.get(archive_type)
        if template is None:
            raise ProxmoxClientError(f"No extraction command configured for type '{archive_type}'")

        # Ensure destination directory exists on the host
        self._run(["mkdir", "-p", dest_dir])

        cmd = [
            part.replace("{archive}", remote_archive).replace("{dest}", dest_dir)
            for part in template
        ]
        log.info("Extracting %s (%s) → %s", remote_archive, archive_type, dest_dir)
        self._run(cmd)
        return dest_dir

    def peek_archive(self, remote_archive: str) -> list[str]:
        """Return a list of filenames inside a remote archive (SSH, no extraction).

        Supports .zip (unzip -l), .7z (7z l), .tar/.tar.gz etc. (tar -tf).
        Returns an empty list when the type is unrecognised or the command fails.
        """
        from .disk import detect_archive_type
        atype = detect_archive_type(remote_archive)
        if atype is None:
            return []
        if atype == "zip":
            cmd = ["unzip", "-l", remote_archive]
            parse = "zip"
        elif atype == "7z":
            cmd = ["7z", "l", "-ba", remote_archive]
            parse = "7z"
        else:
            # all tar variants
            cmd = ["tar", "-tf", remote_archive]
            parse = "tar"

        try:
            out = self._run(cmd)
        except Exception:  # noqa: BLE001
            return []

        lines = out.splitlines()
        filenames: list[str] = []
        if parse == "tar":
            filenames = [l.strip() for l in lines if l.strip()]
        elif parse == "zip":
            # skip header/footer lines; entries look like "  length  date  time  name"
            for line in lines[3:]:
                parts = line.split()
                if len(parts) >= 4:
                    filenames.append(parts[-1])
        elif parse == "7z":
            # 7z -ba lists: "attr  size  date time  name"
            for line in lines:
                parts = line.split()
                if len(parts) >= 5:
                    filenames.append(parts[-1])
        return filenames

    def remove_remote_dir(self, remote_dir: str) -> None:
        """Recursively delete a directory on the Proxmox HOST via SSH.

        Uses 'rm -rf' — call only on temp directories created by extract_archive.
        """
        _SAFE_PREFIXES = ("/tmp/", "/var/tmp/", "/mnt/", "/data/", "/root/tmp/")
        norm = remote_dir.rstrip("/")
        if not norm or not any(norm.startswith(p.rstrip("/")) for p in _SAFE_PREFIXES):
            raise ProxmoxClientError(
                f"Refusing to delete '{remote_dir}': path must be inside "
                f"a safe temp directory ({', '.join(_SAFE_PREFIXES)})"
            )
        log.info("Removing host temp directory %s", remote_dir)
        self._run(["rm", "-rf", remote_dir])
