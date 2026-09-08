"""
评审组配置API
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
from database import get_db, JudgeGroupConfig, Team
from sqlalchemy import func
import pandas as pd
import io

router = APIRouter()


class JudgeGroupInput(BaseModel):
    """评审组配置输入"""
    group_index: int
    group_name: str
    description: Optional[str] = None
    judges: Optional[List[str]] = None


class JudgeGroupsConfig(BaseModel):
    """评审组配置列表"""
    groups: List[JudgeGroupInput]


@router.get("")
async def get_judge_groups(db: Session = Depends(get_db)):
    """获取评审组配置"""
    groups = db.query(JudgeGroupConfig).order_by(JudgeGroupConfig.group_index).all()

    # 如果没有配置，返回默认4组
    if not groups:
        default_groups = []
        for i in range(1, 5):
            default_groups.append({
                "group_index": i,
                "group_name": f"第{i}评审组",
                "description": "",
                "judges": [],
                "team_count": 0
            })
        return {"groups": default_groups, "is_default": True}

    # 添加每组队伍数量
    result = []
    for g in groups:
        count = db.query(func.count(Team.id)).filter(Team.judge_group == g.group_index).scalar()
        result.append({
            "group_index": g.group_index,
            "group_name": g.group_name,
            "description": g.description or "",
            "judges": g.judges or [],
            "team_count": count
        })

    return {"groups": result, "is_default": False}


@router.post("")
async def save_judge_groups(
    config: JudgeGroupsConfig,
    db: Session = Depends(get_db)
):
    """保存评审组配置"""
    # 清除旧配置
    db.query(JudgeGroupConfig).delete()

    # 保存新配置
    for g in config.groups:
        group = JudgeGroupConfig(
            group_index=g.group_index,
            group_name=g.group_name,
            description=g.description,
            judges=g.judges
        )
        db.add(group)

    db.commit()

    return {
        "message": "评审组配置保存成功",
        "total_groups": len(config.groups)
    }


@router.post("/assign")
async def assign_teams_to_groups(db: Session = Depends(get_db)):
    """根据当前配置分配队伍"""
    import random

    # 获取配置
    groups = db.query(JudgeGroupConfig).order_by(JudgeGroupConfig.group_index).all()

    if not groups:
        # 使用默认4组
        num_groups = 4
    else:
        num_groups = len(groups)

    # 获取所有队伍
    teams = db.query(Team).all()

    # 清除不在范围内的组号（比如从4组改为3组后，组号4的队伍需要重新分配）
    for team in teams:
        if team.judge_group and team.judge_group > num_groups:
            team.judge_group = None

    # 按组别分开
    primary_teams = [t for t in teams if t.group_type == "小学组"]
    junior_teams = [t for t in teams if t.group_type == "初中组"]

    # 各自随机打乱后分配
    random.shuffle(primary_teams)
    random.shuffle(junior_teams)

    for i, team in enumerate(primary_teams):
        team.judge_group = (i % num_groups) + 1

    for i, team in enumerate(junior_teams):
        team.judge_group = (i % num_groups) + 1

    db.commit()

    # 统计分配结果
    result = []
    for g in groups if groups else [{"group_index": i, "group_name": f"第{i}评审组"} for i in range(1, 5)]:
        idx = g.group_index if hasattr(g, 'group_index') else g["group_index"]
        group_teams = db.query(Team).filter(Team.judge_group == idx).all()
        result.append({
            "group_index": idx,
            "group_name": g.group_name if hasattr(g, 'group_name') else g["group_name"],
            "total": len(group_teams),
            "primary": len([t for t in group_teams if t.group_type == "小学组"]),
            "junior": len([t for t in group_teams if t.group_type == "初中组"])
        })

    return {
        "message": "队伍分配完成",
        "total_teams": len(teams),
        "groups": result
    }


@router.post("/upload-assignment")
async def upload_judge_assignment(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """
    上传评审老师分配表（Excel格式）
    支持的列名：
    - 编号/短编号/team_code/short_code -> 队伍编号
    - 队伍名称/team_name -> 队伍名称（可选）
    - 评审组/judge_group/组号 -> 评审组号(1,2,3...)
    - 评审老师/judges/评委 -> 评委姓名（可选，多个用逗号分隔）
    """
    if not file.filename or not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="仅支持Excel文件(.xlsx/.xls)")

    content = await file.read()

    try:
        df = pd.read_excel(io.BytesIO(content), engine='openpyxl')
    except Exception as e:
        try:
            df = pd.read_excel(io.BytesIO(content), engine='xlrd')
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"无法解析Excel文件: {str(e)}")

    df = df.fillna("")

    # 识别列名
    code_col = None
    name_col = None
    group_col = None
    judges_col = None

    for col in df.columns:
        col_lower = str(col).lower().strip()
        if col_lower in ("编号", "短编号", "team_code", "short_code"):
            code_col = col
        elif col_lower in ("队伍名称", "team_name"):
            name_col = col
        elif col_lower in ("评审组", "judge_group", "组号", "组别"):
            group_col = col
        elif col_lower in ("评审老师", "judges", "评委"):
            judges_col = col

    if not group_col:
        raise HTTPException(status_code=400, detail="Excel中未找到'评审组'列")

    results = {"total": 0, "matched": 0, "unmatched": 0, "errors": [], "details": []}

    for idx, row in df.iterrows():
        results["total"] += 1

        # 获取评审组号
        try:
            group_val = int(row.get(group_col, 0))
        except:
            results["errors"].append(f"第{idx+2}行: 评审组号无效")
            results["unmatched"] += 1
            continue

        # 查找队伍
        team = None

        # 方式1: 按编号查找
        if code_col:
            code_val = str(row.get(code_col, "")).strip()
            if code_val:
                # 先尝试短编号
                team = db.query(Team).filter(Team.short_code == code_val.upper()).first()
                if not team:
                    # 再尝试原始编号
                    team = db.query(Team).filter(Team.team_code == code_val).first()

        # 方式2: 按队伍名称查找
        if not team and name_col:
            name_val = str(row.get(name_col, "")).strip()
            if name_val:
                teams = db.query(Team).filter(Team.team_name == name_val).all()
                if len(teams) == 1:
                    team = teams[0]

        if team:
            team.judge_group = group_val
            results["matched"] += 1
            results["details"].append({
                "team_id": team.id,
                "short_code": team.short_code,
                "team_name": team.team_name,
                "judge_group": group_val
            })
        else:
            results["unmatched"] += 1
            code_val = str(row.get(code_col, "")) if code_col else ""
            name_val = str(row.get(name_col, "")) if name_col else ""
            results["errors"].append(f"第{idx+2}行: 未找到队伍 (编号={code_val}, 名称={name_val})")

    db.commit()

    return {
        "message": f"分配完成，成功匹配 {results['matched']} 个队伍",
        "total": results["total"],
        "matched": results["matched"],
        "unmatched": results["unmatched"],
        "errors": results["errors"][:10]  # 只返回前10个错误
    }
