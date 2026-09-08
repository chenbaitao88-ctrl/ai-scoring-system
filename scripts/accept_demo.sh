#!/bin/bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${BUDDY_CLAW_PORT:-8000}"
BASE_URL="http://127.0.0.1:${PORT}"
RUNTIME_DIR="${BASE_DIR}/runtime/demo"
PID_FILE="${RUNTIME_DIR}/backend.pid"
DATA_FILE="${RUNTIME_DIR}/data/teams.db"
DIST_INDEX="${BASE_DIR}/frontend/dist/index.html"
BACKEND_DIR="${BASE_DIR}/backend"
PROCESS_MARKER="--buddy-claw-offline-demo"

fail() {
    echo "未通过：$*" >&2
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

check_get() {
    path="$1"
    label="$2"
    body="$(mktemp -t buddy-claw-demo.XXXXXX)"
    if ! curl --silent --show-error --fail --max-time 5 "${BASE_URL}${path}" -o "${body}"; then
        rm -f "${body}"
        fail "${label}（${BASE_URL}${path}）"
    fi
    rm -f "${body}"
    echo "通过：${label}"
}

echo "Buddy-Claw 离线演示验收"
[ -f "${DIST_INDEX}" ] || fail "缺少前端构建结果，请先运行 setup_mac.sh。"
[ -f "${PID_FILE}" ] || fail "未发现演示服务 PID，请先运行 start_demo.command。"
[ -f "${DATA_FILE}" ] || fail "未发现本地演示数据库：${DATA_FILE}"
PID="$(tr -d '[:space:]' < "${PID_FILE}")"
printf '%s' "${PID}" | grep -Eq '^[0-9]+$' || fail "演示 PID 无效。"
kill -0 "${PID}" 2>/dev/null || fail "演示服务进程已退出，请重新启动。"
owned_process "${PID}" || fail "PID ${PID} 不属于本次演示服务。"

check_get "/health" "健康检查"
check_get "/" "前端首页"
check_get "/api/teams" "队伍列表接口"
check_get "/api/teams/stats" "队伍统计接口"
check_get "/api/works/status" "作品状态接口"
check_get "/api/scores/stats" "评分统计接口"

CASE_COUNT="$("${BASE_DIR}/.venv/bin/python" - "${DATA_FILE}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    print(connection.execute("SELECT COUNT(*) FROM teams WHERE source = 'demo'").fetchone()[0])
PY
)"
[ "${CASE_COUNT}" = "12" ] || fail "演示案例数量应为 12，当前为 ${CASE_COUNT}。"
echo "通过：12 个纯虚构演示案例"

echo ""
echo "验收通过：服务可访问，前端已构建，12 个虚构案例已就绪。"
echo "本脚本只做本机 GET 检查，不导入数据、不调用外部网络、不写入真实材料。"
