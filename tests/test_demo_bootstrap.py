import hashlib
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from scripts.init_demo import DemoBootstrapError, _SyntheticRegistration, bootstrap_demo


COMMENT_FIELDS = (
    "theme_comment",
    "presentation_comment",
    "process_comment",
    "ai_literacy_comment",
    "overall_comment",
)

EXPECTED_SAMPLE_ARCHIVES = {
    "小学组+001+校园节水小管家.zip",
    "小学组+002+古诗词节奏训练营.zip",
    "初中组+001+城市无障碍出行助手.zip",
}


def _tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_bootstrap_creates_12_legacy_cases_and_phase11_state(tmp_path):
    result = bootstrap_demo(tmp_path)

    assert result["status"] == "created"
    assert result["case_count"] == 12
    with sqlite3.connect(tmp_path / "teams.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM machine_scores").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM human_scores").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM final_scores").fetchone()[0] == 12
        codes = [row[0] for row in conn.execute("SELECT team_code FROM teams ORDER BY team_code")]
        team_names = [row[0] for row in conn.execute("SELECT team_name FROM teams ORDER BY team_code")]
        comments = conn.execute(
            f"SELECT {', '.join(COMMENT_FIELDS)} FROM machine_scores ORDER BY team_id"
        ).fetchall()
    assert codes == [f"DEMO-M-{i:03d}" for i in range(1, 7)] + [f"DEMO-P-{i:03d}" for i in range(1, 7)]
    assert all(name and not name.startswith("演示作品") for name in team_names)
    assert all(all(value and value.strip() for value in row) for row in comments)

    sample_root = tmp_path / "sample-works"
    archives = {path.name for path in sample_root.glob("*.zip")}
    assert archives == EXPECTED_SAMPLE_ARCHIVES
    for archive_name in EXPECTED_SAMPLE_ARCHIVES:
        with zipfile.ZipFile(sample_root / archive_name) as archive:
            names = set(archive.namelist())
            assert "README.md" in names
            assert "ai-use-record.txt" in names
            assert "preview.svg" in names
            assert any(name.endswith(".py") for name in names)
            assert all(not name.startswith("/") and ".." not in Path(name).parts for name in names)
            for name in names:
                assert archive.read(name).decode("utf-8").strip()

    state = json.loads((tmp_path / "phase11" / "demo_state.json").read_text(encoding="utf-8"))
    assert state["source"] == "offline_synthetic"
    assert len(state["items"]) == 12
    assert {item["review_status"] for item in state["items"]} >= {"pending", "adopted", "locked"}
    assert sum(item["must_review"] for item in state["items"]) == 4


def test_bootstrap_is_idempotent_and_does_not_rewrite_demo_runtime(tmp_path):
    bootstrap_demo(tmp_path)
    before = _tree_hash(tmp_path)

    result = bootstrap_demo(tmp_path)

    assert result["status"] == "verified"
    assert _tree_hash(tmp_path) == before


def test_bootstrap_safely_upgrades_existing_runtime_with_enrichment(tmp_path):
    bootstrap_demo(tmp_path)
    with sqlite3.connect(tmp_path / "teams.db") as conn:
        team_id = conn.execute(
            "SELECT id FROM teams WHERE team_code = ?", ("DEMO-P-001",)
        ).fetchone()[0]
        conn.execute(
            "UPDATE teams SET team_name = ? WHERE id = ?",
            ("演示作品01", team_id),
        )
        conn.execute(
            "UPDATE machine_scores SET theme_comment = ? WHERE team_id = ?",
            ("", team_id),
        )
        conn.commit()

    result = bootstrap_demo(tmp_path)

    assert result["status"] == "verified"
    with sqlite3.connect(tmp_path / "teams.db") as conn:
        team_name = conn.execute(
            "SELECT team_name FROM teams WHERE id = ?", (team_id,)
        ).fetchone()[0]
        theme_comment = conn.execute(
            "SELECT theme_comment FROM machine_scores WHERE team_id = ?", (team_id,)
        ).fetchone()[0]
    assert team_name == "校园节水小管家"
    assert theme_comment and "校园" in theme_comment

    upgraded_hash = _tree_hash(tmp_path)
    assert bootstrap_demo(tmp_path)["status"] == "verified"
    assert _tree_hash(tmp_path) == upgraded_hash


def test_bootstrap_allows_system_defaults_added_after_first_start(tmp_path):
    bootstrap_demo(tmp_path)
    with sqlite3.connect(tmp_path / "teams.db") as conn:
        conn.execute(
            "INSERT INTO task_books (name, group_type, content_md, is_active) VALUES (?, ?, ?, ?)",
            ("演示任务书", "通用", "仅用于测试", 1),
        )
        conn.execute(
            "INSERT INTO competitions (name, scoring_mode, is_active) VALUES (?, ?, ?)",
            ("演示赛事", "mixed", 0),
        )
        conn.execute(
            "INSERT INTO scoring_forms (name, dimensions, is_default, is_active) VALUES (?, ?, ?, ?)",
            ("演示评审表", "[]", 1, 1),
        )
        conn.commit()

    result = bootstrap_demo(tmp_path)

    assert result["status"] == "verified"


def test_bootstrap_refuses_existing_non_demo_data(tmp_path):
    bootstrap_demo(tmp_path)
    with sqlite3.connect(tmp_path / "teams.db") as conn:
        conn.execute("INSERT INTO teams (team_code, short_code, team_name, group_type, source) VALUES (?, ?, ?, ?, ?)", ("REAL-001", "REAL-001", "不应被覆盖", "小学组", "excel"))
        conn.commit()

    with pytest.raises(DemoBootstrapError, match="non-demo|partial"):
        bootstrap_demo(tmp_path)

    with sqlite3.connect(tmp_path / "teams.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM teams WHERE team_code = 'REAL-001'").fetchone()[0] == 1


def test_phase11_formal_stores_and_api_read_synthetic_cases(tmp_path):
    result = bootstrap_demo(tmp_path)
    extension = json.loads((tmp_path / "demo_phase11_manifest.json").read_text(encoding="utf-8"))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import pipeline_tasks, scoring_pipeline
    from services.pipeline_task_manager import PipelineTaskManager
    from services.pipeline_task_store import PipelineTaskStore
    from services.review_case_store import ReviewCaseStore
    from services.score_attempt_store import ScoreAttemptStore
    from services.scoring_pipeline_api_container import ScoringPipelineApiContainer

    task_store = PipelineTaskStore(tmp_path / "pipeline-runtime")
    attempt_store = ScoreAttemptStore(tmp_path / "pipeline-runtime" / "score-attempts")
    review_store = ReviewCaseStore(tmp_path / "pipeline-runtime" / "reviews", attempt_store=attempt_store)
    manager = PipelineTaskManager(
        task_store,
        _SyntheticRegistration(json.loads((Path(__file__).resolve().parents[1] / "demo" / "cases.json").read_text(encoding="utf-8"))),
        clock=lambda: __import__("scripts.init_demo", fromlist=["FIXED_TIME_UTC"]).FIXED_TIME_UTC,
        uuid_factory=lambda: "00000000-0000-4000-8000-000000000001",
    )
    container = ScoringPipelineApiContainer(
        task_manager=manager,
        review_store=review_store,
        attempt_store=attempt_store,
        test_runtime=True,
    )
    app = FastAPI()
    app.include_router(pipeline_tasks.router)
    app.include_router(scoring_pipeline.router)
    app.dependency_overrides[pipeline_tasks.get_pipeline_task_manager] = lambda: manager
    app.dependency_overrides[scoring_pipeline.get_scoring_pipeline_api_container] = lambda: container

    scoring_task = task_store.load_task(extension["scoring_task_id"])
    assert scoring_task is not None
    items = task_store.list_items(scoring_task.task_id)
    assert result["phase11_status"] == "created"
    assert len(items) == 12
    assert {item.status for item in items} == {"completed", "manual_review", "failed"}
    assert len(review_store.list_review_cases(scoring_task.task_id)) == 2
    assert sum(len(attempt_store.list_attempts(scoring_task.task_id, item.item_id)) for item in items) == 12
    locked_item = next(item for item in items if item.item_id == next(case.item_id for case in review_store.list_review_cases(scoring_task.task_id) if case.reason_codes == ["LOW_CONFIDENCE"]))
    assert review_store.get_active_adoption(scoring_task.task_id, locked_item.item_id) is not None
    assert review_store.get_active_final_lock(scoring_task.task_id, locked_item.item_id) is not None

    with TestClient(app) as client:
        task_response = client.get("/api/pipeline-tasks")
        assert task_response.status_code == 200
        assert {item["task_id"] for item in task_response.json()["items"]} == {extension["source_task_id"], extension["scoring_task_id"]}
        item_response = client.get(f"/api/pipeline-tasks/{scoring_task.task_id}/items")
        assert item_response.status_code == 200
        assert item_response.json()["total"] == 12
        scoring_response = client.get(f"/api/scoring-pipeline/tasks/{scoring_task.task_id}")
        assert scoring_response.status_code == 200
        review_response = client.get(f"/api/scoring-pipeline/tasks/{scoring_task.task_id}/review-cases")
        assert review_response.status_code == 200
        assert review_response.json()["total"] == 2


def test_phase11_formal_repeat_preserves_store_bytes_and_success_results(tmp_path):
    bootstrap_demo(tmp_path)
    before = _tree_hash(tmp_path / "pipeline-runtime")
    bootstrap_demo(tmp_path)
    after = _tree_hash(tmp_path / "pipeline-runtime")

    from services.pipeline_task_store import PipelineTaskStore
    from services.score_attempt_store import ScoreAttemptStore

    extension = json.loads((tmp_path / "demo_phase11_manifest.json").read_text(encoding="utf-8"))
    task_store = PipelineTaskStore(tmp_path / "pipeline-runtime")
    attempt_store = ScoreAttemptStore(tmp_path / "pipeline-runtime" / "score-attempts")
    scoring_items = task_store.list_items(extension["scoring_task_id"])
    succeeded = [attempt_store.list_attempts(extension["scoring_task_id"], item.item_id)[0] for item in scoring_items if item.status == "completed"]
    assert before == after
    assert len(succeeded) == 8
    assert all(attempt.status == "succeeded" and attempt.result_snapshot_ref for attempt in succeeded)
