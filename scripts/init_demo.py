"""Create and verify the isolated synthetic demo dataset.

The script never imports the application's .env-backed configuration. It uses
the explicit SCORING_DATA_DIR environment variable, or the repository-local
runtime/demo/data default, and writes only below that directory.
"""
from __future__ import annotations

import json
import hashlib
import io
import os
import sqlite3
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "demo" / "cases.json"
ENRICHMENT_PATH = REPO_ROOT / "demo" / "enrichment.json"
SAMPLE_WORKS_ROOT = REPO_ROOT / "demo" / "sample-works"
DEFAULT_DATA_ROOT = REPO_ROOT / "runtime" / "demo" / "data"
SCHEMA_VERSION = "synthetic-demo/v1"
FIXED_TIME = datetime(2026, 8, 31, 0, 0, 0)
FIXED_TIME_UTC = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)
PHASE11_MANIFEST_NAME = "demo_phase11_manifest.json"
PHASE11_RUNTIME_DIR = "pipeline-runtime"
DEMO_UUID_NAMESPACE = uuid.UUID("2f2e95f5-bb1e-4f61-8ac1-76cb0bd7f3a0")
LEGACY_TABLES = {"teams", "works", "machine_scores", "human_scores", "final_scores"}
SYSTEM_INITIALIZATION_TABLES = {
    "task_books",
    "competitions",
    "scoring_forms",
    "judge_group_configs",
}
_SYNTHETIC_DEMO_IMPORTED_DATABASE = False


class DemoBootstrapError(RuntimeError):
    """Raised when the target is not a clean or complete demo runtime."""


def _stable_uuid(label: str) -> str:
    return str(uuid.uuid5(DEMO_UUID_NAMESPACE, f"synthetic-demo/{label}"))


def _stable_hash(label: str) -> str:
    return hashlib.sha256(f"synthetic-demo/{label}".encode("utf-8")).hexdigest()


def _load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    cases = manifest.get("cases")
    if manifest.get("schema_version") != SCHEMA_VERSION or not isinstance(cases, list):
        raise DemoBootstrapError("invalid synthetic demo manifest")
    if len(cases) != 12:
        raise DemoBootstrapError("synthetic demo must contain exactly 12 cases")
    codes = [case.get("team_code") for case in cases]
    expected_codes = [f"DEMO-P-{i:03d}" for i in range(1, 7)] + [f"DEMO-M-{i:03d}" for i in range(1, 7)]
    if codes != expected_codes or len(set(codes)) != len(codes):
        raise DemoBootstrapError("synthetic demo identifiers are not deterministic")
    return manifest


def _load_enrichment(manifest: dict[str, Any]) -> dict[str, Any]:
    enrichment = json.loads(ENRICHMENT_PATH.read_text(encoding="utf-8"))
    if enrichment.get("schema_version") != "synthetic-demo-enrichment/v1":
        raise DemoBootstrapError("invalid synthetic demo enrichment")
    if enrichment.get("dataset_id") != manifest.get("dataset_id"):
        raise DemoBootstrapError("synthetic demo enrichment dataset mismatch")
    cases = enrichment.get("cases")
    expected_codes = {case["team_code"] for case in manifest["cases"]}
    if not isinstance(cases, dict) or set(cases) != expected_codes:
        raise DemoBootstrapError("synthetic demo enrichment cases mismatch")
    required_comments = {
        "theme_comment",
        "presentation_comment",
        "process_comment",
        "ai_literacy_comment",
        "overall_comment",
    }
    for code, case in cases.items():
        machine = case.get("machine")
        if (
            not isinstance(case.get("team_name"), str)
            or not case["team_name"].strip()
            or not isinstance(machine, dict)
            or not required_comments.issubset(machine)
            or any(not isinstance(machine[field], str) or not machine[field].strip() for field in required_comments)
        ):
            raise DemoBootstrapError(f"incomplete synthetic demo enrichment: {code}")
    return enrichment


def _sample_archive_bytes(source_dir: Path) -> bytes:
    if not source_dir.is_dir():
        raise DemoBootstrapError(f"synthetic sample source missing: {source_dir.name}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source_dir).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 9, 1, 8, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def _materialize_sample_work(data_root: Path, code: str, config: dict[str, Any]) -> Path:
    source_name = config.get("source_dir")
    archive_name = config.get("archive_name")
    if not source_name or Path(source_name).name != source_name:
        raise DemoBootstrapError(f"invalid synthetic sample source: {code}")
    if not archive_name or Path(archive_name).name != archive_name or not archive_name.endswith(".zip"):
        raise DemoBootstrapError(f"invalid synthetic sample archive: {code}")
    payload = _sample_archive_bytes(SAMPLE_WORKS_ROOT / source_name)
    target = data_root / "sample-works" / archive_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != payload:
            raise DemoBootstrapError(f"existing synthetic sample differs: {code}")
        return target
    with target.open("xb") as handle:
        handle.write(payload)
    return target


def _apply_enrichment(session: Any, data_root: Path, manifest: dict[str, Any]) -> None:
    from database import MachineScore, Team, Work

    session.flush()
    enrichment = _load_enrichment(manifest)
    for code, values in enrichment["cases"].items():
        team = session.query(Team).filter_by(team_code=code).one_or_none()
        if team is None or team.source != "demo":
            raise DemoBootstrapError(f"synthetic enrichment target mismatch: {code}")
        team.team_name = values["team_name"]
        machine = session.query(MachineScore).filter_by(team_id=team.id).one_or_none()
        work = session.query(Work).filter_by(team_id=team.id).one_or_none()
        if machine is None or work is None:
            raise DemoBootstrapError(f"synthetic enrichment record missing: {code}")
        for field, value in values["machine"].items():
            if field not in {
                "theme_comment",
                "presentation_comment",
                "process_comment",
                "ai_literacy_comment",
                "overall_comment",
                "code_quality_details",
                "aigc_analysis_details",
                "feature_detection_details",
                "defense_questions",
            }:
                raise DemoBootstrapError(f"unsupported synthetic enrichment field: {field}")
            setattr(machine, field, value)
        sample_work = values.get("sample_work")
        if sample_work:
            work.file_path = str(_materialize_sample_work(data_root, code, sample_work))


def _data_root() -> Path:
    value = os.environ.get("SCORING_DATA_DIR")
    root = Path(value).expanduser() if value else DEFAULT_DATA_ROOT
    return root.resolve()


def _install_import_safe_config(data_root: Path) -> None:
    """Allow ORM model import without loading the repository .env file."""
    if "database" in sys.modules or "config" in sys.modules:
        return
    safe_config = ModuleType("config")
    safe_config.DATA_DIR = data_root
    safe_config.WORKS_DIR = data_root / "works"
    safe_config.BACKUPS_DIR = data_root / "backups"
    safe_config.DATABASE_URL = f"sqlite:///{(data_root / 'teams.db').as_posix()}"
    safe_config.MACHINE_SCORE_WEIGHT = 0.6
    safe_config.HUMAN_SCORE_WEIGHT = 0.4
    safe_config.AVAILABLE_MODELS = {}
    safe_config.LLM_API_KEY = ""
    safe_config.LLM_API_URL = ""
    safe_config.LLM_MODEL = ""
    safe_config.get_active_competition = lambda db_session=None: None
    safe_config.get_scoring_dimensions = lambda: {
        "theme": {"name": "主题立意", "max_score": 20},
        "presentation": {"name": "产品表现力", "max_score": 30},
        "process": {"name": "过程完整性", "max_score": 30},
        "ai_literacy": {"name": "人工智能素养", "max_score": 20},
    }
    safe_config.get_naming_pattern = lambda: r"^(.*)\+(.+)\.zip$"
    safe_config.get_judge_groups = lambda: 4
    safe_config.get_naming_example = lambda: "小学组+演示学生01.zip"
    sys.modules["config"] = safe_config


def _load_orm(data_root: Path):
    global _SYNTHETIC_DEMO_IMPORTED_DATABASE
    fresh_import = "database" not in sys.modules and "config" not in sys.modules
    _install_import_safe_config(data_root)
    backend_path = str((REPO_ROOT / "backend").resolve())
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)
    import database
    if fresh_import:
        _SYNTHETIC_DEMO_IMPORTED_DATABASE = True
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{(data_root / 'teams.db').as_posix()}", connect_args={"check_same_thread": False})
    return database, engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _existing_db_state(
    db_path: Path, *, allow_system_initialization: bool = False
) -> tuple[bool, set[str]]:
    if not db_path.exists():
        return False, set()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            nonempty = set()
            team_codes: set[str] = set()
            for table in tables - {"sqlite_sequence"}:
                count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if count:
                    nonempty.add(table)
                if table == "teams" and count:
                    team_codes = {row[0] for row in conn.execute("SELECT team_code FROM teams")}
            allowed_tables = set(LEGACY_TABLES)
            if allow_system_initialization:
                allowed_tables.update(SYSTEM_INITIALIZATION_TABLES)
            if nonempty and not nonempty.issubset(allowed_tables):
                raise DemoBootstrapError("target database contains non-demo tables with data")
            return bool(nonempty), team_codes
    except sqlite3.DatabaseError as exc:
        raise DemoBootstrapError("target database is not a readable SQLite database") from exc


def _assert_target_shape(data_root: Path, manifest: dict[str, Any]) -> tuple[bool, Path, Path, Path]:
    db_path = data_root / "teams.db"
    manifest_path = data_root / "demo_manifest.json"
    phase_path = data_root / "phase11" / "demo_state.json"
    existing_markers = [path.exists() for path in (db_path, manifest_path, phase_path)]
    if any(existing_markers) and not all(existing_markers):
        raise DemoBootstrapError("demo runtime is partially initialized; refusing repair")
    initialized = all(existing_markers)
    has_rows, team_codes = _existing_db_state(
        db_path, allow_system_initialization=initialized
    )
    expected_codes = {case["team_code"] for case in manifest["cases"]}
    if has_rows and team_codes != expected_codes:
        raise DemoBootstrapError("target database contains non-demo or partial team records")
    return initialized, db_path, manifest_path, phase_path


def _score_fields(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(obj, field) for field in fields}


def _verify_legacy(session: Any, manifest: dict[str, Any]) -> None:
    from database import FinalScore, HumanScore, MachineScore, Team, Work

    cases = manifest["cases"]
    enrichment = _load_enrichment(manifest)["cases"]
    if session.query(Team).count() != 12 or session.query(Work).count() != 12:
        raise DemoBootstrapError("demo legacy record count mismatch")
    for case in cases:
        team = session.query(Team).filter_by(team_code=case["team_code"]).one_or_none()
        enriched_case = enrichment[case["team_code"]]
        if (
            team is None
            or team.short_code != case["short_code"]
            or team.team_name != enriched_case["team_name"]
            or team.school != "示例学校"
        ):
            raise DemoBootstrapError(f"demo team mismatch: {case['team_code']}")
        expected_members = [{"name": name} for name in case["member_names"]]
        if team.members != expected_members or team.status != "confirmed" or team.source != "demo":
            raise DemoBootstrapError(f"demo team metadata mismatch: {case['team_code']}")
        work = session.query(Work).filter_by(team_id=team.id).one_or_none()
        if work is None:
            raise DemoBootstrapError(f"demo work missing: {case['team_code']}")
        work_expected = {"original_filename": case["work"]["filename"]}
        work_expected.update({key: value for key, value in case["work"].items() if key != "filename"})
        if _score_fields(work, tuple(work_expected)) != work_expected:
            raise DemoBootstrapError(f"demo work mismatch: {case['team_code']}")
        machine = session.query(MachineScore).filter_by(team_id=team.id).one_or_none()
        human = session.query(HumanScore).filter_by(team_id=team.id).one_or_none()
        final = session.query(FinalScore).filter_by(team_id=team.id).one_or_none()
        if not machine or not human or not final:
            raise DemoBootstrapError(f"demo score record missing: {case['team_code']}")
        machine_expected = dict(case["machine"])
        machine_expected.update(enriched_case["machine"])
        machine_fields = tuple(machine_expected)
        machine_actual = _score_fields(machine, machine_fields)
        if isinstance(machine_actual.get("flags"), str):
            machine_actual["flags"] = json.loads(machine_actual["flags"])
        if machine_actual != machine_expected:
            raise DemoBootstrapError(f"demo machine score mismatch: {case['team_code']}")
        human_fields = tuple(case["human"].keys())
        if _score_fields(human, human_fields) != case["human"]:
            raise DemoBootstrapError(f"demo human score mismatch: {case['team_code']}")
        final_expected = {"machine_score": case["machine"]["total_score"], "machine_weight": 0.6, "human_score_avg": case["human"]["total_score"], "human_judge_count": 1, "human_weight": 0.4, "final_score": case["final_score"], "ranking": case["ranking"], "composite_score": case["final_score"], "scoring_mode": "mixed"}
        if _score_fields(final, tuple(final_expected)) != final_expected:
            raise DemoBootstrapError(f"demo final score mismatch: {case['team_code']}")


def _phase_state(manifest: dict[str, Any]) -> dict[str, Any]:
    cases = manifest["cases"]
    items = []
    for case in cases:
        status = case["demo_status"]
        items.append({"item_id": case["team_code"], "source_item_id": case["team_code"], "team_code": case["team_code"], "status": status["pipeline_status"], "current_stage": status["current_stage"], "evidence_level": status["evidence_level"], "must_review": status["must_review"], "review_status": status["review_status"], "manual_final_lock": status["manual_final_lock"], "authoritative_result_status": status["authoritative_result_status"], "machine_score": case["machine"]["total_score"], "human_score": case["human"]["total_score"], "final_score": case["final_score"], "flags": case["machine"]["flags"]})
    return {"schema_version": "phase11-demo/v1", "dataset_id": manifest["dataset_id"], "source": "offline_synthetic", "generated_at": "2026-08-31T00:00:00Z", "task": {"task_id": "DEMO-TASK-20260831", "batch_id": "DEMO-BATCH-20260831", "status": "completed_with_errors", "current_stage": "review", "total_items": 12, "manual_review_items": 6, "blocking_items": 4, "description": "Phase 11 页面专用的合成任务状态，禁止用于真实评分或导出。"}, "items": items}


def _write_json_once(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != data:
            raise DemoBootstrapError(f"existing demo file differs: {path.name}")
        return
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(data)


def _create_legacy(session: Any, manifest: dict[str, Any]) -> None:
    from database import FinalScore, HumanScore, MachineScore, Team, Work

    for case in manifest["cases"]:
        team = Team(team_code=case["team_code"], short_code=case["short_code"], team_name=case["team_name"], group_type=case["group_type"], school="示例学校", district="示例区", teacher="演示教师", teacher_name="演示教师", members=[{"name": name} for name in case["member_names"]], judge_group=(int(case["short_code"][-3:]) - 1) % 4 + 1, status="confirmed", source="demo", created_at=FIXED_TIME, updated_at=FIXED_TIME)
        session.add(team)
        session.flush()
        work_data = case["work"]
        session.add(Work(team_id=team.id, original_filename=work_data["filename"], file_path=f"demo/works/{case['team_code']}.zip", created_at=FIXED_TIME, updated_at=FIXED_TIME, **{key: value for key, value in work_data.items() if key != "filename"}))
        machine_data = case["machine"]
        machine_payload = dict(machine_data)
        machine_payload["flags"] = list(machine_data["flags"])
        session.add(MachineScore(team_id=team.id, model_name="synthetic-demo-model", is_adopted=case["demo_status"]["review_status"] == "adopted", scoring_session="DEMO-SESSION-20260831", created_at=FIXED_TIME, is_completed=True, **machine_payload))
        human_data = case["human"]
        session.add(HumanScore(team_id=team.id, created_at=FIXED_TIME, updated_at=FIXED_TIME, **human_data))
        session.add(FinalScore(team_id=team.id, machine_score=machine_data["total_score"], machine_weight=0.6, human_score_avg=human_data["total_score"], human_judge_count=1, human_weight=0.4, final_score=case["final_score"], ranking=case["ranking"], composite_score=case["final_score"], scoring_mode="mixed", created_at=FIXED_TIME, updated_at=FIXED_TIME))


class _SyntheticRegistration:
    """Read-only registration facts used by the formal pipeline creators."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        from models.evidence_sidecar import EvidencePackageRecord, ValidationResult

        self.records: dict[str, Any] = {}
        self.validations: dict[tuple[str, int, str], Any] = {}
        for case in manifest["cases"]:
            code = case["team_code"]
            package_id = _stable_uuid(f"package/{code}")
            record_id = _stable_uuid(f"record/{code}")
            validation_id = _stable_uuid(f"validation/{code}")
            submission_id = _stable_uuid(f"submission/{code}")
            manifest_sha256 = _stable_hash(f"manifest/{code}")
            now = FIXED_TIME_UTC
            record = EvidencePackageRecord(
                record_id=record_id,
                package_id=package_id,
                package_revision=1,
                batch_id="DEMO-BATCH-20260831",
                submission_id=submission_id,
                evidence_version="synthetic-evidence-v1",
                manifest_sha256=manifest_sha256,
                privacy_policy_version="synthetic-privacy-v1",
                publication_status="ready",
                registration_status="registered",
                latest_validation_id=validation_id,
                latest_validation_status="passed",
                source_package_ref=f"demo/{code}.zip",
                registered_at=now,
                registered_by="synthetic-demo",
                created_at=now,
                updated_at=now,
                revision=1,
                record_sha256=_stable_hash(f"record/{code}"),
            )
            validation = ValidationResult(
                validation_id=validation_id,
                record_id=record_id,
                package_id=package_id,
                package_revision=1,
                manifest_sha256=manifest_sha256,
                validator_name="synthetic-demo-validator",
                validator_version="synthetic-validator-v1",
                privacy_policy_version="synthetic-privacy-v1",
                mode="registration",
                status="passed",
                started_at=now,
                completed_at=now,
                duration_ms=0,
                model_input_allowed=True,
                registration_allowed=True,
                validated_file_count=1,
                declared_file_count=1,
                unregistered_file_count=0,
                result_sha256=_stable_hash(f"validation/{code}"),
                evidence_level=case["demo_status"]["evidence_level"],
            )
            self.records[(package_id, 1)] = record
            self.validations[(package_id, 1, validation_id)] = validation

    def read_record_strict(self, package_id: str, package_revision: int) -> Any:
        return self.records.get((package_id, package_revision))

    def read_validation_strict(self, package_id: str, package_revision: int, validation_id: str) -> Any:
        return self.validations.get((package_id, package_revision, validation_id))


class _SyntheticProfileLookup:
    def __init__(self) -> None:
        from models.score_attempt import ScoringInputProfile

        self.profile = ScoringInputProfile(
            profile_version="synthetic-profile-v1",
            scoring_mode="mixed",
            input_modalities=["text"],
            scoring_policy_version="synthetic-policy-v1",
            rubric_version="synthetic-rubric-v1",
            prompt_version="synthetic-prompt-v1",
            response_schema_version="synthetic-response-v1",
            temperature=0.0,
            seed=0,
            max_output_tokens=256,
            system_message_required=True,
            structured_json_required=True,
            max_images=None,
            image_formats=None,
        )

    def get_input_profile(self, profile_version: str) -> Any:
        return self.profile if profile_version == self.profile.profile_version else None


def _ordered_cases(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(manifest["cases"], key=lambda case: _stable_uuid(f"package/{case['team_code']}"))


def _formal_item_index(items: list[Any]) -> list[Any]:
    from models.pipeline_task import PipelineItemIndexEntry

    return [
        PipelineItemIndexEntry(
            item_id=item.item_id,
            task_type=item.task_type,
            source_item_id=item.source_item_id,
            package_id=item.package_id,
            package_revision=item.package_revision,
            status=item.status,
            current_stage=item.current_stage,
            input_fingerprint=item.input_fingerprint,
            item_revision=item.item_revision,
            evidence_level=item.evidence_level,
        )
        for item in sorted(items, key=lambda value: value.item_id)
    ]


def _phase11_stage_summaries(items: list[Any], *, evidence: bool) -> list[Any]:
    from models.pipeline_task import PipelineStageSummary

    zero = dict(total_items=0, pending_items=0, running_items=0, completed_items=0,
                failed_items=0, skipped_items=0, manual_review_items=0,
                blocking_error_codes=[], revision=1)
    if evidence:
        return [
            PipelineStageSummary(stage="import", status="completed", depends_on=[], started_at=FIXED_TIME_UTC,
                                 completed_at=FIXED_TIME_UTC, **{**zero, "total_items": len(items), "completed_items": len(items)}),
            PipelineStageSummary(stage="validate", status="completed", depends_on=["import"], started_at=FIXED_TIME_UTC,
                                 completed_at=FIXED_TIME_UTC, **{**zero, "total_items": len(items), "completed_items": len(items)}),
            PipelineStageSummary(stage="score", status="not_started", depends_on=["validate"], **zero),
            PipelineStageSummary(stage="review", status="not_started", depends_on=["validate", "score"], **zero),
            PipelineStageSummary(stage="export", status="not_started", depends_on=["review"], **zero),
        ]
    by_stage = {stage: [item for item in items if item.current_stage == stage] for stage in ("score", "review", "export")}
    summaries = [
        PipelineStageSummary(stage="import", status="not_started", depends_on=[], **zero),
        PipelineStageSummary(stage="validate", status="not_started", depends_on=["import"], **zero),
    ]
    for stage, depends_on in (("score", []), ("review", ["score"]), ("export", ["review"])):
        stage_items = by_stage[stage]
        counts = {name: sum(item.status == name for item in stage_items) for name in ("pending", "running", "completed", "failed", "skipped", "manual_review")}
        if stage == "score":
            status = "failed"
            blocking = ["DEMO_SYNTHETIC_SCORE_FAILED"]
        elif stage == "review":
            status = "pending"
            blocking = ["DEMO_MANUAL_REVIEW_REQUIRED"]
        else:
            status = "completed"
            blocking = []
        summaries.append(PipelineStageSummary(
            stage=stage,
            status=status,
            depends_on=depends_on,
            started_at=FIXED_TIME_UTC,
            completed_at=FIXED_TIME_UTC if status in ("failed", "completed") else None,
            total_items=len(stage_items),
            pending_items=counts["pending"],
            running_items=counts["running"],
            completed_items=counts["completed"],
            failed_items=counts["failed"],
            skipped_items=counts["skipped"],
            manual_review_items=counts["manual_review"],
            blocking_error_codes=blocking,
            revision=1,
        ))
    return summaries


def _build_formal_attempts(root: Path, manifest: dict[str, Any], scoring_task: Any, scoring_items: dict[str, Any], registration: _SyntheticRegistration) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    from models.score_attempt import (
        ActorRef, AttemptValidation, DimensionScore, Flag, ProviderError,
        ScoreAttempt, ScoreDimensionRef, ScoreResultSnapshot, ScoreScale, ValidationCheck,
    )
    from services.score_attempt_store import ScoreAttemptStore

    attempt_store = ScoreAttemptStore(root / PHASE11_RUNTIME_DIR / "score-attempts")
    actor = ActorRef(actor_type="system", actor_id=_stable_uuid("actor/system"))
    reviewer = ActorRef(actor_type="reviewer", actor_id=_stable_uuid("actor/reviewer"))
    provider_id = _stable_uuid("provider/synthetic")
    model_id = _stable_uuid("model/synthetic")
    attempts: dict[str, Any] = {}
    snapshots: dict[str, Any] = {}
    validations: dict[str, Any] = {}
    scale = ScoreScale(
        scoring_policy_version="synthetic-policy-v1",
        total_score_range_ref="synthetic-total-0-100",
        total_min=0,
        total_max=100,
        dimensions=[
            ScoreDimensionRef(dimension_code="theme", score_range_ref="synthetic-theme-0-20", min_score=0, max_score=20),
            ScoreDimensionRef(dimension_code="presentation", score_range_ref="synthetic-presentation-0-30", min_score=0, max_score=30),
            ScoreDimensionRef(dimension_code="process", score_range_ref="synthetic-process-0-30", min_score=0, max_score=30),
            ScoreDimensionRef(dimension_code="ai_literacy", score_range_ref="synthetic-ai-literacy-0-20", min_score=0, max_score=20),
        ],
    )
    for case in manifest["cases"]:
        code = case["team_code"]
        item = scoring_items[code]
        fact = registration.records[(_stable_uuid(f"package/{code}"), 1)]
        attempt_id = _stable_uuid(f"attempt/{code}")
        request_hash = _stable_hash(f"request/{code}")
        status = item.status
        is_success = status in ("completed", "manual_review")
        error = None
        if not is_success:
            error = ProviderError(
                error_id=_stable_uuid(f"provider-error/{code}"),
                request_id=_stable_uuid(f"provider-request/{code}"),
                task_id=scoring_task.task_id,
                item_id=item.item_id,
                attempt_id=attempt_id,
                provider_id=provider_id,
                model_id=model_id,
                error_code="PROVIDER_SERVER_ERROR",
                error_category="server",
                message_safe="synthetic offline failure",
                retryable="conditional",
                provider_switch_allowed="approval_required",
                manual_review_required="yes",
                consumes_provider_call_attempt=False,
                stop_entire_batch="no",
                occurred_at=FIXED_TIME_UTC,
                details_sha256=_stable_hash(f"provider-error-details/{code}"),
            )
        attempt = ScoreAttempt(
            attempt_id=attempt_id,
            attempt_number=1,
            task_id=scoring_task.task_id,
            item_id=item.item_id,
            submission_id=fact.submission_id,
            package_id=fact.package_id,
            package_revision=1,
            evidence_manifest_sha256=fact.manifest_sha256,
            input_fingerprint=item.input_fingerprint,
            provider_id=provider_id,
            provider_config_version="synthetic-provider-config-v1",
            model_id=model_id,
            model_capability_version="synthetic-capability-v1",
            scoring_policy_version="synthetic-policy-v1",
            rubric_version="synthetic-rubric-v1",
            prompt_version="synthetic-prompt-v1",
            response_schema_version="synthetic-response-v1",
            status="succeeded" if is_success else "failed",
            created_at=FIXED_TIME_UTC,
            created_by=actor,
            started_at=FIXED_TIME_UTC,
            completed_at=FIXED_TIME_UTC,
            duration_ms=0,
            error=error,
            result_snapshot_ref=_stable_uuid(f"snapshot/{code}") if is_success else None,
            validation_ref=_stable_uuid(f"attempt-validation/{code}") if is_success else None,
            request_hash=request_hash,
            response_hash=_stable_hash(f"response/{code}") if is_success else None,
        )
        attempt_store.create_attempt(scoring_task.task_id, item.item_id, attempt)
        attempts[code] = attempt
        if not is_success:
            continue
        machine = case["machine"]
        snapshot_id = _stable_uuid(f"snapshot/{code}")
        is_manual = status == "manual_review"
        snapshot = ScoreResultSnapshot(
            snapshot_id=snapshot_id,
            attempt_id=attempt_id,
            submission_id=fact.submission_id,
            package_id=fact.package_id,
            evidence_manifest_sha256=fact.manifest_sha256,
            scoring_policy_version="synthetic-policy-v1",
            rubric_version="synthetic-rubric-v1",
            response_schema_version="synthetic-response-v1",
            score_scale=scale,
            objective_score=float(machine["total_score"]),
            subjective_score=None,
            dimension_scores=[
                DimensionScore(dimension_code="theme", score=machine["theme_score"], min_score=0, max_score=20, score_range_ref="synthetic-theme-0-20"),
                DimensionScore(dimension_code="presentation", score=machine["presentation_score"], min_score=0, max_score=30, score_range_ref="synthetic-presentation-0-30"),
                DimensionScore(dimension_code="process", score=machine["process_score"], min_score=0, max_score=30, score_range_ref="synthetic-process-0-30"),
                DimensionScore(dimension_code="ai_literacy", score=machine["ai_literacy_score"], min_score=0, max_score=20, score_range_ref="synthetic-ai-literacy-0-20"),
            ],
            total_score=float(machine["total_score"]),
            rationale_summary="synthetic offline score snapshot",
            evidence_level=item.evidence_level,
            evidence_refs=[f"demo/evidence/{code}.json"],
            confidence="low" if is_manual else ("high" if machine["confidence"] == "HIGH" else "medium"),
            flags=[Flag(flag_code="LOW_CONFIDENCE", severity="warning", message_key="LOW_CONFIDENCE")] if is_manual else [],
            manual_review_recommended=is_manual,
            structure_validation_status="passed",
            score_range_validation_status="passed",
            result_hash=_stable_hash(f"result/{code}"),
            created_at=FIXED_TIME_UTC,
        )
        attempt_store.write_snapshot(scoring_task.task_id, item.item_id, snapshot)
        snapshots[code] = snapshot
        validation = AttemptValidation(
            validation_id=_stable_uuid(f"attempt-validation/{code}"),
            attempt_id=attempt_id,
            snapshot_id=snapshot_id,
            validation_revision=1,
            validator_version="synthetic-attempt-validator-v1",
            checks=[ValidationCheck(check_code="STRUCTURE", passed=True, message_key="synthetic"), ValidationCheck(check_code="SCORE_RANGE", passed=True, message_key="synthetic")],
            overall_status="manual_review_required" if is_manual else "passed",
            adoption_candidate=not is_manual,
            manual_review_required=is_manual,
            review_reason_codes=["LOW_CONFIDENCE"] if is_manual else [],
            validated_at=FIXED_TIME_UTC,
            validated_by="synthetic-demo-validator",
        )
        attempt_store.write_validation(scoring_task.task_id, item.item_id, validation)
        validations[code] = validation
    return attempt_store, attempts, {"snapshots": snapshots, "validations": validations, "reviewer": reviewer}


def _build_formal_reviews(root: Path, manifest: dict[str, Any], scoring_task: Any, scoring_items: dict[str, Any], registration: _SyntheticRegistration, attempt_store: Any, attempts: dict[str, Any], snapshot_data: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from models.review_case import AdoptionScope, ManualFinalLock, ResultAdoption, ReviewCase, ReviewDecision
    from services.review_case_store import ReviewCaseStore

    review_store = ReviewCaseStore(root / PHASE11_RUNTIME_DIR / "reviews", attempt_store=attempt_store)
    reviewer = snapshot_data["reviewer"]
    created: dict[str, Any] = {}
    for code in ("DEMO-P-005", "DEMO-M-006"):
        item = scoring_items[code]
        fact = registration.records[(_stable_uuid(f"package/{code}"), 1)]
        case_id = _stable_uuid(f"review-case/{code}")
        case = ReviewCase(
            review_case_id=case_id,
            case_number=_stable_uuid(f"review-number/{code}"),
            submission_id=fact.submission_id,
            task_id=scoring_task.task_id,
            item_id=item.item_id,
            package_id=fact.package_id,
            package_revision=1,
            attempt_ids=[attempts[code].attempt_id],
            snapshot_ids=[snapshot_data["snapshots"][code].snapshot_id] if code in snapshot_data["snapshots"] else [],
            validation_ids=[snapshot_data["validations"][code].validation_id] if code in snapshot_data["validations"] else [],
            source_type="provider_failure" if code.endswith("005") else "attempt_validation",
            reason_codes=["ALL_ATTEMPTS_FAILED"] if code.endswith("005") else ["LOW_CONFIDENCE"],
            priority="high",
            status="open",
            blocks_auto_adoption=True,
            blocks_export=True,
            opened_at=FIXED_TIME_UTC,
            updated_at=FIXED_TIME_UTC,
            current_revision=1,
            idempotency_key=_stable_uuid(f"review-idempotency/{code}"),
        )
        review_store.create_review_case(scoring_task.task_id, item.item_id, case)
        created[code] = {"case": case}

    locked_code = "DEMO-M-006"
    locked_item = scoring_items[locked_code]
    locked_case = created[locked_code]["case"]
    review_store.transition_review_case(
        scoring_task.task_id,
        locked_item.item_id,
        locked_case.review_case_id,
        expected_revision=1,
        to_status="in_review",
        event_type="REVIEW_STARTED",
        actor=reviewer,
        occurred_at=FIXED_TIME_UTC,
    )
    decision = ReviewDecision(
        decision_id=_stable_uuid(f"review-decision/{locked_code}"),
        review_case_id=locked_case.review_case_id,
        case_revision=2,
        decision_type="adopt_existing_attempt",
        reason_codes=["LOW_CONFIDENCE"],
        target_attempt_id=attempts[locked_code].attempt_id,
        target_snapshot_id=snapshot_data["snapshots"][locked_code].snapshot_id,
        decision_note_code="ADOPT_EXISTING_ATTEMPT",
        idempotency_key=_stable_uuid(f"decision-idempotency/{locked_code}"),
        decided_by=reviewer,
        decided_at=FIXED_TIME_UTC,
        decision_hash=_stable_hash(f"decision/{locked_code}"),
    )
    review_store.create_review_decision(scoring_task.task_id, locked_item.item_id, decision)
    scope = AdoptionScope(
        competition_id=_stable_uuid("competition/demo"),
        batch_id="DEMO-SCORE-BATCH-20260831",
        stream_id="default",
        submission_id=locked_case.submission_id,
        scoring_policy_version="synthetic-policy-v1",
        purpose="machine_result",
    )
    adoption = ResultAdoption(
        adoption_id=_stable_uuid(f"adoption/{locked_code}"),
        adoption_scope=scope,
        submission_id=locked_case.submission_id,
        attempt_id=attempts[locked_code].attempt_id,
        snapshot_id=snapshot_data["snapshots"][locked_code].snapshot_id,
        validation_id=snapshot_data["validations"][locked_code].validation_id,
        review_case_id=locked_case.review_case_id,
        decision_id=decision.decision_id,
        status="adopted",
        reason_code="HUMAN_ADOPTED",
        effective_at=FIXED_TIME_UTC,
        decided_at=FIXED_TIME_UTC,
        decided_by=reviewer,
        adoption_hash=_stable_hash(f"adoption/{locked_code}"),
    )
    review_store.create_adoption(scoring_task.task_id, locked_item.item_id, adoption)
    lock = ManualFinalLock(
        lock_id=_stable_uuid(f"final-lock/{locked_code}"),
        task_id=scoring_task.task_id,
        item_id=locked_item.item_id,
        review_case_id=locked_case.review_case_id,
        decision_id=decision.decision_id,
        adoption_id=adoption.adoption_id,
        snapshot_id=adoption.snapshot_id,
        locked_by=reviewer,
        locked_at=FIXED_TIME_UTC,
        reason_code="HUMAN_FINAL_LOCKED",
        status="active",
        revision=1,
        idempotency_key=_stable_uuid(f"lock-idempotency/{locked_code}"),
        content_hash=_stable_hash(f"lock/{locked_code}"),
    )
    review_store.create_final_lock(scoring_task.task_id, locked_item.item_id, lock)
    created[locked_code].update({"decision": decision, "adoption": adoption, "lock": lock})
    return review_store, created


def _phase11_formal_manifest(manifest: dict[str, Any], source_task: Any, scoring_task: Any, source_items: dict[str, Any], scoring_items: dict[str, Any], attempts: dict[str, Any], reviews: dict[str, Any]) -> dict[str, Any]:
    items = []
    for case in manifest["cases"]:
        code = case["team_code"]
        item = scoring_items[code]
        review = reviews.get(code, {})
        item_data = {
            "team_code": code,
            "package_id": item.package_id,
            "submission_id": _stable_uuid(f"submission/{code}"),
            "source_item_id": source_items[code].item_id,
            "scoring_item_id": item.item_id,
            "attempt_id": attempts[code].attempt_id,
            "attempt_status": attempts[code].status,
            "pipeline_status": item.status,
            "evidence_level": item.evidence_level,
        }
        if "case" in review:
            item_data["review_case_id"] = review["case"].review_case_id
        if "adoption" in review:
            item_data["adoption_id"] = review["adoption"].adoption_id
        if "lock" in review:
            item_data["final_lock_id"] = review["lock"].lock_id
        items.append(item_data)
    return {
        "schema_version": "synthetic-demo-phase11/v1",
        "dataset_id": manifest["dataset_id"],
        "source": "offline_synthetic",
        "generated_at": "2026-08-31T00:00:00Z",
        "provider_calls": 0,
        "schema_changes": 0,
        "stores": {
            "pipeline_runtime": PHASE11_RUNTIME_DIR,
            "tasks": f"{PHASE11_RUNTIME_DIR}/tasks",
            "score_attempts": f"{PHASE11_RUNTIME_DIR}/score-attempts",
            "reviews": f"{PHASE11_RUNTIME_DIR}/reviews",
        },
        "source_task_id": source_task.task_id,
        "scoring_task_id": scoring_task.task_id,
        "task_counts": {"source_items": 12, "scoring_items": 12, "completed": 8, "manual_review": 2, "failed": 2},
        "review_cases": [
            {"team_code": code, "review_case_id": reviews[code]["case"].review_case_id, "status": "open" if code.endswith("005") else "in_review", "adoption_status": "adopted" if "adoption" in reviews[code] else None, "final_lock_status": "active" if "lock" in reviews[code] else None}
            for code in ("DEMO-P-005", "DEMO-M-006")
        ],
        "items": items,
    }


def _verify_phase11_formal(root: Path, manifest: dict[str, Any], extension: dict[str, Any]) -> None:
    from services.pipeline_task_store import PipelineTaskStore
    from services.review_case_store import ReviewCaseStore
    from services.score_attempt_store import ScoreAttemptStore

    runtime = root / PHASE11_RUNTIME_DIR
    task_store = PipelineTaskStore(runtime)
    attempt_store = ScoreAttemptStore(runtime / "score-attempts")
    review_store = ReviewCaseStore(runtime / "reviews", attempt_store=attempt_store)
    source = task_store.load_task(extension["source_task_id"])
    scoring = task_store.load_task(extension["scoring_task_id"])
    if source is None or scoring is None or source.total_items != 12 or scoring.total_items != 12:
        raise DemoBootstrapError("formal Phase 11 task count mismatch")
    if scoring.status != "completed_with_errors" or scoring.completed_items != 8 or scoring.manual_review_items != 2 or scoring.failed_items != 2:
        raise DemoBootstrapError("formal Phase 11 scoring task status mismatch")
    scoring_items = task_store.list_items(scoring.task_id)
    if len(scoring_items) != 12 or {item.status for item in scoring_items} != {"completed", "manual_review", "failed"}:
        raise DemoBootstrapError("formal Phase 11 scoring item status mismatch")
    if len(review_store.list_review_cases(scoring.task_id)) != 2:
        raise DemoBootstrapError("formal Phase 11 review case count mismatch")
    for item in scoring_items:
        if len(attempt_store.list_attempts(scoring.task_id, item.item_id)) != 1:
            raise DemoBootstrapError("formal Phase 11 attempt count mismatch")
    locked_item = next(item for item in scoring_items if item.package_id == _stable_uuid("package/DEMO-M-006"))
    if review_store.get_active_adoption(scoring.task_id, locked_item.item_id) is None or review_store.get_active_final_lock(scoring.task_id, locked_item.item_id) is None:
        raise DemoBootstrapError("formal Phase 11 adoption/lock mismatch")
    source_by_code = {next(case["team_code"] for case in manifest["cases"] if _stable_uuid(f"package/{case['team_code']}") == item.package_id): item for item in task_store.list_items(source.task_id)}
    scoring_by_code = {next(case["team_code"] for case in manifest["cases"] if _stable_uuid(f"package/{case['team_code']}") == item.package_id): item for item in scoring_items}
    attempts_by_code = {code: attempt_store.list_attempts(scoring.task_id, item.item_id)[0] for code, item in scoring_by_code.items()}
    reviews_by_code: dict[str, dict[str, Any]] = {}
    for case in review_store.list_review_cases(scoring.task_id):
        code = next(code for code, item in scoring_by_code.items() if item.item_id == case.item_id)
        reviews_by_code[code] = {"case": case}
        adoption = review_store.get_active_adoption(scoring.task_id, case.item_id)
        lock = review_store.get_active_final_lock(scoring.task_id, case.item_id)
        if adoption is not None:
            reviews_by_code[code]["adoption"] = adoption
        if lock is not None:
            reviews_by_code[code]["lock"] = lock
    expected = _phase11_formal_manifest(manifest, source, scoring, source_by_code, scoring_by_code, attempts_by_code, reviews_by_code)
    if extension != expected:
        raise DemoBootstrapError("existing formal Phase 11 manifest differs")


def _bootstrap_phase11(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    from models.pipeline_task import PipelineConfigurationSnapshot, PipelineError, PipelineErrorSummary
    from models.scoring_configuration import ScoringTaskConfiguration, ScoringTaskCreateRequest
    from services.pipeline_task_manager import PackageRef, PipelineTaskCreateRequest, PipelineTaskManager
    from services.pipeline_task_store import PipelineTaskStore
    from services.scoring_task_creator import ScoringTaskCreator
    from models.pipeline_task import sha256_canonical

    extension_path = root / PHASE11_MANIFEST_NAME
    runtime = root / PHASE11_RUNTIME_DIR
    if extension_path.exists():
        extension = json.loads(extension_path.read_text(encoding="utf-8"))
        if extension.get("schema_version") != "synthetic-demo-phase11/v1":
            raise DemoBootstrapError("existing formal Phase 11 marker differs")
        _verify_phase11_formal(root, manifest, extension)
        return {"status": "verified", "file": str(extension_path), "source_task_id": extension["source_task_id"], "scoring_task_id": extension["scoring_task_id"]}
    if any(path.exists() and any(path.iterdir()) for path in (runtime / "tasks", runtime / "score-attempts", runtime / "reviews") if path.exists()):
        raise DemoBootstrapError("formal Phase 11 runtime is partially initialized; refusing repair")

    registration = _SyntheticRegistration(manifest)
    ordered = _ordered_cases(manifest)
    refs = [PackageRef(package_id=_stable_uuid(f"package/{case['team_code']}"), package_revision=1, manifest_sha256=_stable_hash(f"manifest/{case['team_code']}"), registration_record_id=_stable_uuid(f"record/{case['team_code']}"), validation_id=_stable_uuid(f"validation/{case['team_code']}")) for case in manifest["cases"]]
    source_ids = [_stable_uuid("task/source"), *[_stable_uuid(f"source-item/{case['team_code']}" ) for case in ordered], _stable_uuid("event/source")]
    source_uuid = iter(source_ids)
    source_store = PipelineTaskStore(runtime)
    source_manager = PipelineTaskManager(source_store, registration, clock=lambda: FIXED_TIME_UTC, uuid_factory=lambda: next(source_uuid))
    source_request = PipelineTaskCreateRequest(batch_id="DEMO-BATCH-20260831", package_refs=refs, concurrency=4, configuration_snapshot=PipelineConfigurationSnapshot(evidence_contract_version="synthetic-evidence-contract-v1", validator_version="synthetic-validator-v1"))
    source_task, _ = source_manager.create_task(source_request)
    source_items = source_store.list_items(source_task.task_id)
    source_items_by_code = {next(case["team_code"] for case in manifest["cases"] if _stable_uuid(f"package/{case['team_code']}") == item.package_id): item for item in source_items}
    source_items_done = [item.model_copy(update={"status": "completed", "current_stage": "validate", "updated_at": FIXED_TIME_UTC, "item_revision": 2, "output_ref": f"demo/evidence/{item.package_id}.json", "output_sha256": _stable_hash(f"evidence-output/{item.package_id}")}) for item in source_items]
    for item in source_items_done:
        source_store.write_item_snapshot(source_task.task_id, item, expected_revision=1)
    source_task_done = source_task.model_copy(update={"status": "completed", "current_stage": None, "started_at": FIXED_TIME_UTC, "completed_at": FIXED_TIME_UTC, "updated_at": FIXED_TIME_UTC, "pending_items": 0, "running_items": 0, "completed_items": 12, "failed_items": 0, "skipped_items": 0, "manual_review_items": 0, "item_index": _formal_item_index(source_items_done), "stage_summaries": _phase11_stage_summaries(source_items_done, evidence=True), "revision": 2})
    source_store.write_task_snapshot(source_task.task_id, source_task_done, expected_revision=1)

    profile = _SyntheticProfileLookup().profile
    cfg_fields = {"profile_version": profile.profile_version, "provider_id": _stable_uuid("provider/synthetic"), "model_id": _stable_uuid("model/synthetic"), "scoring_policy_version": profile.scoring_policy_version, "rubric_version": profile.rubric_version, "prompt_version": profile.prompt_version, "response_schema_version": profile.response_schema_version}
    scoring_cfg = ScoringTaskConfiguration(**cfg_fields, configuration_fingerprint=sha256_canonical(cfg_fields))
    source_item_ids = [item.item_id for item in source_items_done]
    scoring_ids = [_stable_uuid("task/scoring"), *[_stable_uuid(f"scoring-item/{case['team_code']}" ) for case in sorted(manifest["cases"], key=lambda value: _stable_uuid(f"source-item/{value['team_code']}"))], _stable_uuid("event/scoring")]
    scoring_uuid = iter(scoring_ids)
    creator = ScoringTaskCreator(source_store, registration, _SyntheticProfileLookup(), clock=lambda: FIXED_TIME_UTC, uuid_factory=lambda: next(scoring_uuid))
    scoring_request = ScoringTaskCreateRequest(batch_id="DEMO-SCORE-BATCH-20260831", source_task_id=source_task.task_id, source_item_ids=source_item_ids, configuration=scoring_cfg, concurrency=4)
    scoring_task, _ = creator.create_scoring_task(scoring_request)
    scoring_items = source_store.list_items(scoring_task.task_id)
    scoring_by_code = {next(case["team_code"] for case in manifest["cases"] if _stable_uuid(f"package/{case['team_code']}") == item.package_id): item for item in scoring_items}
    for code, item in scoring_by_code.items():
        if code in ("DEMO-P-004", "DEMO-M-004"):
            new_item = item.model_copy(update={"status": "manual_review", "current_stage": "review", "attempt_count": 1, "updated_at": FIXED_TIME_UTC, "item_revision": 2, "last_error": PipelineError(error_code="DEMO_MANUAL_REVIEW_REQUIRED", message_key="DEMO_MANUAL_REVIEW_REQUIRED", retryable=False, stage="review")})
        elif code in ("DEMO-P-005", "DEMO-M-005"):
            new_item = item.model_copy(update={"status": "failed", "current_stage": "score", "attempt_count": 1, "updated_at": FIXED_TIME_UTC, "item_revision": 2, "last_error": PipelineError(error_code="DEMO_SYNTHETIC_SCORE_FAILED", message_key="DEMO_SYNTHETIC_SCORE_FAILED", retryable=False, stage="score")})
        else:
            new_item = item.model_copy(update={"status": "completed", "current_stage": "export", "attempt_count": 1, "updated_at": FIXED_TIME_UTC, "item_revision": 2, "output_ref": f"demo/results/{code}.json", "output_sha256": _stable_hash(f"output/{code}")})
        source_store.write_item_snapshot(scoring_task.task_id, new_item, expected_revision=1)
        scoring_by_code[code] = new_item
    scoring_done = scoring_task.model_copy(update={"status": "completed_with_errors", "current_stage": None, "started_at": FIXED_TIME_UTC, "completed_at": FIXED_TIME_UTC, "updated_at": FIXED_TIME_UTC, "pending_items": 0, "running_items": 0, "completed_items": 8, "failed_items": 2, "skipped_items": 0, "manual_review_items": 2, "item_index": _formal_item_index(list(scoring_by_code.values())), "stage_summaries": _phase11_stage_summaries(list(scoring_by_code.values()), evidence=False), "error_summary": PipelineErrorSummary(count=4, codes=["DEMO_SYNTHETIC_SCORE_FAILED", "DEMO_MANUAL_REVIEW_REQUIRED"]), "revision": 2})
    source_store.write_task_snapshot(scoring_task.task_id, scoring_done, expected_revision=1)
    attempt_store, attempts, snapshot_data = _build_formal_attempts(root, manifest, scoring_done, scoring_by_code, registration)
    review_store, reviews = _build_formal_reviews(root, manifest, scoring_done, scoring_by_code, registration, attempt_store, attempts, snapshot_data)
    extension = _phase11_formal_manifest(manifest, source_task_done, scoring_done, {code: source_items_done[next(index for index, item in enumerate(source_items_done) if item.package_id == _stable_uuid(f"package/{code}"))] for code in scoring_by_code}, scoring_by_code, attempts, reviews)
    _write_json_once(extension_path, extension)
    _verify_phase11_formal(root, manifest, extension)
    return {"status": "created", "file": str(extension_path), "source_task_id": source_task_done.task_id, "scoring_task_id": scoring_done.task_id}


def bootstrap_demo(data_root: Path | None = None) -> dict[str, Any]:
    manifest = _load_manifest()
    root = Path(data_root).expanduser().resolve() if data_root is not None else _data_root()
    root.mkdir(parents=True, exist_ok=True)
    initialized, db_path, manifest_path, phase_path = _assert_target_shape(root, manifest)
    database, engine, session_factory = _load_orm(root)
    database.Base.metadata.create_all(bind=engine)
    session = session_factory()
    try:
        if initialized:
            _apply_enrichment(session, root, manifest)
            session.commit()
            _verify_legacy(session, manifest)
            if manifest_path.read_text(encoding="utf-8") != json.dumps(manifest, ensure_ascii=False, indent=2) + "\n":
                raise DemoBootstrapError("existing demo manifest differs")
            if phase_path.read_text(encoding="utf-8") != json.dumps(_phase_state(manifest), ensure_ascii=False, indent=2) + "\n":
                raise DemoBootstrapError("existing Phase 11 demo state differs")
            phase11 = _bootstrap_phase11(root, manifest)
            return {"status": "verified", "data_root": str(root), "case_count": 12, "phase11_status": phase11["status"], "files": [str(db_path), str(manifest_path), str(phase_path), phase11["file"]]}
        _create_legacy(session, manifest)
        _apply_enrichment(session, root, manifest)
        session.commit()
        _verify_legacy(session, manifest)
        _write_json_once(manifest_path, manifest)
        _write_json_once(phase_path, _phase_state(manifest))
        phase11 = _bootstrap_phase11(root, manifest)
        return {"status": "created", "data_root": str(root), "case_count": 12, "phase11_status": phase11["status"], "files": [str(db_path), str(manifest_path), str(phase_path), phase11["file"]]}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()
        if _SYNTHETIC_DEMO_IMPORTED_DATABASE:
            getattr(database, "engine", None).dispose()


if __name__ == "__main__":
    result = bootstrap_demo()
    print(json.dumps(result, ensure_ascii=False, indent=2))
