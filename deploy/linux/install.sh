#!/usr/bin/env bash
# oo-agent installer for systemd-based Linux hosts.
#
# Installs the agent into /opt/oo-agent (own venv), puts the config in
# /etc/oo-agent, registers and starts the systemd unit. Safe to re-run:
# existing config and token are never overwritten.
#
# Usage (from an unpacked source tree):
#   sudo deploy/linux/install.sh [--server URL] [--enroll [CODE]]
#
# Options:
#   --server URL    backend base URL, written into agent.ini
#   --enroll        run the enroll flow after install (IP-claim)
#   --enroll CODE   run the enroll flow with a one-time code
#   --force         re-enroll even when a token is already present
#                   (reinstall over an existing agent: the old token is
#                   discarded and the enroll flow runs again)
#   --src DIR       source tree to install from (default: repo root
#                   relative to this script)
#   --no-start      install everything but do not enable/start the unit
#
# To remove the agent later: oo-agent uninstall

set -euo pipefail

PREFIX=/opt/oo-agent
CONF_DIR=/etc/oo-agent
STATE_DIR=/var/lib/oo-agent
UNIT_DST=/etc/systemd/system/oo-agent.service

SERVER=""
ENROLL=""
DO_ENROLL=0
NO_START=0
FORCE=0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="$2"; shift 2 ;;
        --enroll)
            DO_ENROLL=1
            if [ $# -gt 1 ] && [ "${2#--}" = "$2" ]; then ENROLL="$2"; shift; fi
            shift ;;
        --src) SRC="$2"; shift 2 ;;
        --no-start) NO_START=1; shift ;;
        --force) FORCE=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "error: run as root (sudo)" >&2; exit 2; }
[ -f "$SRC/pyproject.toml" ] || {
    echo "error: $SRC does not look like an oo-agent source tree" >&2; exit 2; }

PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "error: python3 not found" >&2; exit 2; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "error: python3 >= 3.10 required (found $("$PY" -V))" >&2; exit 2; }

echo "installing into $PREFIX ..."
mkdir -p "$PREFIX" "$CONF_DIR" "$STATE_DIR"
chmod 700 "$STATE_DIR"

if [ ! -x "$PREFIX/venv/bin/python" ]; then
    "$PY" -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$SRC[gpu,docker,transport]"

if [ ! -f "$CONF_DIR/agent.ini" ]; then
    install -m 644 "$SRC/agent.ini.example" "$CONF_DIR/agent.ini"
    echo "config: created $CONF_DIR/agent.ini"
else
    echo "config: keeping existing $CONF_DIR/agent.ini"
fi

if [ -n "$SERVER" ]; then
    # Uncomment/replace the server line in the [transport] section.
    if grep -qE '^\s*;?\s*server\s*=' "$CONF_DIR/agent.ini"; then
        sed -i -E "0,/^\s*;?\s*server\s*=.*/s||server = $SERVER|" \
            "$CONF_DIR/agent.ini"
    else
        printf '\n[transport]\nserver = %s\n' "$SERVER" >> "$CONF_DIR/agent.ini"
    fi
    echo "config: server = $SERVER"
fi

install -m 644 "$SRC/deploy/linux/oo-agent.service" "$UNIT_DST"
systemctl daemon-reload
# The CLI on PATH: `oo-agent update` / `oo-agent uninstall` from anywhere.
ln -sf "$PREFIX/venv/bin/oo-agent" /usr/local/bin/oo-agent

if ! command -v smartctl >/dev/null; then
    echo "note: smartctl not found — install smartmontools for SMART data"
fi

if [ "$DO_ENROLL" -eq 1 ]; then
    if [ -f "$CONF_DIR/agent.token" ] && [ "$FORCE" -eq 0 ]; then
        echo "enroll: token already present, skipping (use --force to re-enroll)"
    else
        if [ -f "$CONF_DIR/agent.token" ]; then
            # Reinstall over an existing agent: stop the old daemon so it
            # does not keep pushing with the token we are about to replace.
            systemctl stop oo-agent.service 2>/dev/null || true
            rm -f "$CONF_DIR/agent.token"
            echo "enroll: discarded the previous token (--force)"
        fi
        "$PREFIX/venv/bin/oo-agent" --enroll ${ENROLL:+"$ENROLL"}
    fi
fi

if [ "$NO_START" -eq 0 ]; then
    systemctl enable --now oo-agent.service
    echo "service: oo-agent enabled and started"
    systemctl --no-pager --lines=0 status oo-agent.service || true
else
    echo "service: installed but not started (--no-start)"
fi

echo "done."
