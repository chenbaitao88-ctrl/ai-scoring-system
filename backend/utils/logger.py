"""
结构化日志模块 (P2-3)
- 每次评分生成 trace_id
- 关键节点写入 JSON Lines 格式日志
- 决策依据: 涛涛 P2-3 执行指令，要求评分链路可观测
"""
import json
import uuid
import time
import logging
from pathlib import Path
from typing import Dict, Any

# 日志文件路径
LOG_DIR = Path(__file__).parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "scoring.log"

# 配置 Python logging
logger = logging.getLogger("scoring")
logger.setLevel(logging.INFO)

# 文件处理器（追加模式）
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.INFO)
logger.addHandler(file_handler)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)


class ScoringTrace:
    """评分全链路追踪器"""

    def __init__(self, team_id: int, team_name: str = ""):
        self.trace_id = str(uuid.uuid4())[:8]  # 短ID，便于查看
        self.team_id = team_id
        self.team_name = team_name
        self.start_time = time.time()
        self.steps = []

    def log(self, step: str, result: Any = None, duration: float = None, **extra):
        """记录一个步骤"""
        entry = {
            "trace_id": self.trace_id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "step": step,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if duration is not None:
            entry["duration_ms"] = round(duration * 1000, 2)
        if result is not None:
            entry["result"] = result
        entry.update(extra)

        self.steps.append(entry)

        # 写入 JSON Lines
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 同时输出到控制台（便于调试）
        logger.info(f"[{self.trace_id}] {step} | team={self.team_id} | {extra}")

    def finish(self, total_score: float = None, anchor_level: str = None, flags: list = None):
        """评分完成，记录汇总"""
        total_duration = time.time() - self.start_time
        self.log(
            "SCORE_FINISH",
            result={
                "total_score": total_score,
                "anchor_level": anchor_level,
                "flags": flags,
            },
            duration=total_duration,
            step_count=len(self.steps),
        )
        return self.trace_id
