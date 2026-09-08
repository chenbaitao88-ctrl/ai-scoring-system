"""
scores.py 路由接口级 smoke test
覆盖：结果查询、人工评分、评分统计、采用评分
"""
import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from database import Team, Work, MachineScore


@pytest.fixture
def seed_team_with_score(db):
    """辅助 fixture：创建包含机器评分的队伍"""
    team = Team(
        team_code="R001",
        team_name="路由测试队",
        short_code="R001",
        group_type="小学组",
        school="测试学校",
    )
    db.add(team)
    db.commit()

    work = Work(
        team_id=team.id,
        code_language="Python",
        code_line_count=200,
        source_files=[{"ext": ".py", "name": "main.py"}],
        is_parsed=True,
        original_filename="r001.zip",
    )
    db.add(work)
    db.commit()

    ms = MachineScore(
        team_id=team.id,
        model_name="test-model",
        theme_score=15,
        presentation_score=22,
        process_score=20,
        ai_literacy_score=15,
        total_score=72,
        is_completed=True,
        is_adopted=True,
        calibrated_score=75.0,
        confidence="HIGH",
        flags=[],
    )
    db.add(ms)
    db.commit()
    return team, ms


def test_get_score_result_existing(client, seed_team_with_score):
    """GET /api/scores/result/{team_id} — 有评分记录时返回完整结构"""
    team, ms = seed_team_with_score
    resp = client.get(f"/api/scores/result/{team.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["score"] is not None
    assert data["score"]["total_score"] == 72
    assert data["score"]["model_name"] == "test-model"
    assert len(data["all_scores"]) == 1


def test_teams_for_scoring_normalizes_legacy_string_flags(client, db, seed_team_with_score):
    """GET /api/scores/teams-for-scoring always exposes flags as an array."""
    _, machine_score = seed_team_with_score
    machine_score.flags = json.dumps([
        {"code": "DEMO_REVIEW", "level": "MUST_REVIEW", "msg": "demo"}
    ])
    db.commit()

    resp = client.get("/api/scores/teams-for-scoring")

    assert resp.status_code == 200
    flags = resp.json()["teams"][0]["flags"]
    assert isinstance(flags, list)
    assert flags[0]["code"] == "DEMO_REVIEW"


def test_get_score_result_empty(client, db):
    """GET /api/scores/result/{team_id} — 无评分记录时返回友好提示"""
    team = Team(
        team_code="R002", team_name="空队", short_code="R002", group_type="小学组"
    )
    db.add(team)
    db.commit()

    resp = client.get(f"/api/scores/result/{team.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "该队伍暂未评分"
    assert data["score"] is None
    assert data["all_scores"] == []


def test_submit_human_score_success_and_validation(client, db):
    """POST /api/scores/human — 成功提交 + 参数越界校验"""
    team = Team(
        team_code="R003", team_name="人工队", short_code="R003", group_type="小学组"
    )
    db.add(team)
    db.commit()

    payload = {
        "team_id": team.id,
        "judge_name": "评委A",
        "theme_score": 18,
        "presentation_score": 25,
        "process_score": 20,
        "ai_literacy_score": 15,
    }
    resp = client.post("/api/scores/human", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "评分提交成功"
    assert data["old_total"] == 78

    # 参数越界：theme_score > 20，应返回 400
    bad = {**payload, "theme_score": 25}
    resp = client.post("/api/scores/human", json=bad)
    assert resp.status_code == 400
    assert "范围" in resp.json()["detail"]


def test_get_scoring_stats(client, db, seed_team_with_score):
    """GET /api/scores/stats — 统计接口返回预期结构"""
    team2 = Team(
        team_code="R004", team_name="无评分队", short_code="R004", group_type="初中组"
    )
    db.add(team2)
    db.commit()

    resp = client.get("/api/scores/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_teams"] == 2
    assert data["scored_teams"] == 1
    assert data["avg_scores"]["total"] == 72.0


def test_adopt_score(client, db, seed_team_with_score):
    """POST /api/scores/result/{team_id}/adopt/{score_id} — 切换采纳版本"""
    team, ms = seed_team_with_score

    ms2 = MachineScore(
        team_id=team.id,
        model_name="v2-model",
        theme_score=10,
        presentation_score=20,
        process_score=20,
        ai_literacy_score=10,
        total_score=60,
        is_completed=True,
        is_adopted=False,
    )
    db.add(ms2)
    db.commit()

    resp = client.post(f"/api/scores/result/{team.id}/adopt/{ms2.id}")
    assert resp.status_code == 200
    assert resp.json()["message"] == "已采用该评分"

    db.refresh(ms)
    db.refresh(ms2)
    assert ms.is_adopted is False
    assert ms2.is_adopted is True
