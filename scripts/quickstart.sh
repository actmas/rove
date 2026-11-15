#!/usr/bin/env bash
# ==============================================================================
# Rove Workstation · One-Step Local Quickstart (macOS / Linux)
# ==============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "============================================================"
echo "  🚀 Rove · Local-First Multi-Agent AI Coding Workstation"
echo "============================================================"

# 1. Python Virtual Environment
if [ ! -x "${ROOT_DIR}/.venv/bin/python" ]; then
    echo "🐍 Creating Python virtual environment (.venv)..."
    python3 -m venv "${ROOT_DIR}/.venv"
fi

echo "📦 Installing Python runtime dependencies..."
"${ROOT_DIR}/.venv/bin/pip" install --upgrade pip -q
"${ROOT_DIR}/.venv/bin/pip" install -r "${ROOT_DIR}/requirements.txt" -q

# 2. Frontend Build (skipped if pre-built frontend/dist/index.html is already present)
if [ ! -f "${ROOT_DIR}/frontend/dist/index.html" ]; then
    if command -v pnpm >/dev/null 2>&1; then
        echo "🎨 Installing frontend dependencies with pnpm & building..."
        (cd "${ROOT_DIR}/frontend" && pnpm install --frozen-lockfile && pnpm run build)
    elif command -v npm >/dev/null 2>&1; then
        echo "🎨 Installing frontend dependencies with npm & building..."
        (cd "${ROOT_DIR}/frontend" && npm install && npm run build)
    else
        echo "⚠️  Neither pnpm nor npm found; please install Node.js 22+ to rebuild frontend."
    fi
fi

# 3. Initialize Data Store & Auth
echo "🔐 Initializing Rove data store..."
"${ROOT_DIR}/.venv/bin/python" -c "from workstation import store; store.init(); print('Data directory:', store.DATA)"

DATA_DIR="$("${ROOT_DIR}/.venv/bin/python" -c "from workstation import store; print(store.DATA)")"
echo "============================================================"
echo "  ✅ Rove is ready!"
if [ -f "${DATA_DIR}/access-password.txt" ]; then
    echo "  🔑 Initial password file : ${DATA_DIR}/access-password.txt"
fi
echo "  ▶️  Start server now      : ./.venv/bin/python -m uvicorn workstation.server:app --host 127.0.0.1 --port 8765"
echo "  ⚙️  Install background daemon : ./.venv/bin/python scripts/manage.py install"
echo "  🔐 Change access password    : ./.venv/bin/python scripts/manage.py password"
echo "============================================================"
