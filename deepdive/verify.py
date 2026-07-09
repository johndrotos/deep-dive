"""Verify that curated links actually resolve; drop the genuinely-dead ones.

The research model sometimes writes plausible-but-fake URLs — hallucinated YouTube video
IDs, guessed article slugs. No prompt fully prevents this, so this pass is the code-level
enforcement of "every link is real": it fetches each item's URL and removes the ones that
are genuinely dead, while KEEPING links that merely block bots (403/429/etc.), since those
are usually real content that just refuses an automated fetch.

Three verdicts:
  live    — 2xx (and, for YouTube, oEmbed confirms the video is available)
  blocked — 401/403/429/202, timeouts, network errors → KEEP
  dead    — 404/410, 5xx, or a YouTube link oEmbed reports as unavailable → DROP
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple
from urllib.parse import quote

import httpx

from .models import ContentItem, DeepDive
from .runlog import log

_TIMEOUT = 10.0
# A real browser UA cuts down on false 403s from sites that block obvious bots.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_DEAD_STATUSES = {404, 410}


def _is_youtube(url: str) -> bool:
    return "youtube.com/watch" in url or "youtu.be/" in url


def _check_youtube(url: str) -> Tuple[str, str]:
    """Classify a YouTube link via the oEmbed endpoint.

    A watch page's HTML contains "video unavailable"/"removed" strings on *every* video
    (they're baked into the player JS), so scraping the body false-positives on live
    videos. oEmbed is a clean signal instead: 200 = playable, 404/401 = removed/private.
    """
    oembed = f"https://www.youtube.com/oembed?url={quote(url, safe='')}&format=json"
    try:
        with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            resp = client.get(oembed)
    except Exception as exc:  # noqa: BLE001 - network/timeout → keep, don't drop
        return "blocked", f"YouTube oembed error: {type(exc).__name__}"
    if resp.status_code == 200:
        return "live", "YouTube: available"
    if resp.status_code in (401, 404):
        return "dead", f"YouTube: unavailable (oembed {resp.status_code})"
    return "blocked", f"YouTube: oembed {resp.status_code}"


def check_url(url: str) -> Tuple[str, str]:
    """Fetch a URL and classify it. Returns (verdict, detail) — verdict in
    {'live', 'blocked', 'dead'}. Never raises; on error it returns 'blocked' so a
    transient network hiccup can't cause us to drop a real link."""
    if _is_youtube(url):
        return _check_youtube(url)
    try:
        with httpx.Client(
            follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}
        ) as client:
            resp = client.get(url)
    except Exception as exc:  # noqa: BLE001 - network/timeout → keep, don't drop
        return "blocked", f"request error: {type(exc).__name__}"

    code = resp.status_code
    if code in _DEAD_STATUSES or code >= 500:
        return "dead", f"HTTP {code}"
    if 200 <= code < 300:
        return "live", f"HTTP {code}"
    # 401 / 403 / 429 / 202 / anything else non-2xx we couldn't resolve → assume real.
    return "blocked", f"HTTP {code}"


def verify_dive(dive: DeepDive, label: str = "") -> DeepDive:
    """Check every item's URL concurrently and drop the genuinely-dead ones.

    Returns a new DeepDive with only the surviving items (order preserved). Bot-blocked
    and transient-error links are kept. Logs each drop and a per-topic summary.
    """
    items = dive.items
    if not items:
        return dive

    verdicts: List[Tuple[str, str]] = [("blocked", "unchecked")] * len(items)
    with ThreadPoolExecutor(max_workers=min(len(items), 8)) as pool:
        futures = {pool.submit(check_url, item.url): i for i, item in enumerate(items)}
        for future in futures:
            i = futures[future]
            try:
                verdicts[i] = future.result()
            except Exception as exc:  # noqa: BLE001 - keep on unexpected failure
                verdicts[i] = ("blocked", f"verify error: {type(exc).__name__}")

    kept: List[ContentItem] = []
    dropped = 0
    for item, (verdict, detail) in zip(items, verdicts):
        if verdict == "dead":
            dropped += 1
            log(f"  {label}   dropped dead link ({detail}): {item.url}")
        else:
            kept.append(item)

    live = sum(1 for v, _ in verdicts if v == "live")
    blocked = sum(1 for v, _ in verdicts if v == "blocked")
    log(
        f"  {label}   verified {len(items)} link(s): "
        f"{live} live, {blocked} blocked-but-kept, {dropped} dropped"
    )
    if len(kept) < 2:
        log(f"  {label}   WARNING: only {len(kept)} link(s) left after verification")

    return dive.model_copy(update={"items": kept})
