"""
teams.py 路由接口测试
覆盖：列表、统计、查询/筛选、详情、pending/confirmed/review-stats、
      导入、分组分配、删除、确认、批量确认、更新
"""
import pytest
import sys
from pathlib import Path
import io

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from database import Team, Work, MachineScore


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def seed_teams(db):
    """创建 4 支队伍（2小学 pending + 2初中 confirmed，含评审组）"""
    teams = [
        Team(
            team_code="T001", short_code="P001", team_name=" Alpha队",
            group_type="小学组", school="测试小学A", district="雁塔区",
            teacher="张老师 (00000000000)", teacher_name="张老师",
            teacher_phone="00000000000",
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
    # 刷新以获取 id
    for t in teams:
        db.refresh(t)
    return teams


@pytest.fixture
def seed_team_with_work(db):
    """创建一支含作品和机器评分的队伍"""
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
        code_language="Python",
        code_line_count=150,
        is_parsed=True,
        original_filename="w001.zip",
        has_source=True,
    )
    db.add(work)
    db.commit()

    ms = MachineScore(
        team_id=team.id,
        model_name="test-model",
        theme_score=12,
        presentation_score=20,
        process_score=18,
        ai_literacy_score=14,
        total_score=64,
        is_completed=True,
        is_adopted=True,
    )
    db.add(ms)
    db.commit()
    return team


def _make_excel_bytes():
    """在内存中构造一个最小合法的导入 Excel（openpyxl）"""
    try:
        import openpyxl
    except ImportError:
        pytest.skip("openpyxl 未安装，跳过导入测试")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    # 写入表头（使用服务能识别的列名）
    headers = [
        "队伍编号", "队伍名称", "活动类别", "组别", "学校",
        "区县", "指导老师", "团队成员1", "团队成员2",
    ]
    ws.append(headers)
    # 写入 2 行数据（第一行含半角括号，测试 teacher 拆分）
    ws.append([
        "IMP001", "导入队A", "创意编程", "小学组", "导入小学",
        "示例区", "导入老师(00000000000)", "学生甲", "学生乙",
    ])
    ws.append([
        "IMP002", "导入队B", "创意编程", "初中组", "导入初中",
        "碑林区", "导入老师2", "学生丙", "",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# 1. 列表接口
# ---------------------------------------------------------------------------

def test_get_teams_list_all(client, seed_teams):
    """GET /api/teams — 无参数返回全部"""
    resp = client.get("/api/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    assert len(data["teams"]) == 4


def test_get_teams_filter_by_group_type(client, seed_teams):
    """GET /api/teams?group_type=小学组 — 按组别筛选"""
    resp = client.get("/api/teams?group_type=小学组")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert all(t["group_type"] == "小学组" for t in data["teams"])


def test_get_teams_filter_by_judge_group(client, seed_teams):
    """GET /api/teams?judge_group=1 — 按评审组筛选"""
    resp = client.get("/api/teams?judge_group=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert all(t["judge_group"] == 1 for t in data["teams"])


def test_get_teams_search(client, seed_teams):
    """GET /api/teams?search=Alpha — 按名称搜索"""
    resp = client.get("/api/teams?search=Alpha")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["teams"][0]["team_name"] == " Alpha队"


def test_get_teams_search_by_school(client, seed_teams):
    """GET /api/teams?search=测试小学A — 按学校搜索"""
    resp = client.get("/api/teams?search=测试小学A")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert "测试小学A" in data["teams"][0]["school"]


def test_get_teams_combined_filters(client, seed_teams):
    """GET /api/teams?group_type=小学组&judge_group=1 — 组合筛选"""
    resp = client.get("/api/teams?group_type=小学组&judge_group=1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


def test_get_teams_empty_result(client, db):
    """GET /api/teams?search=不存在 — 空数据返回空列表"""
    resp = client.get("/api/teams?search=不存在")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["teams"] == []


# ---------------------------------------------------------------------------
# 2. 统计接口
# ---------------------------------------------------------------------------

def test_get_team_stats(client, seed_teams):
    """GET /api/teams/stats — 返回总数、小学、初中、分组"""
    resp = client.get("/api/teams/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    assert data["primary"] == 2
    assert data["junior"] == 2
    assert "groups" in data
    # group_1 有 2 支小学 pending，group_2 有 1 支初中 confirmed
    assert data["groups"]["group_1"] == 2
    assert data["groups"]["group_2"] == 1


def test_get_team_stats_empty_db(client, db):
    """GET /api/teams/stats — 空数据库返回零值"""
    resp = client.get("/api/teams/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["primary"] == 0
    assert data["junior"] == 0


# ---------------------------------------------------------------------------
# 3. pending / confirmed / review-stats
# ---------------------------------------------------------------------------

def test_get_pending_teams(client, seed_teams):
    """GET /api/teams/pending — 只返回 pending 状态"""
    resp = client.get("/api/teams/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert all(t["status"] == "pending" for t in data["teams"])


def test_get_pending_teams_with_group_filter(client, seed_teams):
    """GET /api/teams/pending?group_type=小学组 — pending + 组别筛选"""
    resp = client.get("/api/teams/pending?group_type=小学组")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


def test_get_confirmed_teams(client, seed_teams):
    """GET /api/teams/confirmed — 只返回 confirmed 状态"""
    resp = client.get("/api/teams/confirmed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert all(t["status"] == "confirmed" for t in data["teams"])


def test_get_confirmed_teams_with_judge_group(client, seed_teams):
    """GET /api/teams/confirmed?judge_group=2 — confirmed + 评审组筛选"""
    resp = client.get("/api/teams/confirmed?judge_group=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["teams"][0]["short_code"] == "P003"


def test_get_review_stats(client, seed_teams):
    """GET /api/teams/review-stats — 审核统计"""
    resp = client.get("/api/teams/review-stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pending"] == 2
    assert data["confirmed"] == 2
    assert data["total"] == 4
    assert "pending_by_source" in data
    # pending 中 1 个 excel, 1 个 work
    assert data["pending_by_source"]["excel"] == 1
    assert data["pending_by_source"]["work"] == 1


def test_get_review_stats_empty(client, db):
    """GET /api/teams/review-stats — 空数据库"""
    resp = client.get("/api/teams/review-stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pending"] == 0
    assert data["confirmed"] == 0
    assert data["total"] == 0


# ---------------------------------------------------------------------------
# 4. 单队伍详情
# ---------------------------------------------------------------------------

def test_get_team_detail_exists(client, seed_teams):
    """GET /api/teams/{id} — 存在的队伍返回完整详情"""
    team = seed_teams[0]
    resp = client.get(f"/api/teams/{team.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == team.id
    assert data["team_code"] == "T001"
    assert data["work"] is None  # 未创建作品


def test_get_team_detail_with_work_and_score(client, seed_team_with_work):
    """GET /api/teams/{id} — 含作品和机器评分的队伍"""
    team = seed_team_with_work
    resp = client.get(f"/api/teams/{team.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["work"] is not None
    assert data["work"]["code_language"] == "Python"
    assert data["machine_score"] == 64
    assert data["machine_theme"] == 12


def test_get_team_detail_not_found(client, db):
    """GET /api/teams/9999 — 不存在的队伍返回 404"""
    resp = client.get("/api/teams/9999")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Excel 导入（轻量路径）
# ---------------------------------------------------------------------------

def test_import_teams_from_excel(client, db):
    """POST /api/teams/import — 成功导入 2 支队伍"""
    xlsx_bytes = _make_excel_bytes()
    resp = client.post(
        "/api/teams/import",
        files={
            "file": ("import_test.xlsx", xlsx_bytes,
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["imported"] == 2
    assert data["errors"] == []
    # 验证数据库中确实存在
    teams = db.query(Team).filter(Team.team_code.in_(["IMP001", "IMP002"])).all()
    assert len(teams) == 2
    # 验证 teacher 拆分（半角括号无空格）
    t1 = next(t for t in teams if t.team_code == "IMP001")
    assert t1.teacher_name == "导入老师"
    assert t1.teacher_phone == "00000000000"
    # 无括号的不拆分
    t2 = next(t for t in teams if t.team_code == "IMP002")
    assert t2.teacher_name == "导入老师2"
    assert t2.teacher_phone == ""


def _make_excel_with_teacher(teacher_value: str, team_code: str):
    """生成仅含一行数据的 Excel，用于测试 teacher 格式"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([
        "队伍编号", "队伍名称", "活动类别", "组别", "学校",
        "区县", "指导老师", "团队成员1",
    ])
    ws.append([
        team_code, "测试队", "创意编程", "小学组", "测试学校",
        "雁塔区", teacher_value, "学生甲",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


@pytest.mark.parametrize("teacher_raw,expected_name,expected_phone", [
    ("张三(00000000000)", "张三", "00000000000"),      # 半角括号，无空格
    ("张三（00000000000）", "张三", "00000000000"),      # 全角括号，无空格
    ("张三 (00000000000)", "张三", "00000000000"),       # 半角括号，有空格
    ("张三 （00000000000）", "张三", "00000000000"),      # 全角括号，有空格
])
def test_import_teams_teacher_parentheses_formats(
    client, db, teacher_raw, expected_name, expected_phone
):
    """POST /api/teams/import — teacher 字段四种括号格式均正确拆分"""
    xlsx_bytes = _make_excel_with_teacher(teacher_raw, "FMT001")
    resp = client.post(
        "/api/teams/import",
        files={
            "file": ("fmt_test.xlsx", xlsx_bytes,
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        },
    )
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1

    team = db.query(Team).filter(Team.team_code == "FMT001").first()
    assert team is not None
    assert team.teacher_name == expected_name
    assert team.teacher_phone == expected_phone
    # 严格断言：teacher_name 中不应残留任何括号字符
    assert "(" not in team.teacher_name
    assert ")" not in team.teacher_name
    assert "（" not in team.teacher_name
    assert "）" not in team.teacher_name
    # 严格断言：teacher_phone 必须是精确的 11 位数字
    assert len(team.teacher_phone) == 11
    assert team.teacher_phone.isdigit()


def test_import_teams_invalid_file_type(client, db):
    """POST /api/teams/import — 非 Excel 文件返回 400"""
    resp = client.post(
        "/api/teams/import",
        files={"file": ("test.txt", b"not an excel", "text/plain")},
    )
    assert resp.status_code == 400
    assert "仅支持Excel" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 6. 评审组分配
# ---------------------------------------------------------------------------

def test_assign_judge_groups(client, seed_teams):
    """POST /api/teams/assign-groups — 随机分配评审组"""
    resp = client.post("/api/teams/assign-groups?num_groups=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["assigned_groups"] == 2
    assert "details" in data
    # 4 支队伍分配到 2 组
    total_assigned = sum(g["total"] for g in data["details"].values())
    assert total_assigned == 4


def test_assign_judge_groups_default(client, seed_teams):
    """POST /api/teams/assign-groups — 默认组数（不传 num_groups）"""
    resp = client.post("/api/teams/assign-groups")
    assert resp.status_code == 200
    data = resp.json()
    # 默认使用 config 中的 get_judge_groups()，通常是 4
    assert "assigned_groups" in data


# ---------------------------------------------------------------------------
# 7. 删除
# ---------------------------------------------------------------------------

def test_delete_team_success(client, seed_teams):
    """DELETE /api/teams/{id} — 删除存在的队伍"""
    team = seed_teams[0]
    resp = client.delete(f"/api/teams/{team.id}")
    assert resp.status_code == 200
    assert resp.json()["message"] == "删除成功"
    # 验证已删除
    resp2 = client.get(f"/api/teams/{team.id}")
    assert resp2.status_code == 404


def test_delete_team_not_found(client, db):
    """DELETE /api/teams/9999 — 删除不存在的队伍返回 404"""
    resp = client.delete("/api/teams/9999")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 8. 确认 / 批量确认
# ---------------------------------------------------------------------------

def test_confirm_team_success(client, seed_teams):
    """POST /api/teams/{id}/confirm — 确认 pending 队伍"""
    team = seed_teams[0]  # pending
    assert team.status == "pending"
    resp = client.post(f"/api/teams/{team.id}/confirm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "确认成功"
    assert data["team"]["status"] == "confirmed"


def test_confirm_team_not_found(client, db):
    """POST /api/teams/9999/confirm — 确认不存在的队伍返回 404"""
    resp = client.post("/api/teams/9999/confirm")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


def test_batch_confirm_teams(client, seed_teams):
    """POST /api/teams/batch-confirm — 批量确认"""
    pending_ids = [t.id for t in seed_teams if t.status == "pending"]
    assert len(pending_ids) == 2
    resp = client.post("/api/teams/batch-confirm", json={"team_ids": pending_ids})
    assert resp.status_code == 200
    data = resp.json()
    assert data["confirmed_count"] == 2
    assert "成功确认 2 支队伍" in data["message"]


def test_batch_confirm_empty(client, db):
    """POST /api/teams/batch-confirm — 空列表确认 0 支"""
    resp = client.post("/api/teams/batch-confirm", json={"team_ids": []})
    assert resp.status_code == 200
    assert resp.json()["confirmed_count"] == 0


# ---------------------------------------------------------------------------
# 9. 更新
# ---------------------------------------------------------------------------

def test_update_team_success(client, seed_teams):
    """PUT /api/teams/{id} — 更新队伍信息"""
    team = seed_teams[0]
    resp = client.put(
        f"/api/teams/{team.id}",
        json={
            "team_name": "更新后的队名",
            "school": "更新后的学校",
            "district": "更新后的区县",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "更新成功"
    assert data["team"]["team_name"] == "更新后的队名"
    assert data["team"]["school"] == "更新后的学校"
    assert data["team"]["district"] == "更新后的区县"
    # 未更新的字段保持原值
    assert data["team"]["team_code"] == "T001"


def test_update_team_partial(client, seed_teams):
    """PUT /api/teams/{id} — 只更新部分字段"""
    team = seed_teams[0]
    resp = client.put(
        f"/api/teams/{team.id}",
        json={"team_name": "仅改名"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["team"]["team_name"] == "仅改名"
    # 其他字段不变
    assert data["team"]["school"] == "测试小学A"


def test_update_team_not_found(client, db):
    """PUT /api/teams/9999 — 更新不存在的队伍返回 404"""
    resp = client.put("/api/teams/9999", json={"team_name": "不存在"})
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


def test_update_team_members(client, seed_teams):
    """PUT /api/teams/{id} — 更新 members 字段"""
    team = seed_teams[0]
    new_members = [
        {"name": "新学生1", "school": "新学校1"},
        {"name": "新学生2", "school": "新学校2"},
    ]
    resp = client.put(
        f"/api/teams/{team.id}",
        json={"members": new_members},
    )
    assert resp.status_code == 200
    assert resp.json()["team"]["members"] == new_members
