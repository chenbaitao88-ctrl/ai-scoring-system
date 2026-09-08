#!/bin/bash
set -eu

# One-time setup for a 2020 Intel Mac. This script installs and builds only.

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
FRONTEND_DIR="${BASE_DIR}/frontend"
VENV_DIR="${BASE_DIR}/.venv"

fail() {
    echo -e "${RED}错误：$*${NC}" >&2
    exit 1
}

echo "========================================"
echo " Buddy-Claw 单机演示安装"
echo "========================================"
echo "项目目录：${BASE_DIR}"
echo "本步骤只安装依赖并构建前端，不会启动服务。"

[ "$(uname -s)" = "Darwin" ] || fail "此脚本只适用于 macOS。"
ARCH="$(uname -m)"
[ "${ARCH}" = "x86_64" ] || fail "需要 Intel Mac（x86_64），当前架构为 ${ARCH}。"
command -v sw_vers >/dev/null 2>&1 && echo "macOS：$(sw_vers -productVersion)"
echo "架构：${ARCH}"

echo -e "${BLUE}[1/5] 检查 Python 3.11...${NC}"
command -v python3.11 >/dev/null 2>&1 || fail "未找到 python3.11。请先安装 Python 3.11。"
PYTHON_BIN="$(command -v python3.11)"
PYTHON_VERSION="$(${PYTHON_BIN} -c 'import platform; print(platform.python_version())')"
case "${PYTHON_VERSION}" in
    3.11.*) echo "Python ${PYTHON_VERSION}" ;;
    *) fail "必须使用 Python 3.11，当前为 ${PYTHON_VERSION}。" ;;
esac

echo -e "${BLUE}[2/5] 检查 Node.js >= 22.12...${NC}"
command -v node >/dev/null 2>&1 || fail "未找到 Node.js。请先安装 Node.js 22.12 或更高版本。"
command -v npm >/dev/null 2>&1 || fail "未找到 npm。请重新安装 Node.js。"
NODE_VERSION="$(node --version | sed 's/^v//')"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
NODE_MINOR="$(node -p 'process.versions.node.split(".")[1]')"
if [ "${NODE_MAJOR}" -lt 22 ] || { [ "${NODE_MAJOR}" -eq 22 ] && [ "${NODE_MINOR}" -lt 12 ]; }; then
    fail "Node.js 版本过低：${NODE_VERSION}，需要 >= 22.12。"
fi
echo "Node.js ${NODE_VERSION}，npm $(npm --version)"

echo -e "${BLUE}[3/5] 创建或检查 Python 虚拟环境...${NC}"
if [ -d "${VENV_DIR}" ]; then
    [ -x "${VENV_DIR}/bin/python" ] || fail "已有 .venv 但缺少可用的 Python 入口。"
else
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_VERSION="$(${VENV_PYTHON} -c 'import platform; print(platform.python_version())')"
case "${VENV_VERSION}" in
    3.11.*) echo ".venv 使用 Python ${VENV_VERSION}" ;;
    *) fail ".venv 必须使用 Python 3.11，当前为 ${VENV_VERSION}。不会自动删除现有环境。" ;;
esac

echo -e "${BLUE}[4/5] 按锁定版本安装 Python 依赖...${NC}"
[ -f "${BASE_DIR}/pyproject.toml" ] || fail "缺少 pyproject.toml。"
[ -f "${BASE_DIR}/uv.lock" ] || fail "缺少 uv.lock。"
"${VENV_PYTHON}" -m pip install "uv==0.11.24"
"${VENV_DIR}/bin/uv" sync --active --frozen --no-dev

echo -e "${BLUE}[5/5] 使用 npm ci 安装并构建前端...${NC}"
[ -f "${FRONTEND_DIR}/package.json" ] || fail "缺少 frontend/package.json。"
[ -f "${FRONTEND_DIR}/package-lock.json" ] || fail "缺少 frontend/package-lock.json，不能执行 npm ci。"
(cd "${FRONTEND_DIR}" && npm ci && npm run build)

echo ""
echo -e "${GREEN}安装完成。${NC}"
echo "没有启动任何服务，也没有读取或写入项目 .env。"
echo "日常启动：bash scripts/start_demo.command"
echo "验收方式：bash scripts/accept_demo.sh"
