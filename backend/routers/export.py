"""
数据导出API（路由层）
- Excel评分汇总表
- Word现场记录表
- 数据备份
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db
from services.export_service import (
    build_excel_export,
    build_word_export,
    get_score_summary_dict,
    create_backup,
)

router = APIRouter()

@router.get("/excel")
async def export_excel(db: Session = Depends(get_db)):
    """导出评分汇总Excel"""
    export_path, filename = build_excel_export(db)
    return FileResponse(
        path=export_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@router.get("/word")
async def export_word(db: Session = Depends(get_db)):
    """导出Word现场记录表"""
    export_path, filename = build_word_export(db)
    return FileResponse(
        path=export_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

@router.get("/score-summary")
async def get_score_summary(db: Session = Depends(get_db)):
    """获取评分汇总数据（前端展示用）"""
    return get_score_summary_dict(db)

@router.post("/backup")
async def backup_data(db: Session = Depends(get_db)):
    """备份数据库和上传的文件"""
    return create_backup()
