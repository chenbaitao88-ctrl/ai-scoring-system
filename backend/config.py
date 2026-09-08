"""
评分系统配置文件
"""
import os
from pathlib import Path

# 基础路径
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(
    os.getenv("SCORING_DATA_DIR", str(BASE_DIR / "data"))
).expanduser().resolve()
TEMPLATES_DIR = BASE_DIR / "templates"
WORKS_DIR = DATA_DIR / "works"
BACKUPS_DIR = DATA_DIR / "backups"

# 数据库配置
DATABASE_URL = f"sqlite:///{DATA_DIR}/teams.db"


def _load_local_env(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


_LOCAL_ENV = _load_local_env(BASE_DIR / ".env")


def _get_env(name: str, default: str = "") -> str:
    return os.getenv(name) or _LOCAL_ENV.get(name) or default


# 评分权重配置（可调整）
MACHINE_SCORE_WEIGHT = 0.6  # 机器评分权重
HUMAN_SCORE_WEIGHT = 0.4    # 人工评分权重

# Optional OpenAI-compatible provider configuration. Empty defaults fail closed.
LLM_API_KEY = _get_env("LLM_API_KEY") or _get_env("DASHSCOPE_API_KEY")
LLM_API_URL = _get_env("LLM_API_URL")
LLM_MODEL = _get_env("LLM_MODEL")

# 便携演示版模型清单。真实调用能力取决于本机配置的 Provider 和 API 权限。
AVAILABLE_MODELS = {
    "qwen3.8-max": {
        "name": "千问 Qwen3.8 Max",
        "description": "图像+文本，旗舰质量，适合深度评分（推荐）",
        "speed": "中等",
        "quality": "极高",
    },
    "qwen3.8-flash": {
        "name": "千问 Qwen3.8 Flash",
        "description": "图像+文本，快速响应，适合快速初评",
        "speed": "快",
        "quality": "高",
    },
    "k3": {
        "name": "Kimi K3",
        "description": "图像+视频+文本，适合长材料综合判断",
        "speed": "较慢",
        "quality": "极高",
    },
    "glm-5v-turbo": {
        "name": "智谱 GLM-5V-Turbo",
        "description": "图像+视频+文本，兼顾视觉理解与代码分析",
        "speed": "快",
        "quality": "极高",
    },
    "doubao-seed-2.1-turbo": {
        "name": "豆包 Seed 2.1 Turbo",
        "description": "新一代通用模型，适合高并发批量评审",
        "speed": "快",
        "quality": "高",
    },
    "MiniMax-M3": {
        "name": "MiniMax M3",
        "description": "原生图像+视频+文本，适合长上下文作品分析",
        "speed": "中等",
        "quality": "极高",
    },
}

# 并行评分配置
PARALLEL_SCORING_ENABLED = True
PARALLEL_SCORING_CONCURRENCY = 5  # 并发数

# 支持的文件类型
SUPPORTED_CODE_EXTENSIONS = [".py", ".sb3", ".sb2", ".js", ".html", ".css", ".bcm", ".kitten"]
SUPPORTED_IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".gif", ".bmp"]
SUPPORTED_VIDEO_EXTENSIONS = [".mp4", ".mov", ".avi", ".mkv"]
SUPPORTED_DOC_EXTENSIONS = [".txt", ".md", ".doc", ".docx", ".pdf", ".pptx", ".jpg"]

# GENERIC: Phase 2 移除硬编码赛事配置，改为懒加载函数
# 以下函数从数据库读取当前活跃赛事配置，提供默认值兜底

_DEFAULT_DIMENSIONS = {
    "theme": {"name": "主题立意", "max_score": 20, "description": "思想性、原创性"},
    "presentation": {"name": "产品表现力", "max_score": 30, "description": "技术性、创新性、艺术性与用户体验"},
    "process": {"name": "过程完整性", "max_score": 30, "description": "过程文档、迭代思维、AIGC交互日志"},
    "ai_literacy": {"name": "人工智能素养", "max_score": 20, "description": "工具策略、批判性思维、伦理意识、沟通表达"},
}

_DEFAULT_NAMING_PATTERN = r"^(.*)\+(.+)\.zip$"
_DEFAULT_NAMING_EXAMPLE = "小学组+张三.zip"
_DEFAULT_JUDGE_GROUPS = 4
_DEFAULT_JUDGES_PER_GROUP = 2
_DEFAULT_MACHINE_WEIGHT = 0.6
_DEFAULT_HUMAN_WEIGHT = 0.4


def get_active_competition(db_session=None):
    """获取当前活跃赛事配置，提供默认值兜底"""
    try:
        if db_session is None:
            from database import SessionLocal
            db = SessionLocal()
            try:
                from database import Competition
                comp = db.query(Competition).filter(Competition.is_active == True).first()
                return comp
            finally:
                db.close()
        else:
            from database import Competition
            return db_session.query(Competition).filter(Competition.is_active == True).first()
    except Exception:
        return None


def get_scoring_dimensions():
    """获取当前赛事评分维度，无活跃赛事时使用默认4维度"""
    comp = get_active_competition()
    if comp and comp.dimensions_config:
        return comp.dimensions_config
    return _DEFAULT_DIMENSIONS


def get_naming_pattern():
    """获取当前赛事作品命名正则，无活跃赛事时使用默认值"""
    comp = get_active_competition()
    if comp and comp.naming_pattern:
        return comp.naming_pattern
    return _DEFAULT_NAMING_PATTERN


def get_naming_example():
    """获取当前赛事作品命名示例，无活跃赛事时使用默认值"""
    comp = get_active_competition()
    if comp and comp.naming_example:
        return comp.naming_example
    return _DEFAULT_NAMING_EXAMPLE


def get_judge_groups():
    """获取当前赛事评审组数量，无活跃赛事时使用默认值"""
    comp = get_active_competition()
    if comp and comp.judge_groups is not None:
        return comp.judge_groups
    return _DEFAULT_JUDGE_GROUPS


def get_judges_per_group():
    """获取当前赛事每组评委数，无活跃赛事时使用默认值"""
    comp = get_active_competition()
    if comp and comp.judges_per_group is not None:
        return comp.judges_per_group
    return _DEFAULT_JUDGES_PER_GROUP


def get_machine_weight():
    """获取当前赛事机器评分权重，无活跃赛事时使用默认值"""
    comp = get_active_competition()
    if comp and comp.machine_weight is not None:
        return comp.machine_weight
    return _DEFAULT_MACHINE_WEIGHT


def get_human_weight():
    """获取当前赛事人工评分权重，无活跃赛事时使用默认值"""
    comp = get_active_competition()
    if comp and comp.human_weight is not None:
        return comp.human_weight
    return _DEFAULT_HUMAN_WEIGHT


# GENERIC: 向后兼容的别名/常量（惰性求值，启动时不会触发数据库查询）
# 旧代码直接引用 SCORING_DIMENSIONS 的，建议改为调用 get_scoring_dimensions()
# 以下属性在模块加载时不会访问数据库，仅在首次访问时求值
class _LazyConfig:
    """惰性配置访问器：首次访问时从数据库加载"""
    @property
    def SCORING_DIMENSIONS(self):
        return get_scoring_dimensions()

    @property
    def FILE_NAMING_PATTERN(self):
        return get_naming_pattern()

    @property
    def JUDGE_GROUPS(self):
        return get_judge_groups()

    @property
    def JUDGES_PER_GROUP(self):
        return get_judges_per_group()

    @property
    def PRIMARY_TASK_NAME(self):
        return "创意编程（小学组）现场任务"

    @property
    def MIDDLE_TASK_NAME(self):
        return "重走三秦长征路"


_lazy_config = _LazyConfig()

# 为了最大兼容旧代码的 import config.SCORING_DIMENSIONS 用法，
# 在模块级别暴露这些属性（通过 __getattr__ 实现惰性加载）
import sys as _sys

_original_module = _sys.modules[__name__]


class _LazyConfigModule(type(_original_module)):
    def __getattr__(self, name):
        if name in ("SCORING_DIMENSIONS", "FILE_NAMING_PATTERN", "JUDGE_GROUPS", "JUDGES_PER_GROUP", "PRIMARY_TASK_NAME", "MIDDLE_TASK_NAME"):
            return getattr(_lazy_config, name)
        raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


_lazy_module = _LazyConfigModule(__name__)
_lazy_module.__dict__.update(_original_module.__dict__)
_sys.modules[__name__] = _lazy_module
