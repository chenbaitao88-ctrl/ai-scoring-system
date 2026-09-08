"""
评审表管理路由
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict

from database import get_db, ScoringForm
from services.scoring_form_service import (
    get_scoring_forms,
    get_scoring_form,
    get_active_scoring_form,
    get_default_scoring_form,
    create_scoring_form,
    update_scoring_form,
    delete_scoring_form,
    activate_scoring_form,
    init_default_scoring_form
)

router = APIRouter(prefix="/api/scoring-forms", tags=["评审表管理"])


# ============ Pydantic 模型 ============
class DimensionModel(BaseModel):
    name: str
    max_score: int
    description: Optional[str] = None


class ScoringFormCreate(BaseModel):
    name: str
    dimensions: List[DimensionModel]


class ScoringFormUpdate(BaseModel):
    name: Optional[str] = None
    dimensions: Optional[List[DimensionModel]] = None


class ScoringFormResponse(BaseModel):
    id: int
    name: str
    dimensions: List[dict]
    is_default: bool
    is_active: bool
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


# ============ API 路由 ============
@router.get("", response_model=List[ScoringFormResponse])
def list_scoring_forms(db: Session = Depends(get_db)):
    """获取评审表列表"""
    forms = get_scoring_forms(db)
    return [
        ScoringFormResponse(
            id=f.id,
            name=f.name,
            dimensions=f.dimensions or [],
            is_default=f.is_default,
            is_active=f.is_active,
            created_at=f.created_at.strftime("%Y-%m-%d %H:%M:%S") if f.created_at else "",
            updated_at=f.updated_at.strftime("%Y-%m-%d %H:%M:%S") if f.updated_at else ""
        )
        for f in forms
    ]


@router.get("/active", response_model=Optional[ScoringFormResponse])
def get_active(db: Session = Depends(get_db)):
    """获取当前激活的评审表"""
    form = get_active_scoring_form(db)
    if not form:
        # 如果没有激活的评审表，返回默认评审表
        form = init_default_scoring_form(db)

    return ScoringFormResponse(
        id=form.id,
        name=form.name,
        dimensions=form.dimensions or [],
        is_default=form.is_default,
        is_active=form.is_active,
        created_at=form.created_at.strftime("%Y-%m-%d %H:%M:%S") if form.created_at else "",
        updated_at=form.updated_at.strftime("%Y-%m-%d %H:%M:%S") if form.updated_at else ""
    )


@router.get("/default", response_model=Optional[ScoringFormResponse])
def get_default(db: Session = Depends(get_db)):
    """获取系统默认评审表"""
    form = get_default_scoring_form(db)
    if not form:
        form = init_default_scoring_form(db)

    return ScoringFormResponse(
        id=form.id,
        name=form.name,
        dimensions=form.dimensions or [],
        is_default=form.is_default,
        is_active=form.is_active,
        created_at=form.created_at.strftime("%Y-%m-%d %H:%M:%S") if form.created_at else "",
        updated_at=form.updated_at.strftime("%Y-%m-%d %H:%M:%S") if form.updated_at else ""
    )


@router.get("/{scoring_form_id}", response_model=ScoringFormResponse)
def get_scoring_form_detail(scoring_form_id: int, db: Session = Depends(get_db)):
    """获取单个评审表详情"""
    form = get_scoring_form(db, scoring_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="评审表不存在")

    return ScoringFormResponse(
        id=form.id,
        name=form.name,
        dimensions=form.dimensions or [],
        is_default=form.is_default,
        is_active=form.is_active,
        created_at=form.created_at.strftime("%Y-%m-%d %H:%M:%S") if form.created_at else "",
        updated_at=form.updated_at.strftime("%Y-%m-%d %H:%M:%S") if form.updated_at else ""
    )


@router.post("", response_model=ScoringFormResponse)
def create(form_data: ScoringFormCreate, db: Session = Depends(get_db)):
    """创建评审表"""
    dimensions = [d.model_dump() for d in form_data.dimensions]

    form = create_scoring_form(
        db,
        name=form_data.name,
        dimensions=dimensions
    )

    return ScoringFormResponse(
        id=form.id,
        name=form.name,
        dimensions=form.dimensions or [],
        is_default=form.is_default,
        is_active=form.is_active,
        created_at=form.created_at.strftime("%Y-%m-%d %H:%M:%S") if form.created_at else "",
        updated_at=form.updated_at.strftime("%Y-%m-%d %H:%M:%S") if form.updated_at else ""
    )


@router.put("/{scoring_form_id}", response_model=ScoringFormResponse)
def update(scoring_form_id: int, form_data: ScoringFormUpdate, db: Session = Depends(get_db)):
    """更新评审表"""
    dimensions = None
    if form_data.dimensions:
        dimensions = [d.model_dump() for d in form_data.dimensions]

    form = update_scoring_form(
        db,
        scoring_form_id,
        name=form_data.name,
        dimensions=dimensions
    )

    if not form:
        raise HTTPException(status_code=404, detail="评审表不存在")

    return ScoringFormResponse(
        id=form.id,
        name=form.name,
        dimensions=form.dimensions or [],
        is_default=form.is_default,
        is_active=form.is_active,
        created_at=form.created_at.strftime("%Y-%m-%d %H:%M:%S") if form.created_at else "",
        updated_at=form.updated_at.strftime("%Y-%m-%d %H:%M:%S") if form.updated_at else ""
    )


@router.delete("/{scoring_form_id}")
def delete(scoring_form_id: int, db: Session = Depends(get_db)):
    """删除评审表"""
    form = get_scoring_form(db, scoring_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="评审表不存在")

    if form.is_default:
        raise HTTPException(status_code=400, detail="不能删除默认评审表")

    if not delete_scoring_form(db, scoring_form_id):
        raise HTTPException(status_code=500, detail="删除失败")

    return {"message": "删除成功"}


@router.post("/{scoring_form_id}/activate", response_model=ScoringFormResponse)
def activate(scoring_form_id: int, db: Session = Depends(get_db)):
    """激活评审表"""
    form = activate_scoring_form(db, scoring_form_id)
    if not form:
        raise HTTPException(status_code=404, detail="评审表不存在")

    return ScoringFormResponse(
        id=form.id,
        name=form.name,
        dimensions=form.dimensions or [],
        is_default=form.is_default,
        is_active=form.is_active,
        created_at=form.created_at.strftime("%Y-%m-%d %H:%M:%S") if form.created_at else "",
        updated_at=form.updated_at.strftime("%Y-%m-%d %H:%M:%S") if form.updated_at else ""
    )
