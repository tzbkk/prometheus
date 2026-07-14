#!/bin/bash
set -e

# Prometheus installer: build a portable patched QQ from an AppImage.
# Usage: bash scripts/setup.sh [--target=qq|viewer|all] [/path/to/QQ.AppImage]
# All paths/tunables come from conf/prometheus.conf.json (via _envconfig.py).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
eval "$(python3 "$PROJECT_DIR/src/prometheus/_envconfig.py")"

# Parse --target argument
TARGET="qq"
APPIMAGE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target=*)
            TARGET="${1#*=}"
            shift
            ;;
        --target)
            TARGET="$2"
            shift 2
            ;;
        *)
            if [[ "$1" == --* ]]; then
                echo "Error: Unknown option '$1'" >&2
                echo "Usage: bash scripts/setup.sh [--target=qq|viewer|all] [/path/to/QQ.AppImage]" >&2
                exit 1
            fi
            APPIMAGE="$1"
            shift
            ;;
    esac
done

# Validate target
case "$TARGET" in
    qq|viewer|all)
        ;;
    *)
        echo "Error: Invalid target '$TARGET'. Must be qq, viewer, or all." >&2
        echo "Usage: bash scripts/setup.sh [--target=qq|viewer|all] [/path/to/QQ.AppImage]" >&2
        exit 1
        ;;
esac

# Function: setup_qq
setup_qq() {
    local APPIMAGE="$1"

    if [ -z "$APPIMAGE" ]; then
        APPIMAGE="${PROMETHEUS_APPIMAGE/#\~/$HOME}"
    fi
    if [ -z "$APPIMAGE" ]; then
        echo "Error: AppImage path not set. Pass it as arg or set 'appimage' in conf/prometheus.conf.json" >&2
        exit 1
    fi
    PATCHED_DIR="${PROMETHEUS_PATCHED_DIR/#\~/$HOME}"
    [ -z "$PATCHED_DIR" ] && PATCHED_DIR="$PROJECT_DIR/qq_patched"
    [[ "$PATCHED_DIR" != /* ]] && PATCHED_DIR="$PROJECT_DIR/$PATCHED_DIR"

    echo "=== Prometheus QQ Setup ==="
    echo "AppImage: $APPIMAGE"
    echo "Project:  $PROJECT_DIR"
    echo "Target:   $PATCHED_DIR"
    echo "QQ ver:   $PROMETHEUS_QQ_VERSION"

    if [ -d "$PATCHED_DIR" ]; then
        echo "[1/3] $PATCHED_DIR already exists, skipping extract"
    else
        echo "[1/3] Extracting AppImage..."
        TMPDIR=$(mktemp -d)
        cd "$TMPDIR"
        "$APPIMAGE" --appimage-extract
        cp -r squashfs-root "$PATCHED_DIR"
        rm -rf "$TMPDIR"
        echo "      Extracted to: $PATCHED_DIR"
    fi

    PKG="$PATCHED_DIR/resources/app/package.json"
    PKG_VER=$(python3 -c "import json; print(json.load(open('$PKG')).get('version','?'))")
    echo "      QQ version: $PKG_VER"

    echo "[2/3] Injecting prometheus.js + modules..."
    cp "$PROJECT_DIR/src/prometheus/inject.js" "$PATCHED_DIR/resources/app/app_launcher/prometheus.js"
    cp "$PROJECT_DIR/src/prometheus/logger.js" "$PATCHED_DIR/resources/app/app_launcher/logger.js"
    cp "$PROJECT_DIR/src/prometheus/lock.js" "$PATCHED_DIR/resources/app/app_launcher/lock.js"
    cp "$PROJECT_DIR/src/prometheus/api-server.js" "$PATCHED_DIR/resources/app/app_launcher/api-server.js"
    python3 -c "
import json
with open('$PKG') as f: pkg = json.load(f)
pkg['main'] = './app_launcher/prometheus.js'
with open('$PKG', 'w') as f: json.dump(pkg, f, indent=2, ensure_ascii=False)
print(f'      main -> {pkg[\"main\"]}')
"

    echo "[3/3] Setting permissions..."
    chmod +x "$PATCHED_DIR/qq" 2>/dev/null || true
    chmod +x "$SCRIPT_DIR/start_qq.sh"
    chmod +x "$PROJECT_DIR/src/prometheus/autoscroll.py"

    mkdir -p "$PROJECT_DIR/data"

    echo ""
    echo "=== QQ Setup complete ==="
    echo "Start:    bash $SCRIPT_DIR/start_qq.sh"
    echo "Data:     $PROJECT_DIR/data/feeds.jsonl"
    echo "Log:      tail -f $PROJECT_DIR/data/prometheus.log"
}

# Function: setup_viewer
setup_viewer() {
    echo "=== Viewer Setup ==="

    # Check if node and npm exist
    if ! command -v node &> /dev/null; then
        echo "Error: node is required for viewer build" >&2
        exit 1
    fi

    if ! command -v npm &> /dev/null; then
        echo "Error: npm is required for viewer build" >&2
        exit 1
    fi

    echo "[1/3] Installing frontend dependencies..."
    cd "$PROJECT_DIR/src/viewer/frontend"
    npm install

    echo "[2/3] Building frontend..."
    npm run build

    echo "[3/3] Verifying build output..."
    if [ ! -f "$PROJECT_DIR/src/viewer/static/index.html" ]; then
        echo "Error: Build output not found at src/viewer/static/index.html" >&2
        exit 1
    fi

    echo ""
    echo "=== Viewer Setup complete ==="
    echo "Static files: $PROJECT_DIR/src/viewer/static/"
}

# Dispatch based on target
case "$TARGET" in
    qq)
        setup_qq "$APPIMAGE"
        ;;
    viewer)
        setup_viewer
        ;;
    all)
        setup_qq "$APPIMAGE"
        setup_viewer
        ;;
esac
