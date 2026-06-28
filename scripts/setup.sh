#!/bin/bash
set -e

# Prometheus installer: build a portable patched QQ from an AppImage.
# Usage: bash scripts/setup.sh [/path/to/QQ.AppImage]
# All paths/tunables come from prometheus.conf.json (via _envconfig.py).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
eval "$(python3 "$PROJECT_DIR/src/prometheus/_envconfig.py")"

APPIMAGE="${1:-${PROMETHEUS_APPIMAGE/#\~/$HOME}}"
if [ -z "$APPIMAGE" ]; then
    echo "Error: AppImage path not set. Pass it as arg or set 'appimage' in prometheus.conf.json" >&2
    exit 1
fi
PATCHED_DIR="${PROMETHEUS_PATCHED_DIR/#\~/$HOME}"
[ -z "$PATCHED_DIR" ] && PATCHED_DIR="$PROJECT_DIR/qq_patched"
[[ "$PATCHED_DIR" != /* ]] && PATCHED_DIR="$PROJECT_DIR/$PATCHED_DIR"

echo "=== Prometheus Setup ==="
echo "AppImage: $APPIMAGE"
echo "Project:  $PROJECT_DIR"
echo "Target:   $PATCHED_DIR"
echo "QQ ver:   $PROMETHEUS_QQ_VERSION"

if [ -d "$PATCHED_DIR" ]; then
    echo "[1/4] $PATCHED_DIR already exists, skipping extract"
else
    echo "[1/4] Extracting AppImage..."
    TMPDIR=$(mktemp -d)
    cd "$TMPDIR"
    "$APPIMAGE" --appimage-extract
    cp -r squashfs-root "$PATCHED_DIR"
    rm -rf "$TMPDIR"
    echo "      Extracted to: $PATCHED_DIR"
fi

echo "[2/4] Injecting prometheus.js..."
cp "$PROJECT_DIR/src/prometheus/inject.js" "$PATCHED_DIR/resources/app/app_launcher/prometheus.js"

echo "[3/4] Patching package.json..."
cp "$PROJECT_DIR/src/prometheus/package_patched.json" "$PATCHED_DIR/resources/app/package.json"

echo "[4/4] Setting permissions..."
chmod +x "$PATCHED_DIR/qq" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/start_qq.sh"
chmod +x "$PROJECT_DIR/src/prometheus/autoscroll.py"

mkdir -p "$PROJECT_DIR/data"

echo ""
echo "=== Setup complete ==="
echo "Start:    bash $SCRIPT_DIR/start_qq.sh"
echo "Data:     $PROJECT_DIR/data/feeds.jsonl"
echo "Log:      tail -f $PROJECT_DIR/data/prometheus.log"
