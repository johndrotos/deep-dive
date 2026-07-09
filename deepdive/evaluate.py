"""Score a curated issue on objective + judged quality metrics, for data-driven tuning.

Two tiers of metric:
  - Objective: computed in code (link liveness, link specificity, format diversity, time
    budget, item count). Free, reproducible, unbiased.
  - Judged: an LLM (Opus by default) scores each topic 1-5 against a rubric — a stronger
    model than the Sonnet that wrote the issue, so it isn't grading its own work.

Runs on a saved issue JSON (written by `main._build`), prints a scorecard, and saves it
with the generating settings recorded — so you accumulate a settings→scores history and
can see whether a change actually moved the numbers.

    python -m deepdive.evaluate [out/issue.json]
"""

from __future__ import annotations

import json
import os
import time
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from urllib.parse import urlparse

import anthropic
from pydantic import BaseModel, Field

from .config import Config
from .models import DeepDive, Newsletter
from .verify import check_url

_DEFAULT_ISSUE = os.path.join("out", "issue.json")
_TIME_TARGET_MIN = 60   # a topic should be ~1-2 hours of content
_TIME_TARGET_MAX = 180


# --- Objective metrics -------------------------------------------------------


def parse_minutes(text: str) -> Optional[float]:
    """Best-effort minutes from a duration string ('52 min', '1.5 hr', '25 min read')."""
    if not text:
        return None
    s = text.lower()
    total = 0.0
    found = False
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:hours|hour|hrs|hr|h)\b", s):
        total += float(m.group(1)) * 60
        found = True
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:minutes|minute|mins|min|m)\b", s):
        total += float(m.group(1))
        found = True
    return round(total) if found else None


def is_specific(url: str) -> bool:
    """False for bare homepages, search-results pages, and channel/handle roots."""
    low = url.lower()
    if "youtube.com/results" in low or "/search" in low:
        return False
    if any(x in low for x in (
        "youtube.com/@", "youtube.com/c/", "youtube.com/channel/", "youtube.com/user/",
    )):
        return False
    path = (urlparse(url).path or "").rstrip("/")
    return path not in ("", "/")


def _link_liveness(urls: List[str]) -> float:
    """Fraction of URLs that resolve (reuses verify.check_url; blocked-but-real counts)."""
    if not urls:
        return 1.0
    with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as pool:
        verdicts = list(pool.map(lambda u: check_url(u)[0], urls))
    return sum(1 for v in verdicts if v != "dead") / len(urls)


def objective_metrics(newsletter: Newsletter, target_items: int) -> dict:
    all_urls = [it.url for d in newsletter.deep_dives for it in d.items]
    per_topic = []
    for d in newsletter.deep_dives:
        kinds = [it.kind.strip().lower() for it in d.items]
        known = [m for m in (parse_minutes(it.duration) for it in d.items) if m is not None]
        total_min = sum(known) if known else None
        specific = sum(1 for it in d.items if is_specific(it.url))
        per_topic.append({
            "title": d.title,
            "item_count": len(d.items),
            "at_item_target": len(d.items) >= target_items,
            "distinct_formats": len(set(kinds)),
            "total_minutes": total_min,
            "time_on_target": total_min is not None and _TIME_TARGET_MIN <= total_min <= _TIME_TARGET_MAX,
            "specific_links": specific,
            "links": len(d.items),
        })
    n = len(per_topic) or 1
    link_total = sum(t["links"] for t in per_topic)
    return {
        "link_liveness": _link_liveness(all_urls),
        "link_specificity": (sum(t["specific_links"] for t in per_topic) / link_total) if link_total else 1.0,
        "avg_distinct_formats": sum(t["distinct_formats"] for t in per_topic) / n,
        "frac_topics_time_on_target": sum(1 for t in per_topic if t["time_on_target"]) / n,
        "frac_topics_at_item_target": sum(1 for t in per_topic if t["at_item_target"]) / n,
        "per_topic": per_topic,
    }


# --- Judged metrics ----------------------------------------------------------


_JUDGE_SYSTEM = """You are a discerning magazine editor evaluating ONE topic from a curated \
"Deep Dive" newsletter, which sends a reader ~1-2 hours of the best existing content on a \
niche subject. Score honestly and critically on a 0-10 integer scale:
  0-2  = poor or broken
  3-4  = weak
  5-6  = solid and competent (the default for decent work)
  7-8  = strong
  9-10 = exceptional
Calibrate carefully and use the full range — reserve 9-10 for genuinely outstanding work, \
and don't inflate. The finer scale exists so you can score consistently: if quality sits \
between two levels, pick the one it's closest to rather than defaulting high."""


class _TopicScores(BaseModel):
    note_quality: int = Field(description="0-10: are the per-item notes specific, warm, and useful — not generic, salesy, or clichéd?")
    curation_quality: int = Field(description="0-10: do these look like genuinely the best, well-rounded picks in a sensible arc?")
    topic_interest: int = Field(description="0-10: is the topic itself niche, surprising, and intellectually rich?")
    comment: str = Field(description="One sentence justifying the scores.")


def _judge_topic(client, judge_model: str, dive: DeepDive) -> _TopicScores:
    items_desc = "\n".join(
        f"- ({it.kind}, {it.duration}) {it.title} — {it.source}\n  note: {it.note}"
        for it in dive.items
    )
    user = (
        f"Topic: {dive.title}\nStandfirst: {dive.dek}\nIntro: {dive.hook}\n\n"
        f"Items:\n{items_desc}\n\nScore this topic."
    )
    resp = client.messages.parse(
        model=judge_model,
        max_tokens=600,
        system=_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=_TopicScores,
    )
    return resp.parsed_output


def judged_metrics(client, judge_model: str, newsletter: Newsletter) -> dict:
    dives = newsletter.deep_dives
    scores: List[Optional[_TopicScores]] = [None] * len(dives)
    with ThreadPoolExecutor(max_workers=max(1, min(len(dives), 4))) as pool:
        futures = {pool.submit(_judge_topic, client, judge_model, d): i for i, d in enumerate(dives)}
        for future in futures:
            i = futures[future]
            try:
                scores[i] = future.result()
            except Exception:  # noqa: BLE001 - a failed judge shouldn't sink the eval
                scores[i] = None

    per_topic, nq, cq, ti, n = [], 0, 0, 0, 0
    for d, s in zip(dives, scores):
        if s is None:
            per_topic.append({"title": d.title, "error": "judge failed"})
            continue
        per_topic.append({
            "title": d.title, "note_quality": s.note_quality,
            "curation_quality": s.curation_quality, "topic_interest": s.topic_interest,
            "comment": s.comment,
        })
        nq += s.note_quality; cq += s.curation_quality; ti += s.topic_interest; n += 1

    agg = {"per_topic": per_topic}
    if n:
        agg.update({
            "avg_note_quality": round(nq / n, 2),
            "avg_curation_quality": round(cq / n, 2),
            "avg_topic_interest": round(ti / n, 2),
        })
    return agg


# --- Orchestration -----------------------------------------------------------


def _print_scorecard(sc: dict) -> None:
    obj, jud = sc["objective"], sc["judged"]
    print("\n" + "=" * 60)
    print(f"  SCORECARD — {sc.get('model')} (judge: {sc.get('judge_model')})")
    s = sc.get("settings", {})
    print(f"  settings: effort={s.get('effort')} max_uses={s.get('max_uses')} "
          f"candidates={s.get('candidate_items')} final={s.get('final_items')} "
          f"tool={'dynamic' if s.get('dynamic_filtering') else 'basic'}")
    print("-" * 60)
    print("  OBJECTIVE")
    print(f"    link liveness ............. {obj['link_liveness']*100:5.1f}%   (target 100%)")
    print(f"    link specificity .......... {obj['link_specificity']*100:5.1f}%   (target 100%)")
    print(f"    avg distinct formats ...... {obj['avg_distinct_formats']:5.2f}    (target >=3)")
    print(f"    topics in time band ....... {obj['frac_topics_time_on_target']*100:5.1f}%   (60-180 min)")
    print(f"    topics at item target ..... {obj['frac_topics_at_item_target']*100:5.1f}%")
    print("  JUDGED (0-10)")
    if "avg_note_quality" in jud:
        print(f"    note quality .............. {jud['avg_note_quality']:5.2f}")
        print(f"    curation quality .......... {jud['avg_curation_quality']:5.2f}")
        print(f"    topic interest ............ {jud['avg_topic_interest']:5.2f}")
    else:
        print("    (judge unavailable)")
    print("=" * 60 + "\n")


def _save_scorecard(sc: dict) -> str:
    os.makedirs(_DEFAULT_ISSUE and os.path.dirname(_DEFAULT_ISSUE) or "out", exist_ok=True)
    path = os.path.join("out", f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sc, fh, indent=2, ensure_ascii=False)
    return os.path.abspath(path)


def compute_scores(
    newsletter: Newsletter, target_items: int, judge_model: str, client
) -> dict:
    """Run objective + judged metrics on an in-memory newsletter. Reused by the A/B runner."""
    print("  scoring — objective metrics...", flush=True)
    obj = objective_metrics(newsletter, target_items)
    print(f"  scoring — LLM judge ({judge_model})...", flush=True)
    jud = judged_metrics(client, judge_model, newsletter)
    return {
        "objective": {k: v for k, v in obj.items() if k != "per_topic"},
        "judged": {k: v for k, v in jud.items() if k != "per_topic"},
        "per_topic_objective": obj["per_topic"],
        "per_topic_judged": jud["per_topic"],
    }


def evaluate_issue(issue_path: str = _DEFAULT_ISSUE, cfg: Optional[Config] = None) -> dict:
    """Load a saved issue, score it, print + save the scorecard, and return it."""
    with open(issue_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    newsletter = Newsletter.model_validate(payload["newsletter"])
    settings = payload.get("settings", {})
    target_items = int(settings.get("final_items", 4))

    if cfg is None:
        cfg = Config.load(require_secrets=True)
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, timeout=120.0, max_retries=2)

    scores = compute_scores(newsletter, target_items, cfg.eval_judge_model, client)
    scorecard = {
        "scored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "issue_generated_at": payload.get("generated_at"),
        "model": payload.get("model"),
        "judge_model": cfg.eval_judge_model,
        "settings": settings,
        **scores,
    }
    _print_scorecard(scorecard)
    out = _save_scorecard(scorecard)
    print(f"Saved scorecard: {out}")
    return scorecard


def main(argv: Optional[List[str]] = None) -> int:
    import sys
    path = (argv or sys.argv[1:] or [_DEFAULT_ISSUE])[0]
    if not os.path.exists(path):
        print(f"No issue file at {path}. Run a curation first (it writes out/issue.json).",
              file=sys.stderr)
        return 2
    evaluate_issue(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
