#!/usr/bin/env bash
# Register MinerWatch with systemd so it starts at boot (Ubuntu, Debian, and
# any other systemd distribution). The Linux counterpart of install-task.ps1.
#
# Installs two services:
#   minerwatch.service      the supervisor (`run`): polls, sleeps, restarts
#   minerwatch-web.service  the dashboard and email alerts (`web`)
# Both restart on failure and run as the user who owns this folder, not root.
#
# Usage (from the minerwatch folder):
#   sudo scripts/install-systemd.sh                     # rehearse: nothing reaches a miner
#   sudo scripts/install-systemd.sh --mode live-sleep   # real sleep/wake, rehearsed restarts
#   sudo scripts/install-systemd.sh --no-web            # supervisor only
#   sudo scripts/install-systemd.sh --uninstall
#
# Modes (same meaning as install-task.ps1):
#   rehearse       Poll and log only, even where miners.yaml sets dry_run: false (default)
#   as-configured  Let each miner's own sleep.dry_run decide
#   live-sleep     Real sleep/wake; watchdog restarts still rehearsed
#   live           Real everything, restarts included
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MODE="rehearse"
CONFIG="miners.yaml"
RUN_AS="$(stat -c %U "$ROOT")"
PORT=8787
WEB=1
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)      MODE="$2"; shift 2 ;;
        --config)    CONFIG="$2"; shift 2 ;;
        --user)      RUN_AS="$2"; shift 2 ;;
        --port)      PORT="$2"; shift 2 ;;
        --no-web)    WEB=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: it writes to /etc/systemd/system." >&2
    exit 1
fi

UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"

if [ "$UNINSTALL" -eq 1 ]; then
    for unit in minerwatch minerwatch-web; do
        if [ -f "$UNIT_DIR/$unit.service" ]; then
            systemctl disable --now "$unit.service" || true
            rm -f "$UNIT_DIR/$unit.service"
            echo "Removed $unit.service"
        fi
    done
    systemctl daemon-reload
    exit 0
fi

case "$MODE" in
    # "rehearse" forces --dry-run rather than passing nothing: passing nothing
    # means "honour each miner's configured dry_run", which can be live.
    rehearse)      RUN_FLAG="--dry-run" ;;
    as-configured) RUN_FLAG="" ;;
    live-sleep)    RUN_FLAG="--live-sleep" ;;
    live)          RUN_FLAG="--live" ;;
    *) echo "Unknown mode: $MODE (rehearse, as-configured, live-sleep, live)" >&2; exit 2 ;;
esac

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "No virtual environment at $ROOT/.venv. Create it first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
fi
if [ ! -f "$CONFIG" ]; then
    echo "Config not found: $CONFIG" >&2
    exit 1
fi
id "$RUN_AS" >/dev/null 2>&1 || { echo "No such user: $RUN_AS" >&2; exit 1; }

# Validate the config as the service user before anything is installed.
sudo -u "$RUN_AS" "$PY" -m minerwatch -c "$CONFIG" config >/dev/null

mkdir -p "$ROOT/logs"
chown "$RUN_AS" "$ROOT/logs"

write_unit() {  # name description command
    cat > "$UNIT_DIR/$1.service" <<EOF
[Unit]
Description=$2
# Wait for the network: a fleet that reads as unreachable on the first poll is noise.
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_AS
WorkingDirectory=$ROOT
Environment=PYTHONUTF8=1
Environment=PYTHONUNBUFFERED=1
ExecStart=$3
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
}

write_unit minerwatch "MinerWatch supervisor (schedule, sleep, watchdog)" \
    "$PY -m minerwatch -c $CONFIG --log-file logs/minerwatch.log run $RUN_FLAG"
UNITS="minerwatch.service"

if [ "$WEB" -eq 1 ]; then
    write_unit minerwatch-web "MinerWatch dashboard and email alerts" \
        "$PY -m minerwatch -c $CONFIG --log-file logs/web.log web --port $PORT"
    UNITS="$UNITS minerwatch-web.service"
fi

systemctl daemon-reload
# shellcheck disable=SC2086
systemctl enable $UNITS
# shellcheck disable=SC2086
systemctl restart $UNITS

echo
echo "Installed and started: $UNITS"
echo "  Mode     : $MODE"
[ "$MODE" != "rehearse" ] && echo "             (can send real commands to miners; see the mode list in --help)"
echo "  Runs as  : $RUN_AS"
echo "  Folder   : $ROOT"
[ "$WEB" -eq 1 ] && echo "  Dashboard: http://localhost:$PORT/"
echo
echo "Check them:   systemctl status $UNITS"
echo "Follow logs:  journalctl -fu minerwatch   (or tail -f logs/minerwatch.log)"
echo "After editing miners.yaml:  sudo systemctl restart minerwatch"
