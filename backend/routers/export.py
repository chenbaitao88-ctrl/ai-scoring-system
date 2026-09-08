"""
数据导出API（路由层）
- Excel评分汇总表
- Word现场记录表
- 数据备份
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from services.export_service import (
    get_score_summary_dict,
    create_backup,
)
from typing import Optional, List
from routers.result_export import get_authoritative_export_service, export_file_response
from services.authoritative_export_service import AuthoritativeExportService

router = APIRouter()

@router.get("/excel")
async def export_excel(
    task_id: Optional[str] = Query(default=None), item_id: Optional[List[str]] = Query(default=None),
    service: AuthoritativeExportService = Depends(get_authoritative_export_service),
):
    """兼容旧URL，但只允许显式任务范围内的权威结果。"""
    return export_file_response(service, task_id, "xlsx", item_id)

@router.get("/word")
async def export_word(
    task_id: Optional[str] = Query(default=None), item_id: Optional[List[str]] = Query(default=None),
    service: AuthoritativeExportService = Depends(get_authoritative_export_service),
):
    return export_file_response(service, task_id, "docx", item_id)

@router.get("/score-summary")
async def get_score_summary(db: Session = Depends(get_db)):
    """获取评分汇总数据（前端展示用）"""
    return get_score_summary_dict(db)

@router.post("/backup")
async def backup_data(db: Session = Depends(get_db)):
    """备份数据库和上传的文件"""
    return create_backup()
