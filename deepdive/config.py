"""Environment configuration, loaded once and validated up front."""

from __future__ import annotations

import os
from dataclasses import dataclass

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
    newsletter_title: str

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

        try:
            deep_dive_count = int(os.environ.get("DEEP_DIVE_COUNT", "3"))
        except ValueError:
            deep_dive_count = 3

        return cls(
            anthropic_api_key=anthropic_api_key,
            resend_api_key=resend_api_key,
            newsletter_to=newsletter_to,
            newsletter_from=newsletter_from,
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8").strip()
            or "claude-opus-4-8",
            data_dir=os.environ.get("DATA_DIR", "./data").strip() or "./data",
            deep_dive_count=max(1, deep_dive_count),
            newsletter_title=os.environ.get("NEWSLETTER_TITLE", "The Deep Dive").strip()
            or "The Deep Dive",
        )
