"""
Flag 规则引擎 (P0-5)
=====================
4 硬规则 (MUST_REVIEW) + 3 软规则 (SUGGEST_REVIEW)

设计决策 (写入注释, 不再询问):
  Q1: 输入 ScoringResult 对象, 输出 List[FlagRecord]
  Q2: 评分耗时由 scorer 在 auto_score 主流程计时, 通过 result.elapsed_sec 传入
  Q3: FLAG_MODEL_DIVERGE 仅注册常量, 实际触发逻辑留给 P0-6 监控脚本
  Q4: WARN_TEMPLATE 用简单关键词列表 (P1-2 上线后再升级)
  Q5: 复用 MONITOR_GUARD 开关 (与 P0-6 共享)

  微调1: WARN_PERFECT_DIM 仅在 anchor_level != "Lv0" 时触发
         理由: Lv0 真代码满分合理, 误报会很多
  微调2: FLAG_TOO_FAST / WARN_FAST 仅在 llm_called=True 时触发
         理由: 缓存命中/Lv3 短路也会 elapsed<3s, 不算异常

异常防护:
  - 单条规则异常被吞 + logger.warning, 不影响其他规则, 也不影响主流程
  - MONITOR_GUARD=off 时 check() 直接返回 [], 不触发任何规则

阈值策略 (已与涛涛确认):
  - P0-5 用 YAML 默认值上线
  - P0-6 跑完真实分布后, 误报率>30% 或漏报率>20% 时回 P0-5 调阈值
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import List, Optional, Literal

from feature_flags import flags

logger = logging.getLogger(__name__)

# ---- 常量 ----

class FlagCode:
    """所有 Flag 常量统一在此, 便于 IDE 补全和单测 import."""
    ZERO_WITH_FILES     = "FLAG_ZERO_WITH_FILES"
    HIGH_SCORE_LOW_CONF = "FLAG_HIGH_SCORE_LOW_CONF"
    TOO_FAST            = "FLAG_TOO_FAST"
    MODEL_DIVERGE       = "FLAG_MODEL_DIVERGE"
    WARN_FAST           = "WARN_FAST"
    WARN_PERFECT_DIM    = "WARN_PERFECT_DIM"
    WARN_TEMPLATE       = "WARN_TEMPLATE"


FlagLevel = Literal["MUST_REVIEW", "SUGGEST_REVIEW"]

# 阈值常量集中, 便于 P0-6 反向调阈值
THRESH_TOO_FAST_SEC  = 3.0
THRESH_WARN_FAST_SEC = 8.0
THRESH_HIGH_SCORE    = 95.0

# 模板痕迹关键词 (来自 scorer.py 四个 _score_* 规则降级函数的固定句式)
TEMPLATE_KEYWORDS = ("规则评分", "基础分5分", "无明显亮点",
                     "基础分8分", "基础分4分")

# 各维度满分 (与 scorer.py / ScoringResult 对齐)
DIM_MAX = {
    "theme": 20,
    "presentation": 30,
    "process": 30,
    "ai_literacy": 20,
}

# 维度字段名映射 (ScoringResult 的属性名)
_DIM_ATTR = {
    "theme": "theme_score",
    "presentation": "presentation_score",
    "process": "process_score",
    "ai_literacy": "ai_literacy_score",
}


@dataclass(frozen=True)
class FlagRecord:
    """单条 Flag 记录, frozen=True 保证不可变."""
    code: str        # 例如 "FLAG_ZERO_WITH_FILES"
    level: FlagLevel # "MUST_REVIEW" 或 "SUGGEST_REVIEW"
    msg: str         # 人类可读说明, 含触发数值, 便于评委复核

    def to_dict(self) -> dict:
        return asdict(self)


class FlagEngine:
    """评分完成后异常打标. 无状态, 单例使用即可."""

    def check(self, result) -> List[FlagRecord]:
        """
        检查评分结果, 返回命中的 Flag 列表.

        MONITOR_GUARD=off 时返回空列表 (不触发任何规则).
        单条规则异常被吞 + logger.warning, 不影响其他规则.
        """
        # MONITOR_GUARD 开关短路 (P0-5 唯一入口)
        if not flags.MONITOR_GUARD:
            return []

        checkers = [
            self._check_zero_with_files,
            self._check_high_score_low_conf,
            self._check_too_fast,
            self._check_model_diverge,
            self._check_warn_fast,
            self._check_perfect_dim,
            self._check_template_comment,
        ]
        records: List[FlagRecord] = []
        for fn in checkers:
            try:
                rec = fn(result)
                if rec is not None:
                    records.append(rec)
            except Exception as e:
                # 单条规则异常不影响其他规则, 也不影响主流程
                logger.warning(f"[flag_engine] {fn.__name__} 异常: {e}")
        return records

    # ==================== 4 硬规则 (MUST_REVIEW) ====================

    def _check_zero_with_files(self, r) -> Optional[FlagRecord]:
        """score=0 且 confidence!=UNKNOWN → 有文件却拿 0 分, 必查."""
        if r.total_score == 0 and r.confidence != "UNKNOWN":
            return FlagRecord(
                FlagCode.ZERO_WITH_FILES, "MUST_REVIEW",
                f"score=0 但 confidence={r.confidence}, 文件齐全却拿 0 分"
            )
        return None

    def _check_high_score_low_conf(self, r) -> Optional[FlagRecord]:
        """score>=95 且 confidence!=HIGH → 低置信度高分, 必查."""
        if r.total_score >= THRESH_HIGH_SCORE and r.confidence != "HIGH":
            return FlagRecord(
                FlagCode.HIGH_SCORE_LOW_CONF, "MUST_REVIEW",
                f"score={r.total_score} 但 confidence={r.confidence}"
            )
        return None

    def _check_too_fast(self, r) -> Optional[FlagRecord]:
        """评分耗时<3s 且 LLM 真被调用 → 异常快速返回, 必查.
        微调2: llm_called=False 时不触发 (缓存/降级不算异常)."""
        if r.elapsed_sec < THRESH_TOO_FAST_SEC and r.llm_called:
            return FlagRecord(
                FlagCode.TOO_FAST, "MUST_REVIEW",
                f"评分耗时 {r.elapsed_sec:.2f}s < {THRESH_TOO_FAST_SEC}s, LLM 异常快速返回"
            )
        return None

    def _check_model_diverge(self, r) -> Optional[FlagRecord]:
        """同组 2 模型分差>25 → Q3 决策: P0-5 仅注册常量, 实际触发留给 P0-6."""
        # 该函数永远返回 None, 但 FlagCode.MODEL_DIVERGE 常量已定义供 P0-6 使用
        return None

    # ==================== 3 软规则 (SUGGEST_REVIEW) ====================

    def _check_warn_fast(self, r) -> Optional[FlagRecord]:
        """评分耗时<8s 且 LLM 真被调用 → 可能未走完完整流程, 建议复核.
        微调2: llm_called=False 时不触发."""
        if r.elapsed_sec < THRESH_WARN_FAST_SEC and r.llm_called:
            return FlagRecord(
                FlagCode.WARN_FAST, "SUGGEST_REVIEW",
                f"评分耗时 {r.elapsed_sec:.2f}s < {THRESH_WARN_FAST_SEC}s, 可能未走完完整流程"
            )
        return None

    def _check_perfect_dim(self, r) -> Optional[FlagRecord]:
        """单维度满分 → 建议复核.
        微调1: anchor_level='Lv0' 时不触发 (真代码满分合理).
        仅低置信度作品拿满分才可疑."""
        if getattr(r, "anchor_level", "Lv0") == "Lv0":
            return None
        perfect_dims = []
        for dim, max_score in DIM_MAX.items():
            attr = _DIM_ATTR[dim]
            score = getattr(r, attr, 0)
            if score >= max_score:
                perfect_dims.append(dim)
        if perfect_dims:
            return FlagRecord(
                FlagCode.WARN_PERFECT_DIM, "SUGGEST_REVIEW",
                f"低置信度作品(level={r.anchor_level})出现单维度满分: {perfect_dims}"
            )
        return None

    def _check_template_comment(self, r) -> Optional[FlagRecord]:
        """评语含模板痕迹 → 建议复核. Q4: 简单关键词匹配.
        关键词来源: scorer.py 四个 _score_* 规则降级函数的固定输出句式."""
        comments = [r.theme_comment, r.presentation_comment,
                    r.process_comment, r.ai_literacy_comment]
        hit_keywords: List[str] = []
        for kw in TEMPLATE_KEYWORDS:
            for c in comments:
                if c and kw in c:
                    hit_keywords.append(kw)
                    break   # 每个关键词只记一次
        if hit_keywords:
            return FlagRecord(
                FlagCode.WARN_TEMPLATE, "SUGGEST_REVIEW",
                f"评语含模板痕迹 (命中关键词: {sorted(set(hit_keywords))}), 可能为规则降级"
            )
        return None
