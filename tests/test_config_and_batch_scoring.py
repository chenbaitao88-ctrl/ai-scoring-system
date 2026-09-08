"""
Phase 6A-4: 配置与批量评分路由测试
覆盖：task_books、scoring_forms、competitions、batch_scoring
"""
import json
import pytest
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from database import TaskBook, ScoringForm, Competition, Team, Work, MachineScore


# ---------------------------------------------------------------------------
# 1. task_books
# ---------------------------------------------------------------------------

def test_list_task_books_empty(client, db):
    """GET /api/task-books — 返回列表（启动时会预初始化默认任务书，不断言空）"""
    resp = client.get("/api/task-books")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_task_books_with_filter(client, db):
    """GET /api/task-books — 有数据 + group_type 过滤"""
    tb1 = TaskBook(name="任务书A", content_md="content A", group_type="小学组", is_active=False)
    tb2 = TaskBook(name="任务书B", content_md="content B", group_type="初中组", is_active=False)
    db.add_all([tb1, tb2])
    db.commit()

    resp = client.get("/api/task-books")
    assert resp.status_code == 200
    data = resp.json()
    # 启动时已预初始化默认任务书，至少要有 2 个
    assert len(data) >= 2

    resp = client.get("/api/task-books?group_type=小学组")
    assert resp.status_code == 200
    # 过滤后只包含小学组（含预初始化的）
    assert len(resp.json()) >= 1
    names = [t["name"] for t in resp.json()]
    assert "任务书A" in names


def test_get_active_task_book(client, db):
    """GET /api/task-books/active — 获取激活的任务书（启动时已激活默认）"""
    resp = client.get("/api/task-books/active")
    assert resp.status_code == 200
    assert resp.json() is not None


def test_get_active_task_book_none(client, db):
    """GET /api/task-books/active — 若取消所有激活后返回 None"""
    # 先取消所有激活
    from services.task_book_service import deactivate_task_book
    tbs = db.query(TaskBook).filter(TaskBook.is_active == True).all()
    for tb in tbs:
        deactivate_task_book(db, tb.id)

    resp = client.get("/api/task-books/active")
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_task_book_detail(client, db):
    """GET /api/task-books/{id} — 详情"""
    tb = TaskBook(name="详情任务书", content_md="md", is_active=False)
    db.add(tb)
    db.commit()
    db.refresh(tb)

    resp = client.get(f"/api/task-books/{tb.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "详情任务书"


def test_get_task_book_not_found(client, db):
    """GET /api/task-books/{id} — 不存在返回 404"""
    resp = client.get("/api/task-books/9999")
    assert resp.status_code == 404


def test_create_task_book(client, db):
    """POST /api/task-books — 创建"""
    resp = client.post("/api/task-books", json={
        "name": "新建任务书",
        "content_md": "# 内容",
        "group_type": "小学组"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "新建任务书"
    assert data["group_type"] == "小学组"
    assert data["is_active"] is False


def test_update_task_book(client, db):
    """PUT /api/task-books/{id} — 更新"""
    tb = TaskBook(name="旧名", content_md="old", is_active=False)
    db.add(tb)
    db.commit()
    db.refresh(tb)

    resp = client.put(f"/api/task-books/{tb.id}", json={
        "name": "新名",
        "content_md": "new content"
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "新名"
    assert resp.json()["content_md"] == "new content"


def test_update_task_book_not_found(client, db):
    """PUT /api/task-books/{id} — 不存在返回 404"""
    resp = client.put("/api/task-books/9999", json={"name": "新名"})
    assert resp.status_code == 404


def test_delete_task_book(client, db):
    """DELETE /api/task-books/{id} — 删除"""
    tb = TaskBook(name="待删", content_md="del", is_active=False)
    db.add(tb)
    db.commit()
    db.refresh(tb)

    resp = client.delete(f"/api/task-books/{tb.id}")
    assert resp.status_code == 200
    assert resp.json()["message"] == "删除成功"


def test_delete_task_book_not_found(client, db):
    """DELETE /api/task-books/{id} — 不存在返回 404"""
    resp = client.delete("/api/task-books/9999")
    assert resp.status_code == 404


def test_activate_task_book(client, db):
    """POST /api/task-books/{id}/activate — 激活"""
    tb = TaskBook(name="待激活", content_md="act", is_active=False)
    db.add(tb)
    db.commit()
    db.refresh(tb)

    resp = client.post(f"/api/task-books/{tb.id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_activate_task_book_not_found(client, db):
    """POST /api/task-books/{id}/activate — 不存在返回 404"""
    resp = client.post("/api/task-books/9999/activate")
    assert resp.status_code == 404


def test_deactivate_task_book(client, db):
    """POST /api/task-books/{id}/deactivate — 取消激活"""
    tb = TaskBook(name="已激活", content_md="deact", is_active=True)
    db.add(tb)
    db.commit()
    db.refresh(tb)

    resp = client.post(f"/api/task-books/{tb.id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


def test_upload_task_book_invalid_type(client, db):
    """POST /api/task-books/upload — 非 Word 文件返回 400"""
    resp = client.post(
        "/api/task-books/upload",
        files={"file": ("test.txt", b"not a word", "text/plain")}
    )
    assert resp.status_code == 400
    assert "只支持 .doc 或 .docx 格式" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. scoring_forms
# ---------------------------------------------------------------------------

def test_list_scoring_forms(client, db):
    """GET /api/scoring-forms — 列表"""
    sf = ScoringForm(name="评审表A", dimensions=[], is_default=False, is_active=False)
    db.add(sf)
    db.commit()

    resp = client.get("/api/scoring-forms")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_active_scoring_form_init_default(client, db):
    """GET /api/scoring-forms/active — 无激活时自动初始化默认"""
    resp = client.get("/api/scoring-forms/active")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "默认评审表（四维度）"
    assert len(data["dimensions"]) == 4


def test_get_default_scoring_form(client, db):
    """GET /api/scoring-forms/default — 获取默认评审表"""
    resp = client.get("/api/scoring-forms/default")
    assert resp.status_code == 200
    assert resp.json()["name"] == "默认评审表（四维度）"


def test_get_scoring_form_detail(client, db):
    """GET /api/scoring-forms/{id} — 详情"""
    sf = ScoringForm(name="详情", dimensions=[], is_default=False, is_active=False)
    db.add(sf)
    db.commit()
    db.refresh(sf)

    resp = client.get(f"/api/scoring-forms/{sf.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "详情"


def test_get_scoring_form_not_found(client, db):
    """GET /api/scoring-forms/{id} — 不存在返回 404"""
    resp = client.get("/api/scoring-forms/9999")
    assert resp.status_code == 404


def test_create_scoring_form(client, db):
    """POST /api/scoring-forms — 创建"""
    resp = client.post("/api/scoring-forms", json={
        "name": "新评审表",
        "dimensions": [
            {"name": "维度A", "max_score": 20, "description": "desc"}
        ]
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "新评审表"
    assert len(data["dimensions"]) == 1


def test_update_scoring_form(client, db):
    """PUT /api/scoring-forms/{id} — 更新"""
    sf = ScoringForm(name="旧评审表", dimensions=[], is_default=False, is_active=False)
    db.add(sf)
    db.commit()
    db.refresh(sf)

    resp = client.put(f"/api/scoring-forms/{sf.id}", json={
        "name": "新评审表",
        "dimensions": [{"name": "维度B", "max_score": 30}]
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "新评审表"


def test_update_scoring_form_not_found(client, db):
    """PUT /api/scoring-forms/{id} — 不存在返回 404"""
    resp = client.put("/api/scoring-forms/9999", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_scoring_form(client, db):
    """DELETE /api/scoring-forms/{id} — 删除"""
    sf = ScoringForm(name="待删", dimensions=[], is_default=False, is_active=False)
    db.add(sf)
    db.commit()
    db.refresh(sf)

    resp = client.delete(f"/api/scoring-forms/{sf.id}")
    assert resp.status_code == 200
    assert resp.json()["message"] == "删除成功"


def test_delete_default_scoring_form(client, db):
    """DELETE /api/scoring-forms/{id} — 删除默认评审表返回 400"""
    sf = ScoringForm(name="默认", dimensions=[], is_default=True, is_active=False)
    db.add(sf)
    db.commit()
    db.refresh(sf)

    resp = client.delete(f"/api/scoring-forms/{sf.id}")
    assert resp.status_code == 400
    assert "不能删除默认评审表" in resp.json()["detail"]


def test_delete_scoring_form_not_found(client, db):
    """DELETE /api/scoring-forms/{id} — 不存在返回 404"""
    resp = client.delete("/api/scoring-forms/9999")
    assert resp.status_code == 404


def test_activate_scoring_form(client, db):
    """POST /api/scoring-forms/{id}/activate — 激活"""
    sf = ScoringForm(name="待激活", dimensions=[], is_default=False, is_active=False)
    db.add(sf)
    db.commit()
    db.refresh(sf)

    resp = client.post(f"/api/scoring-forms/{sf.id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_activate_scoring_form_not_found(client, db):
    """POST /api/scoring-forms/{id}/activate — 不存在返回 404"""
    resp = client.post("/api/scoring-forms/9999/activate")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. competitions
# ---------------------------------------------------------------------------

def test_list_competitions_empty(client, db):
    """GET /api/competitions — 返回列表（启动时会预初始化默认赛事，不断言空）"""
    resp = client.get("/api/competitions")
    assert resp.status_code == 200
    assert isinstance(resp.json()["competitions"], list)
    # 默认赛事存在
    assert len(resp.json()["competitions"]) >= 1


def test_list_competitions_with_data(client, db):
    """GET /api/competitions — 有数据"""
    comp = Competition(name="测试赛", year=2026, scoring_mode="mixed", is_active=False)
    db.add(comp)
    db.commit()

    resp = client.get("/api/competitions")
    assert resp.status_code == 200
    # 含默认赛事 + 新增
    assert len(resp.json()["competitions"]) >= 2
    names = [c["name"] for c in resp.json()["competitions"]]
    assert "测试赛" in names


def test_get_current_competition(client, db):
    """GET /api/competitions/current — 当前激活比赛（默认赛事已激活）"""
    resp = client.get("/api/competitions/current")
    assert resp.status_code == 200
    assert resp.json()["competition"] is not None
    assert resp.json()["competition"]["is_active"] is True


def test_get_current_competition_none(client, db):
    """GET /api/competitions/current — 取消所有激活后返回 None"""
    # 取消所有赛事激活
    db.query(Competition).update({Competition.is_active: False})
    db.commit()

    resp = client.get("/api/competitions/current")
    assert resp.status_code == 200
    assert resp.json()["competition"] is None
    assert resp.json()["message"] == "当前无激活比赛"


def test_create_competition(client, db):
    """POST /api/competitions — 创建"""
    resp = client.post("/api/competitions", json={
        "name": "新比赛",
        "year": 2026,
        "scoring_mode": "ai_only",
        "ai_weight": 0.5,
        "human_weight": 0.5,
    })
    assert resp.status_code == 200
    assert resp.json()["message"] == "比赛创建成功"
    assert resp.json()["competition_id"] > 0


def test_create_competition_invalid_mode(client, db):
    """POST /api/competitions — 无效 scoring_mode 返回 400"""
    resp = client.post("/api/competitions", json={
        "name": "错误比赛",
        "scoring_mode": "invalid_mode",
    })
    assert resp.status_code == 400
    assert "mixed 或 ai_only" in resp.json()["detail"]


def test_update_competition(client, db):
    """PUT /api/competitions/{id} — 更新"""
    comp = Competition(name="旧比赛", scoring_mode="mixed", is_active=False)
    db.add(comp)
    db.commit()
    db.refresh(comp)

    resp = client.put(f"/api/competitions/{comp.id}", json={
        "name": "新比赛名",
        "scoring_mode": "ai_only",
        "ai_weight": 0.7,
    })
    assert resp.status_code == 200
    assert resp.json()["message"] == "比赛配置更新成功"
    assert resp.json()["competition"]["name"] == "新比赛名"


def test_update_competition_not_found(client, db):
    """PUT /api/competitions/{id} — 不存在返回 404"""
    resp = client.put("/api/competitions/9999", json={"name": "x"})
    assert resp.status_code == 404


def test_update_competition_invalid_mode(client, db):
    """PUT /api/competitions/{id} — 无效 scoring_mode 返回 400"""
    comp = Competition(name="比赛", scoring_mode="mixed", is_active=False)
    db.add(comp)
    db.commit()
    db.refresh(comp)

    resp = client.put(f"/api/competitions/{comp.id}", json={"scoring_mode": "bad"})
    assert resp.status_code == 400
    assert "mixed 或 ai_only" in resp.json()["detail"]


def test_activate_competition(client, db):
    """POST /api/competitions/{id}/activate — 激活"""
    comp = Competition(name="待激活", scoring_mode="mixed", is_active=False)
    db.add(comp)
    db.commit()
    db.refresh(comp)

    resp = client.post(f"/api/competitions/{comp.id}/activate")
    assert resp.status_code == 200
    assert resp.json()["competition"]["is_active"] is True


def test_activate_competition_not_found(client, db):
    """POST /api/competitions/{id}/activate — 不存在返回 404"""
    resp = client.post("/api/competitions/9999/activate")
    assert resp.status_code == 404


def test_get_competition_dimensions(client, db):
    """GET /api/competitions/{id}/dimensions — 评分维度"""
    comp = Competition(
        name="维度赛", scoring_mode="mixed", is_active=False,
        dimensions_config={"theme": {"name": "主题", "max_score": 20}}
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)

    resp = client.get(f"/api/competitions/{comp.id}/dimensions")
    assert resp.status_code == 200
    assert resp.json()["dimensions"]["theme"]["name"] == "主题"


def test_get_competition_dimensions_not_found(client, db):
    """GET /api/competitions/{id}/dimensions — 不存在返回 404"""
    resp = client.get("/api/competitions/9999/dimensions")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4. batch_scoring
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_batch_task_dir(monkeypatch):
    """Mock task_manager 的 TASK_DIR 到临时目录，避免测试污染文件系统"""
    import services.task_manager as tm
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(tm, "TASK_DIR", Path(tmpdir))
        yield Path(tmpdir)


def test_batch_scoring_start_disabled(client, db, monkeypatch):
    """POST /api/batch-scoring/start — 功能未启用返回 403"""
    # batch_scoring.py 是 `from feature_flags import is_enabled`，
    # 模块加载时已绑定函数引用，需 patch routers.batch_scoring.is_enabled
    monkeypatch.setattr("routers.batch_scoring.is_enabled", lambda name: False if name == "ENABLE_BATCH_SCORING" else True)

    resp = client.post("/api/batch-scoring/start", json={"model": "qwen3.8-max"})
    assert resp.status_code == 403
    assert "批量评分功能未启用" in resp.json()["detail"]


def test_batch_scoring_start_invalid_model(client, db):
    """POST /api/batch-scoring/start — 无效模型"""
    resp = client.post("/api/batch-scoring/start", json={"model": "invalid-model"})
    assert resp.status_code == 200
    assert "不支持的模型" in resp.json()["message"]
    assert "available_models" in resp.json()


def test_batch_scoring_start_no_works(client, db):
    """POST /api/batch-scoring/start — 没有待评分作品"""
    resp = client.post("/api/batch-scoring/start", json={"model": "qwen3.8-max"})
    assert resp.status_code == 200
    assert resp.json()["message"] == "没有待评分的作品"
    assert resp.json()["task_id"] is None


from unittest.mock import patch, MagicMock

def test_batch_scoring_start_success(client, db, mock_batch_task_dir):
    """POST /api/batch-scoring/start — 启动成功（mock 后台线程）"""
    # seed 一个已解析的作品
    team = Team(
        team_code="TC001", short_code="T001", team_name="测试队",
        school="测试学校", group_type="小学组", status="confirmed"
    )
    db.add(team)
    db.commit()
    db.refresh(team)

    work = Work(
        team_id=team.id,
        code_language="Python",
        is_parsed=True,
    )
    db.add(work)
    db.commit()

    # mock threading.Thread 避免真实启动评分（只 mock 构造函数，不影响 TestClient 内部线程）
    with patch("routers.batch_scoring.threading.Thread") as MockThread:
        instance = MockThread.return_value
        instance.start.return_value = None
        resp = client.post("/api/batch-scoring/start", json={
            "model": "qwen3.8-max",
            "parallel": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "批量评分已启动，请在任务状态页面查看进度"
        assert data["total"] == 1
        assert data["model"] == "qwen3.8-max"
        assert len(data["task_id"]) > 0
        assert MockThread.called


def test_batch_task_status(client, db, mock_batch_task_dir):
    """GET /api/batch-scoring/status/{task_id} — 查询任务状态"""
    import services.task_manager as tm
    tm.task_manager.create_task("test-123", "batch_scoring", total=10)

    resp = client.get("/api/batch-scoring/status/test-123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "test-123"
    assert data["status"] == "pending"
    assert data["progress"]["total"] == 10


def test_batch_task_status_not_found(client, db):
    """GET /api/batch-scoring/status/{task_id} — 不存在返回 404"""
    resp = client.get("/api/batch-scoring/status/nonexistent")
    assert resp.status_code == 404


def test_batch_task_results(client, db, mock_batch_task_dir):
    """GET /api/batch-scoring/results/{task_id} — 查询结果"""
    import services.task_manager as tm
    tm.task_manager.create_task("result-456", "batch_scoring", total=1)
    tm.task_manager.complete_task("result-456")

    resp = client.get("/api/batch-scoring/results/result-456")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "result-456"
    assert data["status"] == "completed"


def test_batch_task_results_not_found(client, db):
    """GET /api/batch-scoring/results/{task_id} — 不存在返回 404"""
    resp = client.get("/api/batch-scoring/results/nonexistent")
    assert resp.status_code == 404


def test_list_batch_tasks(client, db, mock_batch_task_dir):
    """GET /api/batch-scoring/tasks — 任务列表"""
    import services.task_manager as tm
    tm.task_manager.create_task("task-a", "batch_scoring", total=5)

    resp = client.get("/api/batch-scoring/tasks")
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) >= 1
    assert tasks[0]["task_id"] == "task-a"
