#!/usr/bin/env bash
# ==============================================================================
# Rove 行走工作台 · PVE 通用主机/LXC 节点一键部署与自愈模版脚本
# 适用环境: Proxmox VE (PVE) Debian 12/13 或 Ubuntu 22.04/24.04 LXC 容器 / VM
# ==============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/walking-workstation}"
DATA_DIR="${WORKSTATION_DATA:-/var/lib/walking-workstation}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/var/lib/walking-workstation/workspaces}"
PORT="${PORT:-8765}"
NODE_NAME="${WORKSTATION_NODE_NAME:-PVE-Universal-Node}"
SERVICE_NAME="walking-workstation"

echo "============================================================"
echo "  🚀 正在部署 Rove 行走工作台 · PVE 通用多 Agent 节点"
echo "  安装目录 : ${INSTALL_DIR}"
echo "  数据目录 : ${DATA_DIR}"
echo "  工作区根 : ${WORKSPACE_ROOT}"
echo "  监听端口 : ${PORT}"
echo "============================================================"

# 1. 安装基础依赖 (Debian / Ubuntu)
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip git rsync curl sqlite3 ca-certificates >/dev/null
fi

# 2. 准备目录结构
mkdir -p "${INSTALL_DIR}" "${DATA_DIR}/logs" "${DATA_DIR}/snapshots" "${WORKSPACE_ROOT}"
chmod 700 "${DATA_DIR}" "${DATA_DIR}/logs" "${DATA_DIR}/snapshots"

# 若脚本在项目源码目录内执行，自动同步源码至 INSTALL_DIR
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
if [ -f "${SOURCE_ROOT}/workstation/server.py" ] && [ "${SOURCE_ROOT}" != "${INSTALL_DIR}" ]; then
    echo "📦 正在同步工作台核心模版文件至 ${INSTALL_DIR} ..."
    rsync -a --delete \
        --exclude '.venv' \
        --exclude 'node_modules' \
        --exclude '__pycache__' \
        --exclude '.pytest_cache' \
        "${SOURCE_ROOT}/" "${INSTALL_DIR}/"
fi

cd "${INSTALL_DIR}"

# 3. 创建 Python 虚拟环境并安装依赖
if [ ! -x "${INSTALL_DIR}/.venv/bin/python" ]; then
    echo "🐍 正在创建 Python 虚拟环境..."
    python3 -m venv "${INSTALL_DIR}/.venv"
fi

echo "📥 正在安装 Python 运行时依赖..."
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_DEFAULT_TIMEOUT=15
export PIP_RETRIES=1
if [ -f "${INSTALL_DIR}/requirements.txt" ]; then
    "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q
else
    "${INSTALL_DIR}/.venv/bin/pip" install "fastapi>=0.110" "uvicorn[standard]>=0.29" "pydantic>=2.6" "pywebpush>=2.0.0" "cryptography>=42.0.0" -q
fi

# 4. 如果没有预构建的 frontend/dist 且存在 npm，则自动构建前端
if [ ! -f "${INSTALL_DIR}/frontend/dist/index.html" ] && command -v npm >/dev/null 2>&1; then
    echo "🎨 正在构建前端静态产物..."
    "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/scripts/build_frontend.py"
fi

# 5. 初始化数据库、节点配置与默认工作区
export WORKSTATION_DATA="${DATA_DIR}"
export WORKSTATION_NODE_NAME="${NODE_NAME}"
export WORKSTATION_ROLE="pve-standby"
export WORKSTATION_PORT="${PORT}"
"${INSTALL_DIR}/.venv/bin/python" - <<'PYINIT'
import json
import os
import subprocess
from workstation import store, cluster_sync
store.init()
cluster_sync.get_cluster_config()
cfg = store.config()
addresses = subprocess.check_output(['hostname', '-I'], text=True).split()
origins = cfg.setdefault('origins', [])
for address in addresses:
    host = f'[{address}]' if ':' in address else address
    origin = f"http://{host}:{os.environ['WORKSTATION_PORT']}"
    if origin not in origins:
        origins.append(origin)
store.write_private(store.DATA / 'config.json', json.dumps(cfg, ensure_ascii=False, indent=2))
PYINIT

# 6. 配置并启动 systemd 守护服务 (KillMode=process 确保重启 Web 时绝不中断后台运行中的 Agent 任务)
cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Rove Universal Multi-Agent Workstation Node (PVE)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="WORKSTATION_DATA=${DATA_DIR}"
Environment="WORKSTATION_NODE_NAME=${NODE_NAME}"
Environment="WORKSTATION_ROLE=pve-standby"
ExecStart=${INSTALL_DIR}/.venv/bin/python -m uvicorn workstation.server:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=* --no-access-log
Restart=always
RestartSec=3
KillMode=process
StandardOutput=append:${DATA_DIR}/logs/server.log
StandardError=append:${DATA_DIR}/logs/server-error.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service" >/dev/null
systemctl restart "${SERVICE_NAME}.service"

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1')"
echo "============================================================"
echo "  ✅ Rove PVE 通用节点部署完成！"
echo "  🌐 节点访问地址 : http://${IP_ADDR}:${PORT}"
if [ -f "${DATA_DIR}/access-password.txt" ]; then
    echo "  🔑 初始访问密码文件 : ${DATA_DIR}/access-password.txt（请在节点本机查看）"
fi
echo "  📄 服务状态检查 : systemctl status ${SERVICE_NAME}"
echo "  🔄 快照导入恢复 : WORKSTATION_DATA=${DATA_DIR} ${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/scripts/manage.py snapshot-import <path>"
echo "============================================================"
