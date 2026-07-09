"""Shared timestamped logger, used across the pipeline for a single consistent clock."""

from __future__ import annotations

import time

_START = time.monotonic()


def log(msg: str) -> None:
    """Print a flushed log line prefixed with wall-clock time and seconds since start.

    The ``+Ns`` elapsed counter makes it easy to see which step is eating the wall-clock.
    """
    elapsed = int(time.monotonic() - _START)
    print(f"[{time.strftime('%H:%M:%S')} +{elapsed:>4}s] {msg}", flush=True)
