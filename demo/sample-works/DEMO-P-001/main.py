"""Synthetic source for the offline Campus Water Helper demo.

The demo initializer packages this file but never executes it. All values are
fictional and deliberately kept independent from real schools or students.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


SCENE_BASELINES = {
    "wash_area": 120,
    "drinking_area": 75,
    "garden_area": 160,
}

SCENE_LABELS = {
    "wash_area": "洗手区",
    "drinking_area": "饮水区",
    "garden_area": "绿化区",
}


@dataclass(frozen=True)
class WaterRecord:
    day: str
    scene: str
    liters: int


SAMPLE_RECORDS = (
    WaterRecord("周一", "wash_area", 116),
    WaterRecord("周一", "drinking_area", 72),
    WaterRecord("周一", "garden_area", 148),
    WaterRecord("周二", "wash_area", 132),
    WaterRecord("周二", "drinking_area", 70),
    WaterRecord("周二", "garden_area", 155),
    WaterRecord("周三", "wash_area", 110),
    WaterRecord("周三", "drinking_area", 68),
    WaterRecord("周三", "garden_area", 141),
)


def validate_record(record: WaterRecord) -> None:
    if record.scene not in SCENE_BASELINES:
        raise ValueError("unknown water scene")
    if record.liters < 0:
        raise ValueError("water usage cannot be negative")


def group_by_scene(records: tuple[WaterRecord, ...]) -> dict[str, list[int]]:
    grouped = {scene: [] for scene in SCENE_BASELINES}
    for record in records:
        validate_record(record)
        grouped[record.scene].append(record.liters)
    return grouped


def weekly_summary(records: tuple[WaterRecord, ...]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for scene, values in group_by_scene(records).items():
        total = sum(values)
        summary[scene] = {
            "total_liters": float(total),
            "average_liters": round(mean(values), 1) if values else 0.0,
            "baseline_liters": float(SCENE_BASELINES[scene]),
        }
    return summary


def find_overuse(records: tuple[WaterRecord, ...]) -> list[str]:
    alerts = []
    for record in records:
        baseline = SCENE_BASELINES[record.scene]
        if record.liters > baseline:
            alerts.append(
                f"{record.day}{SCENE_LABELS[record.scene]}用水 {record.liters} 升，"
                f"超过演示基线 {baseline} 升。"
            )
    return alerts


def build_suggestions(records: tuple[WaterRecord, ...]) -> list[str]:
    summary = weekly_summary(records)
    suggestions = []
    if summary["wash_area"]["average_liters"] > SCENE_BASELINES["wash_area"]:
        suggestions.append("检查洗手区水龙头是否及时关闭，并安排午间巡查。")
    else:
        suggestions.append("洗手区平均用水低于基线，继续保持随手关水。")

    if summary["garden_area"]["average_liters"] > 150:
        suggestions.append("绿化浇水可调整到清晨，减少蒸发造成的浪费。")
    else:
        suggestions.append("绿化区用水平稳，可继续记录天气后再调整浇水量。")
    return suggestions


def dashboard_snapshot(records: tuple[WaterRecord, ...] = SAMPLE_RECORDS) -> dict[str, object]:
    """Return the fictional values shown by the static preview."""
    return {
        "days_recorded": len({record.day for record in records}),
        "total_liters": sum(record.liters for record in records),
        "alerts": find_overuse(records),
        "suggestions": build_suggestions(records),
        "summary": weekly_summary(records),
    }
