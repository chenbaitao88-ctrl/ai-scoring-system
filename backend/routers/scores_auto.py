"""scores_auto.py - AI自动评分路由"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import String
from pydantic import BaseModel
from typing import Optional
import json
from database import get_db, Team, MachineScore, HumanScore, FinalScore
from services.scorer import AutoScorer

router = APIRouter()



@router.post("/auto/{team_id}")
async def auto_score_team(
    team_id: int,
    model: str = None,
    db: Session = Depends(get_db)
):
    """对单个队伍进行AI自动评分"""
    scorer = AutoScorer(db)
    result = scorer.score_team(team_id, model=model)

    if not result.is_completed:
        raise HTTPException(status_code=400, detail=result.error_message)

    return {
        "message": "评分完成",
        "team_id": result.team_id,
        "team_name": result.team_name,
        "short_code": result.short_code,
        "group_type": result.group_type,
        "model_name": model or ("LLM评分" if result.used_llm else "规则评分"),
        "scores": {
            "theme": result.theme_score,
            "presentation": result.presentation_score,
            "process": result.process_score,
            "ai_literacy": result.ai_literacy_score,
            "total": result.total_score
        },
        "comments": {
            "theme": result.theme_comment,
            "presentation": result.presentation_comment,
            "process": result.process_comment,
            "ai_literacy": result.ai_literacy_comment,
            "overall": result.overall_comment,
        },
        "details": {
            "code_analysis": result.code_analysis,
            "aigc_analysis": result.aigc_analysis,
            "feature_detection": result.feature_detection
        },
        "used_llm": result.used_llm
    }


@router.post("/re-score/{team_id}")
async def re_score_team(
    team_id: int,
    model: str = None,
    db: Session = Depends(get_db)
):
    """对单个队伍强制重新评分（覆盖旧分）"""
    scorer = AutoScorer(db)
    result = scorer.score_team(team_id, model=model)

    if not result.is_completed:
        raise HTTPException(status_code=400, detail=result.error_message)

    return {
        "message": "重新评分完成",
        "team_id": result.team_id,
        "team_name": result.team_name,
        "short_code": result.short_code,
        "model_name": model or ("LLM评分" if result.used_llm else "规则评分"),
        "scores": {
            "theme": result.theme_score,
            "presentation": result.presentation_score,
            "process": result.process_score,
            "ai_literacy": result.ai_literacy_score,
            "total": result.total_score
        },
        "calibrated_score": result.calibrated_score,
        "anchor_level": result.anchor_level,
        "flags": result.flags,
        "comments": {
            "theme": result.theme_comment,
            "presentation": result.presentation_comment,
            "process": result.process_comment,
            "ai_literacy": result.ai_literacy_comment,
            "overall": result.overall_comment,
        },
    }


@router.post("/auto-all")
async def auto_score_all(db: Session = Depends(get_db)):
    """对所有已解析作品进行自动评分（同步版本，可能会超时）"""
    scorer = AutoScorer(db)
    results = scorer.score_all()
    return results


@router.post("/auto-all-async")
async def auto_score_all_async(
    model: str = "qwen3.8-max",
    parallel: bool = True,
    concurrency: int = 5,
    db: Session = Depends(get_db)
):
    """
    异步批量评分 - 立即返回任务ID，后台执行

    参数：
    - model: LLM模型名称（默认 qwen3.8-max）
    - parallel: 是否启用并行评分（默认 True）
    - concurrency: 并发数（默认 5）

    使用 /auto-all-async/status/{task_id} 查询进度
    """
    import uuid
    from services.task_manager import task_manager
    import threading
    from config import AVAILABLE_MODELS

    # 验证模型
    if model not in AVAILABLE_MODELS:
        return {"message": f"不支持的模型: {model}", "available_models": list(AVAILABLE_MODELS.keys())}

    # 获取待评分的作品数量
    from database import Work
    works = db.query(Work).filter(Work.is_parsed == True).all()
    total = len(works)

    if total == 0:
        return {"message": "没有待评分的作品", "task_id": None}

    # 创建任务
    task_id = str(uuid.uuid4())[:8]
    task_manager.create_task(task_id, "auto_score_all", total)

    # 启动后台线程执行评分
    def run_scoring():
        from database import SessionLocal
        from services.scorer import AutoScorer
        import concurrent.futures

        db_thread = SessionLocal()
        try:
            task_manager.update_task(task_id, status="running")

            # 获取所有待评分的作品
            from database import Work
            works = db_thread.query(Work).filter(Work.is_parsed == True).all()
            team_ids = [w.team_id for w in works]

            if parallel:
                # 并行评分
                def score_single(team_id):
                    from database import SessionLocal
                    db_single = SessionLocal()
                    try:
                        print(f"[Scorer-Thread] 开始评分 team_id={team_id}")
                        scorer = AutoScorer(db_single)
                        team = db_single.query(Team).filter(Team.id == team_id).first()
                        if not team:
                            print(f"[Scorer-Thread] team_id={team_id} 队伍不存在")
                            return (False, f"team_{team_id}", "队伍不存在")

                        print(f"[Scorer-Thread] 开始调用 scorer.score_team({team_id}, model={model}, session={task_id})")
                        result = scorer.score_team(team_id, model=model, session_id=task_id)
                        print(f"[Scorer-Thread] score_team返回: is_completed={result.is_completed}, error={result.error_message}")
                        if result.is_completed:
                            return (True, team.short_code, None)
                        else:
                            return (False, team.short_code, result.error_message)
                    except Exception as e:
                        import traceback
                        print(f"[Scorer-Thread] team_id={team_id} 异常: {str(e)}")
                        print(f"[Scorer-Thread] 堆栈: {traceback.format_exc()}")
                        return (False, f"team_{team_id}", str(e))
                    finally:
                        db_single.close()

                with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                    futures = {executor.submit(score_single, tid): tid for tid in team_ids}
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            success, team_code, error = future.result()
                            print(f"[Scorer-Main] 完成: team={team_code}, success={success}, error={error}")
                            if success:
                                task_manager.increment_success(task_id, team_code)
                            else:
                                task_manager.increment_failed(task_id, team_code, error)
                        except Exception as e:
                            tid = futures[future]
                            print(f"[Scorer-Main] future.exception: {str(e)}")
                            task_manager.increment_failed(task_id, f"team_{tid}", str(e))
            else:
                # 串行评分
                scorer = AutoScorer(db_thread)
                for work in works:
                    try:
                        team = db_thread.query(Team).filter(Team.id == work.team_id).first()
                        if not team:
                            task_manager.increment_failed(task_id, f"team_{work.team_id}", "队伍不存在")
                            continue

                        result = scorer.score_team(work.team_id, model=model, session_id=task_id)
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
        "model": model,
        "model_name": AVAILABLE_MODELS[model]["name"],
        "parallel": parallel,
        "concurrency": concurrency if parallel else 1,
    }


@router.get("/auto-all-async/status/{task_id}")
async def get_async_task_status(task_id: str):
    """查询异步批量评分任务状态"""
    from services.task_manager import task_manager

    task = task_manager.get_task(task_id)
    if not task:
        return {"message": "任务不存在", "task": None}

    return {
        "task_id": task.task_id,
        "status": task.status,
        "total": task.total,
        "success": task.success,
        "failed": task.failed,
        "current_team": task.current_team,
        "errors": task.errors[-5:] if task.errors else [],  # 只返回最后5个错误
        "started_at": task.started_at,
        "completed_at": task.completed_at
    }


@router.get("/auto-all-async/tasks")
async def list_async_tasks():
    """列出所有批量评分任务"""
    from services.task_manager import task_manager

    tasks = task_manager.list_tasks("auto_score_all")
    return {"tasks": tasks[:10]}  # 只返回最近10个任务


@router.get("/models")
async def get_available_models():
    """获取可用的LLM模型列表"""
    from config import AVAILABLE_MODELS

    models = []
    for model_id, info in AVAILABLE_MODELS.items():
        models.append({
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "speed": info["speed"],
            "quality": info["quality"],
        })

    return {"models": models}
