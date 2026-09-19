"""An unattended run that crashes must still tell you it crashed."""

import pytest

from deepdive import main
from deepdive.config import Config


def _cfg():
    return Config(
        anthropic_api_key="k", resend_api_key="r",
        newsletter_to="me@example.com", newsletter_from="dive@example.com",
        model="m", data_dir="./data", deep_dive_count=3, topic_candidates=5,
        newsletter_title="The Deep Dive", depth="balanced", research_effort="medium",
        research_max_tokens=8000, research_max_uses=5, research_max_rounds=0,
        research_dynamic_filtering=False, research_candidate_items=6,
        deep_dive_items=4, eval_judge_model="j",
    )


def _boom(*args, **kwargs):
    raise RuntimeError("research exploded")


def test_live_run_emails_an_alert_then_reraises(monkeypatch):
    sent = {}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, **kw: _cfg()))
    monkeypatch.setattr(main, "_run_issue", _boom)
    monkeypatch.setattr(main.mailer, "send", lambda **kw: sent.update(kw) or "id-1")

    with pytest.raises(RuntimeError, match="research exploded"):
        main.run([])

    assert sent["recipient"] == "me@example.com"
    assert "failed to build" in sent["subject"]
    # The traceback travels with the alert, so the inbox says *why*.
    assert "research exploded" in sent["html"]


def test_dry_run_does_not_email(monkeypatch):
    calls = []
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, **kw: _cfg()))
    monkeypatch.setattr(main, "_run_issue", _boom)
    monkeypatch.setattr(main.mailer, "send", lambda **kw: calls.append(kw))

    with pytest.raises(RuntimeError):
        main.run(["--dry-run"])

    assert calls == []


def test_alert_failure_never_masks_the_real_error(monkeypatch):
    """If Resend is also down, the original exception still surfaces."""
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, **kw: _cfg()))
    monkeypatch.setattr(main, "_run_issue", _boom)
    monkeypatch.setattr(main.mailer, "send", _boom)

    with pytest.raises(RuntimeError, match="research exploded"):
        main.run([])
