"""
works.py + judge_groups.py 路由接口测试
覆盖：作品状态查询、上传/解析边界、评审组配置、分配、异常边界
"""
import pytest
import sys
import io
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from database import Team, Work, JudgeGroupConfig


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def seed_teams(db):
    """创建 4 支队伍（2小学 + 2初中）"""
    teams = [
        Team(
            team_code="T001", short_code="P001", team_name="Alpha队",
            group_type="小学组", school="测试小学A", district="雁塔区",
            teacher="张老师", teacher_name="张老师",
            members=[{"name": "学生A", "school": "测试小学A"}],
            status="pending", source="excel", judge_group=1,
        ),
        Team(
            team_code="T002", short_code="P002", team_name="Beta队",
            group_type="小学组", school="测试小学B", district="碑林区",
            teacher="李老师", teacher_name="李老师",
            members=[{"name": "学生B", "school": "测试小学B"}],
            status="pending", source="work", judge_group=1,
        ),
        Team(
            team_code="T003", short_code="P003", team_name="Gamma队",
            group_type="初中组", school="测试初中A", district="雁塔区",
            teacher="王老师", teacher_name="王老师",
            members=[{"name": "学生C", "school": "测试初中A"}],
            status="confirmed", source="excel", judge_group=2,
        ),
        Team(
            team_code="T004", short_code="P004", team_name="Delta队",
            group_type="初中组", school="测试初中B", district="未央区",
            teacher="赵老师", teacher_name="赵老师",
            members=[{"name": "学生D", "school": "测试初中B"}],
            status="confirmed", source="excel", judge_group=None,
        ),
    ]
    for t in teams:
        db.add(t)
    db.commit()
    for t in teams:
        db.refresh(t)
    return teams


@pytest.fixture
def seed_team_with_work(db):
    """创建一支含作品的队伍"""
    team = Team(
        team_code="W001", short_code="P005", team_name="作品队",
        group_type="小学组", school="作品小学", district="高新区",
        status="confirmed", source="excel", judge_group=1,
    )
    db.add(team)
    db.commit()
    db.refresh(team)

    work = Work(
        team_id=team.id,
        original_filename="小学组+张三.zip",
        file_path="/tmp/works/小学组+张三.zip",
        file_size=1024,
        code_language="Python",
        code_line_count=150,
        comment_rate=0.15,
        is_parsed=True,
        has_source=True,
        has_aigc_log=False,
        has_screenshots=True,
        source_files=["main.py"],
        screenshot_files=["screenshot1.png"],
    )
    db.add(work)
    db.commit()
    return team


@pytest.fixture
def seed_work_unparsed(db, seed_teams):
    """创建一支含未解析作品的队伍"""
    team = seed_teams[0]
    work = Work(
        team_id=team.id,
        original_filename="小学组+李四.zip",
        file_path="/tmp/works/小学组+李四.zip",
        file_size=512,
        is_parsed=False,
        has_source=False,
    )
    db.add(work)
    db.commit()
    return work


def _make_zip_bytes(filename: str = "test.py", content: bytes = b"print('hello')"):
    """在内存中构造一个最小 ZIP 文件"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# 1. works 状态查询
# ---------------------------------------------------------------------------

def test_get_works_status_empty(client, db):
    """GET /api/works/status — 空数据库"""
    resp = client.get("/api/works/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_teams"] == 0
    assert data["works_uploaded"] == 0
    assert data["works_parsed"] == 0
    assert data["upload_progress"] == "0/0"


def test_get_works_status_with_data(client, seed_team_with_work):
    """GET /api/works/status — 有队伍和作品"""
    resp = client.get("/api/works/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_teams"] == 1
    assert data["works_uploaded"] == 1
    assert data["works_parsed"] == 1
    assert data["works_with_source"] == 1
    assert data["works_with_screenshots"] == 1
    assert data["works_with_aigc"] == 0
    assert data["works_flagged"] == 0
    assert data["upload_progress"] == "1/1"


# ---------------------------------------------------------------------------
# 2. works 队伍作品详情
# ---------------------------------------------------------------------------

def test_get_team_work_exists(client, seed_team_with_work):
    """GET /api/works/team/{team_id} — 存在作品"""
    team = seed_team_with_work
    resp = client.get(f"/api/works/team/{team.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["work"] is not None
    assert data["work"]["code_language"] == "Python"
    assert data["work"]["code_line_count"] == 150
    assert data["work"]["is_parsed"] is True
    assert data["work"]["has_source"] is True


def test_get_team_work_not_found(client, seed_teams):
    """GET /api/works/team/{team_id} — 无作品返回提示"""
    team = seed_teams[0]
    resp = client.get(f"/api/works/team/{team.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["work"] is None
    assert "暂未上传作品" in data["message"]


def test_get_team_work_invalid_id(client, db):
    """GET /api/works/team/abc — 非数字 ID"""
    resp = client.get("/api/works/team/abc")
    # FastAPI path param int 会自动校验，返回 422
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. works 上传边界
# ---------------------------------------------------------------------------

def test_upload_work_invalid_type(client, db):
    """POST /api/works/upload — 非 ZIP 文件返回 400"""
    resp = client.post(
        "/api/works/upload",
        files={"file": ("test.txt", b"not a zip", "text/plain")},
    )
    assert resp.status_code == 400
    assert "仅支持ZIP" in resp.json()["detail"]


def test_upload_work_success(client, db, mock_works_dir):
    """POST /api/works/upload — ZIP 上传并解析成功（轻量）"""
    # 先创建匹配的队伍，让上传能关联到队伍
    team = Team(
        team_code="Z001", short_code="P009", team_name="张三",
        group_type="小学组", school="测试小学", district="雁塔区",
        teacher="张老师", teacher_name="张老师",
        status="pending", source="excel",
    )
    db.add(team)
    db.commit()
    db.refresh(team)

    zip_bytes = _make_zip_bytes("main.py", b"print('hello world')")
    resp = client.post(
        "/api/works/upload",
        files={
            "file": ("小学组+张三.zip", zip_bytes, "application/zip")
        },
    )
    # 文件名解析成功、ZIP 有效、能走到解析逻辑
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "上传并解析成功"
    assert data["filename"] == "小学组+张三.zip"
    assert "team_id" in data
    assert data["team_id"] == team.id
    assert "parse_result" in data

    # 验证数据库中有作品记录
    work = db.query(Work).filter(Work.team_id == team.id).first()
    assert work is not None
    assert work.original_filename == "小学组+张三.zip"


def test_upload_work_bad_zip(client, db):
    """POST /api/works/upload — 无效 ZIP 文件返回 400"""
    resp = client.post(
        "/api/works/upload",
        files={"file": ("小学组+张三.zip", b"not a real zip", "application/zip")},
    )
    assert resp.status_code == 400
    assert "无效的ZIP" in resp.json()["detail"]


def test_upload_work_bad_filename(client, db):
    """POST /api/works/upload — 文件名不符合命名规范"""
    zip_bytes = _make_zip_bytes()
    resp = client.post(
        "/api/works/upload",
        files={"file": ("bad_name.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 400
    assert "命名不符合规范" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 4. works 重新解析
# ---------------------------------------------------------------------------

def test_parse_work_not_found(client, db):
    """POST /api/works/parse/{work_id} — 不存在的作品"""
    resp = client.post("/api/works/parse/9999")
    assert resp.status_code == 400
    assert "解析失败" in resp.json()["detail"] or "不存在" in resp.json()["detail"]


def test_parse_all_works_empty(client, db):
    """POST /api/works/parse-all — 空数据库"""
    resp = client.post("/api/works/parse-all")
    assert resp.status_code == 200
    data = resp.json()
    assert "parsed" in data or "total" in data or "message" in data


# ---------------------------------------------------------------------------
# 5. judge_groups 配置读取
# ---------------------------------------------------------------------------

def test_get_judge_groups_default(client, db):
    """GET /api/judge-groups — 无配置返回默认 4 组"""
    resp = client.get("/api/judge-groups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_default"] is True
    assert len(data["groups"]) == 4
    assert data["groups"][0]["group_index"] == 1
    assert data["groups"][0]["group_name"] == "第1评审组"
    assert data["groups"][0]["team_count"] == 0


def test_get_judge_groups_with_config(client, db):
    """GET /api/judge-groups — 有自定义配置"""
    cfg = JudgeGroupConfig(
        group_index=1, group_name="A组", description="第一组",
        judges=["评委1", "评委2"]
    )
    db.add(cfg)
    db.commit()

    resp = client.get("/api/judge-groups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_default"] is False
    assert len(data["groups"]) == 1
    assert data["groups"][0]["group_name"] == "A组"
    assert data["groups"][0]["judges"] == ["评委1", "评委2"]
    assert data["groups"][0]["team_count"] == 0


# ---------------------------------------------------------------------------
# 6. judge_groups 保存配置
# ---------------------------------------------------------------------------

def test_save_judge_groups(client, db):
    """POST /api/judge-groups — 保存自定义配置"""
    resp = client.post(
        "/api/judge-groups",
        json={
            "groups": [
                {"group_index": 1, "group_name": "小学A组", "description": "小学第一组", "judges": ["评委A"]},
                {"group_index": 2, "group_name": "小学B组", "description": "", "judges": []},
                {"group_index": 3, "group_name": "初中组", "description": "初中", "judges": ["评委B", "评委C"]},
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "评审组配置保存成功"
    assert data["total_groups"] == 3

    # 验证数据库
    configs = db.query(JudgeGroupConfig).order_by(JudgeGroupConfig.group_index).all()
    assert len(configs) == 3
    assert configs[0].group_name == "小学A组"
    assert configs[2].judges == ["评委B", "评委C"]


def test_save_judge_groups_overwrite(client, db):
    """POST /api/judge-groups — 保存会覆盖旧配置"""
    db.add(JudgeGroupConfig(group_index=1, group_name="旧组"))
    db.commit()

    resp = client.post(
        "/api/judge-groups",
        json={"groups": [{"group_index": 1, "group_name": "新组", "judges": []}]},
    )
    assert resp.status_code == 200
    configs = db.query(JudgeGroupConfig).all()
    assert len(configs) == 1
    assert configs[0].group_name == "新组"


# ---------------------------------------------------------------------------
# 7. judge_groups 分配队伍
# ---------------------------------------------------------------------------

def test_assign_teams_to_groups(client, seed_teams):
    """POST /api/judge-groups/assign — 分配队伍到评审组"""
    resp = client.post("/api/judge-groups/assign")
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "队伍分配完成"
    assert data["total_teams"] == 4
    assert "groups" in data

    # 各组统计之和应等于总队伍数
    total = sum(g["total"] for g in data["groups"])
    assert total == 4

    # 小学组之和应为 2，初中组之和应为 2
    primary = sum(g["primary"] for g in data["groups"])
    junior = sum(g["junior"] for g in data["groups"])
    assert primary == 2
    assert junior == 2


def test_assign_teams_to_groups_with_custom_config(client, db, seed_teams):
    """POST /api/judge-groups/assign — 自定义 2 组配置后分配"""
    db.add(JudgeGroupConfig(group_index=1, group_name="组1"))
    db.add(JudgeGroupConfig(group_index=2, group_name="组2"))
    db.commit()

    resp = client.post("/api/judge-groups/assign")
    assert resp.status_code == 200
    data = resp.json()
    # 只有 2 组配置，所以 groups 长度为 2
    assert len(data["groups"]) == 2
    total = sum(g["total"] for g in data["groups"])
    assert total == 4


# ---------------------------------------------------------------------------
# 8. judge_groups 上传分配表边界
# ---------------------------------------------------------------------------

def test_upload_judge_assignment_invalid_type(client, db):
    """POST /api/judge-groups/upload-assignment — 非 Excel 返回 400"""
    resp = client.post(
        "/api/judge-groups/upload-assignment",
        files={"file": ("test.txt", b"not excel", "text/plain")},
    )
    assert resp.status_code == 400
    assert "仅支持Excel" in resp.json()["detail"]
