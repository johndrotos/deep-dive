"""Entry point and CLI for The Deep Dive.

Usage:
  python -m deepdive.main            Curate (Claude + web search), email, record history.
  python -m deepdive.main --dry-run  Real curation, but write out/preview.html instead of
                                     emailing. History untouched unless --record.
  python -m deepdive.main --fake     Offline: canned content -> out/preview.html. No API,
                                     no email. For iterating on the design.
  python -m deepdive.main --record   With --dry-run, also record the topics to history.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime

from . import curator, history, mailer, renderer
from .config import Config, ConfigError
from .models import Newsletter
from .samples import sample_newsletter

_PREVIEW_PATH = os.path.join("out", "preview.html")
_ISSUE_PATH = os.path.join("out", "issue.json")


def _write_preview(html: str) -> str:
    os.makedirs(os.path.dirname(_PREVIEW_PATH), exist_ok=True)
    with open(_PREVIEW_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    return os.path.abspath(_PREVIEW_PATH)


def _write_issue_json(newsletter: Newsletter, cfg: Config, settings) -> str:
    """Persist the structured issue + the settings that produced it, for the evaluator."""
    os.makedirs(os.path.dirname(_ISSUE_PATH), exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": cfg.model,
        "settings": settings.model_dump(),
        "newsletter": newsletter.model_dump(),
    }
    with open(_ISSUE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return os.path.abspath(_ISSUE_PATH)


def _build(cfg: Config) -> Newsletter:
    import anthropic

    # The research call is streamed, so the connection stays active for the several
    # minutes a web-search-heavy request takes (no more silent-wire read timeouts).
    # A couple of retries absorb transient connection-level overloads cheaply; the
    # research call adds its own backoff loop for mid-stream overload errors.
    client = anthropic.Anthropic(
        api_key=cfg.anthropic_api_key,
        timeout=600.0,
        max_retries=2,
    )
    settings = curator.ResearchSettings(
        effort=cfg.research_effort,
        max_tokens=cfg.research_max_tokens,
        max_uses=cfg.research_max_uses,
        max_rounds=cfg.research_max_rounds,
        dynamic_filtering=cfg.research_dynamic_filtering,
        candidate_items=cfg.research_candidate_items,
        final_items=cfg.deep_dive_items,
    )
    past = history.past_topics(cfg.data_dir)
    print(f"  Avoiding {len(past)} previously-covered topics.")
    tool = "dynamic-filtering" if settings.dynamic_filtering else "basic"
    print(
        f"  Search depth: effort={settings.effort}, max_tokens={settings.max_tokens}, "
        f"max_uses={settings.max_uses}, max_rounds={settings.max_rounds}, tool={tool}."
    )
    print(
        f"  Curation: research {cfg.topic_candidates} topics, judge-select the best "
        f"{cfg.deep_dive_count}; per topic research {settings.candidate_items} "
        f"candidates, verify, then select the best {settings.final_items}."
    )
    print("  Selecting topics and researching (this takes a few minutes)...")
    newsletter = curator.build_newsletter(
        client, cfg.model, past, cfg.deep_dive_count, settings,
        cfg.eval_judge_model, topic_candidates=cfg.topic_candidates,
    )
    for dive in newsletter.deep_dives:
        print(f"    - {dive.title} ({len(dive.items)} items)")
    _write_issue_json(newsletter, cfg, settings)
    return newsletter


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deepdive", description="The Deep Dive newsletter agent.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Curate for real but write a local HTML preview instead of emailing.",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Use canned content (no API calls, no email) to preview the design.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="With --dry-run, also record this issue's topics to history.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't auto-open the preview in a browser.",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        help="Curate (no email), then score the issue on quality metrics and print a scorecard.",
    )
    args = parser.parse_args(argv)

    # --- Fake mode: offline design preview -------------------------------------
    if args.fake:
        cfg = Config.load(require_secrets=False)
        newsletter = sample_newsletter()
        html = renderer.render_html(newsletter, cfg.newsletter_title)
        path = _write_preview(html)
        print(f"Wrote preview: {path}")
        if not args.no_open:
            webbrowser.open(f"file://{path}")
        return 0

    # --- Real curation (dry-run or live send) ----------------------------------
    try:
        cfg = Config.load(require_secrets=True)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2

    print(f"The Deep Dive — building this week's issue with {cfg.model}.")
    newsletter = _build(cfg)
    html = renderer.render_html(newsletter, cfg.newsletter_title)
    subject = renderer.subject_line(newsletter, cfg.newsletter_title)
    topics = [d.title for d in newsletter.deep_dives]

    if args.dry_run or args.eval:
        path = _write_preview(html)
        label = "Eval run" if args.eval else "Dry run"
        print(f"\n{label}. Subject would be:\n  {subject}")
        print(f"Wrote preview: {path}")
        if args.record:
            history.record(cfg.data_dir, topics)
            print(f"Recorded {len(topics)} topics to history.")
        if args.eval:
            from . import evaluate
            evaluate.evaluate_issue(_ISSUE_PATH, cfg)
        elif not args.no_open:
            webbrowser.open(f"file://{path}")
        return 0

    # Live send.
    message_id = mailer.send(
        api_key=cfg.resend_api_key,
        sender=cfg.newsletter_from,
        recipient=cfg.newsletter_to,
        subject=subject,
        html=html,
    )
    print(f"\nSent to {cfg.newsletter_to} (Resend id: {message_id or 'n/a'}).")
    history.record(cfg.data_dir, topics)
    print(f"Recorded {len(topics)} topics to history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
