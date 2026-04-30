#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

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
load_env_file "${TUNING_PROFILE:-profiles/paper.env}" "profiles/paper.env.example"

: "${EXECUTION_MODE:=alpaca_paper}"
if [[ "$EXECUTION_MODE" != "alpaca_paper" ]]; then
  echo "Refusing to run paper wrapper with EXECUTION_MODE=$EXECUTION_MODE. Set EXECUTION_MODE=alpaca_paper in ${TUNING_PROFILE:-profiles/paper.env}." >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing Python runtime: $PYTHON_BIN" >&2
  echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec "$PYTHON_BIN" main.py "$@"
