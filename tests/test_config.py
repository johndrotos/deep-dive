"""Config env-parsing tests."""

import os

from deepdive.config import Config


def _load_with(env, **overrides):
    """Load Config with a clean-ish env plus the required secrets."""
    base = {
        "ANTHROPIC_API_KEY": "k",
        "RESEND_API_KEY": "k",
        "NEWSLETTER_TO": "a@b.com",
        "NEWSLETTER_FROM": "c@d.com",
    }
    base.update(env)
    saved = dict(os.environ)
    try:
        # Clear any TOPIC_CANDIDATES leaking from the real .env / shell.
        os.environ.pop("TOPIC_CANDIDATES", None)
        os.environ.update(base)
        return Config.load(**overrides)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_topic_candidates_defaults_to_5():
    cfg = _load_with({})
    assert cfg.topic_candidates == 5


def test_topic_candidates_reads_env():
    cfg = _load_with({"TOPIC_CANDIDATES": "6"})
    assert cfg.topic_candidates == 6


def test_topic_candidates_has_minimum_1():
    cfg = _load_with({"TOPIC_CANDIDATES": "0"})
    assert cfg.topic_candidates == 1
