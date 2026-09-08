"""
比赛配置API
- 创建/管理比赛
- 获取当前激活比赛
- 切换评分模式（mixed / ai_only）
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Dict, Any
from database import get_db, Competition

router = APIRouter()


class CompetitionCreate(BaseModel):
    name: str
    year: Optional[int] = None
    group_type: Optional[str] = None
    scoring_mode: str = "mixed"
    ai_weight: float = 0.4
    human_weight: float = 0.6
    description: Optional[str] = None
    # GENERIC: Phase 2 新增字段
    dimensions_config: Optional[Dict[str, Any]] = None
    naming_pattern: Optional[str] = None
    naming_example: Optional[str] = None
    judge_groups: Optional[int] = 4
    judges_per_group: Optional[int] = 2
    machine_weight: Optional[float] = 0.6
    task_book_ids: Optional[list] = None


class CompetitionUpdate(BaseModel):
    name: Optional[str] = None
    scoring_mode: Optional[str] = None
    ai_weight: Optional[float] = None
    human_weight: Optional[float] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None
    # GENERIC: Phase 2 新增字段
    dimensions_config: Optional[Dict[str, Any]] = None
    naming_pattern: Optional[str] = None
    naming_example: Optional[str] = None
    judge_groups: Optional[int] = None
    judges_per_group: Optional[int] = None
    machine_weight: Optional[float] = None
    task_book_ids: Optional[list] = None


def _comp_to_dict(comp: Competition) -> dict:
    """将 Competition ORM 对象转为字典"""
    return {
        "id": comp.id,
        "name": comp.name,
        "year": comp.year,
        "group_type": comp.group_type,
        "scoring_mode": comp.scoring_mode,
        "ai_weight": comp.ai_weight,
        "human_weight": comp.human_weight,
        "is_active": comp.is_active,
        "description": comp.description,
        "dimensions_config": comp.dimensions_config,
        "naming_pattern": comp.naming_pattern,
        "naming_example": comp.naming_example,
        "judge_groups": comp.judge_groups,
        "judges_per_group": comp.judges_per_group,
        "machine_weight": comp.machine_weight,
        "task_book_ids": comp.task_book_ids,
        "created_at": comp.created_at.isoformat() if comp.created_at else None,
        "updated_at": comp.updated_at.isoformat() if comp.updated_at else None,
    }


@router.get("")
async def list_competitions(db: Session = Depends(get_db)):
    """获取所有比赛列表"""
    comps = db.query(Competition).order_by(Competition.created_at.desc()).all()
    return {
        "competitions": [_comp_to_dict(c) for c in comps]
    }


@router.get("/current")
async def get_current_competition(db: Session = Depends(get_db)):
    """获取当前活跃的比赛（包含完整配置）"""
    comp = db.query(Competition).filter(Competition.is_active == True).first()
    if not comp:
        return {"message": "当前无激活比赛", "competition": None}
    return {
        "competition": _comp_to_dict(comp)
    }


@router.post("")
async def create_competition(data: CompetitionCreate, db: Session = Depends(get_db)):
    """创建新比赛"""
    if data.scoring_mode not in ("mixed", "ai_only"):
        raise HTTPException(status_code=400, detail="scoring_mode 必须是 mixed 或 ai_only")

    comp = Competition(
        name=data.name,
        year=data.year,
        group_type=data.group_type,
        scoring_mode=data.scoring_mode,
        ai_weight=data.ai_weight,
        human_weight=data.human_weight,
        description=data.description,
        is_active=False,
        dimensions_config=data.dimensions_config,
        naming_pattern=data.naming_pattern,
        naming_example=data.naming_example,
        judge_groups=data.judge_groups,
        judges_per_group=data.judges_per_group,
        machine_weight=data.machine_weight,
        task_book_ids=data.task_book_ids,
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)
    return {"message": "比赛创建成功", "competition_id": comp.id}


@router.put("/{competition_id}")
async def update_competition(
    competition_id: int,
    data: CompetitionUpdate,
    db: Session = Depends(get_db)
):
    """更新比赛配置"""
    comp = db.query(Competition).filter(Competition.id == competition_id).first()
    if not comp:
        raise HTTPException(status_code=404, detail="比赛不存在")

    if data.scoring_mode is not None and data.scoring_mode not in ("mixed", "ai_only"):
        raise HTTPException(status_code=400, detail="scoring_mode 必须是 mixed 或 ai_only")

    for field in [
        "name", "scoring_mode", "ai_weight", "human_weight", "is_active",
        "description", "dimensions_config", "naming_pattern", "naming_example",
        "judge_groups", "judges_per_group", "machine_weight", "task_book_ids",
    ]:
        value = getattr(data, field)
        if value is not None:
            setattr(comp, field, value)

    db.commit()
    db.refresh(comp)
    return {"message": "比赛配置更新成功", "competition": _comp_to_dict(comp)}


@router.post("/{competition_id}/activate")
async def activate_competition(competition_id: int, db: Session = Depends(get_db)):
    """激活指定比赛（同时取消其他激活）"""
    db.query(Competition).update({Competition.is_active: False})

    comp = db.query(Competition).filter(Competition.id == competition_id).first()
    if not comp:
        raise HTTPException(status_code=404, detail="比赛不存在")

    comp.is_active = True
    db.commit()
    return {"message": f"比赛 '{comp.name}' 已激活", "competition": _comp_to_dict(comp)}


@router.get("/{competition_id}/dimensions")
async def get_competition_dimensions(competition_id: int, db: Session = Depends(get_db)):
    """获取指定比赛的评分维度配置"""
    comp = db.query(Competition).filter(Competition.id == competition_id).first()
    if not comp:
        raise HTTPException(status_code=404, detail="比赛不存在")

    dims = comp.dimensions_config or {}
    return {
        "competition_id": comp.id,
        "competition_name": comp.name,
        "dimensions": dims,
    }
