"""
任务书管理服务
"""
import os
import tempfile
from pathlib import Path
from typing import List, Optional
from sqlalchemy.orm import Session
from database import TaskBook


def get_task_books(db: Session, group_type: Optional[str] = None) -> List[TaskBook]:
    """获取任务书列表"""
    query = db.query(TaskBook)
    if group_type:
        query = query.filter(TaskBook.group_type == group_type)
    return query.order_by(TaskBook.created_at.desc()).all()


def get_task_book(db: Session, task_book_id: int) -> Optional[TaskBook]:
    """获取单个任务书"""
    return db.query(TaskBook).filter(TaskBook.id == task_book_id).first()


def get_active_task_book(db: Session, group_type: Optional[str] = None) -> Optional[TaskBook]:
    """获取当前激活的任务书"""
    query = db.query(TaskBook).filter(TaskBook.is_active == True)
    if group_type:
        query = query.filter(TaskBook.group_type == group_type)
    return query.first()


def create_task_book(
    db: Session,
    name: str,
    content_md: str,
    group_type: Optional[str] = None
) -> TaskBook:
    """创建任务书"""
    task_book = TaskBook(
        name=name,
        content_md=content_md,
        group_type=group_type,
        is_active=False
    )
    db.add(task_book)
    db.commit()
    db.refresh(task_book)
    return task_book


def update_task_book(
    db: Session,
    task_book_id: int,
    name: Optional[str] = None,
    content_md: Optional[str] = None,
    group_type: Optional[str] = None
) -> Optional[TaskBook]:
    """更新任务书"""
    task_book = get_task_book(db, task_book_id)
    if not task_book:
        return None

    if name is not None:
        task_book.name = name
    if content_md is not None:
        task_book.content_md = content_md
    if group_type is not None:
        task_book.group_type = group_type

    db.commit()
    db.refresh(task_book)
    return task_book


def delete_task_book(db: Session, task_book_id: int) -> bool:
    """删除任务书"""
    task_book = get_task_book(db, task_book_id)
    if not task_book:
        return False

    db.delete(task_book)
    db.commit()
    return True


def activate_task_book(db: Session, task_book_id: int) -> Optional[TaskBook]:
    """激活任务书（同时取消其他任务书的激活状态）"""
    # 取消所有任务书的激活状态
    db.query(TaskBook).update({TaskBook.is_active: False})

    # 激活指定任务书
    task_book = get_task_book(db, task_book_id)
    if not task_book:
        return None

    task_book.is_active = True
    db.commit()
    db.refresh(task_book)
    return task_book


def deactivate_task_book(db: Session, task_book_id: int) -> Optional[TaskBook]:
    """取消激活任务书"""
    task_book = get_task_book(db, task_book_id)
    if not task_book:
        return None

    task_book.is_active = False
    db.commit()
    db.refresh(task_book)
    return task_book


def parse_word_to_markdown(file_path: str) -> str:
    """解析 Word 文件为 Markdown

    注意：需要安装 python-docx 库
    """
    try:
        from docx import Document

        doc = Document(file_path)
        markdown_lines = []

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            # 根据段落样式判断标题级别
            style_name = para.style.name.lower()
            if 'heading 1' in style_name:
                markdown_lines.append(f"# {text}")
            elif 'heading 2' in style_name:
                markdown_lines.append(f"## {text}")
            elif 'heading 3' in style_name:
                markdown_lines.append(f"### {text}")
            elif 'heading 4' in style_name:
                markdown_lines.append(f"#### {text}")
            else:
                markdown_lines.append(text)

        return "\n\n".join(markdown_lines)

    except ImportError:
        # 如果没有安装 python-docx，返回提示
        return "请安装 python-docx 库以支持 Word 文件解析：pip install python-docx"

    except Exception as e:
        return f"解析 Word 文件失败：{str(e)}"
