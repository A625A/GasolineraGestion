#!/usr/bin/env bash
#
# Ensure the user-level Python 3.9 scripts directory is on PATH.
#
# Usage:
#   source scripts/add_local_python_bin.sh
#
# The script is idempotent and can be safely sourced multiple times.

PYTHON_USER_BIN="$HOME/Library/Python/3.9/bin"

if [ ! -d "$PYTHON_USER_BIN" ]; then
  echo "Directory not found: $PYTHON_USER_BIN" >&2
  echo "Install packages with 'python3 -m pip install --user <package>' before running this helper." >&2
  return 1 2>/dev/null || exit 1
fi

case ":$PATH:" in
  *":$PYTHON_USER_BIN:"*)
    echo "PATH already contains $PYTHON_USER_BIN"
    ;;
  *)
    export PATH="$PYTHON_USER_BIN:$PATH"
    echo "Added $PYTHON_USER_BIN to PATH"
    ;;
esac

echo "Current PATH:"
echo "$PATH"
