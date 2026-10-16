#!/bin/sh
# Reclaimer Supervisor: the one way to start your servers from now on.
# Run ./start.sh (or sh start.sh), or have a systemd unit run it at boot.
# The same script runs the supervisor's other commands:
#   ./start.sh check          check the settings, servers and commands
#   ./start.sh stop           stop the supervisor and the servers it runs
#   ./start.sh update         look for a new version, and install it if you say yes
#   ./start.sh rcon players   run one console command on the server
# The first start sets up Python packages in engine/.venv (needs Python 3.11+).
set -e
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"
engine="$here/engine"
venv="$engine/.venv"

if [ ! -x "$venv/bin/python" ]; then
    base=""
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            base=$candidate
            break
        fi
    done
    if [ -z "$base" ]; then
        echo "Reclaimer Supervisor needs Python 3.11 or newer (Debian/Ubuntu: sudo apt install python3 python3-venv)." >&2
        exit 1
    fi
    echo "Setting up for the first time: Python packages go in engine/.venv"
    if ! "$base" -m venv "$venv"; then
        rm -rf "$venv"
        echo "Making engine/.venv failed. On Debian/Ubuntu: sudo apt install python3-venv, then start again." >&2
        exit 1
    fi
fi
if ! cmp -s "$engine/requirements.txt" "$venv/requirements.txt"; then
    echo "Installing the Python packages the engine needs..."
    "$venv/bin/python" -m pip install --disable-pip-version-check --quiet -r "$engine/requirements.txt"
    cp "$engine/requirements.txt" "$venv/requirements.txt"
fi

[ $# -eq 0 ] && set -- start
export PYTHONPATH="$engine" RECLAIMER_LAUNCHER=./start.sh
exec "$venv/bin/python" -m reclaimer_supervisor "$@"
