#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Watch list, strategies, and other tunables come only from `.env` and `profiles/*.env` (sourced below).
# Never inherit SYMBOLS or STRATEGIES from a parent shell or tmux — stale exports can override
# strategy plan files or select the wrong strategy. To set them, add them to a profile or `.env`.
unset SYMBOLS
unset STRATEGIES

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

# Tuning profile: full path, or PROFILE=test -> profiles/test.env (letters, digits, _ - only).
if [[ -n "${TUNING_PROFILE:-}" ]]; then
  _tuning_env="$TUNING_PROFILE"
elif [[ -n "${PROFILE:-}" ]]; then
  if [[ ! "$PROFILE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
    echo "Invalid PROFILE=$PROFILE (use letters, digits, underscore, hyphen only)." >&2
    exit 1
  fi
  _tuning_env="profiles/${PROFILE}.env"
else
  _tuning_env="profiles/paper.env"
fi
if [[ "$_tuning_env" == "profiles/test.env" ]]; then
  _tuning_example="profiles/test.env.example"
else
  _tuning_example="profiles/paper.env.example"
fi
load_env_file "$_tuning_env" "$_tuning_example"

: "${EXECUTION_MODE:=alpaca_paper}"
if [[ "$EXECUTION_MODE" != "alpaca_paper" ]]; then
  echo "Refusing to run paper wrapper with EXECUTION_MODE=$EXECUTION_MODE. This script targets Alpaca paper (or a mock with the same API). Set EXECUTION_MODE=alpaca_paper in $_tuning_env." >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing Python runtime: $PYTHON_BIN" >&2
  echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec "$PYTHON_BIN" main.py "$@"
