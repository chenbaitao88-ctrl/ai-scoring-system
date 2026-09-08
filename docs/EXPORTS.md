# 已确认结果导出

Excel、Word和CSV都从同一个权威结果服务生成。用户先选择评分任务，再明确选择条目；文件包含条目、材料、结果版本、评分尝试、采用/锁定记录和分数。旧版队伍综合分留作历史参考，不重新加权到这些文件中。

![合成任务中选择已锁定的77分条目](screenshots/authoritative-export.png)

## 操作与保护条件

1. 打开“结果导出”，选择评分任务。每条结果显示确认状态、分数及阻断原因。
2. 勾选可导出条目，或点“选择可导出条目”。页面明确显示已选数量和任务总数。
3. 下载Excel、Word或CSV。页面会提交预览时的版本编号；结果发生变化时，下载失败并刷新列表，需要重新选择。

人工最终锁定优先于已采用结果；有新评分尝试不会自动替换锁定版本。未结案的阻断工单、没有采用记录、缺失或损坏快照均不能导出。直接请求整个任务时，任何失败条目都会阻止整份文件，不会静默漏项。生成文件前后再次检查来源与任务修订；这是单用户、本机单实例范围内的版本检查，不是多用户事务锁。

文件不包含原始材料、路径、模型响应或评语。空白主观分表示快照未提供该值，不代表零分。Excel的数值保持数值类型，文本不会执行为公式；CSV对潜在公式前缀进行转义。Word为每个条目单独起页。

下载不会自动推进任务生命周期，任务仍可显示 `export/pending`。当前没有新增结案UI；下面的演示用现有工单服务结案，不代表完成了在线评分流水线。

## 离线复现一个可下载条目

先按[首页](../README.md)启动演示。默认12个合成案例包含未采用和待复核状态，不应为演示下载而自动放行。下面命令只把 `DEMO-M-006` 的预置最终锁定对应工单结案，不改分数、不调用模型。它会修改所选演示目录里的这一份工单；其余案例继续展示阻断状态。请在仓库根目录执行，使用Python 3.11和锁定依赖。

```bash
uv run --frozen python - <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "backend"))
from services.score_attempt_store import ScoreAttemptStore
from services.review_case_store import ReviewCaseStore

data = Path(os.environ.get("SCORING_DATA_DIR", "runtime/demo/data")).resolve()
manifest = json.loads((data / "demo_phase11_manifest.json").read_text())
if manifest.get("source") != "offline_synthetic" or manifest.get("provider_calls") != 0:
    raise SystemExit("Only the offline synthetic demo is supported.")
item = next(row for row in manifest["items"] if row["team_code"] == "DEMO-M-006")
task_id, item_id = manifest["scoring_task_id"], item["scoring_item_id"]
root = data / "pipeline-runtime"
attempts = ScoreAttemptStore(root / "score-attempts")
reviews = ReviewCaseStore(root / "reviews", attempt_store=attempts)
case = reviews.get_review_case(task_id, item_id, item["review_case_id"])
lock = reviews.get_active_final_lock(task_id, item_id)
if lock is None or lock.review_case_id != case.review_case_id:
    raise SystemExit("The expected demo final lock is missing; no changes made.")
if case.status != "resolved":
    if case.status != "in_review":
        raise SystemExit("Unexpected case state; no changes made.")
    reviews.transition_review_case(
        task_id, item_id, case.review_case_id,
        expected_revision=case.current_revision, to_status="resolved",
        event_type="CASE_RESOLVED", actor=lock.locked_by,
        occurred_at=datetime.now(timezone.utc), resolution_decision_id=lock.decision_id,
    )
print({"task_id": task_id, "item_id": item_id, "status": "resolved"})
PY
```

回到结果页刷新，选中已锁定条目，分别下载三种文件。2026-09-08实测该预置条目总分77，维度分为17、23、22、15；三个文件与权威结果API一致。其他条目不会被这一命令采用或结案。

## API兼容

| 请求 | 行为 |
|---|---|
| `GET /api/result-export/tasks` | 列出评分任务 |
| `GET /api/result-export/tasks/{task_id}/results` | 所有条目的可导出性、结果版本或阻断原因 |
| `GET /api/result-export/tasks/{task_id}/export?format=xlsx` | 导出整个任务；任一条目失败则整体409 |
| 同上，添加重复的 `item_id` 查询参数 | 仅导出显式选择的条目 |
| `POST /api/result-export/tasks/{task_id}/export` | JSON参数 `format`、`item_ids`、`expected_derivations`；界面使用此入口校验预览版本 |
| `GET /api/export/excel`、`GET /api/export/word`、`GET /api/scores/export` | 兼容URL，必须传 `task_id`；不带任务返回409，带任务后使用相同服务，可选 `item_id` |

格式为 `xlsx`、`docx` 或 `csv`。POST省略 `item_ids` 表示整任务，空数组或重复/未知条目被拒绝。`expected_derivations` 是条目ID到推导ID的映射；若提供，必须与所选条目完整一致。成功响应为附件且禁止缓存。无效选择400、未知任务404、结果阻断或版本变化409；请求结构不合法时FastAPI返回422。

[服务实现](../backend/services/authoritative_export_service.py) · [回归测试](../tests/test_authoritative_file_export.py) · [验证范围](VALIDATION.md)
