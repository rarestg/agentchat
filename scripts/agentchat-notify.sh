#!/usr/bin/env bash

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SELF_DIR}/.." && pwd)"

EVENT_JSON="${1:-}"
if [[ -n "${EVENT_JSON}" ]]; then
  EVENT_TYPE="$(EVENT_JSON="${EVENT_JSON}" python3 -c '
import json
import os
try:
    payload = json.loads(os.environ.get("EVENT_JSON", ""))
except Exception:
    print("__skip__")
    raise SystemExit(0)
value = payload.get("type")
print(value if isinstance(value, str) else "__skip__")
')"
  if [[ "${EVENT_TYPE}" != "agent-turn-complete" ]]; then
    exit 0
  fi
fi

if ! command -v git >/dev/null 2>&1; then
  exit 0
fi

if [[ -z "${AGENTCHAT_NAME:-}" ]]; then
  exit 0
fi

PROJECT_KEY="$(git -C "${PWD}" rev-parse --show-toplevel 2>/dev/null || pwd -P)"
BOOTSTRAP_PATH="${PROJECT_KEY}/.codex/agentchat/${AGENTCHAT_NAME}.json"

if [[ ! -f "${BOOTSTRAP_PATH}" ]]; then
  exit 0
fi

PYTHON_BIN="python3"
if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
fi

STATE_KEY="$("${PYTHON_BIN}" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest())' "${BOOTSTRAP_PATH}" 2>/dev/null)"
STATE_FILE="${TMPDIR:-/tmp}/agentchat-notify-${STATE_KEY}.json"

if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  "${PROJECT_DIR}/.venv/bin/python" -W ignore -m agentchat check \
    "${BOOTSTRAP_PATH}" \
    --notify \
    --state-file "${STATE_FILE}" \
    --min-interval-seconds 120 \
    2>/dev/null
else
  uv run --project "${PROJECT_DIR}" python -W ignore -m agentchat check \
    "${BOOTSTRAP_PATH}" \
    --notify \
    --state-file "${STATE_FILE}" \
    --min-interval-seconds 120 \
    2>/dev/null
fi
