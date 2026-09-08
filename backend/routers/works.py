"""
作品管理API
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.orm import Session
from typing import Optional, List
from database import get_db
from services.work_parser import WorkParser
import os
from pathlib import Path

router = APIRouter()


@router.post("/upload")
async def upload_work(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """上传作品ZIP文件并自动解析"""
    if not file.filename or not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="仅支持ZIP文件")

    parser = WorkParser(db)
    result = await parser.upload_and_parse(file)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "上传失败"))

    return {
        "message": "上传并解析成功",
        "filename": result["filename"],
        "team_info": result.get("team_info"),
        "team_id": result.get("team_id"),
        "parse_result": result.get("parse_result")
    }


@router.post("/upload-folder")
async def upload_folder(
    files: List[UploadFile] = File(...),
    folder_name: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    上传文件夹（前端使用webkitdirectory选择文件夹）
    所有文件会被保存到一个临时目录，然后解析
    """
    parser = WorkParser(db)
    result = await parser.upload_folder(files, folder_name)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "上传失败"))

    return {
        "message": "文件夹上传并解析成功",
        "folder_name": folder_name,
        "team_info": result.get("team_info"),
        "team_id": result.get("team_id"),
        "files_count": result.get("files_count"),
        "parse_result": result.get("parse_result")
    }


@router.post("/batch-upload-folder")
async def batch_upload_folders(
    files: List[UploadFile] = File(...),
    max_concurrency: int = Query(default=10, ge=1, le=50, description="最大并发数"),
    db: Session = Depends(get_db)
):
    """
    批量上传多个文件夹（并发处理）
    - max_concurrency: 最大同时处理的文件夹数（默认10，最大50）
    - 前端需要传递文件夹路径信息，后端按文件夹分组处理
    """
    parser = WorkParser(db)
    result = await parser.batch_upload_folders(files, max_concurrency=max_concurrency)

    return result


@router.post("/batch-upload")
async def batch_upload_works(
    files: List[UploadFile] = File(...),
    max_concurrency: int = Query(default=10, ge=1, le=50, description="最大并发数"),
    db: Session = Depends(get_db)
):
    """
    批量上传作品ZIP文件（并发处理）
    - max_concurrency: 最大同时处理的文件数（默认10，最大50）
    """
    parser = WorkParser(db)
    return await parser.batch_upload_works(files, max_concurrency=max_concurrency)


@router.get("/status")
async def get_works_status(db: Session = Depends(get_db)):
    """获取所有作品上传和解析状态"""
    parser = WorkParser(db)
    return parser.get_all_works_status()


@router.get("/team/{team_id}")
async def get_team_work(team_id: int, db: Session = Depends(get_db)):
    """获取队伍的作品详情"""
    parser = WorkParser(db)
    work = parser.get_work_by_team(team_id)
    if not work:
        return {"message": "该队伍暂未上传作品", "work": None}
    return {"work": work}


@router.post("/parse/{work_id}")
async def parse_work(work_id: int, db: Session = Depends(get_db)):
    """重新解析指定作品"""
    parser = WorkParser(db)
    result = parser.parse_existing(work_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "解析失败"))
    return result


@router.post("/parse-all")
async def parse_all_works(db: Session = Depends(get_db)):
    """批量解析所有未解析的作品"""
    parser = WorkParser(db)
    return parser.batch_parse_all()
