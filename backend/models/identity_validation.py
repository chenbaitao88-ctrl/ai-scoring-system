"""轻量模型校验工具（Phase 11E-2a-prerequisite-impl-2-fix-1）。

供 pipeline_task / scoring_configuration 共用，消除两者之间的运行期循环导入。
仅包含无依赖的纯校验函数（标识符 / UUID / SHA-256），不 import 任何业务模型。

用途边界：
- 只做格式校验，不读取环境变量、不触碰文件、不构造业务对象。
- pipeline_task / scoring_configuration 从本模块导入；其他模型继续使用各自既有校验。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ASCII_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def canonical_json_bytes(value: Any) -> bytes:
    """冻结规范化：UTF-8 无 BOM、键按 Unicode 升序、无多余空白、禁止 NaN/Infinity。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_canonical(value: Any) -> str:
    """对 canonical JSON 计算 SHA-256 小写十六进制。"""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _validate_sha256(value: str) -> str:
    """SHA-256：64 位小写十六进制。"""
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ValueError("SHA-256 必须为 64 位小写十六进制")
    return value


def _validate_uuid(value: str) -> str:
    """脱敏 UUID：拒绝任何非 UUID 形态的自由文本（含中文姓名/URL/路径/空白）。"""
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise ValueError("ID 必须为脱敏 UUID（8-4-4-4-12 小写十六进制）")
    return value


def _validate_ascii_id(value: str) -> str:
    """ASCII 安全标识符：仅 [A-Za-z0-9_-.]，拒绝中文姓名、URL、路径、空白和自由文本。"""
    if not isinstance(value, str) or not _ASCII_ID_RE.match(value):
        raise ValueError("标识符必须为 ASCII 安全标识符（仅字母数字 _ - .）")
    return value
