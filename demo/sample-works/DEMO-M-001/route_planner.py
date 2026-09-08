"""Weighted routing for a fully fictional accessibility map."""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush


@dataclass(frozen=True)
class PathSegment:
    start: str
    end: str
    distance: int
    slope: int = 0
    has_steps: bool = False
    elevator: bool = False
    closed: bool = False


def accessibility_cost(segment: PathSegment) -> int | None:
    if segment.closed or segment.has_steps:
        return None
    cost = segment.distance
    cost += max(segment.slope - 3, 0) * 25
    if segment.elevator:
        cost = max(1, cost - 20)
    return cost


def build_graph(segments: tuple[PathSegment, ...]) -> dict[str, list[tuple[str, int, PathSegment]]]:
    graph: dict[str, list[tuple[str, int, PathSegment]]] = {}
    for segment in segments:
        cost = accessibility_cost(segment)
        if cost is None:
            continue
        graph.setdefault(segment.start, []).append((segment.end, cost, segment))
        graph.setdefault(segment.end, []).append((segment.start, cost, segment))
    return graph


def find_route(
    segments: tuple[PathSegment, ...],
    start: str,
    destination: str,
) -> dict[str, object]:
    graph = build_graph(segments)
    if start not in graph or destination not in graph:
        return {"status": "unavailable", "message": "起点或终点暂无可用无障碍路径。"}

    queue: list[tuple[int, str, tuple[PathSegment, ...]]] = [(0, start, ())]
    best_cost = {start: 0}

    while queue:
        cost, node, path = heappop(queue)
        if node == destination:
            return {
                "status": "available",
                "cost": cost,
                "nodes": [start] + [segment.end if segment.start == current else segment.start for current, segment in _walk(start, path)],
                "segments": path,
            }
        if cost > best_cost.get(node, cost):
            continue
        for next_node, segment_cost, segment in graph.get(node, []):
            next_cost = cost + segment_cost
            if next_cost < best_cost.get(next_node, 10**9):
                best_cost[next_node] = next_cost
                heappush(queue, (next_cost, next_node, path + (_oriented(segment, node),)))

    return {"status": "unavailable", "message": "当前条件下没有连通的无障碍路线。"}


def _oriented(segment: PathSegment, current: str) -> PathSegment:
    if segment.start == current:
        return segment
    return PathSegment(
        start=segment.end,
        end=segment.start,
        distance=segment.distance,
        slope=segment.slope,
        has_steps=segment.has_steps,
        elevator=segment.elevator,
        closed=segment.closed,
    )


def _walk(start: str, path: tuple[PathSegment, ...]):
    current = start
    for segment in path:
        yield current, segment
        current = segment.end


def explain_route(result: dict[str, object]) -> list[str]:
    if result.get("status") != "available":
        return [str(result.get("message", "暂无路线说明。"))]
    notes = []
    for segment in result["segments"]:
        description = f"从{segment.start}到{segment.end}，约 {segment.distance} 米"
        if segment.elevator:
            description += "，可使用电梯"
        elif segment.slope:
            description += f"，坡度等级 {segment.slope}"
        notes.append(description + "。")
    return notes
