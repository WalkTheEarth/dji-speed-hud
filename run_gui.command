#!/usr/bin/env bash
# macOS double-click launcher.
cd "$(dirname "$0")" || exit 1
if [ -x "./venv/bin/python" ]; then
    exec "./venv/bin/python" gui.py "$@"
fi
exec python3 gui.py "$@"
