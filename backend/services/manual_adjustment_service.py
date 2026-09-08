"""
11F-3b-prerequisite-impl-2：可恢复事务 ManualAdjustmentService。

流程：prepared → committing → committed / failed。
使用 ManualAdjustmentOperation 协调写入，支持进程中断后恢复。
"""
from __future__ import annotations

import hashlib
import json as _json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from models.review_case import (
    ManualAdjustment,
    ManualAdjustmentChange,
    ManualAdjustmentOperation,
    ResultAdoption,
)
from models.score_attempt import AttemptValidation, DimensionScore, ScoreResultSnapshot
from services.review_case_store import ReviewFactStoreError


def _stable_ids(request_id: str) -> Dict[str, str]:
    return {
        "request_id": request_id,
        "operation_id": f"op-{request_id}",
        "adjustment_id": f"adj-{request_id}",
        "adjusted_snapshot_id": f"snap-adjusted-{request_id}",
        "adjusted_validation_id": f"val-adjusted-{request_id}",
        "new_adoption_id": f"adp-adjusted-{request_id}",
    }


def _payload_hash(
    task_id: str, item_id: str, case_id: str, decision_id: str,
    request_id: str, changes: list, reason_codes: list, note_code: str,
    base_snapshot_id: str, old_adoption_id: str,
) -> str:
    data = _json.dumps({
        "task_id": task_id, "item_id": item_id, "case_id": case_id,
        "decision_id": decision_id, "request_id": request_id,
        "changes": changes, "reason_codes": reason_codes, "note_code": note_code,
        "base_snapshot_id": base_snapshot_id, "old_adoption_id": old_adoption_id,
    }, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


class ManualAdjustmentService:
    """可恢复事务调整服务。"""

    def __init__(self, review_store: Any, attempt_store: Any):
        self._review_store = review_store
        self._attempt_store = attempt_store

    def build_and_apply(
        self, task_id: str, item_id: str, case_id: str, decision_id: str,
        request_id: str, changes: List[Dict[str, Any]], reason_codes: List[str],
        adjustment_note_code: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        ids = _stable_ids(request_id)

        # ---- 幂等检查（优先） ----
        existing = self._review_store.get_operation(task_id, item_id, ids["operation_id"])
        if existing is not None:
            return self._handle_existing_operation(
                ids, existing, task_id, item_id, case_id, decision_id,
                changes, reason_codes, adjustment_note_code)

        # ---- 前置检查 ----
        case = self._review_store.get_review_case(task_id, item_id, case_id)
        if case is None or case.task_id != task_id or case.item_id != item_id:
            return None, "REVIEW_CASE_NOT_FOUND"
        decision = self._review_store.get_review_decision(task_id, item_id, decision_id)
        if decision is None or decision.decision_type != "apply_manual_adjustment" or decision.review_case_id != case_id:
            return None, "REVIEW_DECISION_NOT_FOUND"
        if self._review_store.get_active_final_lock(task_id, item_id) is not None:
            return None, "MANUAL_ADJUSTMENT_BLOCKED_BY_LOCK"
        adoption = self._review_store.get_active_adoption(task_id, item_id)
        if adoption is None or adoption.status != "adopted":
            return None, "REVIEW_FACT_BINDING_MISMATCH"
        base_snap = self._attempt_store.get_snapshot(task_id, item_id, adoption.snapshot_id)
        if base_snap is None or not base_snap.dimension_scores:
            return None, "MANUAL_ADJUSTMENT_RANGE_INVALID"
        err = self._validate_changes(changes, base_snap)
        if err is not None:
            return None, err

        return self._execute_new(
            ids, task_id, item_id, case_id, decision_id,
            adoption, base_snap, changes, reason_codes, adjustment_note_code)

    def _handle_existing_operation(
        self, ids, existing, task_id, item_id, case_id, decision_id,
        changes, reason_codes, note_code,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """处理已有 operation：committed 幂等 / failed 阻断 / prepared/committing 续跑。"""
        if existing.stage == "committed":
            if existing.base_snapshot_id is not None and existing.old_adoption_id is not None:
                p_hash = _payload_hash(
                    task_id, item_id, case_id, decision_id, ids["request_id"],
                    changes, reason_codes, note_code,
                    existing.base_snapshot_id, existing.old_adoption_id)
                if existing.payload_hash != p_hash:
                    return None, "MANUAL_ADJUSTMENT_CONFLICT"
            return self._committed_result(ids, task_id, item_id), None
        if existing.stage == "failed":
            return None, existing.error_code or "MANUAL_ADJUSTMENT_TRANSACTION_FAILED"

        # 使用 operation 中保存的基线重建 payload_hash
        if existing.base_snapshot_id is None or existing.old_adoption_id is None:
            return None, "MANUAL_ADJUSTMENT_CONFLICT"
        p_hash = _payload_hash(
            task_id, item_id, case_id, decision_id, ids["request_id"],
            changes, reason_codes, note_code,
            existing.base_snapshot_id, existing.old_adoption_id)
        if existing.payload_hash != p_hash:
            return None, "MANUAL_ADJUSTMENT_CONFLICT"

        # 续跑前检查：case/decision/lock 必须仍然有效
        case = self._review_store.get_review_case(task_id, item_id, case_id)
        if case is None or case.task_id != task_id or case.item_id != item_id:
            return None, "REVIEW_CASE_NOT_FOUND"
        decision = self._review_store.get_review_decision(task_id, item_id, decision_id)
        if decision is None or decision.decision_type != "apply_manual_adjustment" or decision.review_case_id != case_id:
            return None, "REVIEW_DECISION_NOT_FOUND"
        if self._review_store.get_active_final_lock(task_id, item_id) is not None:
            return None, "MANUAL_ADJUSTMENT_BLOCKED_BY_LOCK"

        # 加载旧 adoption 和 base snapshot
        base_snap = self._attempt_store.get_snapshot(task_id, item_id, existing.base_snapshot_id)
        if base_snap is None:
            return None, "MANUAL_ADJUSTMENT_CONFLICT"
        # 续跑必须使用 operation 保存的旧 adoption，不得切换到当前 active adjusted adoption
        adoption = self._review_store.get_adoption(task_id, item_id, existing.old_adoption_id)
        if adoption is None:
            return None, "MANUAL_ADJUSTMENT_CONFLICT"

        return self._recover(
            ids, existing, task_id, item_id, case_id, decision_id,
            adoption, base_snap, changes, reason_codes, note_code)

    def _execute_new(
        self, ids, task_id, item_id, case_id, decision_id,
        adoption, base_snap, changes, reason_codes, note_code,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """完整创建新 adjustment 事务。"""
        p_hash = _payload_hash(
            task_id, item_id, case_id, decision_id, ids["request_id"],
            changes, reason_codes, note_code, base_snap.snapshot_id, adoption.adoption_id)

        now = datetime.now(timezone.utc)
        op = ManualAdjustmentOperation(
            contract_version="score-attempt-review/v1",
            schema_version="manual-adjustment-operation/v1",
            operation_id=ids["operation_id"],
            request_id=ids["request_id"] if "request_id" in ids else "",
            stage="prepared",
            payload_hash=p_hash,
            created_at=now, updated_at=now,
            base_snapshot_id=base_snap.snapshot_id,
            adjusted_snapshot_id=ids["adjusted_snapshot_id"],
            adjusted_validation_id=ids["adjusted_validation_id"],
            changes=changes, reason_codes=reason_codes,
            old_adoption_id=adoption.adoption_id,
            old_adoption_status_before=adoption.status,
        )
        self._review_store.create_operation(task_id, item_id, op)

        # ---- prepared：写入 snapshot + validation ----
        adj_snap = self._build_adjusted_snapshot(base_snap, changes, ids["adjusted_snapshot_id"])
        try:
            self._attempt_store.write_snapshot(task_id, item_id, adj_snap)
        except Exception:
            self._review_store.mark_operation_failed(
                task_id, item_id, ids["operation_id"],
                "MANUAL_ADJUSTMENT_TRANSACTION_FAILED", "prepared")
            return None, "MANUAL_ADJUSTMENT_TRANSACTION_FAILED"

        validation = self._build_validation(base_snap, ids["adjusted_validation_id"], ids["adjusted_snapshot_id"])
        try:
            self._attempt_store.write_validation(task_id, item_id, validation)
        except Exception:
            self._cleanup_snapshot(task_id, item_id, ids["adjusted_snapshot_id"])
            self._review_store.mark_operation_failed(
                task_id, item_id, ids["operation_id"],
                "MANUAL_ADJUSTMENT_TRANSACTION_FAILED", "prepared")
            return None, "MANUAL_ADJUSTMENT_TRANSACTION_FAILED"

        # 推进到 committing
        committing_op = op.model_copy(update={"stage": "committing", "updated_at": datetime.now(timezone.utc)})
        self._review_store.advance_operation(task_id, item_id, committing_op)

        # ---- committing ----
        return self._commit(ids, task_id, item_id, case_id, decision_id,
                            adoption, base_snap, adj_snap, changes, reason_codes, note_code)

    # ---- committing ----

    def _commit(self, ids, task_id, item_id, case_id, decision_id,
                adoption, base_snap, adj_snap, changes, reason_codes, note_code):
        adj = self._build_adjustment(
            case_id, decision_id, adoption, base_snap, ids["adjusted_snapshot_id"],
            changes, reason_codes, note_code, ids["adjustment_id"])
        try:
            self._review_store.create_manual_adjustment(task_id, item_id, adj)
        except Exception:
            return self._fail_and_cleanup(
                ids, task_id, item_id, "committing", "MANUAL_ADJUSTMENT_TRANSACTION_FAILED")

        new_adoption = self._build_new_adoption(
            adoption, base_snap, adj_snap, ids["adjusted_validation_id"],
            ids["new_adoption_id"], case_id, decision_id, ids["adjustment_id"])
        try:
            self._review_store.adopt_result(task_id, item_id, new_adoption)
        except Exception:
            err = self._review_store.restore_adoption_status(
                task_id, item_id, adoption.adoption_id,
                operation_id=ids["operation_id"], new_adoption_id=ids["new_adoption_id"])
            if err is not None:
                return None, err
            return self._fail_and_cleanup(
                ids, task_id, item_id, "committing", "MANUAL_ADJUSTMENT_TRANSACTION_FAILED")

        # 完成：读取当前 operation 并推进到 committed
        now = datetime.now(timezone.utc)
        current_op = self._review_store.get_operation(task_id, item_id, ids["operation_id"])
        if current_op is None:
            return None, "MANUAL_ADJUSTMENT_TRANSACTION_FAILED"
        committed_op = current_op.model_copy(update={
            "stage": "committed",
            "adjustment_id": ids["adjustment_id"],
            "new_adoption_id": ids["new_adoption_id"],
            "updated_at": now,
        })
        self._review_store.advance_operation(task_id, item_id, committed_op)
        return {
            "adjustment_id": ids["adjustment_id"],
            "adjusted_snapshot_id": ids["adjusted_snapshot_id"],
            "adoption_id": ids["new_adoption_id"],
            "outcome": "created",
            "idempotent": False,
        }, None

    # ---- 恢复 ----

    def _recover(self, ids, op, task_id, item_id, case_id, decision_id,
                 adoption, base_snap, changes, reason_codes, note_code):
        if op.stage == "committed":
            return self._committed_result(ids, task_id, item_id), None
        if op.stage == "failed":
            return None, op.error_code or "MANUAL_ADJUSTMENT_TRANSACTION_FAILED"

        # prepared：检查 snapshot 和 validation
        snap = self._attempt_store.get_snapshot(task_id, item_id, ids["adjusted_snapshot_id"])
        if snap is None:
            adj_snap = self._build_adjusted_snapshot(base_snap, changes, ids["adjusted_snapshot_id"])
            self._attempt_store.write_snapshot(task_id, item_id, adj_snap)
        validation = self._attempt_store.get_validation(task_id, item_id, ids["adjusted_validation_id"])
        if validation is None:
            val = self._build_validation(base_snap, ids["adjusted_validation_id"], ids["adjusted_snapshot_id"])
            self._attempt_store.write_validation(task_id, item_id, val)

        if op.stage == "prepared":
            committing_op = op.model_copy(update={"stage": "committing", "updated_at": datetime.now(timezone.utc)})
            self._review_store.advance_operation(task_id, item_id, committing_op)

        return self._commit(ids, task_id, item_id, case_id, decision_id,
                            adoption, base_snap, snap or adj_snap, changes, reason_codes, note_code)

    # ---- 构建 ----

    def _validate_changes(self, changes, snap):
        if not changes:
            return "MANUAL_ADJUSTMENT_RANGE_INVALID"
        seen = set()
        for ch in changes:
            code = ch.get("dimension_code", "")
            if code in seen:
                return "MANUAL_ADJUSTMENT_RANGE_INVALID"
            seen.add(code)
            dim = None
            for d in snap.dimension_scores:
                if d.dimension_code == code:
                    dim = d; break
            if dim is None:
                return "MANUAL_ADJUSTMENT_RANGE_INVALID"
            if ch.get("before_value") != dim.score:
                return "MANUAL_ADJUSTMENT_BEFORE_VALUE_MISMATCH"
            after = ch.get("after_value")
            if after < dim.min_score or after > dim.max_score:
                return "MANUAL_ADJUSTMENT_RANGE_INVALID"
        return None

    def _build_adjusted_snapshot(self, base, changes, snap_id):
        new_dims, delta = [], 0.0
        new_obj, new_sub = base.objective_score, base.subjective_score
        for d in base.dimension_scores:
            found = next((c for c in changes if c["dimension_code"] == d.dimension_code), None)
            if found:
                ns = float(found["after_value"])
                delta += (ns - d.score)
                new_dims.append(DimensionScore(
                    dimension_code=d.dimension_code, score=ns,
                    min_score=d.min_score, max_score=d.max_score,
                    score_range_ref=d.score_range_ref, evidence_refs=list(d.evidence_refs)))
                if d.dimension_code == "objective": new_obj = ns
                elif d.dimension_code == "subjective": new_sub = ns
            else:
                new_dims.append(d)
        now = datetime.now(timezone.utc)
        return ScoreResultSnapshot(
            schema_version=base.schema_version, snapshot_id=snap_id,
            attempt_id=base.attempt_id, submission_id=base.submission_id,
            package_id=base.package_id, evidence_manifest_sha256=base.evidence_manifest_sha256,
            scoring_policy_version=base.scoring_policy_version, rubric_version=base.rubric_version,
            response_schema_version=base.response_schema_version, score_scale=base.score_scale,
            objective_score=new_obj, subjective_score=new_sub, dimension_scores=new_dims,
            total_score=base.total_score + delta,
            rationale_summary=f"adjusted from {base.snapshot_id}",
            evidence_level=base.evidence_level, evidence_refs=list(base.evidence_refs),
            confidence=base.confidence, flags=list(base.flags),
            manual_review_recommended=True, structure_validation_status="passed",
            score_range_validation_status="passed",
            result_hash=hashlib.sha256(_json.dumps({"total": base.total_score + delta, "changes": changes}).encode()).hexdigest(),
            created_at=now)

    def _build_validation(self, base_snap, validation_id, adjusted_snapshot_id):
        now = datetime.now(timezone.utc)
        from models.score_attempt import ValidationCheck
        return AttemptValidation(
            schema_version="attempt-validation/v1",
            validation_id=validation_id,
            attempt_id=base_snap.attempt_id,
            snapshot_id=adjusted_snapshot_id,
            validation_revision=1,
            validator_version="manual-adjustment/v1",
            checks=[ValidationCheck(check_code="MANUAL_ADJUSTMENT_VALIDATED", passed=True)],
            overall_status="passed",
            adoption_candidate=True,
            manual_review_required=False,
            validated_at=now,
            validated_by="local-reviewer",
        )

    def _build_adjustment(self, case_id, decision_id, adoption, base_snap, adjusted_sid,
                          changes, reason_codes, note_code, adj_id):
        now = datetime.now(timezone.utc)
        return ManualAdjustment(
            contract_version="score-attempt-review/v1", schema_version="manual-adjustment/v1",
            adjustment_id=adj_id, review_case_id=case_id, decision_id=decision_id,
            submission_id=base_snap.submission_id, base_attempt_id=base_snap.attempt_id,
            base_snapshot_id=base_snap.snapshot_id, adjusted_snapshot_id=adjusted_sid,
            scoring_policy_version=base_snap.scoring_policy_version, rubric_version=base_snap.rubric_version,
            changes=[ManualAdjustmentChange(
                field_path=f"dimension_scores.{c['dimension_code']}.score",
                before_value=float(c["before_value"]), after_value=float(c["after_value"]),
                allowed_range_ref=self._dim_range_ref(base_snap, c["dimension_code"]),
                change_reason_code=c.get("change_reason_code", "OTHER")) for c in changes],
            reason_codes=reason_codes, evidence_refs=[], adjustment_note_code=note_code,
            adjusted_by={"actor_type": "reviewer", "actor_id": "local-reviewer"},
            adjusted_at=now,
            adjustment_hash=hashlib.sha256(_json.dumps({"adj_id": adj_id, "changes": changes}).encode()).hexdigest())

    def _dim_range_ref(self, snap, code):
        for d in snap.dimension_scores:
            if d.dimension_code == code:
                return d.score_range_ref
        return "unknown"

    def _build_new_adoption(self, old_adoption, base_snap, adj_snap, validation_id,
                            adoption_id, case_id, decision_id, adj_id):
        now = datetime.now(timezone.utc)
        scope = old_adoption.adoption_scope
        if hasattr(scope, "model_dump"):
            scope = scope.model_dump(mode="json")
        return ResultAdoption(
            contract_version="score-attempt-review/v1", schema_version="result-adoption/v1",
            adoption_id=adoption_id, adoption_scope=scope,
            submission_id=base_snap.submission_id, attempt_id=base_snap.attempt_id,
            snapshot_id=adj_snap.snapshot_id, validation_id=validation_id,
            review_case_id=case_id, decision_id=decision_id,
            status="adopted", reason_code="MANUAL_ADJUSTMENT_APPLIED",
            supersedes_adoption_id=old_adoption.adoption_id,
            effective_at=now, decided_at=now,
            decided_by={"actor_type": "reviewer", "actor_id": "local-reviewer"},
            adoption_hash=hashlib.sha256(adoption_id.encode()).hexdigest())

    def _committed_result(self, ids, task_id, item_id):
        active = self._review_store.get_active_adoption(task_id, item_id)
        return {
            "adjustment_id": ids["adjustment_id"],
            "adjusted_snapshot_id": ids["adjusted_snapshot_id"],
            "adoption_id": active.adoption_id if active else ids["new_adoption_id"],
            "outcome": "idempotent_hit", "idempotent": True,
        }

    def _fail_and_cleanup(self, ids, task_id, item_id, stage, error_code):
        self._cleanup_snapshot(task_id, item_id, ids["adjusted_snapshot_id"])
        self._cleanup_validation(task_id, item_id, ids["adjusted_validation_id"])
        try:
            self._review_store.mark_operation_failed(task_id, item_id, ids["operation_id"], error_code, stage)
        except Exception:
            pass
        return None, error_code

    def _cleanup_snapshot(self, task_id, item_id, snapshot_id):
        try:
            self._attempt_store.delete_snapshot(task_id, item_id, snapshot_id)
        except Exception:
            pass

    def _cleanup_validation(self, task_id, item_id, validation_id):
        try:
            self._attempt_store.delete_validation(task_id, item_id, validation_id)
        except Exception:
            pass
