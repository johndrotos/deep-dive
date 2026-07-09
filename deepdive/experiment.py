"""A/B experiment runner: compare settings on a FIXED topic set, so score deltas are
attributable to the settings rather than to which random topics got drawn.

Each ``--variant`` is a set of ResearchSettings overrides (e.g. "candidate_items=8" or
"deep:effort=high,max_uses=6"); an empty variant is the baseline (.env settings). Every
variant researches the SAME topics from the golden set, then each issue is scored with the
same objective + Opus-judge metrics as ``--eval``. Results print side-by-side with deltas
vs the first variant, and save to out/experiment-<ts>.json.

    python -m deepdive.experiment --topics experiments/golden.json \\
        --variant "" --variant "candidate_items=8,max_uses=6" [--limit 2]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional, Tuple

import anthropic

from . import curator, evaluate
from .config import Config

_METRIC_ROWS = [
    ("link_liveness", "link liveness", "obj", "%"),
    ("link_specificity", "link specificity", "obj", "%"),
    ("avg_distinct_formats", "avg distinct formats", "obj", "n"),
    ("frac_topics_time_on_target", "topics in time band", "obj", "%"),
    ("frac_topics_at_item_target", "topics at item target", "obj", "%"),
    ("avg_note_quality", "note quality (0-10)", "jud", "n"),
    ("avg_curation_quality", "curation quality (0-10)", "jud", "n"),
    ("avg_topic_interest", "topic interest (0-10)", "jud", "n"),
]


def _load_topics(path: str, limit: Optional[int]) -> List:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    raw = data["topics"] if isinstance(data, dict) else data
    if limit:
        raw = raw[:limit]
    return [curator.make_topic(t["title"], t["angle"]) for t in raw]


def _parse_variant(spec: str, base: "curator.ResearchSettings") -> Tuple[str, "curator.ResearchSettings"]:
    """Turn 'label:key=val,key=val' (label optional) into (label, settings)."""
    label, _, body = spec.partition(":") if ":" in spec else ("", "", spec)
    body = body.strip()
    overrides = {}
    if body:
        for pair in body.split(","):
            key, _, val = pair.partition("=")
            key, val = key.strip(), val.strip()
            if key:
                overrides[key] = val
    # Re-construct through Pydantic so string values are coerced to the right types.
    merged = {**base.model_dump(), **overrides}
    try:
        settings = curator.ResearchSettings(**merged)
    except Exception as exc:  # noqa: BLE001 - surface a bad override clearly
        raise SystemExit(f"Bad variant '{spec}': {exc}")
    label = (label.strip() or body) or "baseline"
    return label, settings


def _metric_value(scores: dict, key: str, tier: str):
    return scores[{"obj": "objective", "jud": "judged"}[tier]].get(key)


def _fmt(val, unit: str) -> str:
    if val is None:
        return "  -  "
    return f"{val*100:5.1f}%" if unit == "%" else f"{val:5.2f}"


def _print_table(results: List[dict]) -> None:
    labels = [r["label"] for r in results]
    width = max(22, *(len(l) for l in labels)) if labels else 22
    print("\n" + "=" * (26 + (width + 3) * len(results)))
    header = "  " + "metric".ljust(24) + "".join(l.ljust(width + 3) for l in labels)
    print(header)
    print("-" * len(header))
    base = results[0]["scores"] if results else {}
    for key, name, tier, unit in _METRIC_ROWS:
        row = "  " + name.ljust(24)
        base_val = _metric_value(base, key, tier)
        for r in results:
            val = _metric_value(r["scores"], key, tier)
            cell = _fmt(val, unit)
            if r is not results[0] and val is not None and base_val is not None:
                delta = (val - base_val) * (100 if unit == "%" else 1)
                cell += f" ({delta:+.1f})" if unit == "%" else f" ({delta:+.2f})"
            row += cell.ljust(width + 3)
        print(row)
    print("=" * len(header) + "\n")


def run_experiment(topics_path: str, variant_specs: List[str], limit: Optional[int]) -> dict:
    cfg = Config.load(require_secrets=True)
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, timeout=600.0, max_retries=2)
    topics = _load_topics(topics_path, limit)
    base = curator.ResearchSettings(
        effort=cfg.research_effort, max_tokens=cfg.research_max_tokens,
        max_uses=cfg.research_max_uses, max_rounds=cfg.research_max_rounds,
        dynamic_filtering=cfg.research_dynamic_filtering,
        candidate_items=cfg.research_candidate_items, final_items=cfg.deep_dive_items,
    )
    variants = [_parse_variant(s, base) for s in (variant_specs or [""])]

    print(f"A/B over {len(topics)} fixed topic(s), {len(variants)} variant(s), "
          f"model={cfg.model}, judge={cfg.eval_judge_model}.")
    for t in topics:
        print(f"  - {t.title}")

    results = []
    for label, settings in variants:
        print(f"\n=== variant: {label} ===", flush=True)
        newsletter = curator.build_newsletter(
            client, cfg.model, [], len(topics), settings, topics=topics
        )
        scores = evaluate.compute_scores(
            newsletter, settings.final_items, cfg.eval_judge_model, client
        )
        results.append({"label": label, "settings": settings.model_dump(), "scores": scores})

    _print_table(results)
    os.makedirs("out", exist_ok=True)
    path = os.path.join("out", f"experiment-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": cfg.model, "judge_model": cfg.eval_judge_model,
            "topics": [t.title for t in topics], "results": results,
        }, fh, indent=2, ensure_ascii=False)
    print(f"Saved experiment: {os.path.abspath(path)}")
    return {"results": results}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="deepdive.experiment")
    parser.add_argument("--topics", default=os.path.join("experiments", "golden.json"),
                        help="JSON file with a fixed topic set (default experiments/golden.json).")
    parser.add_argument("--variant", action="append", default=[],
                        help="A settings override set, e.g. 'candidate_items=8' or "
                             "'deep:effort=high,max_uses=6'. Repeat to compare several. "
                             "Empty string = baseline (.env settings).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only use the first N topics (cheaper runs).")
    args = parser.parse_args(argv)
    if not os.path.exists(args.topics):
        print(f"No topics file at {args.topics}.", file=sys.stderr)
        return 2
    run_experiment(args.topics, args.variant, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
