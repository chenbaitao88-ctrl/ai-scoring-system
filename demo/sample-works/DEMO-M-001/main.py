"""Synthetic entry data for the offline Accessible Route Assistant demo."""

from __future__ import annotations

from route_planner import PathSegment, explain_route, find_route


DEMO_SEGMENTS = (
    PathSegment("文化广场", "图书中心", distance=180, slope=2),
    PathSegment("图书中心", "社区展厅", distance=140, elevator=True),
    PathSegment("文化广场", "滨水步道", distance=210, slope=4),
    PathSegment("滨水步道", "社区展厅", distance=160, slope=2),
    PathSegment("文化广场", "旧街入口", distance=95, has_steps=True),
    PathSegment("旧街入口", "社区展厅", distance=120, slope=1),
    PathSegment("图书中心", "临时通道", distance=75, closed=True),
)


def list_places() -> list[str]:
    places = {segment.start for segment in DEMO_SEGMENTS}
    places.update(segment.end for segment in DEMO_SEGMENTS)
    return sorted(places)


def plan_demo_trip(start: str, destination: str) -> dict[str, object]:
    result = find_route(DEMO_SEGMENTS, start, destination)
    return {
        "start": start,
        "destination": destination,
        "route_status": result["status"],
        "route_cost": result.get("cost"),
        "route_nodes": result.get("nodes", []),
        "instructions": explain_route(result),
        "data_notice": "本结果仅使用虚构演示地图，不代表真实出行建议。",
    }


def demo_snapshot() -> dict[str, object]:
    return plan_demo_trip("文化广场", "社区展厅")
