"""
评审组配置模型
"""
from sqlalchemy import Column, Integer, String, JSON
from database import Base


class JudgeGroupConfig(Base):
    """评审组配置"""
    __tablename__ = "judge_group_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_index = Column(Integer, nullable=False, comment="组序号(1,2,3...)")
    group_name = Column(String(100), nullable=False, comment="组名称")
    description = Column(String(500), comment="组描述")
    judges = Column(JSON, comment="评委列表")
