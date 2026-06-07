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

run_agentchat() {
  if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
    "${PROJECT_DIR}/.venv/bin/python" -W ignore -m agentchat "$@"
    return
  fi
  uv run --project "${PROJECT_DIR}" python -W ignore -m agentchat "$@"
}

BOOTSTRAP_PATH="$(run_agentchat bootstrap-path "${PROJECT_KEY}" "${AGENTCHAT_NAME}" 2>/dev/null || true)"

if [[ -z "${BOOTSTRAP_PATH}" || ! -f "${BOOTSTRAP_PATH}" ]]; then
  exit 0
fi

run_agentchat check \
  "${BOOTSTRAP_PATH}" \
  --notify \
  --min-interval-seconds 120 \
  2>/dev/null
