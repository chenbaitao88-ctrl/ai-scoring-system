"""
公共证据身份核对（Phase 11E-2a-prerequisite-impl-2）。

从 11E-2a dry-run 抽取的无行为变化公共只读函数：dry-run Orchestrator 与
ScoringTaskCreator 共用同一套权威判断，避免复制出口径不同的逻辑。

返回稳定错误码（None=通过）。不读取正文/Key/证据内容。
"""
from __future__ import annotations

from typing import Optional

from models.pipeline_task import compute_item_input_fingerprint

_VALIDATION_STATUSES_ALLOWED = ("passed", "passed_with_warnings")

ERR_EVIDENCE_RECORD_MISSING = "DRY_RUN_EVIDENCE_RECORD_MISSING"
ERR_EVIDENCE_MISMATCH = "DRY_RUN_EVIDENCE_MISMATCH"
ERR_VALIDATION_MISSING = "DRY_RUN_VALIDATION_MISSING"
ERR_VALIDATION_FAILED = "DRY_RUN_VALIDATION_FAILED"


def check_evidence_identity(item, record) -> Optional[str]:
    """核对 EvidencePackageRecord（sidecar 登记事实）与 item 的绑定。

    返回 None=通过；否则返回稳定错误码。item 可为 PipelineItem 或同字段鸭子对象。
    """
    if record is None:
        return ERR_EVIDENCE_RECORD_MISSING
    expected_fp = compute_item_input_fingerprint(
        package_id=item.package_id,
        package_revision=item.package_revision,
        manifest_sha256=item.manifest_sha256,
        registration_record_id=item.registration_record_id,
        validation_id=item.validation_id,
    )
    if (
        record.package_id != item.package_id
        or record.package_revision != item.package_revision
        or record.manifest_sha256 != item.manifest_sha256
        or record.record_id != item.registration_record_id
        or record.latest_validation_id != item.validation_id
        or record.latest_validation_status not in _VALIDATION_STATUSES_ALLOWED
        or record.registration_status != "registered"
        or record.publication_status != "ready"
        or item.input_fingerprint != expected_fp
    ):
        return ERR_EVIDENCE_MISMATCH
    return None


def check_validation_binding(item, record, validation) -> Optional[str]:
    """核对 ValidationResult 全绑定（含 model_input_allowed 与 evidence_level 一致性）。

    返回 None=通过；否则返回稳定错误码。
    """
    if validation is None:
        return ERR_VALIDATION_MISSING
    if (
        validation.validation_id != item.validation_id
        or validation.record_id != record.record_id
        or validation.package_id != item.package_id
        or validation.package_revision != item.package_revision
        or validation.manifest_sha256 != item.manifest_sha256
        or validation.status not in _VALIDATION_STATUSES_ALLOWED
        or validation.model_input_allowed is not True
    ):
        return ERR_VALIDATION_FAILED
    if validation.evidence_level != item.evidence_level:
        return ERR_EVIDENCE_MISMATCH
    return None
