"""
队伍管理API
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel
from database import get_db, Team
from services.team_service import TeamService

router = APIRouter()


# ============ Pydantic 模型 ============
class TeamUpdateModel(BaseModel):
    team_name: Optional[str] = None
    school: Optional[str] = None
    district: Optional[str] = None
    teacher: Optional[str] = None
    members: Optional[List[dict]] = None


class BatchConfirmModel(BaseModel):
    team_ids: List[int]


@router.get("")
async def get_teams(
    group_type: Optional[str] = None,
    judge_group: Optional[int] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取队伍列表"""
    service = TeamService(db)
    teams = service.get_teams(group_type=group_type, judge_group=judge_group, search=search)
    return {"teams": teams, "total": len(teams)}


@router.get("/stats")
async def get_team_stats(db: Session = Depends(get_db)):
    """获取队伍统计信息"""
    service = TeamService(db)
    stats = service.get_stats()
    return stats


# ============ 队伍审核API（必须在 /{team_id} 前定义）============
@router.get("/pending")
async def get_pending_teams(
    group_type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取待确认队伍列表"""
    query = db.query(Team).filter(Team.status == "pending")
    if group_type:
        query = query.filter(Team.group_type == group_type)

    teams = query.order_by(Team.created_at.desc()).all()

    return {
        "teams": [
            {
                "id": t.id,
                "team_code": t.team_code,
                "short_code": t.short_code,
                "team_name": t.team_name,
                "group_type": t.group_type,
                "school": t.school,
                "district": t.district,
                "teacher": t.teacher,
                "members": t.members,
                "status": t.status,
                "source": t.source,
                "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else ""
            }
            for t in teams
        ],
        "total": len(teams)
    }


@router.get("/confirmed")
async def get_confirmed_teams(
    group_type: Optional[str] = None,
    judge_group: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """获取已确认队伍列表"""
    query = db.query(Team).filter(Team.status == "confirmed")
    if group_type:
        query = query.filter(Team.group_type == group_type)
    if judge_group:
        query = query.filter(Team.judge_group == judge_group)

    teams = query.order_by(Team.short_code).all()

    return {
        "teams": [
            {
                "id": t.id,
                "team_code": t.team_code,
                "short_code": t.short_code,
                "team_name": t.team_name,
                "group_type": t.group_type,
                "school": t.school,
                "district": t.district,
                "teacher": t.teacher,
                "members": t.members,
                "judge_group": t.judge_group,
                "status": t.status,
                "source": t.source,
                "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else ""
            }
            for t in teams
        ],
        "total": len(teams)
    }


@router.get("/review-stats")
async def get_review_stats(db: Session = Depends(get_db)):
    """获取队伍审核统计"""
    pending_count = db.query(Team).filter(Team.status == "pending").count()
    confirmed_count = db.query(Team).filter(Team.status == "confirmed").count()

    # 按来源统计
    excel_count = db.query(Team).filter(
        Team.status == "pending",
        Team.source == "excel"
    ).count()
    work_count = db.query(Team).filter(
        Team.status == "pending",
        Team.source == "work"
    ).count()

    return {
        "pending": pending_count,
        "confirmed": confirmed_count,
        "total": pending_count + confirmed_count,
        "pending_by_source": {
            "excel": excel_count,
            "work": work_count
        }
    }


@router.get("/{team_id}")
async def get_team(team_id: int, db: Session = Depends(get_db)):
    """获取单个队伍详情"""
    service = TeamService(db)
    team = service.get_team_by_id(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="队伍不存在")
    return team


@router.post("/import")
async def import_teams_from_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """从Excel导入队伍信息"""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="仅支持Excel文件(.xlsx/.xls)")

    service = TeamService(db)
    result = await service.import_from_excel(file)
    return result


@router.post("/assign-groups")
async def assign_judge_groups(
    num_groups: int = 4,
    db: Session = Depends(get_db)
):
    """随机分配评审组"""
    service = TeamService(db)
    result = service.assign_judge_groups(num_groups)
    return result


@router.delete("/{team_id}")
async def delete_team(team_id: int, db: Session = Depends(get_db)):
    """删除队伍"""
    service = TeamService(db)
    success = service.delete_team(team_id)
    if not success:
        raise HTTPException(status_code=404, detail="队伍不存在")
    return {"message": "删除成功"}


@router.post("/{team_id}/confirm")
async def confirm_team(team_id: int, db: Session = Depends(get_db)):
    """确认单个队伍"""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="队伍不存在")

    team.status = "confirmed"
    db.commit()

    return {
        "message": "确认成功",
        "team": {
            "id": team.id,
            "team_code": team.team_code,
            "short_code": team.short_code,
            "team_name": team.team_name,
            "status": team.status
        }
    }


@router.post("/batch-confirm")
async def batch_confirm_teams(data: BatchConfirmModel, db: Session = Depends(get_db)):
    """批量确认队伍"""
    teams = db.query(Team).filter(Team.id.in_(data.team_ids)).all()

    confirmed_count = 0
    for team in teams:
        team.status = "confirmed"
        confirmed_count += 1

    db.commit()

    return {
        "message": f"成功确认 {confirmed_count} 支队伍",
        "confirmed_count": confirmed_count
    }


@router.put("/{team_id}")
async def update_team_info(
    team_id: int,
    team_data: TeamUpdateModel,
    db: Session = Depends(get_db)
):
    """更新队伍信息"""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="队伍不存在")

    # 更新字段
    if team_data.team_name is not None:
        team.team_name = team_data.team_name
    if team_data.school is not None:
        team.school = team_data.school
    if team_data.district is not None:
        team.district = team_data.district
    if team_data.teacher is not None:
        team.teacher = team_data.teacher
    if team_data.members is not None:
        team.members = team_data.members

    db.commit()
    db.refresh(team)

    return {
        "message": "更新成功",
        "team": {
            "id": team.id,
            "team_code": team.team_code,
            "short_code": team.short_code,
            "team_name": team.team_name,
            "school": team.school,
            "district": team.district,
            "teacher": team.teacher,
            "members": team.members,
            "status": team.status,
            "source": team.source
        }
    }
