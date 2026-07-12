#!/bin/bash
# Launch patched QQ with Prometheus injection.
# Usage: bash scripts/start_qq.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Prerequisite checks ---
QQ_BIN="$PROJECT_DIR/qq_patched/qq"
if [ ! -x "$QQ_BIN" ]; then
    echo "ERROR: Patched QQ not found at $QQ_BIN" >&2
    echo "       Run setup first: bash scripts/setup.sh /path/to/QQ_*.AppImage" >&2
    exit 1
fi

if [ ! -f "$PROJECT_DIR/conf/prometheus.conf.json" ]; then
    echo "ERROR: conf/prometheus.conf.json not found in $PROJECT_DIR" >&2
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found in PATH" >&2
    exit 1
fi

# --- Duplicate launch protection ---
EXISTING_PID=$(pgrep -f "[q]q_patched/qq --ozone" 2>/dev/null | head -1)
if [ -n "$EXISTING_PID" ]; then
    echo "ERROR: QQ already running (PID $EXISTING_PID). Kill it first:" >&2
    echo "       kill $EXISTING_PID" >&2
    exit 1
fi

# --- Environment setup ---
set -a
eval "$(python3 "$PROJECT_DIR/src/prometheus/_envconfig.py")"
set +a

PATCHED_DIR="${PROMETHEUS_PATCHED_DIR/#\~/$HOME}"
[ -z "$PATCHED_DIR" ] && PATCHED_DIR="$PROJECT_DIR/qq_patched"
[[ "$PATCHED_DIR" != /* ]] && PATCHED_DIR="$PROJECT_DIR/$PATCHED_DIR"
export PROMETHEUS_DATA="$PROJECT_DIR/data"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
export HOME="${HOME:-$(eval echo ~)}"
export YDOTOOL_SOCKET="$PROMETHEUS_YDOTOOL_SOCKET"
export OZONE_PLATFORM="$PROMETHEUS_OZONE_PLATFORM"

mkdir -p "$PROMETHEUS_DATA"

echo "=== Prometheus QQ Archive ==="
echo "Channel:  $PROMETHEUS_CHANNEL_NAME ($PROMETHEUS_CHANNEL_ID)"
echo "Data:     $PROMETHEUS_DATA"
echo "Log:      tail -f $PROMETHEUS_DATA/prometheus.log"
echo "============================"

exec "$PATCHED_DIR/qq" --ozone-platform="$OZONE_PLATFORM" --no-sandbox --disable-background-networking
