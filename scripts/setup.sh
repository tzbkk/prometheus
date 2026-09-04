#!/bin/bash
set -e

# Prometheus installer: build the Viewer frontend.
# Usage: bash scripts/setup.sh [--target=viewer]
#
# Viewer build is the only remaining target.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Parse --target argument
TARGET="viewer"
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
            echo "Error: Unknown argument '$1'" >&2
            echo "Usage: bash scripts/setup.sh [--target=viewer]" >&2
            exit 1
            ;;
    esac
done

if [ "$TARGET" != "viewer" ]; then
    echo "Error: Invalid target '$TARGET'. Only --target=viewer remains." >&2
    echo "Usage: bash scripts/setup.sh [--target=viewer]" >&2
    exit 1
fi

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

setup_viewer
