"""
任务书管理路由
"""
import os
import tempfile
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict

from database import get_db, TaskBook
from services.task_book_service import (
    get_task_books,
    get_task_book,
    get_active_task_book,
    create_task_book,
    update_task_book,
    delete_task_book,
    activate_task_book,
    deactivate_task_book,
    parse_word_to_markdown
)

router = APIRouter(prefix="/api/task-books", tags=["任务书管理"])


# ============ Pydantic 模型 ============
class TaskBookCreate(BaseModel):
    name: str
    content_md: str
    group_type: Optional[str] = None


class TaskBookUpdate(BaseModel):
    name: Optional[str] = None
    content_md: Optional[str] = None
    group_type: Optional[str] = None


class TaskBookResponse(BaseModel):
    id: int
    name: str
    group_type: Optional[str]
    content_md: Optional[str]
    is_active: bool
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


# ============ API 路由 ============
@router.get("", response_model=List[TaskBookResponse])
def list_task_books(
    group_type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取任务书列表"""
    task_books = get_task_books(db, group_type)
    return [
        TaskBookResponse(
            id=tb.id,
            name=tb.name,
            group_type=tb.group_type,
            content_md=tb.content_md,
            is_active=tb.is_active,
            created_at=tb.created_at.strftime("%Y-%m-%d %H:%M:%S") if tb.created_at else "",
            updated_at=tb.updated_at.strftime("%Y-%m-%d %H:%M:%S") if tb.updated_at else ""
        )
        for tb in task_books
    ]


@router.get("/active", response_model=Optional[TaskBookResponse])
def get_active(group_type: Optional[str] = None, db: Session = Depends(get_db)):
    """获取当前激活的任务书"""
    task_book = get_active_task_book(db, group_type)
    if not task_book:
        return None

    return TaskBookResponse(
        id=task_book.id,
        name=task_book.name,
        group_type=task_book.group_type,
        content_md=task_book.content_md,
        is_active=task_book.is_active,
        created_at=task_book.created_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.created_at else "",
        updated_at=task_book.updated_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.updated_at else ""
    )


@router.get("/{task_book_id}", response_model=TaskBookResponse)
def get_task_book_detail(task_book_id: int, db: Session = Depends(get_db)):
    """获取单个任务书详情"""
    task_book = get_task_book(db, task_book_id)
    if not task_book:
        raise HTTPException(status_code=404, detail="任务书不存在")

    return TaskBookResponse(
        id=task_book.id,
        name=task_book.name,
        group_type=task_book.group_type,
        content_md=task_book.content_md,
        is_active=task_book.is_active,
        created_at=task_book.created_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.created_at else "",
        updated_at=task_book.updated_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.updated_at else ""
    )


@router.post("", response_model=TaskBookResponse)
def create(task_book_data: TaskBookCreate, db: Session = Depends(get_db)):
    """创建任务书"""
    task_book = create_task_book(
        db,
        name=task_book_data.name,
        content_md=task_book_data.content_md,
        group_type=task_book_data.group_type
    )

    return TaskBookResponse(
        id=task_book.id,
        name=task_book.name,
        group_type=task_book.group_type,
        content_md=task_book.content_md,
        is_active=task_book.is_active,
        created_at=task_book.created_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.created_at else "",
        updated_at=task_book.updated_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.updated_at else ""
    )


@router.post("/upload", response_model=TaskBookResponse)
async def upload_word(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    group_type: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """上传 Word 文件并解析为任务书"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    # 检查文件类型
    if not file.filename.lower().endswith(('.doc', '.docx')):
        raise HTTPException(status_code=400, detail="只支持 .doc 或 .docx 格式")

    # 保存临时文件
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # 解析 Word 文件
        content_md = parse_word_to_markdown(tmp_path)

        # 使用文件名作为任务书名称（如果未提供）
        task_book_name = name or Path(file.filename).stem

        # 创建任务书
        task_book = create_task_book(
            db,
            name=task_book_name,
            content_md=content_md,
            group_type=group_type
        )

        return TaskBookResponse(
            id=task_book.id,
            name=task_book.name,
            group_type=task_book.group_type,
            content_md=task_book.content_md,
            is_active=task_book.is_active,
            created_at=task_book.created_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.created_at else "",
            updated_at=task_book.updated_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.updated_at else ""
        )

    finally:
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.put("/{task_book_id}", response_model=TaskBookResponse)
def update(task_book_id: int, task_book_data: TaskBookUpdate, db: Session = Depends(get_db)):
    """更新任务书"""
    task_book = update_task_book(
        db,
        task_book_id,
        name=task_book_data.name,
        content_md=task_book_data.content_md,
        group_type=task_book_data.group_type
    )

    if not task_book:
        raise HTTPException(status_code=404, detail="任务书不存在")

    return TaskBookResponse(
        id=task_book.id,
        name=task_book.name,
        group_type=task_book.group_type,
        content_md=task_book.content_md,
        is_active=task_book.is_active,
        created_at=task_book.created_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.created_at else "",
        updated_at=task_book.updated_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.updated_at else ""
    )


@router.delete("/{task_book_id}")
def delete(task_book_id: int, db: Session = Depends(get_db)):
    """删除任务书"""
    if not delete_task_book(db, task_book_id):
        raise HTTPException(status_code=404, detail="任务书不存在")
    return {"message": "删除成功"}


@router.post("/{task_book_id}/activate", response_model=TaskBookResponse)
def activate(task_book_id: int, db: Session = Depends(get_db)):
    """激活任务书"""
    task_book = activate_task_book(db, task_book_id)
    if not task_book:
        raise HTTPException(status_code=404, detail="任务书不存在")

    return TaskBookResponse(
        id=task_book.id,
        name=task_book.name,
        group_type=task_book.group_type,
        content_md=task_book.content_md,
        is_active=task_book.is_active,
        created_at=task_book.created_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.created_at else "",
        updated_at=task_book.updated_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.updated_at else ""
    )


@router.post("/{task_book_id}/deactivate", response_model=TaskBookResponse)
def deactivate_route(task_book_id: int, db: Session = Depends(get_db)):
    """取消激活任务书"""
    task_book = deactivate_task_book(db, task_book_id)
    if not task_book:
        raise HTTPException(status_code=404, detail="任务书不存在")

    return TaskBookResponse(
        id=task_book.id,
        name=task_book.name,
        group_type=task_book.group_type,
        content_md=task_book.content_md,
        is_active=task_book.is_active,
        created_at=task_book.created_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.created_at else "",
        updated_at=task_book.updated_at.strftime("%Y-%m-%d %H:%M:%S") if task_book.updated_at else ""
    )
