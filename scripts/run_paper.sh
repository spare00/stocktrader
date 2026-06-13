#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Start from a clean process environment by default so stale shell/tmux exports
# cannot override `.env` or `profiles/*.env`. Preserve only runner controls that
# are intentionally passed on the command line.
if [[ "${RUN_PAPER_CLEAN_ENV_DONE:-}" != "1" && "${RUN_PAPER_CLEAN_ENV:-true}" != "false" ]]; then
  _clean_env=(
    "RUN_PAPER_CLEAN_ENV_DONE=1"
    "HOME=${HOME:-}"
    "PATH=${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
    "SHELL=${SHELL:-/bin/bash}"
    "PWD=$ROOT"
    "TMPDIR=${TMPDIR:-/tmp}"
  )
  [[ -n "${PROFILE:-}" ]] && _clean_env+=("PROFILE=$PROFILE")
  [[ -n "${TUNING_PROFILE:-}" ]] && _clean_env+=("TUNING_PROFILE=$TUNING_PROFILE")
  [[ -n "${PYTHON_BIN:-}" ]] && _clean_env+=("PYTHON_BIN=$PYTHON_BIN")
  exec env -i "${_clean_env[@]}" "$SCRIPT_PATH" "$@"
fi

load_env_file() {
  local file="$1"
  local example="$2"

  if [[ ! -f "$file" ]]; then
    echo "Missing $file. Create it from $example first." >&2
    echo "Example: cp $example $file" >&2
    exit 1
  fi

  set -a
  # shellcheck disable=SC1090
  source "$file"
  set +a
}

load_env_file ".env" ".env.example"
load_env_file "profiles/paper.env" "profiles/paper.env.example"

if [[ -n "${PROFILE:-}" && "$PROFILE" != "paper" ]]; then
  if [[ ! "$PROFILE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
    echo "Invalid PROFILE=$PROFILE (use letters, digits, underscore, hyphen only)." >&2
    exit 1
  fi
  _named_profile="profiles/${PROFILE}.env"
  _named_example="profiles/paper.env.example"
  [[ -f "${_named_profile}.example" ]] && _named_example="${_named_profile}.example"
  load_env_file "$_named_profile" "$_named_example"
fi
if [[ -n "${TUNING_PROFILE:-}" ]]; then
  _tuning_example="profiles/paper.env.example"
  [[ -f "${TUNING_PROFILE}.example" ]] && _tuning_example="${TUNING_PROFILE}.example"
  load_env_file "$TUNING_PROFILE" "$_tuning_example"
fi

: "${EXECUTION_MODE:=alpaca_paper}"
if [[ "$EXECUTION_MODE" != "alpaca_paper" ]]; then
  echo "Refusing to run paper wrapper with EXECUTION_MODE=$EXECUTION_MODE. This script targets Alpaca paper (or a mock with the same API). Set EXECUTION_MODE=alpaca_paper in profiles/paper.env." >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing Python runtime: $PYTHON_BIN" >&2
  echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec "$PYTHON_BIN" main.py "$@"
