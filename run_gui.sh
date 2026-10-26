#!/usr/bin/env bash
# Linux / macOS launcher.
cd "$(dirname "$0")" || exit 1
if [ -x "./venv/bin/python" ]; then
    exec "./venv/bin/python" gui.py "$@"
fi
if [ -x "./venv/Scripts/python.exe" ]; then
    exec "./venv/Scripts/python.exe" gui.py "$@"
fi
exec python3 gui.py "$@"
