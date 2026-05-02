#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# If SYMBOLS was already set when this script started (e.g. SYMBOLS=RIG ./run_paper.sh),
# restore it after sourcing only when the tuning profile does *not* assign SYMBOLS.
# Otherwise a shell inherited SYMBOLS=AAPL,... would overwrite RIG from profiles/test.env.
_cmdline_symbols_set=0
if [ -n "${SYMBOLS+x}" ]; then
  _cmdline_saved_symbols="$SYMBOLS"
  _cmdline_symbols_set=1
fi

_profile_defines_symbols() {
  local f="$1"
  [[ -f "$f" ]] && grep -qE '^[[:space:]]*(export[[:space:]]+)?SYMBOLS[[:space:]]*=' "$f"
}

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

if [ "$_cmdline_symbols_set" = 1 ] && ! _profile_defines_symbols "$_tuning_env"; then
  export SYMBOLS="$_cmdline_saved_symbols"
fi

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
