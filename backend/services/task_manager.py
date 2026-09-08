"""
异步任务状态管理
用于批量评分等长时间运行的任务
"""
import json
import os
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict
from dataclasses import dataclass, asdict

TASK_DIR = Path(__file__).parent.parent / "data" / "tasks"
TASK_DIR.mkdir(parents=True, exist_ok=True)

@dataclass
class TaskStatus:
    task_id: str
    task_type: str  # "auto_score_all"
    status: str  # "pending", "running", "completed", "failed"
    total: int = 0
    success: int = 0
    failed: int = 0
    errors: list = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    current_team: Optional[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self):
        """保存任务状态到文件"""
        filepath = TASK_DIR / f"{self.task_id}.json"
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(task_id: str) -> Optional['TaskStatus']:
        """从文件加载任务状态"""
        filepath = TASK_DIR / f"{task_id}.json"
        if not filepath.exists():
            return None
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return TaskStatus(**data)
        except:
            return None

    @staticmethod
    def delete(task_id: str):
        """删除任务状态文件"""
        filepath = TASK_DIR / f"{task_id}.json"
        if filepath.exists():
            filepath.unlink()


class TaskManager:
    """任务管理器"""

    _instances: Dict[str, TaskStatus] = {}
    _lock = threading.Lock()

    @classmethod
    def create_task(cls, task_id: str, task_type: str, total: int = 0) -> TaskStatus:
        """创建新任务"""
        with cls._lock:
            task = TaskStatus(
                task_id=task_id,
                task_type=task_type,
                status="pending",
                total=total,
                started_at=datetime.now().isoformat()
            )
            task.save()
            cls._instances[task_id] = task
            return task

    @classmethod
    def get_task(cls, task_id: str) -> Optional[TaskStatus]:
        """获取任务状态"""
        # 先从内存获取
        if task_id in cls._instances:
            return cls._instances[task_id]
        # 从文件加载
        task = TaskStatus.load(task_id)
        if task:
            cls._instances[task_id] = task
        return task

    @classmethod
    def update_task(cls, task_id: str, **kwargs):
        """更新任务状态"""
        task = cls.get_task(task_id)
        if task:
            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            task.save()
            cls._instances[task_id] = task

    @classmethod
    def increment_success(cls, task_id: str, team_code: str):
        """成功计数+1"""
        task = cls.get_task(task_id)
        if task:
            task.success += 1
            task.current_team = team_code
            task.save()
            cls._instances[task_id] = task

    @classmethod
    def increment_failed(cls, task_id: str, team_code: str, error: str):
        """失败计数+1"""
        task = cls.get_task(task_id)
        if task:
            task.failed += 1
            task.current_team = team_code
            task.errors.append(f"{team_code}: {error}")
            task.save()
            cls._instances[task_id] = task

    @classmethod
    def complete_task(cls, task_id: str):
        """标记任务完成"""
        task = cls.get_task(task_id)
        if task:
            task.status = "completed"
            task.completed_at = datetime.now().isoformat()
            task.save()
            cls._instances[task_id] = task

    @classmethod
    def fail_task(cls, task_id: str, error: str):
        """标记任务失败"""
        task = cls.get_task(task_id)
        if task:
            task.status = "failed"
            task.completed_at = datetime.now().isoformat()
            if error:
                task.errors.append(f"[系统] {error}")
            task.save()
            cls._instances[task_id] = task

    @classmethod
    def list_tasks(cls, task_type: Optional[str] = None) -> list:
        """列出所有任务"""
        tasks = []
        for filepath in TASK_DIR.glob("*.json"):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if task_type is None or data.get('task_type') == task_type:
                    tasks.append(data)
            except:
                pass
        return sorted(tasks, key=lambda x: x.get('started_at', ''), reverse=True)


task_manager = TaskManager()
