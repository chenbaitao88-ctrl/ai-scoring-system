"""Compatibility helpers for JSON columns that contain legacy encoded strings."""
from __future__ import annotations

import json
from typing import Any


def json_list(value: Any) -> list:
    """Return a list from current JSON arrays or up to two legacy encodings."""
    candidate = value
    for _ in range(2):
        if isinstance(candidate, list):
            return candidate
        if not isinstance(candidate, str):
            return []
        try:
            candidate = json.loads(candidate)
        except (TypeError, ValueError):
            return []
    return candidate if isinstance(candidate, list) else []
