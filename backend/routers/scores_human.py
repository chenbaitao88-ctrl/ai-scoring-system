"""scores_human.py - 人工评分与结果管理路由"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import String
from pydantic import BaseModel
from typing import Optional
from database import get_db, Team, MachineScore, HumanScore, FinalScore
from utils.json_fields import json_list

router = APIRouter()


class HumanScoreInput(BaseModel):
    """人工评分输入模型（双轨评分：4 个人工维度）"""
    team_id: int
    judge_name: str
    # 旧 4 维（兼容存量）
    theme_score: Optional[float] = 0
    presentation_score: Optional[float] = 0
    process_score: Optional[float] = 0
    ai_literacy_score: Optional[float] = 0
    # 双轨评分：人工维度（混合模式评委输入）
    human_presentation_score: Optional[float] = 0
    human_creativity_score: Optional[float] = 0
    human_process_score: Optional[float] = 0
    human_performance_score: Optional[float] = 0
    comment: Optional[str] = None



@router.get("/history/{team_id}")
async def get_score_history(
    team_id: int,
    db: Session = Depends(get_db)
):
    """获取单个队伍的评分历史记录"""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="队伍不存在")

    scores = db.query(MachineScore).filter(
        MachineScore.team_id == team_id
    ).order_by(MachineScore.created_at.desc()).all()

    return {
        "team_id": team_id,
        "team_name": team.team_name,
        "short_code": team.short_code,
        "history": [
            {
                "id": s.id,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "model": s.model_name,
                "ai_score": s.total_score,
                "calibrated_score": s.calibrated_score,
                "anchor_level": team.work.anchor_level if team.work else "Lv0",
                "flags": json_list(s.flags),
                "is_adopted": s.is_adopted,
                "dimensions": {
                    "theme": s.theme_score,
                    "presentation": s.presentation_score,
                    "process": s.process_score,
                    "ai_literacy": s.ai_literacy_score,
                },
            }
            for s in scores
        ]
    }


@router.get("/result/{team_id}")
async def get_score_result(
    team_id: int,
    db: Session = Depends(get_db)
):
    """获取队伍的机器评分结果（多版本）"""
    all_scores = db.query(MachineScore).filter(
        MachineScore.team_id == team_id
    ).order_by(MachineScore.created_at.desc()).all()

    if not all_scores:
        return {"message": "该队伍暂未评分", "score": None, "all_scores": []}

    # 找到采用的评分
    adopted = next((s for s in all_scores if s.is_adopted), all_scores[0])

    # 获取队伍的锚点等级（从 work 关联读取）
    team = db.query(Team).filter(Team.id == team_id).first()
    anchor_level = team.work.anchor_level if team and team.work else "Lv0"

    return {
        "score": {
            "id": adopted.id,
            "team_id": adopted.team_id,
            "model_name": adopted.model_name,
            "theme_score": adopted.theme_score,
            "presentation_score": adopted.presentation_score,
            "process_score": adopted.process_score,
            "ai_literacy_score": adopted.ai_literacy_score,
            "total_score": adopted.total_score,
            # P1-6 (2026-05-26): 新增校准分、锚点等级、置信度、Flag标签
            "calibrated_score": adopted.calibrated_score,
            "anchor_level": anchor_level,
            "confidence": adopted.confidence,
            "flags": json_list(adopted.flags),
            "is_adopted": adopted.is_adopted,
            "is_completed": adopted.is_completed,
            "scoring_details": adopted.scoring_details,
            "theme_comment": adopted.theme_comment,
            "presentation_comment": adopted.presentation_comment,
            "process_comment": adopted.process_comment,
            "ai_literacy_comment": adopted.ai_literacy_comment,
            "overall_comment": adopted.overall_comment,
            "code_quality_details": adopted.code_quality_details,
            "aigc_analysis_details": adopted.aigc_analysis_details,
            "feature_detection_details": adopted.feature_detection_details,
            "defense_questions": adopted.defense_questions,
            "created_at": adopted.created_at.isoformat() if adopted.created_at else None
        },
        "all_scores": [
            {
                "id": s.id,
                "model_name": s.model_name,
                "theme_score": s.theme_score,
                "presentation_score": s.presentation_score,
                "process_score": s.process_score,
                "ai_literacy_score": s.ai_literacy_score,
                "total_score": s.total_score,
                "is_adopted": s.is_adopted,
                "scoring_session": s.scoring_session,
                "created_at": s.created_at.isoformat() if s.created_at else None
            }
            for s in all_scores
        ]
    }


@router.post("/result/{team_id}/adopt/{score_id}")
async def adopt_score(
    team_id: int,
    score_id: int,
    db: Session = Depends(get_db)
):
    """采用指定版本的评分"""
    # 取消该队伍所有评分的"采用"状态
    db.query(MachineScore).filter(MachineScore.team_id == team_id).update({"is_adopted": False})

    # 设置指定评分为采用
    score = db.query(MachineScore).filter(MachineScore.id == score_id, MachineScore.team_id == team_id).first()
    if not score:
        raise HTTPException(status_code=404, detail="评分记录不存在")

    score.is_adopted = True
    db.commit()

    return {"message": "已采用该评分", "score_id": score_id}


@router.get("/teams-for-scoring")
async def get_teams_for_scoring(
    judge_group: Optional[int] = None,
    short_code: Optional[str] = None,
    member_name: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取待评分队伍列表（可按评审组、编号、选手姓名筛选）"""
    from sqlalchemy.orm import joinedload

    query = db.query(Team).options(
        joinedload(Team.work),
        joinedload(Team.machine_scores),
        joinedload(Team.human_scores)
    )

    if judge_group:
        query = query.filter(Team.judge_group == judge_group)

    if short_code:
        query = query.filter(Team.short_code.contains(short_code))

    teams = query.all()

    # 在Python层面过滤选手姓名（支持members数组中任意成员的name匹配）
    if member_name:
        member_name_lower = member_name.lower()
        filtered_teams = []
        for team in teams:
            # 同时搜索队名和成员姓名
            match = False
            if team.team_name and member_name_lower in team.team_name.lower():
                match = True
            else:
                members = team.members or []
                if isinstance(members, list):
                    for m in members:
                        if isinstance(m, dict) and m.get('name') and member_name_lower in m['name'].lower():
                            match = True
                            break
            if match:
                filtered_teams.append(team)
        teams = filtered_teams

    result = []
    for team in teams:
        # 获取该队伍的机器评分（取已采用的最新评分，或最新的已完成评分）
        machine_scores_list = team.machine_scores or []
        # 先找已采用的评分
        machine = next((m for m in machine_scores_list if m.is_completed and m.is_adopted), None)
        # 如果没有已采用的，取最新的已完成评分（按id降序）
        if not machine:
            completed_scores = sorted(
                [m for m in machine_scores_list if m.is_completed],
                key=lambda m: m.id,
                reverse=True
            )
            machine = completed_scores[0] if completed_scores else None
        # 获取人工评分列表
        human_list = team.human_scores or []

        # 获取综合分
        final_score = db.query(FinalScore).filter(FinalScore.team_id == team.id).first()

        # 提取选手1姓名
        members = team.members or []
        member1_name = ""
        if isinstance(members, list) and len(members) > 0:
            member1_name = members[0].get("name", "") if isinstance(members[0], dict) else ""
        elif isinstance(members, dict):
            member1_name = members.get("player1_name", "")

        result.append({
            "id": team.id,
            "short_code": team.short_code,
            "team_name": team.team_name,
            "school": team.school,
            "group_type": team.group_type,
            "judge_group": team.judge_group,
            "member1_name": member1_name,
            "machine_score": machine.total_score if machine and machine.is_completed else None,
            # P1-6 (2026-05-26): 新增校准分、锚点等级、置信度、Flag标签
            "calibrated_score": machine.calibrated_score if machine and machine.is_completed else None,
            "anchor_level": team.work.anchor_level if team.work else "Lv0",
            "confidence": machine.confidence if machine else "UNKNOWN",
            "flags": json_list(machine.flags) if machine else [],
            "machine_details": {
                "theme": machine.theme_score if machine else None,
                "presentation": machine.presentation_score if machine else None,
                "process": machine.process_score if machine else None,
                "ai_literacy": machine.ai_literacy_score if machine else None,
            } if machine and machine.is_completed else None,
            # 双轨AI维度分
            "ai_dimensions": {
                "theme": machine.ai_theme_score if machine else None,
                "code_quality": machine.ai_code_quality_score if machine else None,
                "completeness": machine.ai_completeness_score if machine else None,
                "aigc": machine.ai_aigc_score if machine else None,
                "total": machine.ai_total_score if machine else None,
            } if machine and machine.is_completed else None,
            "machine_comments": {
                "theme": machine.theme_comment if machine else None,
                "presentation": machine.presentation_comment if machine else None,
                "process": machine.process_comment if machine else None,
                "ai_literacy": machine.ai_literacy_comment if machine else None,
                "overall": machine.overall_comment if machine else None,
            } if machine and machine.is_completed else None,
            "defense_questions": machine.defense_questions if machine and machine.defense_questions else None,
            "human_scores": [
                {
                    "judge_name": h.judge_name,
                    # 旧维度（兼容）
                    "theme_score": h.theme_score,
                    "presentation_score": h.presentation_score,
                    "process_score": h.process_score,
                    "ai_literacy_score": h.ai_literacy_score,
                    "total_score": h.total_score,
                    # 双轨人工维度
                    "human_presentation_score": h.human_presentation_score,
                    "human_creativity_score": h.human_creativity_score,
                    "human_process_score": h.human_process_score,
                    "human_performance_score": h.human_performance_score,
                    "human_total_score": h.human_total_score,
                    "comment": h.comment,
                    "created_at": h.created_at.isoformat() if h.created_at else None,
                }
                for h in human_list
            ],
            "has_human_score": len(human_list) > 0,
            "composite_score": final_score.composite_score if final_score else None,
            "scoring_mode": final_score.scoring_mode if final_score else None,
            "inferred_dimensions": {
                "presentation": machine.inferred_presentation_score if machine else None,
                "creativity": machine.inferred_creativity_score if machine else None,
                "process": machine.inferred_process_score if machine else None,
                "performance": machine.inferred_performance_score if machine else None,
                "total": machine.inferred_total_score if machine else None,
            } if machine and machine.is_completed else None,
        })

    return {"teams": result, "total": len(result)}


@router.post("/human")
async def submit_human_score(
    score: HumanScoreInput,
    db: Session = Depends(get_db)
):
    """提交人工评分"""
    # 验证队伍存在
    team = db.query(Team).filter(Team.id == score.team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="队伍不存在")

    # 验证旧维度分数范围（兼容）
    if score.theme_score and not (0 <= score.theme_score <= 20):
        raise HTTPException(status_code=400, detail="主题立意分数范围0-20")
    if score.presentation_score and not (0 <= score.presentation_score <= 30):
        raise HTTPException(status_code=400, detail="产品表现力分数范围0-30")
    if score.process_score and not (0 <= score.process_score <= 30):
        raise HTTPException(status_code=400, detail="过程完整性分数范围0-30")
    if score.ai_literacy_score and not (0 <= score.ai_literacy_score <= 20):
        raise HTTPException(status_code=400, detail="AI素养分数范围0-20")

    # 验证双轨人工维度分数范围
    if score.human_presentation_score and not (0 <= score.human_presentation_score <= 25):
        raise HTTPException(status_code=400, detail="产品表现力(人工)分数范围0-25")
    if score.human_creativity_score and not (0 <= score.human_creativity_score <= 15):
        raise HTTPException(status_code=400, detail="创意深度分数范围0-15")
    if score.human_process_score and not (0 <= score.human_process_score <= 15):
        raise HTTPException(status_code=400, detail="过程深度分数范围0-15")
    if score.human_performance_score and not (0 <= score.human_performance_score <= 5):
        raise HTTPException(status_code=400, detail="现场表现分数范围0-5")

    # 旧总分（兼容）
    old_total = (score.theme_score or 0) + (score.presentation_score or 0) + (score.process_score or 0) + (score.ai_literacy_score or 0)
    # 双轨人工总分
    human_total = (score.human_presentation_score or 0) + (score.human_creativity_score or 0) + (score.human_process_score or 0) + (score.human_performance_score or 0)

    # 检查该评委是否已评分
    existing = db.query(HumanScore).filter(
        HumanScore.team_id == score.team_id,
        HumanScore.judge_name == score.judge_name
    ).first()

    if existing:
        # 更新评分
        existing.theme_score = score.theme_score or existing.theme_score
        existing.presentation_score = score.presentation_score or existing.presentation_score
        existing.process_score = score.process_score or existing.process_score
        existing.ai_literacy_score = score.ai_literacy_score or existing.ai_literacy_score
        existing.total_score = old_total or existing.total_score
        # 双轨人工维度
        existing.human_presentation_score = score.human_presentation_score or existing.human_presentation_score
        existing.human_creativity_score = score.human_creativity_score or existing.human_creativity_score
        existing.human_process_score = score.human_process_score or existing.human_process_score
        existing.human_performance_score = score.human_performance_score or existing.human_performance_score
        existing.human_total_score = human_total or existing.human_total_score
        existing.comment = score.comment or existing.comment
    else:
        # 新建评分
        human_score = HumanScore(
            team_id=score.team_id,
            judge_name=score.judge_name,
            judge_group=team.judge_group,
            theme_score=score.theme_score or 0,
            presentation_score=score.presentation_score or 0,
            process_score=score.process_score or 0,
            ai_literacy_score=score.ai_literacy_score or 0,
            total_score=old_total,
            # 双轨人工维度
            human_presentation_score=score.human_presentation_score or 0,
            human_creativity_score=score.human_creativity_score or 0,
            human_process_score=score.human_process_score or 0,
            human_performance_score=score.human_performance_score or 0,
            human_total_score=human_total,
            comment=score.comment
        )
        db.add(human_score)

    db.commit()

    return {
        "message": "评分提交成功",
        "team_id": score.team_id,
        "judge_name": score.judge_name,
        "old_total": old_total,
        "human_total": human_total
    }


@router.get("/final/{team_id}")
async def get_final_score(team_id: int, db: Session = Depends(get_db)):
    """获取队伍综合评分"""
    final = db.query(FinalScore).filter(FinalScore.team_id == team_id).first()
    if not final:
        return {"message": "该队伍暂无综合评分", "final_score": None}

    return {
        "final_score": {
            "team_id": final.team_id,
            "machine_score": final.machine_score,
            "machine_weight": final.machine_weight,
            "human_score_avg": final.human_score_avg,
            "human_judge_count": final.human_judge_count,
            "human_weight": final.human_weight,
            "final_score": final.final_score,
            "ranking": final.ranking,
            "composite_score": final.composite_score,
            "scoring_mode": final.scoring_mode,
        }
    }


@router.get("/score/{score_id}")
async def get_score_by_id(
    score_id: int,
    db: Session = Depends(get_db)
):
    """根据ID获取单条机器评分的完整详情（用于版本切换）"""
    score = db.query(MachineScore).filter(MachineScore.id == score_id).first()
    if not score:
        raise HTTPException(status_code=404, detail="评分记录不存在")

    return {
        "id": score.id,
        "team_id": score.team_id,
        "model_name": score.model_name,
        "theme_score": score.theme_score,
        "presentation_score": score.presentation_score,
        "process_score": score.process_score,
        "ai_literacy_score": score.ai_literacy_score,
        "total_score": score.total_score,
        "is_adopted": score.is_adopted,
        "is_completed": score.is_completed,
        "scoring_details": score.scoring_details,
        "theme_comment": score.theme_comment,
        "presentation_comment": score.presentation_comment,
        "process_comment": score.process_comment,
        "ai_literacy_comment": score.ai_literacy_comment,
        "overall_comment": score.overall_comment,
        "code_quality_details": score.code_quality_details,
        "aigc_analysis_details": score.aigc_analysis_details,
        "feature_detection_details": score.feature_detection_details,
        "defense_questions": score.defense_questions,
        "scoring_session": score.scoring_session,
        "created_at": score.created_at.isoformat() if score.created_at else None
    }
