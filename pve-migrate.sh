#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="python3"
PIP_BIN="pip3"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "python3 is required on the Proxmox host" >&2
  exit 1
fi

if ! command -v "${PIP_BIN}" >/dev/null 2>&1; then
  echo "pip3 is required on the Proxmox host" >&2
  exit 1
fi

if [ ! -d "${VENV_DIR}" ]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null
"${VENV_DIR}/bin/pip" install -r "${ROOT_DIR}/requirements.txt" >/dev/null
"${VENV_DIR}/bin/pip" install -e "${ROOT_DIR}" >/dev/null

exec "${VENV_DIR}/bin/python" -m vmware_to_proxmox.cli "$@"
