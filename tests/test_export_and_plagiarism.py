"""
export.py + plagiarism.py 路由接口测试
覆盖：导出汇总/Excel/Word/备份、查重检测/嫌疑列表/清除标记
"""
import pytest
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from database import Team, Work, MachineScore, HumanScore, FinalScore


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def seed_teams_with_scores(db):
    """创建 3 支队伍（含机器评分、人工评分、最终评分）"""
    teams = []
    for i, (gt, sc) in enumerate([("小学组", "P00"), ("小学组", "P01"), ("初中组", "P02")], 1):
        t = Team(
            team_code=f"E00{i}", short_code=sc, team_name=f"队伍{i}",
            group_type=gt, school=f"测试学校{i}", district="雁塔区",
            teacher="张老师", teacher_name="张老师",
            members=[{"name": f"学生{i}", "school": f"测试学校{i}"}],
            status="confirmed", source="excel", judge_group=1,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        teams.append(t)

        # 机器评分
        ms = MachineScore(
            team_id=t.id, model_name="test-model",
            theme_score=10 + i, presentation_score=20 + i,
            process_score=15 + i, ai_literacy_score=10 + i,
            total_score=55 + i * 4, is_completed=True, is_adopted=True,
            flags='[]',
        )
        db.add(ms)

        # 人工评分
        hs = HumanScore(
            team_id=t.id, judge_name="评委1",
            theme_score=12 + i, presentation_score=22 + i,
            process_score=18 + i, ai_literacy_score=12 + i,
            total_score=64 + i * 4,
        )
        db.add(hs)

        # 最终评分
        fs = FinalScore(
            team_id=t.id,
            final_score=60.0 + i * 5,
            ranking=i,
        )
        db.add(fs)

        # 作品
        work = Work(
            team_id=t.id,
            original_filename=f"{gt}+学生{i}.zip",
            code_language="Python",
            is_parsed=True,
            has_source=True,
        )
        db.add(work)

    db.commit()
    return teams


@pytest.fixture
def temp_excel_file():
    """创建一个临时 Excel 文件用于 mock 导出"""
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        # 写入最小合法的 xlsx 结构（实际只需文件存在即可）
        f.write(b"PK\x03\x04")  # ZIP 魔数
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def temp_word_file():
    """创建一个临时 Word 文件用于 mock 导出"""
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
        f.write(b"PK\x03\x04")
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 1. export score-summary
# ---------------------------------------------------------------------------

def test_export_score_summary_empty(client, db):
    """GET /api/export/score-summary — 空数据库"""
    resp = client.get("/api/export/score-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_teams"] == 0
    assert data["with_final_score"] == 0
    assert data["with_machine_score"] == 0
    assert data["with_human_score"] == 0
    assert data["flagged_count"] == 0
    assert data["primary_count"] == 0
    assert data["middle_count"] == 0
    assert data["results"] == []
    assert data["score_ranges"]["90-100"] == 0


def test_export_score_summary_with_data(client, seed_teams_with_scores):
    """GET /api/export/score-summary — 有评分数据"""
    resp = client.get("/api/export/score-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_teams"] == 3
    assert data["with_final_score"] == 3
    assert data["with_machine_score"] == 3
    assert data["with_human_score"] == 3
    assert data["flagged_count"] == 0
    assert data["primary_count"] == 2
    assert data["middle_count"] == 1
    assert len(data["results"]) == 3
    # 结果按最终分数降序
    assert data["results"][0]["final_score"] >= data["results"][1]["final_score"]


# ---------------------------------------------------------------------------
# 2. export Excel / Word (mock 文件生成)
# ---------------------------------------------------------------------------

def test_export_excel(client, db, temp_excel_file):
    """GET /api/export/excel — 返回 Excel 文件响应"""
    with patch("routers.export.build_excel_export") as mock_build:
        mock_build.return_value = (temp_excel_file, "评分汇总表_20260101_120000.xlsx")
        resp = client.get("/api/export/excel")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        # content-disposition 中中文被 URL 编码，解码后检查
        cd = unquote(resp.headers.get("content-disposition", ""))
        assert "评分汇总表" in cd


def test_export_word(client, db, temp_word_file):
    """GET /api/export/word — 返回 Word 文件响应"""
    with patch("routers.export.build_word_export") as mock_build:
        mock_build.return_value = (temp_word_file, "评分汇总表_20260101_120000.docx")
        resp = client.get("/api/export/word")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        cd = unquote(resp.headers.get("content-disposition", ""))
        assert "评分汇总表" in cd


# ---------------------------------------------------------------------------
# 3. export backup
# ---------------------------------------------------------------------------

def test_export_backup(client, db):
    """POST /api/export/backup — 备份成功（mock 服务层）"""
    with patch("routers.export.create_backup") as mock_backup:
        mock_backup.return_value = {
            "message": "备份成功",
            "backup_file": "backup_20260101_120000.zip",
            "path": "/tmp/backups/backup_20260101_120000.zip",
        }
        resp = client.post("/api/export/backup")
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "备份成功"
        assert "backup_file" in data


def test_export_backup_real_filesystem(client, db, tmp_path):
    """POST /api/export/backup — 真实文件系统路径，验证 shutil.copytree 不崩溃"""
    fake_data_dir = tmp_path / "data"
    fake_backups_dir = tmp_path / "backups"
    fake_data_dir.mkdir()
    fake_backups_dir.mkdir()

    # 模拟数据库文件
    fake_db = fake_data_dir / "teams.db"
    fake_db.write_text("fake db")

    # 模拟 works 目录（触发 shutil.copytree 的关键路径）
    fake_works = fake_data_dir / "works"
    fake_works.mkdir()
    (fake_works / "dummy.txt").write_text("dummy")

    with patch("services.export_service.DATA_DIR", fake_data_dir), \
         patch("services.export_service.BACKUPS_DIR", fake_backups_dir):
        resp = client.post("/api/export/backup")
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "备份成功"
        assert data["backup_file"].startswith("backup_")
        assert data["backup_file"].endswith(".zip")

        # 确认压缩包确实生成在临时备份目录
        backup_path = fake_backups_dir / data["backup_file"]
        assert backup_path.exists()


# ---------------------------------------------------------------------------
# 4. plagiarism detect
# ---------------------------------------------------------------------------

def test_plagiarism_detect_empty(client, db):
    """POST /api/plagiarism/detect — 空数据库"""
    with patch("routers.plagiarism.PlagiarismDetector") as MockDet:
        mock_report = MagicMock()
        mock_report.total_pairs = 0
        mock_report.suspicious_pairs = 0
        mock_report.high_risk = 0
        mock_report.medium_risk = 0
        mock_report.low_risk = 0
        mock_report.results = []
        mock_report.team_suspicion_scores = {}
        MockDet.return_value.detect_all.return_value = mock_report

        resp = client.post("/api/plagiarism/detect?language=Python")
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "抄袭检测完成"
        assert data["total_pairs"] == 0
        assert data["suspicious_pairs"] == 0
        assert data["results"] == []


def test_plagiarism_detect_with_results(client, db):
    """POST /api/plagiarism/detect — 有嫌疑结果"""
    with patch("routers.plagiarism.PlagiarismDetector") as MockDet:
        # 构造一个模拟的相似度结果
        sim_result = MagicMock()
        sim_result.team1_id = 1
        sim_result.team1_name = "队伍A"
        sim_result.team2_id = 2
        sim_result.team2_name = "队伍B"
        sim_result.token_similarity = 0.85
        sim_result.structure_similarity = 0.72
        sim_result.overall_similarity = 0.78
        sim_result.suspicion_level = "high"
        sim_result.is_suspicious = True

        mock_report = MagicMock()
        mock_report.total_pairs = 1
        mock_report.suspicious_pairs = 1
        mock_report.high_risk = 1
        mock_report.medium_risk = 0
        mock_report.low_risk = 0
        mock_report.results = [sim_result]
        mock_report.team_suspicion_scores = {1: 0.78, 2: 0.78}
        MockDet.return_value.detect_all.return_value = mock_report

        resp = client.post("/api/plagiarism/detect?language=Python")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_pairs"] == 1
        assert data["suspicious_pairs"] == 1
        assert data["high_risk"] == 1
        assert len(data["results"]) == 1
        r = data["results"][0]
        assert r["team1"]["name"] == "队伍A"
        assert r["team2"]["name"] == "队伍B"
        assert r["overall_similarity"] == 0.78
        assert r["suspicion_level"] == "high"
        assert data["team_suspicion_scores"]["1"] == 0.78


# ---------------------------------------------------------------------------
# 5. plagiarism suspicious teams
# ---------------------------------------------------------------------------

def test_plagiarism_suspicious_teams_empty(client, db):
    """GET /api/plagiarism/suspicious — 空列表"""
    with patch("routers.plagiarism.PlagiarismDetector") as MockDet:
        MockDet.return_value.get_suspicious_teams.return_value = []
        resp = client.get("/api/plagiarism/suspicious")
        assert resp.status_code == 200
        assert resp.json()["teams"] == []


def test_plagiarism_suspicious_teams_with_data(client, db):
    """GET /api/plagiarism/suspicious — 有嫌疑队伍"""
    with patch("routers.plagiarism.PlagiarismDetector") as MockDet:
        MockDet.return_value.get_suspicious_teams.return_value = [
            {"team_id": 1, "team_name": "队伍A", "suspicion_score": 0.85},
            {"team_id": 2, "team_name": "队伍B", "suspicion_score": 0.72},
        ]
        resp = client.get("/api/plagiarism/suspicious")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["teams"]) == 2
        assert data["teams"][0]["team_name"] == "队伍A"


# ---------------------------------------------------------------------------
# 6. plagiarism clear-flags
# ---------------------------------------------------------------------------

def test_plagiarism_clear_flags(client, db):
    """POST /api/plagiarism/clear-flags — 清除抄袭标记"""
    # 创建 2 支带标记的队伍
    t1 = Team(team_code="F001", short_code="F01", team_name="标记队1", group_type="小学组",
              school="测试小学", district="雁塔区", teacher="张老师", teacher_name="张老师",
              status="confirmed")
    t2 = Team(team_code="F002", short_code="F02", team_name="标记队2", group_type="初中组",
              school="测试初中", district="碑林区", teacher="李老师", teacher_name="李老师",
              status="confirmed")
    db.add_all([t1, t2])
    db.commit()
    db.refresh(t1)
    db.refresh(t2)

    w1 = Work(team_id=t1.id, original_filename="f1.zip", plagiarism_flag=True, flag_reason="高度相似")
    w2 = Work(team_id=t2.id, original_filename="f2.zip", plagiarism_flag=True, flag_reason="代码雷同")
    # w3 关联到另一支无标记队伍（work.team_id 有 UNIQUE 约束）
    t3 = Team(team_code="F003", short_code="F03", team_name="无标记队", group_type="小学组",
              school="测试小学", district="雁塔区", teacher="王老师", teacher_name="王老师",
              status="confirmed")
    db.add(t3)
    db.commit()
    db.refresh(t3)
    w3 = Work(team_id=t3.id, original_filename="f3.zip", plagiarism_flag=False)
    db.add_all([w1, w2, w3])
    db.commit()

    resp = client.post("/api/plagiarism/clear-flags")
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "已清除 2 个抄袭标记"

    # 验证标记已清除
    works = db.query(Work).filter(Work.plagiarism_flag == True).all()
    assert len(works) == 0
    # flag_reason 也应被清空
    w1_refreshed = db.query(Work).filter(Work.id == w1.id).first()
    assert w1_refreshed.flag_reason is None


def test_plagiarism_clear_flags_empty(client, db):
    """POST /api/plagiarism/clear-flags — 无标记可清除"""
    resp = client.post("/api/plagiarism/clear-flags")
    assert resp.status_code == 200
    assert resp.json()["message"] == "已清除 0 个抄袭标记"
