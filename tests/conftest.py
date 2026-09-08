"""
测试 fixtures
- 内存数据库（每次测试独立）
- Mock LLM 服务
- Mock 文件系统依赖
"""
import pytest
import sys
import tempfile
from pathlib import Path

# 添加 backend 到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from database import Base, create_engine, sessionmaker, SessionLocal
from services.llm_service import LLMService
from sqlalchemy.pool import StaticPool


# 内存数据库引擎（测试专用）
# 使用 StaticPool 确保 :memory: 数据库在连接归还后不会丢失
_test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_test_engine)


@pytest.fixture(scope="function")
def db(monkeypatch):
    """每个测试用例独立的内存数据库"""
    # 创建表
    Base.metadata.create_all(bind=_test_engine)
    session = _TestSessionLocal()

    # 覆盖 database 模块的全局 SessionLocal，让业务代码也用内存数据库
    import database as db_module
    monkeypatch.setattr(db_module, "SessionLocal", _TestSessionLocal)
    monkeypatch.setattr(db_module, "engine", _test_engine)

    try:
        yield session
    finally:
        session.close()
        # 清理表数据（保留表结构）— SQLAlchemy 2.0 语法
        with _test_engine.connect() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
            conn.commit()


@pytest.fixture
def mock_llm_service(monkeypatch):
    """Mock LLM 服务，返回固定评分结果"""
    async def mock_chat_json(self, system_prompt, user_prompt, temperature=0.2, model=None, seed=None):
        return {
            "theme_score": 15,
            "presentation_score": 22,
            "process_score": 20,
            "ai_literacy_score": 15,
            "theme_comment": "主题明确，有创意",
            "presentation_comment": "功能完整，界面清晰",
            "process_comment": "过程记录较完整",
            "ai_literacy_comment": "AI工具使用合理",
            "overall_comment": "总体表现良好",
            "defense_questions": ["Q1", "Q2", "Q3", "Q4", "Q5"],
        }

    monkeypatch.setattr(LLMService, "chat_json", mock_chat_json)
    return LLMService()


@pytest.fixture
def mock_works_dir(monkeypatch):
    """Mock WORKS_DIR 为临时目录，并创建必要的子目录"""
    import config
    import services.scorer as scorer_module
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        # 同时 mock config 和 scorer 模块里的 WORKS_DIR（后者是导入时固化的）
        monkeypatch.setattr(config, "WORKS_DIR", tmp_path)
        monkeypatch.setattr(scorer_module, "WORKS_DIR", tmp_path)
        yield tmp_path


@pytest.fixture
def mock_scorer_methods(monkeypatch):
    """Mock AutoScorer 的文件系统依赖方法"""
    from services.scorer import AutoScorer

    # Mock 代码分析（字段必须匹配 _code_result_to_dict）
    monkeypatch.setattr(
        AutoScorer, "_analyze_code",
        lambda self, work, extract_dir: type("obj", (object,), {
            "language": work.code_language or "Python",
            "total_lines": work.code_line_count or 50,
            "code_lines": work.code_line_count or 40,
            "comment_lines": work.code_line_count or 10,
            "comment_rate": work.comment_rate or 0.2,
            "function_count": 2,
            "class_count": 1,
            "cyclomatic_complexity": 3,
            "features": ["basic_loop"],
            "game_mechanics": [],
            "quality_score": 7.0,
            "creativity_score": 6.0,
            "issues": [],
            "warnings": [],
            # 保留旧 mock 字段以防其他地方用到
            "line_count": work.code_line_count or 0,
            "has_main_loop": True,
            "functions": [],
            "classes": [],
            "imports": [],
            "complexity": {"cyclomatic": 1},
        })()
    )

    # Mock AIGC分析（字段必须匹配 _aigc_result_to_dict）
    monkeypatch.setattr(
        AutoScorer, "_analyze_aigc",
        lambda self, work, extract_dir: type("obj", (object,), {
            "tool_name": None,
            "total_interactions": 0,
            "user_messages": 0,
            "code_generation_count": 0,
            "debugging_count": 0,
            "concept_count": 0,
            "optimization_count": 0,
            "iteration_depth": 0,
            "duration_minutes": 0,
            "features": [],
            "process_score": 0,
            "ai_literacy_score": 0,
            "warnings": [],
            # 保留旧字段
            "has_aigc": False,
            "platform": None,
            "logs": [],
        })()
    )

    # Mock 文档分析（字段必须匹配 _doc_result_to_dict）
    monkeypatch.setattr(
        AutoScorer, "_analyze_document",
        lambda self, work, extract_dir: {
            "has_document": False,
            "document_char_count": 0,
            "document_content": "",
            "has_doc": False,
        }
    )

    # Mock 功能检测
    monkeypatch.setattr(
        AutoScorer, "_detect_features",
        lambda self, work, extract_dir, code_result: {
            "has_ui": False, "has_game": False, "has_animation": False,
            "has_data_processing": False, "has_ai_integration": False,
        }
    )

    # Mock 代码内容收集
    monkeypatch.setattr(
        AutoScorer, "_collect_code_content",
        lambda self, work, extract_dir: "print('hello')"
    )

    # Mock AIGC摘要收集
    monkeypatch.setattr(
        AutoScorer, "_collect_aigc_summary",
        lambda self, aigc_result: ""
    )


@pytest.fixture
def client(db):
    """FastAPI TestClient，使用内存数据库（与 db fixture 共享 session）"""
    from fastapi.testclient import TestClient
    from main import app
    from database import get_db

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
