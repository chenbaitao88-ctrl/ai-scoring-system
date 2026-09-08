"""
锚点分层评分模块 (P0-4 简版)
===========================
判定依据 (P0-4):
  仅看 submitted source_files 的扩展名 (O(n) 扫一遍).
  P1-4 将升级为多信号融合 (AST/AIGC 质量/大小阈值/main 入口),
  那时启用独立的 ANCHOR_ALGO_V2 算法开关.

设计决策 (写进注释,不再询问):
  D1: Lv0 加 .html (简化版, 不做 <script> 检查; HTML+JS 也算真代码)
  D2: 接受 YAML 原数值 100/75/50/40, 实现用 min(raw, cap) 硬截断,
      不做 raw_score * weight 乘法缩放. 理由: 硬截断更直观, 回溯解释更清楚.
  D3: 保留开关命名 ANCHOR_V2 (与 YAML 一致, 5 个 ticket 共用此名).
  D4: _apply_anchor_cap 顺手填 machine_scores.confidence,
      映射 Lv0→HIGH / Lv1→MEDIUM / Lv2→LOW / Lv3→UNKNOWN
      (注意 Lv3 是 UNKNOWN 不是 LOW).
  D5: 注入 LLM prompt 1 句提示 + 4 行 level_desc 模板, 详细分层留 P1-3.

ANCHOR_V2 开关:
  在 3 个入口短路 (compute_anchor_level / apply_anchor_cap / build_level_hint),
  确保 OFF 时与 P0-4 上线前行为完全一致.

DB 字段 (已在 P0-3 准备好):
  works.anchor_level         VARCHAR(10) DEFAULT 'Lv0'
  machine_scores.confidence  VARCHAR(20) DEFAULT 'UNKNOWN'
"""
from __future__ import annotations

import logging
from typing import Dict, Tuple, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from feature_flags import flags
from database import Work

logger = logging.getLogger(__name__)

# ---- 常量 ----
ANCHOR_LEVELS = ("Lv0", "Lv1", "Lv2", "Lv3")

# Lv0: 真代码 (D1: 加 .html)
_LV0_EXTS = {".py", ".js", ".html", ".sb3", ".sb2"}
# Lv1: 二进制工程文件 (P0-2 解析器实现前无法读内容)
_LV1_EXTS = {".bcm4", ".bcm", ".mp", ".kitten"}
# Lv2: 代码描述/作品说明类
_LV2_EXTS = {".txt", ".md", ".doc", ".docx", ".pdf"}
# Lv3: 兜底, 不写白名单 (仅 .textClipping/纯链接/仅图片视频/空目录都归此)

# 维度上限 (D2: 各维度满分参照 scorer 现有维度: theme 20 / presentation 30 / process 30 / ai_literacy 20)
# 数值设计: 维度 cap 总和略大于 TOTAL_CAPS, 由 TOTAL_CAPS 兜底.
# Lv0 全部恒等 (不影响真代码作品).
LEVEL_CAPS: Dict[str, Dict[str, int]] = {
    "Lv0": {"theme": 20, "presentation": 30, "process": 30, "ai_literacy": 20},  # 100% 恒等
    "Lv1": {"theme": 18, "presentation": 22, "process": 22, "ai_literacy": 18},  # 削表现/过程
    "Lv2": {"theme": 14, "presentation": 14, "process": 14, "ai_literacy": 14},  # 进一步削
    "Lv3": {"theme": 10, "presentation": 10, "process": 10, "ai_literacy": 10},  # 重削表现/过程
}

# 总分上限 (D2: 严格按 YAML 100/75/50/40)
TOTAL_CAPS: Dict[str, int] = {"Lv0": 100, "Lv1": 75, "Lv2": 50, "Lv3": 40}

# 置信度映射 (D4: 注意 Lv3 是 UNKNOWN)
CONFIDENCE_MAP: Dict[str, str] = {
    "Lv0": "HIGH",
    "Lv1": "MEDIUM",
    "Lv2": "LOW",
    "Lv3": "UNKNOWN",
}

# 等级文字描述 (D5: 注入 LLM prompt)
LEVEL_DESC: Dict[str, str] = {
    "Lv0": "完整代码项目",
    "Lv1": "部分代码+辅助材料",
    "Lv2": "仅含代码描述/截图，缺少可执行源码",
    "Lv3": "仅含 AI 对话链接或文字片段，信息严重不足",
}


def _classify_by_exts(exts: set) -> str:
    """纯函数: 给一组扩展名, 返回锚点等级. 便于单测."""
    if exts & _LV0_EXTS:
        return "Lv0"
    if exts & _LV1_EXTS:
        return "Lv1"
    if exts & _LV2_EXTS:
        return "Lv2"
    return "Lv3"   # 兜底


# P1-4 (2026-05-26): 多信号融合 v2
_EXT_WEIGHTS = {
    ".py": 0.9, ".js": 0.9, ".html": 0.9,
    ".sb3": 0.8, ".sb2": 0.8,
    ".bcm4": 0.6, ".mp": 0.6, ".bcm": 0.6, ".kitten": 0.6,
    ".txt": 0.2, ".md": 0.2, ".textclipping": 0.2,
}

def _compute_level_v2(work) -> str:
    """多信号融合计算锚点等级 v2"""
    if work is None:
        return "Lv3"

    source_files = work.source_files or []
    signals = []

    # 信号1: 文件类型权重 (取最高)
    max_weight = 0.0
    for sf in source_files:
        ext = (sf.get("ext") or "").lower()
        max_weight = max(max_weight, _EXT_WEIGHTS.get(ext, 0.0))
    signals.append(max_weight)

    # 信号2: 代码行数
    code_lines = getattr(work, 'code_line_count', None) or 0
    if code_lines >= 500:
        signals.append(0.15)
    elif code_lines >= 100:
        signals.append(0.1)
    else:
        signals.append(0.0)

    # 信号3: AIGC证据数量
    aigc_files = work.aigc_log_files or []
    if len(aigc_files) >= 3:
        signals.append(0.1)
    else:
        signals.append(0.0)

    # 信号4: 文件结构完整性 (有主程序+资源)
    code_exts = {".py", ".js", ".html", ".sb3", ".sb2"}
    resource_exts = {".png", ".jpg", ".jpeg", ".gif", ".wav", ".mp3"}
    has_main = any((sf.get("ext") or "").lower() in code_exts for sf in source_files)
    has_resource = any((sf.get("ext") or "").lower() in resource_exts for sf in source_files)
    signals.append(0.1 if (has_main and has_resource) else 0.0)

    # 信号5: 解析深度 (有结构化项目文件)
    parsed_exts = {".bcm4", ".mp", ".sb3", ".sb2"}
    has_parsed = any((sf.get("ext") or "").lower() in parsed_exts for sf in source_files)
    signals.append(0.1 if has_parsed else 0.0)

    raw_score = sum(signals)
    if raw_score >= 0.9:
        return "Lv0"
    elif raw_score >= 0.6:
        return "Lv1"
    elif raw_score >= 0.3:
        return "Lv2"
    else:
        return "Lv3"


def compute_anchor_level(db: Session, work_id: int) -> str:
    """
    计算并持久化锚点等级.

    流程:
      1. ANCHOR_V2=off  → 直接返回 'Lv0', 不写库 (回退路径)
      2. 查 works.source_files (P0-1 已统一字段) → 扫扩展名 → 判定
      3. 写 works.anchor_level (每次评分前覆盖)
      4. commit 失败时降级 'Lv0' + logger.warning, 不抛异常 (绝不让主流程崩)

    异常处理 (硬性要求):
      - except 捕获范围: SQLAlchemyError (DB 层) + Exception (兜底)
      - 失败时降级 level='Lv0' 并 logger.warning, 不静默
      - 主流程绝不因为锚点 commit 失败而崩
    """
    # ANCHOR_V2 开关入口 #1
    if not flags.ANCHOR_V2:
        return "Lv0"

    work: Optional[Work] = None
    try:
        work = db.query(Work).filter(Work.id == work_id).first()
        if work is None:
            logger.warning(f"[anchor] work_id={work_id} 不存在, 降级 Lv0")
            return "Lv0"

        # 从 work.source_files JSON 字段提取扩展名 (P0-1 已统一字段格式)
        source_files = work.source_files or []
        exts: set = set()
        for sf in source_files:
            ext = (sf.get("ext") or "").lower() if isinstance(sf, dict) else ""
            if ext:
                exts.add(ext)

        if flags.ANCHOR_V2:
            level = _compute_level_v2(work)
        else:
            level = _classify_by_exts(exts)
    except SQLAlchemyError as e:
        logger.warning(f"[anchor] DB 查询失败 work_id={work_id}: {e}, 降级 Lv0")
        return "Lv0"
    except Exception as e:
        logger.warning(f"[anchor] 计算异常 work_id={work_id}: {e}, 降级 Lv0")
        return "Lv0"

    # 持久化 (commit 失败不阻断主流程)
    try:
        work.anchor_level = level
        db.commit()
    except SQLAlchemyError as e:
        logger.warning(f"[anchor] commit 失败 work_id={work_id} level={level}: {e}, 主流程继续")
        try:
            db.rollback()
        except Exception:
            pass
        # 即使写库失败也返回判定结果, 让本次评分用得上
        return level
    except Exception as e:
        logger.warning(f"[anchor] commit 未知异常 work_id={work_id}: {e}, 主流程继续")
        try:
            db.rollback()
        except Exception:
            pass
        return level

    return level


def apply_anchor_cap(
    dim_scores: Dict[str, float],
    level: str,
) -> Tuple[Dict[str, float], float, str]:
    """
    应用锚点等级硬截断 (D2: min(raw, cap), 无乘法缩放).

    输入:
      dim_scores: {"theme": float, "presentation": float,
                   "process": float, "ai_literacy": float}
      level: "Lv0"/"Lv1"/"Lv2"/"Lv3"

    返回: (capped_dims, capped_total, confidence)

    ANCHOR_V2=off 时直接返回原分 + confidence='UNKNOWN' (与 DB 默认值一致).
    """
    # ANCHOR_V2 开关入口 #2
    if not flags.ANCHOR_V2:
        return dim_scores, float(sum(dim_scores.values())), "UNKNOWN"

    caps = LEVEL_CAPS.get(level, LEVEL_CAPS["Lv0"])
    # Step 1: 维度硬截断 min(raw, cap)
    capped = {dim: min(float(score), caps.get(dim, score)) for dim, score in dim_scores.items()}
    # Step 2: 总分硬截断 min(sum, total_cap)
    total = min(float(sum(capped.values())), float(TOTAL_CAPS.get(level, 100)))
    # 置信度
    confidence = CONFIDENCE_MAP.get(level, "UNKNOWN")
    return capped, total, confidence


def build_level_hint(level: str) -> str:
    """
    生成 LLM prompt 前缀 (D5: 1 句提示 + 4 行 level_desc).

    ANCHOR_V2=off 时返回空字符串 (prompt 不注入, 行为同 P0-4 前).
    """
    # ANCHOR_V2 开关入口 #3
    if not flags.ANCHOR_V2:
        return ""

    desc = LEVEL_DESC.get(level, "")
    return (
        f"[评分提示] 本作品锚点等级为 {level}（{desc}），请根据可用信息保守评分。\n"
        f"Lv0: {LEVEL_DESC['Lv0']}\n"
        f"Lv1: {LEVEL_DESC['Lv1']}\n"
        f"Lv2: {LEVEL_DESC['Lv2']}\n"
        f"Lv3: {LEVEL_DESC['Lv3']}\n\n"
    )
