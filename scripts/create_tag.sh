#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

usage() {
  echo "Usage: scripts/create_tag.sh <tag name>" >&2
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

TAG_NAME="$1"

if ! git check-ref-format --allow-onelevel "refs/tags/$TAG_NAME"; then
  echo "Invalid tag name: $TAG_NAME" >&2
  exit 2
fi

if git rev-parse -q --verify "refs/tags/$TAG_NAME" >/dev/null; then
  echo "Tag already exists: $TAG_NAME" >&2
  exit 1
fi

if ! git diff --cached --quiet; then
  echo "Refusing to create tag while unrelated changes are already staged." >&2
  echo "Commit or unstage them first, then rerun scripts/create_tag.sh." >&2
  exit 1
fi

for file in profiles/test.env profiles/paper.env; do
  if [[ ! -f "$file" ]]; then
    echo "Missing $file; cannot snapshot profiles." >&2
    exit 1
  fi
done

cp profiles/test.env profiles/test.env.example
cp profiles/paper.env profiles/paper.env.example

git add profiles/test.env.example profiles/paper.env.example

if git diff --cached --quiet; then
  echo "Profile examples already match current profiles; creating an empty tag marker commit."
  git commit --allow-empty -m "Create a new tag"
else
  git commit -m "Create a new tag"
fi

git tag "$TAG_NAME"

echo "Created tag $TAG_NAME at $(git rev-parse --short HEAD)"
