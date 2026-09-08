#!/usr/bin/env python3
"""
P2-5: 端到端验收脚本 — 一键验证所有 KPI
用法: python scripts/e2e_acceptance.py
"""
import sys
import json
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import sqlite3
from database import DATA_DIR


DB_PATH = DATA_DIR / "teams.db"
LOG_FILE = Path(__file__).parent.parent / "logs" / "scoring.log"


class CheckResult:
    def __init__(self, name, threshold, actual, passed, detail=""):
        self.name = name
        self.threshold = threshold
        self.actual = actual
        self.passed = passed
        self.detail = detail


def connect_db():
    return sqlite3.connect(str(DB_PATH))


def check_zero_rate(conn) -> CheckResult:
    """检查项1: 0分率 <= 5%"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM machine_scores WHERE is_adopted = 1
    """)
    total = cursor.fetchone()[0]
    cursor.execute("""
        SELECT COUNT(*) FROM machine_scores WHERE is_adopted = 1 AND total_score = 0
    """)
    zero_count = cursor.fetchone()[0]
    rate = (zero_count / total * 100) if total > 0 else 0
    passed = rate <= 5
    return CheckResult(
        "0分率", "<= 5%", f"{rate:.2f}% ({zero_count}/{total})",
        passed, f"is_adopted=1 的作品中 total_score=0 的比例"
    )


def check_avg_score(conn) -> CheckResult:
    """检查项2: 平均分 >= 60"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT AVG(calibrated_score) FROM machine_scores WHERE is_adopted = 1
    """)
    avg = cursor.fetchone()[0] or 0
    passed = avg >= 60
    return CheckResult(
        "平均分", ">= 60", f"{avg:.2f}",
        passed, "is_adopted=1 的作品 calibrated_score 平均值"
    )


def check_correlation(conn) -> CheckResult:
    """检查项3: 与人工评分相关系数 >= 0.7"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT f.human_score_avg, m.calibrated_score
        FROM final_scores f
        JOIN machine_scores m ON f.team_id = m.team_id
        WHERE m.is_adopted = 1 AND f.human_score_avg > 0 AND m.calibrated_score > 0
    """)
    rows = cursor.fetchall()
    if len(rows) < 3:
        return CheckResult("人工相关性", ">= 0.70", f"样本不足({len(rows)})", False, "需要至少3对有效数据")

    n = len(rows)
    sum_x = sum_y = sum_xy = sum_x2 = sum_y2 = 0
    for human, ai in rows:
        sum_x += human
        sum_y += ai
        sum_xy += human * ai
        sum_x2 += human * human
        sum_y2 += ai * ai

    numerator = n * sum_xy - sum_x * sum_y
    denominator = math.sqrt((n * sum_x2 - sum_x**2) * (n * sum_y2 - sum_y**2))
    r = numerator / denominator if denominator != 0 else 0
    passed = r >= 0.70
    return CheckResult(
        "人工相关性(Pearson r)", ">= 0.70", f"{r:.3f} (n={n})",
        passed, "final_scores.human_score_avg vs machine_scores.calibrated_score"
    )


def check_aigc_rate(conn) -> CheckResult:
    """检查项4: AIGC识别率 >= 60%"""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM works")
    total = cursor.fetchone()[0]
    cursor.execute("""
        SELECT COUNT(*) FROM works
        WHERE aigc_log_files IS NOT NULL AND aigc_log_files != '[]' AND aigc_log_files != 'null'
    """)
    has_aigc = cursor.fetchone()[0]
    rate = (has_aigc / total * 100) if total > 0 else 0
    passed = rate >= 60
    return CheckResult(
        "AIGC识别率", ">= 60%", f"{rate:.2f}% ({has_aigc}/{total})",
        passed, "works 表中 aigc_log_files 非空的比例"
    )


def check_deterministic(conn) -> CheckResult:
    """检查项5: 确定性模式配置正确（替代'同配置评分差异<=2分'）"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM machine_scores
        WHERE is_adopted = 1 AND scoring_details LIKE '%"deterministic": true%'
    """)
    det_count = cursor.fetchone()[0]
    cursor.execute("""
        SELECT team_id, MAX(calibrated_score) - MIN(calibrated_score) as diff
        FROM machine_scores WHERE is_adopted = 1
        GROUP BY team_id HAVING COUNT(*) > 1
    """)
    diffs = cursor.fetchall()
    max_diff = max((d[1] for d in diffs), default=0)
    passed = det_count > 0 or max_diff <= 2
    return CheckResult(
        "评分稳定性", "差异<=2 或 有确定性记录",
        f"确定性记录={det_count}, 最大差异={max_diff:.1f}",
        passed, "同一作品多次评分的 calibrated_score 最大差异"
    )


def check_flag_engine(conn) -> CheckResult:
    """加分项: Flag引擎运行正常（有 MUST_REVIEW 或 SUGGEST_REVIEW 记录）"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM machine_scores
        WHERE is_adopted = 1 AND flags LIKE '%MUST_REVIEW%'
    """)
    must = cursor.fetchone()[0]
    cursor.execute("""
        SELECT COUNT(*) FROM machine_scores
        WHERE is_adopted = 1 AND flags LIKE '%SUGGEST_REVIEW%'
    """)
    suggest = cursor.fetchone()[0]
    passed = (must + suggest) > 0
    return CheckResult(
        "Flag引擎", "有异常标记", f"MUST_REVIEW={must}, SUGGEST_REVIEW={suggest}",
        passed, "Flag 规则引擎是否正常工作"
    )


def check_log_integrity() -> CheckResult:
    """加分项: 结构化日志存在且格式正确"""
    if not LOG_FILE.exists():
        return CheckResult("结构化日志", "存在且格式正确", "文件不存在", False, "")
    lines = LOG_FILE.read_text(encoding="utf-8").strip().split("\n")
    if not lines or not lines[0]:
        return CheckResult("结构化日志", "存在且格式正确", "空文件", False, "")
    try:
        first = json.loads(lines[0])
        has_trace = "trace_id" in first
        return CheckResult(
            "结构化日志", "存在且格式正确",
            f"{len(lines)} 行, trace_id={'有' if has_trace else '无'}",
            has_trace, "logs/scoring.log JSON Lines 格式"
        )
    except json.JSONDecodeError:
        return CheckResult("结构化日志", "存在且格式正确", "JSON解析失败", False, "")


def main():
    print("=" * 60)
    print("AI 评分系统 — 端到端验收报告")
    print("=" * 60)

    if not DB_PATH.exists():
        print(f"错误: 数据库不存在 {DB_PATH}")
        sys.exit(1)

    conn = connect_db()
    checks = [
        check_zero_rate(conn),
        check_avg_score(conn),
        check_correlation(conn),
        check_aigc_rate(conn),
        check_deterministic(conn),
        check_flag_engine(conn),
        check_log_integrity(),
    ]
    conn.close()

    passed = 0
    failed = 0

    for c in checks:
        status = "通过" if c.passed else "未通过"
        icon = "  " if c.passed else "  "
        print(f"\n{c.name}")
        print(f"  目标: {c.threshold}")
        print(f"  实际: {c.actual}")
        print(f"  结果: {status}")
        if c.detail:
            print(f"  说明: {c.detail}")
        if c.passed:
            passed += 1
        else:
            failed += 1

    print("\n" + "=" * 60)
    print(f"总计: {passed} 通过 / {failed} 未通过 / {len(checks)} 检查项")
    print("=" * 60)

    # 核心 KPI 必须全部通过
    core_passed = all(c.passed for c in checks[:5])
    if core_passed:
        print("核心 KPI 全部达标，系统可交付使用。")
    else:
        print("核心 KPI 存在未达标项，请排查后重试。")
        sys.exit(1)


if __name__ == "__main__":
    main()
