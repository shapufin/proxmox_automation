from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from .models import DiskFormat


class DiskConversionError(RuntimeError):
    pass


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_qemu_img() -> None:
    if shutil.which("qemu-img") is None:
        raise DiskConversionError("qemu-img was not found on the Proxmox host")


def detect_disk_format(path: Path) -> str | None:
    ensure_qemu_img()
    proc = subprocess.run(["qemu-img", "info", "--output", "json", str(path)], text=True, capture_output=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "{}").get("format")
    except json.JSONDecodeError:
        return None


def convert_disk(source: Path, target: Path, target_format: DiskFormat, source_format: str = "vmdk") -> Path:
    ensure_qemu_img()
    target.parent.mkdir(parents=True, exist_ok=True)
    args = ["qemu-img", "convert", "-p", "-f", source_format, "-O", target_format.value, str(source), str(target)]
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        raise DiskConversionError(
            f"qemu-img conversion failed:\nCMD: {' '.join(args)}\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
    return target


def qemu_info(path: Path) -> dict[str, object]:
    ensure_qemu_img()
    proc = subprocess.run(["qemu-img", "info", "--output", "json", str(path)], text=True, capture_output=True)
    if proc.returncode != 0:
        raise DiskConversionError(proc.stderr or proc.stdout)
    import json

    return json.loads(proc.stdout or "{}")
