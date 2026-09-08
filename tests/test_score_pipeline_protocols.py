"""
Phase 11E-1b：评分流水线只读协议合成测试。

只验证协议形状（只读方法、无写方法、无敏感返回）；协议实现于后续切片。
"""
from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from services.score_pipeline_protocols import (
    AttemptValidationLookup,
    ProviderIdempotencyCapabilityLookup,
    ReviewCaseLookup,
    ScoreAttemptLookup,
    SuccessfulResultSnapshotLookup,
)


@pytest.mark.parametrize("proto", [
    ScoreAttemptLookup,
    SuccessfulResultSnapshotLookup,
    AttemptValidationLookup,
    ProviderIdempotencyCapabilityLookup,
    ReviewCaseLookup,
])
def test_protocol_only_readonly_methods(proto):
    """Protocol 只有只读方法（get/list/has/is），无 create/update/delete/write。"""
    methods = [n for n, m in inspect.getmembers(proto, inspect.isfunction) if not n.startswith("_")]
    assert methods, proto.__name__
    for name in methods:
        assert name.startswith(("get_", "list_", "has_", "is_")), (proto.__name__, name)
        assert not name.startswith(("create_", "update_", "delete_", "write_", "save_"))


@pytest.mark.parametrize("proto", [
    ScoreAttemptLookup,
    SuccessfulResultSnapshotLookup,
    AttemptValidationLookup,
    ProviderIdempotencyCapabilityLookup,
    ReviewCaseLookup,
])
def test_protocol_return_types(proto):
    """每个方法都有返回类型注解（只读语义：返回 Optional 或 List）。"""
    for name in [n for n, m in inspect.getmembers(proto, inspect.isfunction) if not n.startswith("_")]:
        hints = get_type_hints(getattr(proto, name))
        assert "return" in hints, (proto.__name__, name)
        ret = hints["return"]
        ret_name = getattr(ret, "_name", None) or getattr(ret, "__name__", str(ret))
        assert "List" in str(ret) or "Optional" in str(ret) or "bool" in str(ret), (proto.__name__, name, ret)


def test_protocol_no_sensitive_returns():
    """协议方法签名不含正文/Key/endpoint 相关参数或返回。"""
    for proto in (ScoreAttemptLookup, SuccessfulResultSnapshotLookup, AttemptValidationLookup,
                  ProviderIdempotencyCapabilityLookup, ReviewCaseLookup):
        src = inspect.getsource(proto)
        for bad in ("api_key", "secret", "token", "endpoint", "prompt_text", "response_body"):
            assert bad not in src.lower(), (proto.__name__, bad)


def test_protocol_module_no_env_access():
    """协议模块不读取环境变量（源码零 os.getenv/environ）。"""
    import services.score_pipeline_protocols as mod
    src = open(mod.__file__, encoding="utf-8").read()
    assert "os.getenv" not in src
    assert "environ" not in src
