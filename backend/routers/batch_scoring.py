"""
批量评分 API（纯 AI 批量评分模式）
- 用于无人工评审场景：初赛筛选、内部平台批量评分
- 复用现有评分核心逻辑，只包装为批量任务管理
"""
import uuid
import threading
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, Team, Work, MachineScore
from services.task_manager import task_manager
from config import AVAILABLE_MODELS
from feature_flags import is_enabled
from utils.json_fields import json_list

router = APIRouter()


class BatchScoringStartRequest(BaseModel):
    """启动批量评分请求"""
    model: str = "qwen3.8-max"
    parallel: bool = True
    concurrency: int = 5
    competition_id: Optional[int] = None


# ============ 批量评分任务管理 ============

@router.post("/start")
async def start_batch_scoring(
    req: BatchScoringStartRequest,
    db: Session = Depends(get_db)
):
    """启动批量评分任务，返回 task_id"""
    if not is_enabled("ENABLE_BATCH_SCORING"):
        raise HTTPException(status_code=403, detail="批量评分功能未启用")

    # 验证模型
    if req.model not in AVAILABLE_MODELS:
        return {
            "message": f"不支持的模型: {req.model}",
            "available_models": list(AVAILABLE_MODELS.keys())
        }

    # 获取待评分的作品
    works = db.query(Work).filter(Work.is_parsed == True).all()
    total = len(works)

    if total == 0:
        return {"message": "没有待评分的作品", "task_id": None}

    # 创建任务
    task_id = str(uuid.uuid4())[:8]
    task_manager.create_task(task_id, "batch_scoring", total)

    # 启动后台线程执行评分
    def run_scoring():
        from database import SessionLocal
        from services.scorer import AutoScorer
        import concurrent.futures

        db_thread = SessionLocal()
        try:
            task_manager.update_task(task_id, status="running")

            works = db_thread.query(Work).filter(Work.is_parsed == True).all()
            team_ids = [w.team_id for w in works]

            if req.parallel:
                def score_single(tid: int):
                    db_single = SessionLocal()
                    try:
                        scorer = AutoScorer(db_single)
                        team = db_single.query(Team).filter(Team.id == tid).first()
                        if not team:
                            return (False, f"team_{tid}", "队伍不存在")

                        result = scorer.score_team(tid, model=req.model, session_id=task_id)
                        if result.is_completed:
                            return (True, team.short_code, None)
                        else:
                            return (False, team.short_code, result.error_message)
                    except Exception as e:
                        import traceback
                        return (False, f"team_{tid}", f"{str(e)}\n{traceback.format_exc()}")
                    finally:
                        db_single.close()

                with concurrent.futures.ThreadPoolExecutor(max_workers=req.concurrency) as executor:
                    futures = {executor.submit(score_single, tid): tid for tid in team_ids}
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            success, team_code, error = future.result()
                            if success:
                                task_manager.increment_success(task_id, team_code)
                            else:
                                task_manager.increment_failed(task_id, team_code, error)
                        except Exception as e:
                            tid = futures[future]
                            task_manager.increment_failed(task_id, f"team_{tid}", str(e))
            else:
                scorer = AutoScorer(db_thread)
                for work in works:
                    try:
                        team = db_thread.query(Team).filter(Team.id == work.team_id).first()
                        if not team:
                            task_manager.increment_failed(task_id, f"team_{work.team_id}", "队伍不存在")
                            continue

                        result = scorer.score_team(work.team_id, model=req.model, session_id=task_id)
                        if result.is_completed:
                            task_manager.increment_success(task_id, team.short_code)
                        else:
                            task_manager.increment_failed(task_id, team.short_code, result.error_message)
                    except Exception as e:
                        task_manager.increment_failed(task_id, f"team_{work.team_id}", str(e))

            task_manager.complete_task(task_id)
        except Exception as e:
            task_manager.fail_task(task_id, str(e))
        finally:
            db_thread.close()

    thread = threading.Thread(target=run_scoring, daemon=True)
    thread.start()

    return {
        "message": "批量评分已启动，请在任务状态页面查看进度",
        "task_id": task_id,
        "total": total,
        "model": req.model,
        "model_name": AVAILABLE_MODELS[req.model]["name"],
        "parallel": req.parallel,
        "concurrency": req.concurrency if req.parallel else 1,
    }


@router.get("/status/{task_id}")
async def get_batch_task_status(task_id: str):
    """查询批量评分任务状态"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return {
        "task_id": task.task_id,
        "status": task.status,
        "progress": {
            "total": task.total,
            "completed": task.success,
            "failed": task.failed,
        },
        "current_team": task.current_team,
        "errors": task.errors[-5:] if task.errors else [],
        "started_at": task.started_at,
        "completed_at": task.completed_at,
    }


@router.get("/results/{task_id}")
async def get_batch_task_results(
    task_id: str,
    db: Session = Depends(get_db)
):
    """获取批量评分任务的完整结果列表"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 按 scoring_session 查询本次任务的所有评分记录
    scores = db.query(MachineScore).filter(
        MachineScore.scoring_session == task_id
    ).order_by(MachineScore.created_at.desc()).all()

    results = []
    for s in scores:
        team = db.query(Team).filter(Team.id == s.team_id).first()
        work = db.query(Work).filter(Work.team_id == s.team_id).first()
        results.append({
            "team_id": s.team_id,
            "team_name": team.team_name if team else "",
            "short_code": team.short_code if team else "",
            "group_type": team.group_type if team else "",
            "school": team.school if team else "",
            "model_name": s.model_name,
            "scores": {
                "theme": s.theme_score,
                "presentation": s.presentation_score,
                "process": s.process_score,
                "ai_literacy": s.ai_literacy_score,
                "total": s.total_score,
            },
            "calibrated_score": s.calibrated_score,
            "anchor_level": work.anchor_level if work else "Lv0",
            "confidence": s.confidence,
            "flags": json_list(s.flags),
            "comments": {
                "theme": s.theme_comment,
                "presentation": s.presentation_comment,
                "process": s.process_comment,
                "ai_literacy": s.ai_literacy_comment,
                "overall": s.overall_comment,
            },
            "defense_questions": s.defense_questions,
            "is_adopted": s.is_adopted,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })

    return {
        "task_id": task_id,
        "status": task.status,
        "progress": {
            "total": task.total,
            "completed": task.success,
            "failed": task.failed,
        },
        "results": results,
    }


@router.get("/tasks")
async def list_batch_tasks():
    """返回所有批量评分任务列表"""
    tasks = task_manager.list_tasks("batch_scoring")
    return {
        "tasks": [
            {
                "task_id": t["task_id"],
                "status": t["status"],
                "total": t["total"],
                "completed": t["success"],
                "failed": t["failed"],
                "started_at": t["started_at"],
                "completed_at": t["completed_at"],
            }
            for t in tasks[:50]  # 最多返回最近 50 个
        ]
    }
