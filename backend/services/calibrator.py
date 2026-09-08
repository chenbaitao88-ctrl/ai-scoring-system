"""
评分校准模块 (P1-6)
==================
基于金标准数据集的分段校准器。

支持两种模式：
1. 分段线性回归（按 anchor_level: Lv0/Lv1/Lv2/Lv3）
2. 分位数映射（np.interp，Plan B 兜底）

使用方式：
    from services.calibrator import get_calibrator
    cal = get_calibrator()
    calibrated = cal.adjust(raw_score, anchor_level)
"""
import json
import math
from pathlib import Path
from typing import Dict, List, Optional

# 从 config.py 获取 DATA_DIR，避免循环导入
import sys
_BACKEND_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BACKEND_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))
from config import DATA_DIR
sys.path.pop(0)


class Calibrator:
    """分段校准器"""

    PARAMS_PATH = DATA_DIR / "calibration_params.json"
    MIN_SAMPLES_PER_LEVEL = 3   # 单段最少样本数，低于此 fallback 全局

    def __init__(self):
        self.params: Dict[str, dict] = {}   # {level: {"offset": float, "scale": float, "r2": float}}
        self.method: str = "linear"         # "linear" or "quantile"
        self.quantile_pairs: Optional[tuple] = None  # (raw_scores, human_scores) for interp
        self._load()

    # ------------------------------------------------------------------ #
    # 拟合
    # ------------------------------------------------------------------ #
    def fit(self, gold_records: List[dict]) -> dict:
        """
        接收金标准记录列表，拟合校准参数。
        返回每段统计摘要 dict。
        """
        # 1. 清洗
        valid = [r for r in gold_records if r.get("ai_score") is not None]
        if not valid:
            raise ValueError("金标准数据中没有有效的 ai_score 记录")

        # 2. 全局回归（作为 fallback）
        global_params = self._fit_segment(valid)
        self.params["global"] = global_params

        # 3. 按 anchor_level 分段
        levels = ["Lv0", "Lv1", "Lv2", "Lv3"]
        summary = {}
        for level in levels:
            segment = [r for r in valid if r.get("anchor_level") == level]
            n = len(segment)
            if n >= self.MIN_SAMPLES_PER_LEVEL:
                p = self._fit_segment(segment)
                # 反直觉保护
                if p["scale"] < 0 or p["r2"] < 0:
                    p = dict(global_params)
                    p["is_fallback"] = True
                    p["fallback_reason"] = "scale<0 or r2<0"
                else:
                    p["is_fallback"] = False
                    p["fallback_reason"] = ""
                p["n"] = n
            else:
                p = dict(global_params)
                p["is_fallback"] = True
                p["fallback_reason"] = f"样本不足({n}<{self.MIN_SAMPLES_PER_LEVEL})"
                p["n"] = n
            self.params[level] = p
            summary[level] = {
                "offset": round(p["offset"], 4),
                "scale": round(p["scale"], 4),
                "r2": round(p.get("r2", 0), 4),
                "n": p["n"],
                "is_fallback": p["is_fallback"],
                "reason": p.get("fallback_reason", ""),
            }

        self.method = "linear"
        self.quantile_pairs = None
        self._save()
        return summary

    def fit_quantile(self, gold_records: List[dict]) -> dict:
        """
        Plan B：分位数映射（线性插值）。
        不依赖线性假设，直接用 np.interp 映射 raw->human。
        """
        import numpy as np

        valid = [r for r in gold_records if r.get("ai_score") is not None]
        if not valid:
            raise ValueError("金标准数据中没有有效的 ai_score 记录")

        valid.sort(key=lambda x: x["ai_score"])
        raw_scores = np.array([r["ai_score"] for r in valid], dtype=float)
        human_scores = np.array([r["human_score"] for r in valid], dtype=float)

        # 去重 raw_scores（np.interp 要求 x 单调递增，但可以有重复值）
        # 重复值取对应 human 的平均
        unique_raw = []
        unique_human = []
        i = 0
        while i < len(raw_scores):
            j = i
            while j < len(raw_scores) and raw_scores[j] == raw_scores[i]:
                j += 1
            unique_raw.append(float(raw_scores[i]))
            unique_human.append(float(np.mean(human_scores[i:j])))
            i = j

        self.quantile_pairs = (unique_raw, unique_human)
        self.method = "quantile"
        self.params = {
            "global": {"method": "quantile", "n": len(valid)},
            "Lv0": {"method": "quantile", "n": len(valid)},
            "Lv1": {"method": "quantile", "n": len(valid)},
            "Lv2": {"method": "quantile", "n": len(valid)},
            "Lv3": {"method": "quantile", "n": len(valid)},
        }
        self._save()
        return {"method": "quantile", "n": len(valid), "raw_range": (unique_raw[0], unique_raw[-1])}

    # ------------------------------------------------------------------ #
    # 校准应用
    # ------------------------------------------------------------------ #
    def adjust(self, raw_score: float, level: str = "Lv0") -> float:
        """
        将原始 AI 分映射为校准分。
        raw_score 为 float，level 为 Lv0/Lv1/Lv2/Lv3。
        结果 clamp 到 [0, 100]。
        """
        if raw_score is None:
            raw_score = 0.0
        raw_score = float(raw_score)

        if self.method == "quantile" and self.quantile_pairs is not None:
            import numpy as np
            raw_arr, human_arr = self.quantile_pairs
            # np.interp 对超出范围的值取端点值
            cal = float(np.interp(raw_score, raw_arr, human_arr))
            # 对于 raw > max(raw_arr) 的情况，端点值可能偏低（最后两点斜率为负），
            # 做一个保底外延：用最后两个点的斜率外推，但斜率下限为 0，避免高分被压死
            if raw_score > raw_arr[-1] and len(raw_arr) >= 2:
                slope = (human_arr[-1] - human_arr[-2]) / (raw_arr[-1] - raw_arr[-2]) if raw_arr[-1] != raw_arr[-2] else 0
                slope = max(0.0, slope)  # 斜率不能为负
                cal = human_arr[-1] + slope * (raw_score - raw_arr[-1])
            return max(0.0, min(100.0, cal))

        # linear fallback
        p = self.params.get(level) or self.params.get("global", {"offset": 0.0, "scale": 1.0})
        cal = p["offset"] + p["scale"] * raw_score
        return max(0.0, min(100.0, cal))

    # ------------------------------------------------------------------ #
    # 验证
    # ------------------------------------------------------------------ #
    def validate(self, gold_records: List[dict]) -> dict:
        """
        对金标准数据集进行回测，返回统计指标。
        """
        import numpy as np
        from scipy import stats

        valid = [r for r in gold_records if r.get("ai_score") is not None]
        if not valid:
            return {"error": "无有效记录"}

        raw_vals = []
        human_vals = []
        cal_vals = []
        diffs = []
        per_level = {}

        for r in valid:
            raw = float(r["ai_score"])
            human = float(r["human_score"])
            level = r.get("anchor_level", "Lv0")
            cal = self.adjust(raw, level)

            raw_vals.append(raw)
            human_vals.append(human)
            cal_vals.append(cal)
            diffs.append(abs(cal - human))

            per_level.setdefault(level, {"raw": [], "human": [], "cal": [], "diff": []})
            per_level[level]["raw"].append(raw)
            per_level[level]["human"].append(human)
            per_level[level]["cal"].append(cal)
            per_level[level]["diff"].append(abs(cal - human))

        raw_arr = np.array(raw_vals)
        human_arr = np.array(human_vals)
        cal_arr = np.array(cal_vals)
        diff_arr = np.array(diffs)

        # 原始分 vs 人工分
        raw_pearson = stats.pearsonr(raw_arr, human_arr)[0] if len(raw_arr) > 1 else 0.0
        raw_spearman = stats.spearmanr(raw_arr, human_arr)[0] if len(raw_arr) > 1 else 0.0

        # 校准分 vs 人工分
        cal_pearson = stats.pearsonr(cal_arr, human_arr)[0] if len(cal_arr) > 1 else 0.0
        cal_spearman = stats.spearmanr(cal_arr, human_arr)[0] if len(cal_arr) > 1 else 0.0

        # 分段统计
        level_stats = {}
        for level, d in per_level.items():
            if len(d["cal"]) > 1:
                r = stats.pearsonr(np.array(d["cal"]), np.array(d["human"]))[0]
            else:
                r = 0.0
            level_stats[level] = {
                "n": len(d["cal"]),
                "mae": round(float(np.mean(d["diff"])), 2),
                "rmse": round(float(np.sqrt(np.mean(np.square(np.array(d["cal"]) - np.array(d["human"]))))), 2),
                "pearson": round(float(r), 4) if not math.isnan(r) else 0.0,
                "raw_mean": round(float(np.mean(d["raw"])), 2),
                "human_mean": round(float(np.mean(d["human"])), 2),
                "cal_mean": round(float(np.mean(d["cal"])), 2),
            }

        return {
            "method": self.method,
            "n_total": len(valid),
            "raw": {
                "mean": round(float(np.mean(raw_arr)), 2),
                "std": round(float(np.std(raw_arr)), 2),
                "pearson": round(float(raw_pearson), 4) if not math.isnan(raw_pearson) else 0.0,
                "spearman": round(float(raw_spearman), 4) if not math.isnan(raw_spearman) else 0.0,
            },
            "calibrated": {
                "mean": round(float(np.mean(cal_arr)), 2),
                "std": round(float(np.std(cal_arr)), 2),
                "mae": round(float(np.mean(diff_arr)), 2),
                "rmse": round(float(np.sqrt(np.mean(np.square(cal_arr - human_arr)))), 2),
                "pearson": round(float(cal_pearson), 4) if not math.isnan(cal_pearson) else 0.0,
                "spearman": round(float(cal_spearman), 4) if not math.isnan(cal_spearman) else 0.0,
            },
            "by_level": level_stats,
            "records": [
                {
                    "short_code": r.get("short_code", ""),
                    "level": r.get("anchor_level", "Lv0"),
                    "raw": float(r["ai_score"]),
                    "human": float(r["human_score"]),
                    "calibrated": round(self.adjust(float(r["ai_score"]), r.get("anchor_level", "Lv0")), 2),
                    "diff": round(abs(self.adjust(float(r["ai_score"]), r.get("anchor_level", "Lv0")) - float(r["human_score"])), 2),
                }
                for r in valid
            ],
        }

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _fit_segment(self, records: List[dict]) -> dict:
        """对一组记录做最小二乘线性回归: human = offset + scale * ai"""
        import numpy as np

        x = np.array([r["ai_score"] for r in records], dtype=float)
        y = np.array([r["human_score"] for r in records], dtype=float)
        n = len(x)

        if n == 0:
            return {"offset": 0.0, "scale": 1.0, "r2": 0.0, "n": 0}
        if n == 1:
            # 单点：直接过原点斜率 y/x（避免除零）
            scale = y[0] / x[0] if x[0] != 0 else 1.0
            return {"offset": 0.0, "scale": float(scale), "r2": 0.0, "n": 1}

        # 最小二乘
        x_mean, y_mean = np.mean(x), np.mean(y)
        ss_xy = np.sum((x - x_mean) * (y - y_mean))
        ss_xx = np.sum((x - x_mean) ** 2)

        if ss_xx == 0:
            # x 全相同，无法做回归，直接用均值差
            return {"offset": float(y_mean - x_mean), "scale": 1.0, "r2": 0.0, "n": n}

        scale = ss_xy / ss_xx
        offset = y_mean - scale * x_mean

        # R²
        ss_tot = np.sum((y - y_mean) ** 2)
        ss_res = np.sum((y - (offset + scale * x)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot != 0 else 0.0

        return {
            "offset": float(offset),
            "scale": float(scale),
            "r2": float(r2),
            "n": n,
        }

    def _save(self):
        """持久化参数到 JSON"""
        payload = {
            "method": self.method,
            "params": self.params,
        }
        if self.quantile_pairs is not None:
            payload["quantile_pairs"] = {
                "raw": self.quantile_pairs[0],
                "human": self.quantile_pairs[1],
            }
        self.PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.PARAMS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load(self):
        """从 JSON 加载参数"""
        if not self.PARAMS_PATH.exists():
            return
        try:
            payload = json.loads(self.PARAMS_PATH.read_text(encoding="utf-8"))
            self.method = payload.get("method", "linear")
            self.params = payload.get("params", {})
            qp = payload.get("quantile_pairs")
            if qp:
                self.quantile_pairs = (qp["raw"], qp["human"])
        except Exception:
            self.params = {}
            self.method = "linear"
            self.quantile_pairs = None


# 全局单例（延迟初始化，首次 import 时加载已有参数）
_calibrator_instance: Optional[Calibrator] = None


def get_calibrator() -> Calibrator:
    """获取全局校准器实例"""
    global _calibrator_instance
    if _calibrator_instance is None:
        _calibrator_instance = Calibrator()
    return _calibrator_instance


def reset_calibrator():
    """重置全局实例（主要用于测试）"""
    global _calibrator_instance
    _calibrator_instance = None
