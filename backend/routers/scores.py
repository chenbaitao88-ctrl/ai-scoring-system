"""
评分管理API（聚合入口）
- AI自动评分
- 人工评分
- 综合评分
"""
from fastapi import APIRouter
from .scores_auto import router as scores_auto_router
from .scores_human import router as scores_human_router
from .scores_admin import router as scores_admin_router

router = APIRouter()
router.include_router(scores_auto_router)
router.include_router(scores_human_router)
router.include_router(scores_admin_router)
