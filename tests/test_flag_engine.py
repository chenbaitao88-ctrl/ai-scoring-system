"""
Flag 引擎测试（7个用例）
"""
import pytest
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from services.flag_engine import FlagEngine, FlagRecord


def _make_result(**kwargs):
    """构造 FlagEngine.check() 需要的 mock 结果对象"""
    defaults = {
        "total_score": 0,
        "confidence": "UNKNOWN",
        "source_files": [],
        "aigc_log_files": [],
        "elapsed_sec": 60,
        "llm_called": True,
        "model_scores": {},
        "theme_score": 0,
        "presentation_score": 0,
        "process_score": 0,
        "ai_literacy_score": 0,
        "theme_comment": "",
        "presentation_comment": "",
        "process_comment": "",
        "ai_literacy_comment": "",
        "anchor_level": "Lv0",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.flag
class TestFlagEngine:

    def test_zero_score_with_files(self):
        """作品有文件但评分为0 -> MUST_REVIEW"""
        engine = FlagEngine()
        flags = engine.check(_make_result(
            total_score=0,
            confidence="HIGH",
            source_files=[{"ext": ".py"}],
            aigc_log_files=[],
        ))
        assert any(f.code == "FLAG_ZERO_WITH_FILES" for f in flags)

    def test_high_score_low_confidence(self):
        """高分(>=95)+低置信度 -> MUST_REVIEW"""
        engine = FlagEngine()
        flags = engine.check(_make_result(
            total_score=95,
            confidence="LOW",
            source_files=[{"ext": ".txt"}],
        ))
        assert any(f.code == "FLAG_HIGH_SCORE_LOW_CONF" for f in flags)

    def test_too_fast(self):
        """评分过快(<3s) + LLM被调用 -> MUST_REVIEW"""
        engine = FlagEngine()
        flags = engine.check(_make_result(
            elapsed_sec=2,  # 2秒
            llm_called=True,
            total_score=60,
        ))
        assert any(f.code == "FLAG_TOO_FAST" for f in flags)

    def test_no_flags_normal(self):
        """正常作品无 Flag"""
        engine = FlagEngine()
        flags = engine.check(_make_result(
            total_score=70,
            confidence="HIGH",
            source_files=[{"ext": ".py"}],
            elapsed_sec=60,
            llm_called=True,
        ))
        assert len(flags) == 0

    def test_model_diverge_not_implemented(self):
        """模型差异检查当前返回 None（留给 P0-6 实现）"""
        engine = FlagEngine()
        flags = engine.check(_make_result(
            total_score=50,
            model_scores={"qwen": 50, "kimi": 80},
        ))
        # _check_model_diverge 当前永远返回 None
        assert not any(f.code == "FLAG_MODEL_DIVERGE" for f in flags)

    def test_warn_fast(self):
        """评分较快(<8s) + LLM被调用 -> SUGGEST_REVIEW"""
        engine = FlagEngine()
        flags = engine.check(_make_result(
            elapsed_sec=5,
            llm_called=True,
            total_score=60,
        ))
        assert any(f.code == "WARN_FAST" for f in flags)

    def test_multiple_flags(self):
        """多个 Flag 同时触发：零分有文件 + 评分过快"""
        engine = FlagEngine()
        flags = engine.check(_make_result(
            total_score=0,
            source_files=[{"ext": ".py"}],
            confidence="LOW",
            elapsed_sec=2,
            llm_called=True,
        ))
        assert len(flags) >= 2
        codes = {f.code for f in flags}
        assert "FLAG_ZERO_WITH_FILES" in codes
        assert "FLAG_TOO_FAST" in codes
