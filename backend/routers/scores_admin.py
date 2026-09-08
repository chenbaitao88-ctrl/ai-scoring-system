"""scores_admin.py - 评分运维路由"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import String
from pydantic import BaseModel
from typing import Optional, List
import json
from routers.result_export import get_authoritative_export_service, export_file_response
from services.authoritative_export_service import AuthoritativeExportService
from database import get_db, Team, MachineScore, HumanScore, FinalScore

router = APIRouter()
from sqlalchemy.orm import joinedload



@router.get("/stats")
async def get_scoring_stats(db: Session = Depends(get_db)):
    """获取评分统计（基于已采用的评分）"""
    from sqlalchemy import func

    total_teams = db.query(Team).count()
    # 只统计已采用的评分
    scored_teams = db.query(MachineScore).filter(MachineScore.is_adopted == True).count()

    # 各维度平均分（只取已采用的）
    avg_query = db.query(func.avg(MachineScore.theme_score)).filter(MachineScore.is_adopted == True)
    avg_theme = avg_query.scalar() or 0
    avg_presentation = db.query(func.avg(MachineScore.presentation_score)).filter(MachineScore.is_adopted == True).scalar() or 0
    avg_process = db.query(func.avg(MachineScore.process_score)).filter(MachineScore.is_adopted == True).scalar() or 0
    avg_ai_literacy = db.query(func.avg(MachineScore.ai_literacy_score)).filter(MachineScore.is_adopted == True).scalar() or 0
    avg_total = db.query(func.avg(MachineScore.total_score)).filter(MachineScore.is_adopted == True).scalar() or 0

    return {
        "total_teams": total_teams,
        "scored_teams": scored_teams,
        "progress": f"{scored_teams}/{total_teams}",
        "avg_scores": {
            "theme": round(avg_theme, 1),
            "presentation": round(avg_presentation, 1),
            "process": round(avg_process, 1),
            "ai_literacy": round(avg_ai_literacy, 1),
            "total": round(avg_total, 1)
        }
    }


@router.post("/calculate-all")
async def calculate_all_scores(db: Session = Depends(get_db)):
    """计算所有队伍的综合评分（机器60% + 人工40%）"""
    from config import MACHINE_SCORE_WEIGHT, HUMAN_SCORE_WEIGHT

    teams = db.query(Team).all()
    results = {"total": len(teams), "calculated": 0, "skipped": 0}

    for team in teams:
        # 获取机器评分（优先用已采纳的，否则取最高分版本）
        all_machines = db.query(MachineScore).filter(
            MachineScore.team_id == team.id,
            MachineScore.is_completed == True
        ).all()
        # 优先采纳版本
        machine = next((m for m in all_machines if m.is_adopted), None)
        # 没有采纳版本则取最高分
        if not machine and all_machines:
            machine = max(all_machines, key=lambda m: m.total_score)

        # 获取人工评分（多评委取平均）
        human_scores = db.query(HumanScore).filter(HumanScore.team_id == team.id).all()

        if not machine and not human_scores:
            results["skipped"] += 1
            continue

        # 计算机器评分
        machine_total = machine.total_score if machine else 0

        # 计算人工评分平均
        if human_scores:
            human_avg = sum(h.total_score for h in human_scores) / len(human_scores)
            human_count = len(human_scores)
        else:
            human_avg = 0
            human_count = 0

        # 加权计算
        final = machine_total * MACHINE_SCORE_WEIGHT + human_avg * HUMAN_SCORE_WEIGHT

        # 保存
        existing = db.query(FinalScore).filter(FinalScore.team_id == team.id).first()

        if existing:
            existing.machine_score = machine_total
            existing.machine_weight = MACHINE_SCORE_WEIGHT
            existing.human_score_avg = human_avg
            existing.human_judge_count = human_count
            existing.human_weight = HUMAN_SCORE_WEIGHT
            existing.final_score = round(final, 2)
        else:
            new_score = FinalScore(
                team_id=team.id,
                machine_score=machine_total,
                machine_weight=MACHINE_SCORE_WEIGHT,
                human_score_avg=round(human_avg, 2),
                human_judge_count=human_count,
                human_weight=HUMAN_SCORE_WEIGHT,
                final_score=round(final, 2)
            )
            db.add(new_score)

        results["calculated"] += 1

    db.commit()

    # 计算排名
    all_finals = db.query(FinalScore).order_by(FinalScore.final_score.desc()).all()
    for rank, fs in enumerate(all_finals, 1):
        fs.ranking = rank
    db.commit()

    return results


@router.post("/compute-dual/{team_id}")
async def compute_dual_track_score(team_id: int, db: Session = Depends(get_db)):
    """计算单个队伍的双轨综合分（根据当前比赛模式的权重）
    mixed:  综合分 = AI维度分 × ai_weight + 人工维度分 × human_weight
    ai_only: 综合分 = AI维度分 × ai_weight + AI推断人工维度分 × human_weight
    """
    from database import Competition

    # 获取当前比赛配置
    comp = db.query(Competition).filter(Competition.is_active == True).first()
    if not comp:
        ai_weight, human_weight = 0.4, 0.6
        scoring_mode = "mixed"
    else:
        ai_weight = comp.ai_weight
        human_weight = comp.human_weight
        scoring_mode = comp.scoring_mode

    # 获取 AI 评分（已采纳的最新版本）
    machine = db.query(MachineScore).filter(
        MachineScore.team_id == team_id,
        MachineScore.is_adopted == True
    ).order_by(MachineScore.created_at.desc()).first()

    if not machine:
        raise HTTPException(status_code=400, detail="该队伍暂无 AI 评分")

    ai_total = machine.ai_total_score if machine else 0

    if scoring_mode == "ai_only":
        # 纯AI模式：使用AI推断的人工维度分
        inferred_total = machine.inferred_total_score or (
            (machine.inferred_presentation_score or 0) +
            (machine.inferred_creativity_score or 0) +
            (machine.inferred_process_score or 0) +
            (machine.inferred_performance_score or 0)
        )
        if inferred_total == 0:
            raise HTTPException(status_code=400, detail="该作品暂无AI推断人工维度分，请重新评分")

        composite = round(ai_total * ai_weight + inferred_total * human_weight, 2)

        existing = db.query(FinalScore).filter(FinalScore.team_id == team_id).first()
        if existing:
            existing.machine_score = machine.total_score if machine else 0
            existing.human_score_avg = inferred_total
            existing.human_judge_count = 0
            existing.composite_score = composite
            existing.scoring_mode = "ai_only"
            existing.final_score = composite
        else:
            new_fs = FinalScore(
                team_id=team_id,
                machine_score=machine.total_score if machine else 0,
                human_score_avg=round(inferred_total, 2),
                human_judge_count=0,
                composite_score=composite,
                scoring_mode="ai_only",
                final_score=composite
            )
            db.add(new_fs)

        db.commit()

        return {
            "team_id": team_id,
            "scoring_mode": "ai_only",
            "ai_weight": ai_weight,
            "human_weight": human_weight,
            "ai_total": ai_total,
            "inferred_human_score": inferred_total,
            "human_judge_count": 0,
            "composite_score": composite,
            "ai_dimensions": {
                "theme": machine.ai_theme_score if machine else 0,
                "code_quality": machine.ai_code_quality_score if machine else 0,
                "completeness": machine.ai_completeness_score if machine else 0,
                "aigc": machine.ai_aigc_score if machine else 0,
            } if machine else None,
            "inferred_dimensions": {
                "presentation": machine.inferred_presentation_score if machine else 0,
                "creativity": machine.inferred_creativity_score if machine else 0,
                "process": machine.inferred_process_score if machine else 0,
                "performance": machine.inferred_performance_score if machine else 0,
            } if machine else None,
            "note": "人工维度为AI推断，准确性有限",
        }

    else:
        # mixed 模式：使用真实人工评分
        human_scores = db.query(HumanScore).filter(HumanScore.team_id == team_id).all()
        if not human_scores:
            raise HTTPException(status_code=404, detail="该队伍暂无人评分")

        human_presentation = sum(h.human_presentation_score for h in human_scores) / len(human_scores)
        human_creativity = sum(h.human_creativity_score for h in human_scores) / len(human_scores)
        human_process = sum(h.human_process_score for h in human_scores) / len(human_scores)
        human_performance = sum(h.human_performance_score for h in human_scores) / len(human_scores)
        human_total = sum(h.human_total_score for h in human_scores) / len(human_scores)
        human_count = len(human_scores)

        composite = round(ai_total * ai_weight + human_total * human_weight, 2)

        existing = db.query(FinalScore).filter(FinalScore.team_id == team_id).first()
        if existing:
            existing.machine_score = machine.total_score if machine else 0
            existing.human_score_avg = human_total
            existing.human_judge_count = human_count
            existing.composite_score = composite
            existing.scoring_mode = "mixed"
            existing.final_score = composite
        else:
            new_fs = FinalScore(
                team_id=team_id,
                machine_score=machine.total_score if machine else 0,
                human_score_avg=round(human_total, 2),
                human_judge_count=human_count,
                composite_score=composite,
                scoring_mode="mixed",
                final_score=composite
            )
            db.add(new_fs)

        db.commit()

        return {
            "team_id": team_id,
            "scoring_mode": "mixed",
            "ai_weight": ai_weight,
            "human_weight": human_weight,
            "ai_total": ai_total,
            "human_total": round(human_total, 2),
            "human_judge_count": human_count,
            "composite_score": composite,
            "ai_dimensions": {
                "theme": machine.ai_theme_score if machine else 0,
                "code_quality": machine.ai_code_quality_score if machine else 0,
                "completeness": machine.ai_completeness_score if machine else 0,
                "aigc": machine.ai_aigc_score if machine else 0,
            } if machine else None,
            "human_dimensions": {
                "presentation": round(human_presentation, 2),
                "creativity": round(human_creativity, 2),
                "process": round(human_process, 2),
                "performance": round(human_performance, 2),
            } if human_scores else None,
        }



@router.post("/adopt-best")
async def adopt_best_score(db: Session = Depends(get_db)):
    """全局最优采纳：每个队伍自动采纳最高分版本"""
    teams = db.query(Team).all()
    adopted_count = 0

    for team in teams:
        all_scores = db.query(MachineScore).filter(
            MachineScore.team_id == team.id,
            MachineScore.is_completed == True
        ).all()
        if not all_scores:
            continue

        # 取消所有现有采纳
        for s in all_scores:
            s.is_adopted = False

        # 找出最高分版本
        best = max(all_scores, key=lambda m: m.total_score)
        best.is_adopted = True
        adopted_count += 1

    db.commit()
    return {"message": f"全局最优采纳完成，共处理 {adopted_count} 支队伍", "adopted_count": adopted_count}


@router.post("/calibrate")
async def calibrate_scores(
    min_target: float = 60.0,
    max_target: float = 90.0,
    db: Session = Depends(get_db)
):
    """DEPRECATED (2026-05-26 P1-6): 已被分位数映射校准替代

    原功能：将所有已采纳分数线性拉伸到 [min_target, max_target] 区间
    现行为：直接返回错误，提示使用 P1-6 实时校准（calibrated_score 字段）
    """
    raise HTTPException(
        status_code=410,
        detail="此接口已废弃。评分时自动写入 calibrated_score（分位数映射校准），无需手动调用。"
    )

# --- 原 calibrate 实现（已废弃，保留备查）---
# @router.post("/calibrate")
# async def calibrate_scores_old(...):
#     ...
#     # 计算公式：new_score = ((old_score - old_min) / (old_max - old_min)) * (max_target - min_target) + min_target
#     ...
# # END DEPRECATED


@router.get("/export")
def export_scores(
    task_id: Optional[str] = Query(default=None), item_id: Optional[List[str]] = Query(default=None),
    service: AuthoritativeExportService = Depends(get_authoritative_export_service),
):
    """旧CSV地址也必须经过权威结果检查，不再回退到旧综合分。"""
    return export_file_response(service, task_id, "csv", item_id)
