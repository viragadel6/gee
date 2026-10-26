#!/usr/bin/env bash
# Build the site cloner with Zig 0.14.x, optionally running it afterwards.
#
# Usage:
#   ./site_cloner/build.sh                      # build only
#   ./site_cloner/build.sh https://example.com  # build, then run in ./out
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if command -v zig >/dev/null 2>&1; then
  ZIG=(zig)
elif [ -x "$REPO_ROOT/.venv-zig/bin/python" ]; then
  ZIG=("$REPO_ROOT/.venv-zig/bin/python" -m ziglang)
elif python3 -c "import ziglang" >/dev/null 2>&1; then
  ZIG=(python3 -m ziglang)
else
  echo "error: Zig 0.14.x not found." >&2
  echo "Install the bundled toolchain with:  pip install ziglang==0.14.0" >&2
  exit 1
fi

"${ZIG[@]}" build-exe "$SCRIPT_DIR/main.zig" -O ReleaseSafe \
  -femit-bin="$SCRIPT_DIR/site_cloner"

if [ "$#" -gt 0 ]; then
  mkdir -p "$REPO_ROOT/out"
  cd "$REPO_ROOT/out"
  exec "$SCRIPT_DIR/site_cloner" "$@"
fi
