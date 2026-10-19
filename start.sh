#!/bin/sh
# Huragok, the Engineer: the one way to start your servers from now on.
# Run ./start.sh: the window opens and starts every server in SERVERS/.
# Closing the window leaves the servers running; opening it again picks them up.
# The same script runs Huragok's other commands:
#   ./start.sh status                 what each server is doing
#   ./start.sh stop [server] [--now]  stop servers (none named: all, then Huragok)
#   ./start.sh close                  close Huragok; the servers keep running
#   ./start.sh restart <server>       restart a server, warning its players first
#   ./start.sh say all "text"         a chat line on every server
#   ./start.sh rcon players           one console command
#   ./start.sh watch <server>         follow a server's console output
#   ./start.sh check                  check the settings, servers and commands
#   ./start.sh update                 look for a new version, and install it if you say yes
#   ./start.sh start --no-menu        run with no dashboard (for a systemd unit at boot)
# The first start sets up Python packages in engine/.venv (needs Python 3.11+).
set -e
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"
engine="$here/engine"
venv="$engine/.venv"

# A venv whose Python is gone (bin/python left pointing at a Python since upgraded or
# removed) can't run at all: it's made again.
if [ -e "$venv" ] && ! "$venv/bin/python" -c '' 2>/dev/null; then
    echo "engine/.venv doesn't work any more (was Python upgraded or removed?): setting it up again"
    rm -rf "$venv"
fi
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
        echo "Huragok needs Python 3.11 or newer (Debian/Ubuntu: sudo apt install python3 python3-venv)." >&2
        exit 1
    fi
    echo "Setting up for the first time: Python packages go in engine/.venv"
    if ! "$base" -m venv "$venv"; then
        rm -rf "$venv"
        echo "Making engine/.venv failed. On Debian/Ubuntu: sudo apt install python3-venv, then start again." >&2
        exit 1
    fi
    rm -f "$venv/requirements.txt"  # a new venv has none of the packages
fi
install_packages() {
    "$venv/bin/python" -m pip install --disable-pip-version-check --quiet -r "$engine/requirements.txt"
    cp "$engine/requirements.txt" "$venv/requirements.txt"
}
if ! cmp -s "$engine/requirements.txt" "$venv/requirements.txt"; then
    echo "Installing the Python packages the engine needs..."
    install_packages
fi
# Packages marked as installed that don't load: installed again, once.
if ! PYTHONPATH="$engine" "$venv/bin/python" -c 'import huragok, psutil, websockets' 2>/dev/null; then
    echo "Installing the Python packages the engine needs again (they don't load)..."
    install_packages
    if ! PYTHONPATH="$engine" "$venv/bin/python" -c 'import huragok, psutil, websockets'; then
        echo "Huragok doesn't load, even with its Python packages installed again (the error is above). To set" >&2
        echo "the packages up from the start: rm -rf engine/.venv, then start again." >&2
        exit 1
    fi
fi

[ $# -eq 0 ] && set -- start
export PYTHONPATH="$engine" HURAGOK_LAUNCHER=./start.sh
exec "$venv/bin/python" -m huragok "$@"
