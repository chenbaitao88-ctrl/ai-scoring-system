"""
作品命名解析服务
从作品文件名自动提取队伍信息
"""
import re
from typing import Optional, Dict


def parse_team_from_filename(filename: str) -> Optional[Dict]:
    """从作品文件名解析队伍信息

    命名规则：{组别}_{队伍编号}_{团队名称}.zip
    示例：
    - 小学组_2604171115054319_1队.zip
    - 初中组_2604171115054319_创意编程队.zip

    Args:
        filename: 文件名（包含.zip后缀）

    Returns:
        解析结果字典，包含：
        - group_type: 组别（小学组/初中组）
        - team_code: 队伍编号
        - team_name: 团队名称
        - school: 学校（空字符串，需用户补充）
        - status: pending（待确认）
        - source: work（作品生成）

        如果解析失败，返回 None
    """
    # 去除后缀
    name = filename.replace('.zip', '').replace('.ZIP', '')

    # 按下划线分割
    parts = name.split('_')

    # 至少需要3部分：组别、编号、名称
    if len(parts) < 3:
        return None

    # 提取组别
    group_type = parts[0]
    if group_type not in ['小学组', '初中组']:
        # 尝试匹配更宽松的组别
        if '小学' in group_type:
            group_type = '小学组'
        elif '初中' in group_type:
            group_type = '初中组'
        else:
            return None

    # 提取队伍编号（第二部分）
    team_code = parts[1]

    # 提取团队名称（第三部分及之后，用下划线连接）
    team_name = '_'.join(parts[2:])

    return {
        "group_type": group_type,
        "team_code": team_code,
        "team_name": team_name,
        "school": "",  # 学校信息需用户补充
        "district": "",  # 区县需用户补充
        "teacher": "",  # 指导老师需用户补充
        "members": [],  # 选手信息需用户补充
        "status": "pending",  # 待确认状态
        "source": "work"  # 来源：作品自动生成
    }


def generate_short_code(team_code: str, existing_count: int) -> str:
    """生成短编号

    Args:
        team_code: 原始长编号
        existing_count: 已存在的队伍数量

    Returns:
        短编号（如 P001、P002）
    """
    # 使用现有数量生成序号
    return f"P{existing_count + 1:03d}"


def validate_team_info(team_info: Dict) -> bool:
    """验证队伍信息是否完整

    Args:
        team_info: 队伍信息字典

    Returns:
        是否完整（至少包含组别、编号、名称）
    """
    required_fields = ['group_type', 'team_code', 'team_name']

    for field in required_fields:
        if not team_info.get(field):
            return False

    return True


def batch_parse_filenames(filenames: list) -> list:
    """批量解析文件名

    Args:
        filenames: 文件名列表

    Returns:
        解析结果列表（包含成功和失败的）
    """
    results = []

    for filename in filenames:
        team_info = parse_team_from_filename(filename)

        results.append({
            "filename": filename,
            "success": team_info is not None,
            "team_info": team_info
        })

    return results
