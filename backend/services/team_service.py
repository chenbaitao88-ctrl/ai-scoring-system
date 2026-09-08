"""
队伍管理服务
"""
import random
import re
from typing import Optional, List
from fastapi import UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import func
import pandas as pd
import io

from database import Team, AuditLog, MachineScore
from config import get_judge_groups


class TeamService:
    def __init__(self, db: Session):
        self.db = db

    def get_teams(
        self,
        group_type: Optional[str] = None,
        judge_group: Optional[int] = None,
        search: Optional[str] = None
    ) -> List[dict]:
        """获取队伍列表"""
        query = self.db.query(Team)

        if group_type:
            query = query.filter(Team.group_type == group_type)
        if judge_group:
            query = query.filter(Team.judge_group == judge_group)
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                (Team.team_name.like(search_term)) |
                (Team.school.like(search_term)) |
                (Team.team_code.like(search_term))
            )

        teams = query.order_by(Team.team_code).all()
        return [self._team_to_dict(t) for t in teams]

    def get_team_by_id(self, team_id: int) -> Optional[dict]:
        """根据ID获取队伍"""
        team = self.db.query(Team).filter(Team.id == team_id).first()
        if not team:
            return None
        return self._team_to_dict(team)

    def get_stats(self) -> dict:
        """获取队伍统计信息"""
        total = self.db.query(func.count(Team.id)).scalar()
        primary = self.db.query(func.count(Team.id)).filter(Team.group_type == "小学组").scalar()
        junior = self.db.query(func.count(Team.id)).filter(Team.group_type == "初中组").scalar()

        # 从评审组配置读取实际组数
        from database import JudgeGroupConfig
        groups = self.db.query(JudgeGroupConfig).order_by(JudgeGroupConfig.group_index).all()

        if groups:
            group_stats = {}
            for g in groups:
                count = self.db.query(func.count(Team.id)).filter(Team.judge_group == g.group_index).scalar()
                group_stats[f"group_{g.group_index}"] = count
        else:
            # 默认4组
            group_stats = {}
            for g in range(1, get_judge_groups() + 1):
                count = self.db.query(func.count(Team.id)).filter(Team.judge_group == g).scalar()
                group_stats[f"group_{g}"] = count

        return {
            "total": total,
            "primary": primary,
            "junior": junior,
            "groups": group_stats
        }

    async def import_from_excel(self, file: UploadFile) -> dict:
        """从Excel导入队伍信息（使用pandas解析，兼容多种格式）"""
        print(f"\n[导入] ========== 开始导入文件: {file.filename} ==========")
        content = await file.read()
        print(f"[导入] 文件读取完成，大小: {len(content)} 字节")

        # 使用pandas读取，自动适配格式
        try:
            df = pd.read_excel(io.BytesIO(content), engine='openpyxl')
        except Exception:
            try:
                df = pd.read_excel(io.BytesIO(content), engine='xlrd')
            except Exception as e:
                print(f"[导入] Excel解析失败: {e}")
                return {
                    "imported": 0,
                    "skipped": 0,
                    "errors": [f"无法解析Excel文件: {str(e)}"],
                    "total_in_db": self.db.query(func.count(Team.id)).scalar()
                }

        print(f"[导入] Excel解析完成，共 {len(df)} 行")
        print(f"[导入] 列名: {list(df.columns)}")  # 打印所有列名

        # 打印前3行原始数据（调试用）
        print(f"[导入] 前3行原始数据:")
        for i in range(min(3, len(df))):
            print(f"[导入]   行{i+2}: {dict(df.iloc[i])}")

        # 填充NaN
        df = df.fillna("")

        imported = 0
        skipped = 0
        errors = []

        print(f"[导入] 开始处理 {len(df)} 行数据...")

        # 预先查询当前数据库中的最大 short_code（避免在循环中重复查询）
        max_short = 0
        for (sc,) in self.db.query(Team.short_code).all():
            if sc and sc.startswith('P'):
                try:
                    max_short = max(max_short, int(sc[1:]))
                except:
                    pass
        next_short_num = max_short + 1  # 下一个可用的序号
        print(f"[导入] 当前最大 short_code: P{max_short:03d}, 下一个: P{next_short_num:03d}")

        # 智能模糊匹配列名（不区分中英文、括号格式，空格容错）
        def find_col(df, keywords):
            """根据关键词模糊查找列名，返回第一个匹配的列名"""
            for col in df.columns:
                # 标准化：去除空格、括号
                col_normalized = col.replace('（', '(').replace('）', ')').replace(' ', '').strip()
                for kw in keywords:
                    kw_normalized = kw.replace('（', '(').replace('）', ')').replace(' ', '').strip()
                    if kw_normalized in col_normalized:
                        return col
            return None

        for idx, row in df.iterrows():
            if idx % 20 == 0:
                print(f"\n[导入] ========== 正在处理第 {idx+2} 行 ==========")

            # 打印前5行详细数据（调试用）
            if idx < 5:
                print(f"[导入] 第{idx+2}行原始数据: {dict(row)}")

            try:
                # 活动类别筛选（宽松策略：如果列不存在或为空，不过滤）
                activity_col = find_col(df, ["活动类别", "赛项", "项目类别", "类别", "项目", "赛项名称"])
                if activity_col:
                    activity_type = str(row.get(activity_col, "")).strip()
                    if activity_type and "创意编程" not in activity_type and "编程" not in activity_type:
                        print(f"[导入] 第{idx+2}行 跳过：非创意编程 ({activity_type})")
                        skipped += 1
                        continue

                # 灵活适配Excel列名（模糊匹配）
                team_code_col = find_col(df, ["编号", "队伍编号", "参赛编号", "队号"])
                team_code = str(row.get(team_code_col, "")).strip() if team_code_col else ""

                # 参赛人名称(团队名称) -> 找"参赛人"或"团队名称"
                team_name_col = find_col(df, ["参赛人名称", "团队名称", "队伍名称", "参赛作品", "作品名称", "队名"])
                team_name = str(row.get(team_name_col, "")).strip() if team_name_col else ""

                group_type_col = find_col(df, ["组别", "参赛组别", "分组"])
                group_type = str(row.get(group_type_col, "")).strip() if group_type_col else ""

                # 所属机构 -> 找"所属机构"或"机构"
                school_col = find_col(df, ["所属机构", "指导老师学校", "学校", "所在学校", "单位"])
                school = str(row.get(school_col, "")).strip() if school_col else ""

                # 县 -> 找"县"或"区县"
                district_col = find_col(df, ["县", "区县教育局", "区县", "地区", "区域"])
                district = str(row.get(district_col, "")).strip() if district_col else ""

                teacher_col = find_col(df, ["指导老师", "指导教师", "导师", "指导老师姓名"])
                teacher_name = str(row.get(teacher_col, "")).strip() if teacher_col else ""

                # 指导老师电话 -> 找"老师电话"
                teacher_phone_col = find_col(df, ["老师电话", "指导老师电话", "手机号"])
                teacher_phone = row.get(teacher_phone_col, "")
                # 处理电话号码（Excel可能读取为浮点数，如 00000000000.0）
                if teacher_phone:
                    if isinstance(teacher_phone, float):
                        teacher_phone = str(int(teacher_phone))
                    elif isinstance(teacher_phone, int):
                        teacher_phone = str(teacher_phone)
                    else:
                        teacher_phone = str(teacher_phone).strip()
                else:
                    teacher_phone = ""

                if teacher_name and not teacher_phone and teacher_col:
                    # 尝试从指导老师字段中拆分姓名和电话
                    # 兼容格式：姓名(手机号)、姓名（手机号）、姓名 (手机号)、姓名 （手机号）
                    match = re.match(r'^(.+?)\s*[(（](\d{11})[)）]$', teacher_name)
                    if match:
                        teacher_name = match.group(1)
                        teacher_phone = match.group(2)

                # 兼容旧数据：teacher字段仍保留
                if teacher_name and teacher_phone:
                    teacher = f"{teacher_name} ({teacher_phone})"
                elif teacher_name:
                    teacher = teacher_name
                else:
                    teacher = ""
                # 解析选手信息（模糊匹配列名）
                members = []

                # 选手1 - 支持"团队成员1"格式
                player1_name_col = find_col(df, ["团队成员1", "选手1", "队员1", "学生1", "姓名1", "学生姓名", "姓名", "选手姓名"])
                player1_name = str(row.get(player1_name_col, "")).strip() if player1_name_col else ""
                if idx < 5:
                    print(f"[导入] 第{idx+2}行 选手1列名: {player1_name_col}, 选手1姓名: '{player1_name}'")

                player1_school_col = find_col(df, ["学生学校", "队员1学校", "选手1学校", "学校1"])
                player1_school = str(row.get(player1_school_col, "")).strip() if player1_school_col else ""

                if player1_name:
                    members.append({"name": player1_name, "school": player1_school})

                # 选手2 - 支持"团队成员2"格式
                player2_name_col = find_col(df, ["团队成员2", "选手2", "队员2", "学生2", "姓名2"])
                player2_name = str(row.get(player2_name_col, "")).strip() if player2_name_col else ""

                if player2_name:
                    player2_school_col = find_col(df, ["学生学校", "队员2学校", "选手2学校", "学校2"])
                    player2_school = str(row.get(player2_school_col, "")).strip() if player2_school_col else ""
                    members.append({"name": player2_name, "school": player2_school})

                # 选手3、4
                for i in [3, 4]:
                    player_name_col = find_col(df, [f"团队成员{i}", f"选手{i}", f"队员{i}", f"学生{i}"])
                    player_name = str(row.get(player_name_col, "")).strip() if player_name_col else ""
                    if player_name:
                        members.append({"name": player_name, "school": ""})

                # 如果team_name为空，尝试使用第一个学生姓名补全
                if not team_name:
                    if members and len(members) > 0:
                        team_name = members[0]["name"]
                        print(f"[导入] 第{idx+2}行 使用学生姓名作为队伍名称: {team_name}")
                    elif team_code:
                        team_name = team_code
                    else:
                        # 最后尝试：使用"区县+组别"生成一个名称
                        if district and group_type:
                            team_name = f"{district}_{group_type}_未命名"
                        elif district:
                            team_name = f"{district}_未命名"
                        else:
                            skipped += 1
                            errors.append(f"第{idx+2}行: 缺少队伍编号和队伍名称，已跳过")
                            print(f"[导入] 第{idx+2}行 跳过：无队伍编号和名称")
                            continue

                # 如果team_code为空，自动生成
                if not team_code:
                    team_code = f"ROW_{idx+2}"
                    print(f"[导入] 第{idx+2}行 自动生成队伍编号: {team_code}")

                # 检查是否已存在
                existing = self.db.query(Team).filter(Team.team_code == team_code).first()
                if existing:
                    # 更新现有记录
                    existing.team_name = team_name
                    existing.group_type = group_type
                    existing.school = school
                    existing.district = district
                    existing.teacher = teacher
                    existing.teacher_name = teacher_name
                    existing.teacher_phone = teacher_phone
                    existing.members = members
                    skipped += 1
                    print(f"[导入] 第{idx+2}行 更新现有记录: {team_name}")
                else:
                    # 分配短编号（使用预先计算的序号，避免重复）
                    short_code = f"P{next_short_num:03d}"
                    next_short_num += 1

                    # 双重保险：确保short_code在数据库中不存在
                    while self.db.query(Team).filter(Team.short_code == short_code).first():
                        short_code = f"P{next_short_num:03d}"
                        next_short_num += 1

                    # 创建新记录
                    team = Team(
                        team_code=team_code,
                        short_code=short_code,
                        team_name=team_name,
                        group_type=group_type,
                        school=school,
                        district=district,
                        teacher=teacher,  # 兼容旧数据
                        teacher_name=teacher_name,
                        teacher_phone=teacher_phone,
                        members=members,
                        status="confirmed",
                        source="excel"
                    )
                    self.db.add(team)
                    imported += 1
                    print(f"[导入] 第{idx+2}行 新增记录: {team_name} (short_code={short_code})")

            except Exception as e:
                error_msg = f"第{idx+2}行: {str(e)}"
                errors.append(error_msg)
                skipped += 1
                print(f"[导入] 第{idx+2}行 错误: {e}")

        print(f"\n[导入] 准备提交数据库...")
        self.db.commit()
        print(f"[导入] 数据库提交完成")

        print(f"\n[导入] ========== 完成！新增 {imported} 支，跳过 {skipped} 支，错误 {len(errors)} 条 ==========")

        # 记录审计日志
        log = AuditLog(
            operator="system",
            action="import_teams",
            target_type="team",
            detail={"imported": imported, "skipped": skipped, "errors": errors}
        )
        self.db.add(log)
        self.db.commit()

        return {
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "total_in_db": self.db.query(func.count(Team.id)).scalar()
        }

    def assign_judge_groups(self, num_groups: int = None) -> dict:
        if num_groups is None:
            num_groups = get_judge_groups()
        """随机分配评审组"""
        teams = self.db.query(Team).all()

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

        self.db.commit()

        # 统计分配结果
        result = {}
        for g in range(1, num_groups + 1):
            group_teams = self.db.query(Team).filter(Team.judge_group == g).all()
            result[f"group_{g}"] = {
                "total": len(group_teams),
                "primary": len([t for t in group_teams if t.group_type == "小学组"]),
                "junior": len([t for t in group_teams if t.group_type == "初中组"])
            }

        return {"assigned_groups": num_groups, "details": result}

    def delete_team(self, team_id: int) -> bool:
        """删除队伍"""
        team = self.db.query(Team).filter(Team.id == team_id).first()
        if not team:
            return False
        self.db.delete(team)
        self.db.commit()
        return True

    def _team_to_dict(self, team: Team) -> dict:
        """将Team对象转为字典"""
        result = {
            "id": team.id,
            "team_code": team.team_code,
            "short_code": team.short_code,
            "team_name": team.team_name,
            "group_type": team.group_type,
            "school": team.school,
            "district": team.district,
            "teacher": team.teacher,
            "teacher_name": team.teacher_name,
            "teacher_phone": team.teacher_phone,
            "members": team.members or [],
            "judge_group": team.judge_group,
            "created_at": team.created_at.isoformat() if team.created_at else None,
            "updated_at": team.updated_at.isoformat() if team.updated_at else None
        }
        # 添加作品简要信息
        if team.work:
            result["work"] = {
                "id": team.work.id,
                "has_source": team.work.has_source,
                "has_aigc_log": team.work.has_aigc_log,
                "has_screenshots": team.work.has_screenshots,
                "is_parsed": team.work.is_parsed,
                "code_language": team.work.code_language,
                "code_line_count": team.work.code_line_count,
            }
        else:
            result["work"] = None

        # 添加机器评分（采用版本）
        machine_score = self.db.query(MachineScore).filter(
            MachineScore.team_id == team.id,
            MachineScore.is_adopted == True
        ).first()
        if not machine_score:
            # 如果没有已采用的，找最新的一个
            machine_score = self.db.query(MachineScore).filter(
                MachineScore.team_id == team.id,
                MachineScore.is_completed == True
            ).order_by(MachineScore.created_at.desc()).first()

        if machine_score:
            result["machine_score"] = machine_score.total_score
            result["machine_theme"] = machine_score.theme_score
            result["machine_presentation"] = machine_score.presentation_score
            result["machine_process"] = machine_score.process_score
            result["machine_ai_literacy"] = machine_score.ai_literacy_score
            result["machine_model"] = machine_score.model_name  # 新增：使用的模型
            result["machine_is_adopted"] = machine_score.is_adopted  # 新增：是否为采用版本
            # 添加AI评语
            result["machine_comments"] = {
                "theme": machine_score.theme_comment,
                "presentation": machine_score.presentation_comment,
                "process": machine_score.process_comment,
                "ai_literacy": machine_score.ai_literacy_comment,
                "overall": machine_score.overall_comment,
            }
        else:
            result["machine_score"] = None
            result["machine_theme"] = None
            result["machine_presentation"] = None
            result["machine_process"] = None
            result["machine_ai_literacy"] = None
            result["machine_model"] = None
            result["machine_is_adopted"] = None
            result["machine_comments"] = None

        return result
