"""Persistent memory of past topics so weeks never repeat.

Stored as a small JSON file at ``DATA_DIR/history.json``. On Railway, point
``DATA_DIR`` at a mounted Volume (e.g. ``/data``) so it survives redeploys.
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import List

_FILENAME = "history.json"
# How many past topic titles to feed back into the model when asking for fresh ones.
_LOOKBACK = 200


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, _FILENAME)


def load_entries(data_dir: str) -> List[dict]:
    """Return the raw history entries (each: {"date": str, "topics": [str, ...]})."""
    path = _path(data_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def past_topics(data_dir: str, limit: int = _LOOKBACK) -> List[str]:
    """Flattened list of recently covered topic titles, most recent last."""
    topics: List[str] = []
    for entry in load_entries(data_dir):
        for topic in entry.get("topics", []):
            if isinstance(topic, str):
                topics.append(topic)
    return topics[-limit:]


def record(data_dir: str, topics: List[str]) -> None:
    """Append this issue's topics to history, creating the data dir if needed."""
    os.makedirs(data_dir, exist_ok=True)
    entries = load_entries(data_dir)
    entries.append({"date": date.today().isoformat(), "topics": topics})
    with open(_path(data_dir), "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False)
