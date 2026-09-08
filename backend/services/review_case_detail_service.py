"""
11F-2c：复核详情安全组装服务。

只组装白名单字段，不返回个人信息、evidence/Prompt/模型原文、绝对路径、分项得分。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from models.review_case import (
    ManualFinalLock,
    ResultAdoption,
    ReviewCase,
    ReviewDecision,
)
from models.score_attempt import ScoreAttempt, ScoreResultSnapshot


class ReviewCaseDetailService:
    """安全的复核详情组装器。

    从 review_case_store + attempt_store 两处读取事实，组装脱敏详情。
    所有绑定关系均 fail closed：歧义或冲突时拒绝返回。
    """

    def __init__(self, review_store: Any, attempt_store: Any):
        self._review_store = review_store
        self._attempt_store = attempt_store

    def build_detail(
        self, task_id: str, item_id: str, review_case_id: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """组装详情，返回 (detail_dict, None) 或 (None, error_code)。"""
        case = self._review_store.get_review_case(task_id, item_id, review_case_id)
        if case is None:
            return None, "REVIEW_CASE_NOT_FOUND"

        # ---- 绑定校验 ----
        if case.task_id != task_id or case.item_id != item_id:
            return None, "REVIEW_FACT_BINDING_MISMATCH"

        # ---- 组装 case ----
        detail: Dict[str, Any] = {
            "case": {
                "review_case_id": case.review_case_id,
                "item_id": case.item_id,
                "priority": case.priority,
                "status": case.status,
                "package_revision": case.package_revision,
                "current_revision": case.current_revision,
                "reopen_count": case.reopen_count,
                "reason_codes": list(case.reason_codes),
                "opened_at": _iso(case.opened_at),
                "blocks_auto_adoption": case.blocks_auto_adoption,
                "blocks_export": case.blocks_export,
            },
            "attempts": [],
            "decisions": [],
            "active_adoption": None,
            "active_lock": None,
            "manual_adjustment_context": {
                "allowed": False,
                "block_reason_code": "NO_ACTIVE_ADOPTION",
                "attempt_id": None,
                "snapshot_id": None,
                "current_total_score": None,
                "dimensions": [],
            },
        }

        # ---- attempts（仅该 case 明确引用的） ----
        ref_attempt_ids = set(case.attempt_ids)
        all_attempts = self._attempt_store.list_attempts(task_id, item_id)
        for att in all_attempts:
            if att.attempt_id not in ref_attempt_ids:
                continue
            entry = {
                "attempt_id": att.attempt_id,
                "attempt_number": att.attempt_number,
                "status": att.status,
                "snapshot_id": att.result_snapshot_ref,
                "total_score": None,
                "created_at": _iso(att.created_at),
            }
            # 安全读取快照总数
            if att.result_snapshot_ref:
                snap = self._attempt_store.get_snapshot(
                    task_id, item_id, att.result_snapshot_ref)
                if snap is not None:
                    entry["total_score"] = snap.total_score
            detail["attempts"].append(entry)

        # 稳定排序：attempt_number 升序
        detail["attempts"].sort(key=lambda a: a["attempt_number"])

        # ---- decisions（仅该 case 范围内） ----
        decisions = self._review_store.list_review_decisions(
            task_id, item_id, review_case_id=review_case_id)
        for dec in decisions:
            detail["decisions"].append({
                "decision_id": dec.decision_id,
                "decision_type": dec.decision_type,
                "requested_package_revision": dec.requested_package_revision,
                "decided_at": _iso(dec.decided_at),
                "decided_by": {"actor_type": dec.decided_by.actor_type},
            })
        # 稳定排序：decided_at 升序
        detail["decisions"].sort(key=lambda d: d["decided_at"] or "")

        # ---- active_adoption + manual_adjustment_context ----
        adoption = self._review_store.get_active_adoption(task_id, item_id)
        adjustment_snapshot = None
        if adoption is not None:
            # 绑定校验：adoption 必须与当前 case/submission/item 上下文一致
            if adoption.submission_id != case.submission_id:
                return None, "REVIEW_FACT_BINDING_MISMATCH"
            if adoption.review_case_id is not None and adoption.review_case_id != review_case_id:
                return None, "REVIEW_FACT_BINDING_MISMATCH"
            detail["active_adoption"] = {
                "adoption_id": adoption.adoption_id,
                "status": adoption.status,
                "snapshot_id": adoption.snapshot_id,
                "decided_at": _iso(adoption.decided_at),
            }
            adjustment_snapshot = self._attempt_store.get_snapshot(
                task_id, item_id, adoption.snapshot_id)
            context = detail["manual_adjustment_context"]
            context["attempt_id"] = adoption.attempt_id
            context["snapshot_id"] = adoption.snapshot_id
            if adjustment_snapshot is None:
                context["block_reason_code"] = "ADJUSTMENT_SNAPSHOT_UNAVAILABLE"
            elif adjustment_snapshot.submission_id != case.submission_id:
                return None, "REVIEW_FACT_BINDING_MISMATCH"
            elif adoption.attempt_id is not None and adjustment_snapshot.attempt_id != adoption.attempt_id:
                return None, "REVIEW_FACT_BINDING_MISMATCH"
            else:
                context["attempt_id"] = adjustment_snapshot.attempt_id
                context["current_total_score"] = adjustment_snapshot.total_score
                context["dimensions"] = [
                    {
                        "dimension_code": dim.dimension_code,
                        "current_score": dim.score,
                        "min_score": dim.min_score,
                        "max_score": dim.max_score,
                    }
                    for dim in adjustment_snapshot.dimension_scores
                ]
                if context["dimensions"]:
                    context["allowed"] = True
                    context["block_reason_code"] = None
                else:
                    context["block_reason_code"] = "ADJUSTMENT_DIMENSIONS_UNAVAILABLE"

        # ---- active_lock ----
        lock = self._review_store.get_active_final_lock(task_id, item_id)
        if lock is not None:
            # 绑定校验：lock 的 review_case_id 必须匹配
            if lock.review_case_id != review_case_id:
                return None, "REVIEW_FACT_BINDING_MISMATCH"
            detail["active_lock"] = {
                "lock_id": lock.lock_id,
                "locked_at": _iso(lock.locked_at),
                "reason_code": lock.reason_code,
            }
            # 锁定时保留只读分项，但禁止调整。
            detail["manual_adjustment_context"]["allowed"] = False
            detail["manual_adjustment_context"]["block_reason_code"] = (
                "MANUAL_ADJUSTMENT_BLOCKED_BY_LOCK")

        return detail, None


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
