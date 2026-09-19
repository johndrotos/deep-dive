"""Environment configuration, loaded once and validated up front."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .curator import DEPTHS, DEFAULT_DEPTH

try:
    # Optional: load a local .env when present. Never required in production.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a dev convenience only
    pass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    resend_api_key: str
    newsletter_to: str
    newsletter_from: str
    model: str
    data_dir: str
    deep_dive_count: int
    # How many candidate topics to research before the judge keeps the best
    # `deep_dive_count`. Over-provisioning lets us drop topics whose researched
    # content turns out thin. Only used in the normal (non-fixed-topics) path.
    topic_candidates: int
    newsletter_title: str
    # Pipeline-wide effort profile: fast | balanced | deep. Shifts EVERY stage's
    # reasoning effort a notch (curator.policy_for), not just research.
    depth: str
    # Search-depth knobs (speed vs. thoroughness). Lower = faster/cheaper.
    # Deprecated explicit override for research's per-turn effort; empty means "derive
    # it from `depth`". Kept so an A/B sweep (and any stale SEARCH_EFFORT) still works.
    research_effort: str
    research_max_tokens: int
    research_max_uses: int
    research_max_rounds: int
    # Web-search tool variant. True = newer dynamic-filtering tool (thorough but can
    # run away in a filtering loop); False = basic tool (fast, results go straight to Claude).
    research_dynamic_filtering: bool
    # Over-provision + select: research this many candidate items, verify them, then keep
    # the best `deep_dive_items` survivors.
    research_candidate_items: int
    deep_dive_items: int
    # Model used to JUDGE issue quality in the eval (a stronger model than the writer, so
    # it isn't grading its own work).
    eval_judge_model: str

    @classmethod
    def load(cls, *, require_secrets: bool = True) -> "Config":
        """Build config from the environment.

        When ``require_secrets`` is False (e.g. ``--fake`` runs that never touch the
        network), missing secrets are tolerated and left blank.
        """
        missing = []

        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value and require_secrets:
                missing.append(name)
            return value

        anthropic_api_key = required("ANTHROPIC_API_KEY")
        resend_api_key = required("RESEND_API_KEY")
        newsletter_to = required("NEWSLETTER_TO")
        newsletter_from = required("NEWSLETTER_FROM")

        if missing:
            raise ConfigError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ".\nCopy .env.example to .env and fill them in, or set them in Railway."
            )

        def int_env(name: str, default: int, minimum: int = 0) -> int:
            try:
                return max(minimum, int(os.environ.get(name, str(default))))
            except ValueError:
                return default

        def bool_env(name: str, default: bool) -> bool:
            raw = os.environ.get(name, "").strip().lower()
            if raw in ("1", "true", "yes", "on"):
                return True
            if raw in ("0", "false", "no", "off"):
                return False
            return default

        deep_dive_count = int_env("DEEP_DIVE_COUNT", 3, minimum=1)

        depth = os.environ.get("DEPTH", DEFAULT_DEPTH).strip().lower() or DEFAULT_DEPTH
        if depth not in DEPTHS:
            raise ConfigError(
                f"DEPTH must be one of {'|'.join(DEPTHS)} (got {depth!r})."
            )
        research_effort = os.environ.get("SEARCH_EFFORT", "").strip()

        return cls(
            anthropic_api_key=anthropic_api_key,
            resend_api_key=resend_api_key,
            newsletter_to=newsletter_to,
            newsletter_from=newsletter_from,
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5").strip()
            or "claude-opus-5",
            data_dir=os.environ.get("DATA_DIR", "./data").strip() or "./data",
            deep_dive_count=deep_dive_count,
            topic_candidates=int_env("TOPIC_CANDIDATES", 5, minimum=1),
            newsletter_title=os.environ.get("NEWSLETTER_TITLE", "The Deep Dive").strip()
            or "The Deep Dive",
            depth=depth,
            research_effort=research_effort,
            research_max_tokens=int_env("SEARCH_MAX_TOKENS", 8000, minimum=1000),
            research_max_uses=int_env("SEARCH_MAX_USES", 5, minimum=1),
            research_max_rounds=int_env("SEARCH_MAX_ROUNDS", 1, minimum=0),
            research_dynamic_filtering=bool_env("SEARCH_DYNAMIC_FILTERING", False),
            research_candidate_items=int_env("SEARCH_CANDIDATE_ITEMS", 6, minimum=2),
            deep_dive_items=int_env("DEEP_DIVE_ITEMS", 4, minimum=1),
            eval_judge_model=os.environ.get("EVAL_JUDGE_MODEL", "claude-opus-5").strip()
            or "claude-opus-5",
        )
