"""
Provider 切换服务（Phase 11D-3b）：切换申请、人工批准、审计持久化。

- 权威查询全部注入式只读协议（ProviderAttemptLookup / SuccessfulResultLookup），调用方不得传入
  successful_result_exists、任意 same_*、source error 五维策略或 target_attempt_id。
- 九项 same_* 由服务比较 source ProviderRequest 与 ProviderSwitchTargetProposal 后产生。
- 状态机：pending_approval -> approved/rejected/cancelled；approved -> executed/cancelled。
- 不调用 Gateway/Transport，不启动目标 Provider；executed 只表示决定已交付 Phase 11E。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Callable, List, Literal, Optional, Protocol, Tuple

from models.provider_switch import (
    ProviderSwitchRecord,
    ProviderSwitchTargetProposal,
    SwitchEvent,
)
from models.scoring_provider import (
    CONTRACT_VERSION,
    ModelCapability,
    ProviderError,
    ProviderRequest,
    ProviderSwitchDecision,
    ScoringProvider,
)
from services.provider_switch_store import (
    ERR_BINDING_MISMATCH,
    ERR_IDEMPOTENCY_CONFLICT,
    ERR_INVALID_STATE,
    ERR_NOT_FOUND,
    ERR_REVISION_CONFLICT,
    ERR_SAME_PROVIDER,
    ERR_SCORING_IDENTITY_MISMATCH,
    ERR_SOURCE_ERROR_NOT_ALLOWED,
    ERR_SOURCE_ERROR_NOT_FOUND,
    ERR_SOURCE_REQUEST_NOT_FOUND,
    ERR_SUCCESS_EXISTS,
    ERR_TARGET_CAPABILITY_MISMATCH,
    ERR_TARGET_DISABLED,
    ERR_TARGET_NOT_FOUND,
    ERR_TRANSACTION_ERROR,
    ProviderSwitchStore,
    SwitchStoreError,
)
from services.scoring_provider_registry import ProviderRegistryError, ScoringProviderRegistry

_SAFE_ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ProviderAttemptLookup(Protocol):
    def get_source_request(self, task_id: str, item_id: str, attempt_id: str) -> Optional[ProviderRequest]:
        ...

    def get_source_error(self, error_id: str) -> Optional[ProviderError]:
        ...


class SuccessfulResultLookup(Protocol):
    def has_successful_result(self, task_id: str, item_id: str, attempt_id: str) -> bool:
        ...


class ProviderSwitchService:
    """切换申请与批准服务。"""

    def __init__(
        self,
        store: ProviderSwitchStore,
        registry: ScoringProviderRegistry,
        attempt_lookup: ProviderAttemptLookup,
        success_lookup: SuccessfulResultLookup,
        clock: Optional[Callable[[], datetime]] = None,
        uuid_factory: Optional[Callable[[], str]] = None,
    ):
        self._store = store
        self._registry = registry
        self._attempt_lookup = attempt_lookup
        self._success_lookup = success_lookup
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._uuid = uuid_factory or (lambda: f"switch-{datetime.now(timezone.utc).timestamp()}")

    # ---------------- 创建 ---------------- #

    def create_switch_request(
        self,
        task_id: str,
        item_id: str,
        proposal: ProviderSwitchTargetProposal,
        source_attempt_id: str,
        reason_error_id: str,
    ) -> Tuple[ProviderSwitchDecision, bool]:
        """创建 pending_approval 切换申请；返回 (decision, created)。重复申请幂等命中返回 (原 decision, False)。"""
        now = self._clock()
        # 1. source request
        request = self._attempt_lookup.get_source_request(task_id, item_id, source_attempt_id)
        if request is None:
            raise SwitchStoreError(ERR_SOURCE_REQUEST_NOT_FOUND)
        # 2. reason error
        error = self._attempt_lookup.get_source_error(reason_error_id)
        if error is None:
            raise SwitchStoreError(ERR_SOURCE_ERROR_NOT_FOUND)
        # 3. 绑定核对
        if (
            error.request_id != request.request_id
            or error.task_id != task_id
            or error.item_id != item_id
            or error.attempt_id != source_attempt_id
            or error.provider_id != request.provider_id
            or error.model_id != request.model_id
        ):
            raise SwitchStoreError(ERR_BINDING_MISMATCH)
        # 4. 错误允许申请切换
        if error.provider_switch_allowed == "no":
            raise SwitchStoreError(ERR_SOURCE_ERROR_NOT_ALLOWED)
        # 5. 成功结果检查
        if self._success_lookup.has_successful_result(task_id, item_id, source_attempt_id):
            raise SwitchStoreError(ERR_SUCCESS_EXISTS)
        # 6-8. target 注册表与能力
        target_provider, target_cap = self._resolve_target(proposal, request)
        # 9. 同口径比较（九项）
        same = self.evaluate_same_policy(request, proposal)
        if not all(same.values()):
            raise SwitchStoreError(ERR_SCORING_IDENTITY_MISMATCH)
        # 10. 幂等键（先查后建：同 key 命中返回原 decision）
        idem_key = self._idempotency_key(task_id, item_id, source_attempt_id, proposal)
        existing = self._store.find_by_idempotency(task_id, item_id, idem_key)
        if existing is not None:
            return self._read_decision(task_id, item_id, existing), False
        # 11. 构造 pending decision
        decision = ProviderSwitchDecision(
            contract_version=CONTRACT_VERSION,
            switch_decision_id=self._uuid(),
            task_id=task_id,
            item_id=item_id,
            source_attempt_id=source_attempt_id,
            target_attempt_id=None,
            source_provider_id=request.provider_id,
            source_model_id=request.model_id,
            target_provider_id=proposal.target_provider_id,
            target_model_id=proposal.target_model_id,
            reason_error_id=reason_error_id,
            reason_error_code=error.error_code,
            decision_status="pending_approval",
            same_scoring_policy=same["same_scoring_policy"],
            same_prompt_version=same["same_prompt_version"],
            same_evidence_manifest_sha256=same["same_evidence_manifest_sha256"],
            same_input_fingerprint=same["same_input_fingerprint"],
            same_response_schema_version=same["same_response_schema_version"],
            same_scoring_mode=same["same_scoring_mode"],
            same_temperature=same["same_temperature"],
            same_seed=same["same_seed"],
            same_max_output_tokens=same["same_max_output_tokens"],
            successful_result_exists=False,
            approval_required=True,
            approved_by=None,
            approved_at=None,
            created_at=now,
            executed_at=None,
            event_ref=None,
        )
        record = ProviderSwitchRecord(
            contract_version=CONTRACT_VERSION,
            revision=1,
            decision=decision,
            proposal=proposal,
            idempotency_key=idem_key,
            transition_reason_code=None,
            created_at=now,
            updated_at=now,
            event_sequence=1,
        )
        # 12. 原子持久化
        outcome = self._store.create(record)
        if outcome == "created":
            return decision, True
        if outcome == "idempotent_hit":
            return self._read_decision(task_id, item_id, decision.switch_decision_id), False
        raise SwitchStoreError(ERR_IDEMPOTENCY_CONFLICT)

    # ---------------- 批准/拒绝/取消/执行 ---------------- #

    def approve_switch(
        self,
        task_id: str,
        item_id: str,
        decision_id: str,
        approved_by: str,
        expected_revision: int,
    ) -> ProviderSwitchDecision:
        """批准：pending_approval -> approved；重新复核成功结果/注册表/口径；生成新 target_attempt_id。"""
        if not _SAFE_ROLE_RE.fullmatch(approved_by):
            raise SwitchStoreError(ERR_INVALID_STATE, message_key="unsafe approved_by")
        now = self._clock()
        record = self._load_record(task_id, item_id, decision_id)
        decision = record.decision
        # 幂等：已 approved 且批准人一致
        if decision.decision_status == "approved" and decision.approved_by == approved_by:
            return decision
        if decision.decision_status != "pending_approval":
            raise SwitchStoreError(ERR_INVALID_STATE)
        self._recheck(task_id, item_id, record)
        target_attempt_id = self._uuid()
        if target_attempt_id == decision.source_attempt_id:
            raise SwitchStoreError(ERR_TRANSACTION_ERROR, message_key="target attempt collision")
        new_decision = decision.model_copy(update={
            "decision_status": "approved",
            "target_attempt_id": target_attempt_id,
            "approved_by": approved_by,
            "approved_at": now,
        })
        return self._commit(task_id, item_id, record, new_decision, expected_revision,
                            transition="approved", reason_code=None, approved_by=approved_by)

    def reject_switch(
        self,
        task_id: str,
        item_id: str,
        decision_id: str,
        reason_code: str,
        expected_revision: int,
    ) -> ProviderSwitchDecision:
        """拒绝：pending_approval -> rejected；只接受稳定 reason_code；不生成 target attempt。"""
        if not _SAFE_REASON_RE.fullmatch(reason_code):
            raise SwitchStoreError(ERR_INVALID_STATE, message_key="unsafe reason_code")
        record = self._load_record(task_id, item_id, decision_id)
        decision = record.decision
        if decision.decision_status == "rejected":
            return decision  # 幂等
        if decision.decision_status != "pending_approval":
            raise SwitchStoreError(ERR_INVALID_STATE)
        new_decision = decision.model_copy(update={"decision_status": "rejected"})
        return self._commit(task_id, item_id, record, new_decision, expected_revision,
                            transition="rejected", reason_code=reason_code)

    def cancel_switch(
        self,
        task_id: str,
        item_id: str,
        decision_id: str,
        expected_revision: int,
    ) -> ProviderSwitchDecision:
        """取消：pending_approval/approved -> cancelled；approved 取消保留 target_attempt_id 审计。"""
        record = self._load_record(task_id, item_id, decision_id)
        decision = record.decision
        if decision.decision_status == "cancelled":
            return decision  # 幂等
        if decision.decision_status not in ("pending_approval", "approved"):
            raise SwitchStoreError(ERR_INVALID_STATE)
        new_decision = decision.model_copy(update={"decision_status": "cancelled"})
        return self._commit(task_id, item_id, record, new_decision, expected_revision,
                            transition="cancelled", reason_code="CANCELLED")

    def mark_executed(
        self,
        task_id: str,
        item_id: str,
        decision_id: str,
        expected_revision: int,
    ) -> ProviderSwitchDecision:
        """执行交付：approved -> executed；写 executed_at；只表示已交付 Phase 11E。"""
        now = self._clock()
        record = self._load_record(task_id, item_id, decision_id)
        decision = record.decision
        if decision.decision_status == "executed":
            return decision  # 幂等
        if decision.decision_status != "approved":
            raise SwitchStoreError(ERR_INVALID_STATE)
        if decision.target_attempt_id is None:
            raise SwitchStoreError(ERR_INVALID_STATE, message_key="executed requires target attempt")
        # 再次确认无成功结果保护冲突
        if self._success_lookup.has_successful_result(task_id, item_id, decision.source_attempt_id):
            raise SwitchStoreError(ERR_SUCCESS_EXISTS)
        new_decision = decision.model_copy(update={"decision_status": "executed", "executed_at": now})
        return self._commit(task_id, item_id, record, new_decision, expected_revision,
                            transition="executed", reason_code="EXECUTED")

    # ---------------- 查询 ---------------- #

    def get_switch_decision(self, task_id: str, item_id: str, decision_id: str) -> Optional[ProviderSwitchDecision]:
        record = self._store.read(task_id, item_id, decision_id)
        return record.decision if record else None

    def list_switch_decisions(
        self, task_id: str, item_id: Optional[str] = None, offset: int = 0, limit: int = 50,
    ) -> List[ProviderSwitchDecision]:
        records = self._store.list(task_id, item_id=item_id, offset=offset, limit=limit)
        return [r.decision for r in records]

    # ---------------- 权威比较 ---------------- #

    def evaluate_same_policy(
        self, source: ProviderRequest, proposal: ProviderSwitchTargetProposal,
    ) -> dict:
        """服务比较 source ProviderRequest 与 Proposal，产生九项 same_*（调用方不得传入）。"""
        return {
            "same_scoring_policy": source.scoring_policy_version == proposal.scoring_policy_version,
            "same_prompt_version": source.prompt_version == proposal.prompt_version,
            "same_evidence_manifest_sha256": source.evidence_manifest_sha256 == proposal.evidence_manifest_sha256,
            "same_input_fingerprint": source.input_fingerprint == proposal.input_fingerprint,
            "same_response_schema_version": source.response_schema_version == proposal.response_schema_version,
            "same_scoring_mode": source.scoring_mode == proposal.scoring_mode,
            "same_temperature": source.temperature == proposal.temperature,
            "same_seed": source.seed == proposal.seed,
            "same_max_output_tokens": source.max_output_tokens == proposal.max_output_tokens,
        }

    # ---------------- 内部 ---------------- #

    def _resolve_target(
        self, proposal: ProviderSwitchTargetProposal, request: ProviderRequest,
    ) -> Tuple[ScoringProvider, ModelCapability]:
        """target 注册表解析 + enabled/retired + 能力匹配（按 source request 声明）。"""
        if proposal.target_provider_id == request.provider_id and proposal.target_model_id == request.model_id:
            raise SwitchStoreError(ERR_SAME_PROVIDER)
        try:
            provider, capability = self._registry.resolve_provider_capability(
                proposal.target_provider_id, proposal.target_model_id,
            )
        except ProviderRegistryError as exc:
            raise SwitchStoreError(ERR_TARGET_NOT_FOUND, message_key=exc.message_key) from exc
        if not provider.enabled or provider.deprecation_status == "retired":
            raise SwitchStoreError(ERR_TARGET_DISABLED)
        if not self._capability_matches(request, capability):
            raise SwitchStoreError(ERR_TARGET_CAPABILITY_MISMATCH)
        return provider, capability

    @staticmethod
    def _capability_matches(request: ProviderRequest, capability: ModelCapability) -> bool:
        if "text" in request.input_modalities and capability.text_input is not True:
            return False
        if "image" in request.input_modalities and capability.image_input is not True:
            return False
        if capability.structured_json_output is not True:
            return False
        if capability.system_message is not True:
            return False
        if request.temperature is not None and capability.temperature_supported is not True:
            return False
        if request.seed is not None and capability.seed_supported is not True:
            return False
        if request.max_output_tokens is not None and capability.max_output_tokens is not None:
            if request.max_output_tokens > capability.max_output_tokens:
                return False
        return True

    def _recheck(self, task_id: str, item_id: str, record: ProviderSwitchRecord) -> None:
        """approve 前复核：成功结果 / 注册表与口径（防御性，权威数据不可变）。"""
        if self._success_lookup.has_successful_result(task_id, item_id, record.decision.source_attempt_id):
            raise SwitchStoreError(ERR_SUCCESS_EXISTS)
        request = self._attempt_lookup.get_source_request(task_id, item_id, record.decision.source_attempt_id)
        if request is None:
            raise SwitchStoreError(ERR_SOURCE_REQUEST_NOT_FOUND)
        self._resolve_target(record.proposal, request)
        if not all(self.evaluate_same_policy(request, record.proposal).values()):
            raise SwitchStoreError(ERR_SCORING_IDENTITY_MISMATCH)

    def _commit(
        self,
        task_id: str,
        item_id: str,
        record: ProviderSwitchRecord,
        new_decision: ProviderSwitchDecision,
        expected_revision: int,
        transition: str,
        reason_code: Optional[str],
        approved_by: Optional[str] = None,
    ) -> ProviderSwitchDecision:
        now = self._clock()
        new_record = ProviderSwitchRecord(
            contract_version=CONTRACT_VERSION,
            revision=record.revision + 1,
            decision=new_decision,
            proposal=record.proposal,
            idempotency_key=record.idempotency_key,
            transition_reason_code=reason_code,
            created_at=record.created_at,
            updated_at=now,
            event_sequence=record.event_sequence + 1,
        )
        event = SwitchEvent(
            sequence=record.event_sequence + 1,
            decision_id=record.decision.switch_decision_id,
            transition=transition,
            from_status=record.decision.decision_status,
            to_status=new_decision.decision_status,
            revision_before=record.revision,
            revision_after=new_record.revision,
            occurred_at=now,
            reason_code=reason_code,
            approved_by=approved_by,
        )
        try:
            self._store.update(
                decision_id=record.decision.switch_decision_id,
                task_id=task_id,
                item_id=item_id,
                new_record=new_record,
                expected_revision=expected_revision,
                event=event,
            )
        except SwitchStoreError:
            raise
        except Exception as exc:
            raise SwitchStoreError(ERR_TRANSACTION_ERROR) from exc
        return new_decision

    def _load_record(self, task_id: str, item_id: str, decision_id: str) -> ProviderSwitchRecord:
        record = self._store.read(task_id, item_id, decision_id)
        if record is None:
            raise SwitchStoreError(ERR_NOT_FOUND)
        return record

    def _read_decision(self, task_id: str, item_id: str, decision_id: str) -> ProviderSwitchDecision:
        record = self._store.read(task_id, item_id, decision_id)
        if record is None:
            raise SwitchStoreError(ERR_NOT_FOUND)
        return record.decision

    @staticmethod
    def _idempotency_key(task_id: str, item_id: str, source_attempt_id: str,
                         proposal: ProviderSwitchTargetProposal) -> str:
        canonical = json.dumps({
            "task_id": task_id, "item_id": item_id, "source_attempt_id": source_attempt_id,
            "target_provider_id": proposal.target_provider_id,
            "target_model_id": proposal.target_model_id,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
