#!/bin/bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${BASE_DIR}/backend"
RUNTIME_DIR="${BASE_DIR}/runtime/demo"
PID_FILE="${RUNTIME_DIR}/backend.pid"
PROCESS_MARKER="--buddy-claw-offline-demo"

owned_process() {
    pid="$1"
    command_line="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    case "${command_line}" in
        *"${BACKEND_DIR}/main.py"*"${PROCESS_MARKER}"*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ ! -f "${PID_FILE}" ]; then
    echo "演示服务当前未运行。"
    exit 0
fi

PID="$(tr -d '[:space:]' < "${PID_FILE}")"
printf '%s' "${PID}" | grep -Eq '^[0-9]+$' || {
    echo "错误：PID 文件内容无效，已保留现场：${PID_FILE}" >&2
    exit 1
}

if ! kill -0 "${PID}" 2>/dev/null; then
    rm -f "${PID_FILE}"
    echo "已清理失效的演示 PID 记录。"
    exit 0
fi

owned_process "${PID}" || {
    echo "错误：PID ${PID} 不属于本次演示服务，未执行停止操作。" >&2
    exit 1
}

kill "${PID}"
for _ in $(seq 1 15); do
    if ! kill -0 "${PID}" 2>/dev/null; then
        rm -f "${PID_FILE}"
        echo "演示服务已停止。"
        exit 0
    fi
    sleep 1
done

echo "错误：演示服务未在 15 秒内退出，请查看 runtime/demo/backend.log。" >&2
exit 1
