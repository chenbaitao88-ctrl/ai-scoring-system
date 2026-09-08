#!/bin/bash
set -eu

# Local-only demo starter. The marker makes PID ownership checkable.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${BASE_DIR}/backend"
PYTHON_BIN="${BASE_DIR}/.venv/bin/python"
DIST_INDEX="${BASE_DIR}/frontend/dist/index.html"
RUNTIME_DIR="${BASE_DIR}/runtime/demo"
PID_FILE="${RUNTIME_DIR}/backend.pid"
LOG_FILE="${RUNTIME_DIR}/backend.log"
DEMO_DATA_DIR="${RUNTIME_DIR}/data"
HOST="127.0.0.1"
PORT="${BUDDY_CLAW_PORT:-8000}"
URL="http://${HOST}:${PORT}"
PROCESS_MARKER="--buddy-claw-offline-demo"

fail() {
    echo "错误：$*" >&2
    exit 1
}

owned_process() {
    pid="$1"
    command_line="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    case "${command_line}" in
        *"${BACKEND_DIR}/main.py"*"${PROCESS_MARKER}"*) return 0 ;;
        *) return 1 ;;
    esac
}

[ -x "${PYTHON_BIN}" ] || fail "未找到 .venv/bin/python。请先运行 setup_mac.sh。"
[ -f "${DIST_INDEX}" ] || fail "未找到前端构建结果。请先运行 setup_mac.sh。"
mkdir -p "${RUNTIME_DIR}" "${DEMO_DATA_DIR}"

SCORING_DATA_DIR="${DEMO_DATA_DIR}" \
    "${PYTHON_BIN}" "${BASE_DIR}/scripts/init_demo.py"

if [ -f "${PID_FILE}" ]; then
    PID="$(tr -d '[:space:]' < "${PID_FILE}")"
    printf '%s' "${PID}" | grep -Eq '^[0-9]+$' || fail "PID 文件内容无效：${PID_FILE}。"
    if kill -0 "${PID}" 2>/dev/null; then
        owned_process "${PID}" || fail "PID ${PID} 已被其他进程占用，未执行任何停止操作。"
        echo "演示服务已在运行：${URL}（PID ${PID}）"
        exit 0
    fi
    rm -f "${PID_FILE}"
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "端口 ${PORT} 已被占用。请人工确认；本脚本不会杀端口进程。"
fi

echo "正在启动本机离线演示..."
(
    cd "${BACKEND_DIR}"
    nohup env \
        SCORING_DATA_DIR="${DEMO_DATA_DIR}" \
        DATA_DIR="${DEMO_DATA_DIR}" \
        SCORING_PORT="${PORT}" \
        PYTHONUNBUFFERED="1" \
        LLM_API_KEY="" \
        DASHSCOPE_API_KEY="" \
        LLM_API_URL="" \
        LLM_MODEL="" \
        FEATURE_NEW_PARSER="true" \
        AIGC_OCR="false" \
        FEWSHOT="false" \
        ANCHOR_V2="true" \
        MONITOR_GUARD="true" \
        DETERMINISTIC_MODE="true" \
        ENABLE_BATCH_SCORING="false" \
        "${PYTHON_BIN}" "${BACKEND_DIR}/main.py" "${PROCESS_MARKER}" \
        >"${LOG_FILE}" 2>&1 &
    echo $! > "${PID_FILE}"
)

PID="$(tr -d '[:space:]' < "${PID_FILE}")"
READY=0
for _ in $(seq 1 30); do
    if ! kill -0 "${PID}" 2>/dev/null; then
        break
    fi
    if curl --silent --show-error --fail --max-time 2 "${URL}/health" | grep -q 'healthy'; then
        READY=1
        break
    fi
    sleep 1
done

if [ "${READY}" -ne 1 ]; then
    echo "服务未能在 30 秒内就绪，日志如下：" >&2
    tail -n 40 "${LOG_FILE}" 2>/dev/null || true
    if kill -0 "${PID}" 2>/dev/null && owned_process "${PID}"; then
        kill "${PID}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}"
    exit 1
fi

echo "演示服务已启动：${URL}"
echo "数据目录：${DEMO_DATA_DIR}"
echo "停止方式：bash scripts/stop_demo.command"
if [ "${BUDDY_CLAW_NO_BROWSER:-0}" != "1" ]; then
    open "${URL}" >/dev/null 2>&1 || true
fi
