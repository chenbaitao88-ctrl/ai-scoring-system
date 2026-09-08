"""
评审表管理服务
"""
from typing import List, Optional
from sqlalchemy.orm import Session
from database import ScoringForm


def get_scoring_forms(db: Session) -> List[ScoringForm]:
    """获取评审表列表"""
    return db.query(ScoringForm).order_by(ScoringForm.created_at.desc()).all()


def get_scoring_form(db: Session, scoring_form_id: int) -> Optional[ScoringForm]:
    """获取单个评审表"""
    return db.query(ScoringForm).filter(ScoringForm.id == scoring_form_id).first()


def get_active_scoring_form(db: Session) -> Optional[ScoringForm]:
    """获取当前激活的评审表"""
    return db.query(ScoringForm).filter(ScoringForm.is_active == True).first()


def get_default_scoring_form(db: Session) -> Optional[ScoringForm]:
    """获取系统默认评审表"""
    return db.query(ScoringForm).filter(ScoringForm.is_default == True).first()


def create_scoring_form(
    db: Session,
    name: str,
    dimensions: List[dict],
    is_default: bool = False
) -> ScoringForm:
    """创建评审表"""
    scoring_form = ScoringForm(
        name=name,
        dimensions=dimensions,
        is_default=is_default,
        is_active=True if is_default else False
    )
    db.add(scoring_form)
    db.commit()
    db.refresh(scoring_form)
    return scoring_form


def update_scoring_form(
    db: Session,
    scoring_form_id: int,
    name: Optional[str] = None,
    dimensions: Optional[List[dict]] = None
) -> Optional[ScoringForm]:
    """更新评审表"""
    scoring_form = get_scoring_form(db, scoring_form_id)
    if not scoring_form:
        return None

    if name is not None:
        scoring_form.name = name
    if dimensions is not None:
        scoring_form.dimensions = dimensions

    db.commit()
    db.refresh(scoring_form)
    return scoring_form


def delete_scoring_form(db: Session, scoring_form_id: int) -> bool:
    """删除评审表"""
    scoring_form = get_scoring_form(db, scoring_form_id)
    if not scoring_form:
        return False

    # 不允许删除默认评审表
    if scoring_form.is_default:
        return False

    db.delete(scoring_form)
    db.commit()
    return True


def activate_scoring_form(db: Session, scoring_form_id: int) -> Optional[ScoringForm]:
    """激活评审表（同时取消其他评审表的激活状态）"""
    # 取消所有评审表的激活状态
    db.query(ScoringForm).update({ScoringForm.is_active: False})

    # 激活指定评审表
    scoring_form = get_scoring_form(db, scoring_form_id)
    if not scoring_form:
        return None

    scoring_form.is_active = True
    db.commit()
    db.refresh(scoring_form)
    return scoring_form


def init_default_scoring_form(db: Session) -> ScoringForm:
    """初始化默认评审表（如果不存在）"""
    default_form = get_default_scoring_form(db)
    if default_form:
        return default_form

    # 创建默认评审表（四维度）
    default_dimensions = [
        {
            "name": "主题立意",
            "max_score": 20,
            "description": "1.思想性：主题真实有效，立意积极健康，具有清晰的用户或社会价值；\n2.原创性：解决方案新颖、巧妙，具备独特的创见。"
        },
        {
            "name": "产品表现力",
            "max_score": 30,
            "description": "1.技术性：产品架构完整，体系设计清晰，功能实现完整、稳定，程序逻辑严谨，代码算法准确，并具有一定的技术探索性；\n2.创新性：对传统场景，具有新颖思考，具有实际价值，设计创意巧妙，具有想象力，展现个性表现力；\n3.艺术性与用户体验：产品界面（UI）设计美观、协调，交互（UX）流程直观、流畅，设计风格和主题一致，体现以用户为中心的设计思想。"
        },
        {
            "name": "过程完整性",
            "max_score": 30,
            "description": "1.过程完整性：过程性文档等交付成果齐全、翔实、规范，完整体现0-1产品研发全流程；\n2.迭代性思维：从测试评估或AIGC交互中发现问题，有效改进迭代产品方案；\n3.AIGC交互日志：完整记录AIGC辅助实现产品研发的全过程，清晰地呈现学生与AI的互动轨迹。"
        },
        {
            "name": "AI素养",
            "max_score": 20,
            "description": "1.工具策略性：策略性地选择和使用AIGC工具以提升项目质量和效率；\n2.批判性思维：精准设计提示词，以专业视角引导大模型，确保其高效输出优质结果，AIGC交互日志体现对AI输出的深度分析、甄别与优化；\n3.社会伦理意识：正确认识AIGC的价值，负责任、合乎规范地使用AIGC工具；\n4.沟通与表达：路演讲解逻辑清晰、具有说服力，有效传达产品价值。"
        }
    ]

    return create_scoring_form(
        db,
        name="默认评审表（四维度）",
        dimensions=default_dimensions,
        is_default=True
    )
