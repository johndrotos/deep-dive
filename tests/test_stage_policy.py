"""The effort policy: per-stage effort, the depth profile, and named truncation.

These are the tests the old fakes could not fail. The fake response objects here carry a
real ``stop_reason``, so "the model ran out of budget" is distinguishable from "the model
had nothing to say" — the confusion that took three weekly issues down silently.
"""

import pytest
from pydantic import ValidationError

from deepdive import curator
from deepdive.curator import (
    StagePolicy,
    TruncatedError,
    _TopicSlate,
    policy_for,
    research_effort_for,
    shift_effort,
)
from deepdive.models import ContentItem, DeepDive


class _Resp:
    """Minimal stand-in for a Messages response."""

    def __init__(self, parsed=None, stop_reason="end_turn", output_tokens=42, text=""):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = None
        self.usage = type("_Usage", (), {"output_tokens": output_tokens})()
        self.content = [type("_Block", (), {"type": "text", "text": text})()]


class _ScriptedMessages:
    """Returns the scripted responses in order, repeating the last one.

    A scripted entry that is an exception is raised instead of returned - that is how
    the real SDK reports a half-written JSON body.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def parse(self, **kwargs):
        return self._next(kwargs)

    def create(self, **kwargs):
        return self._next(kwargs)


class _Client:
    def __init__(self, *responses):
        self.messages = _ScriptedMessages(responses)


def _dive(title="T"):
    return DeepDive(
        title=title, dek="dek", hook="hook", estimated_time="~2 hours",
        items=[ContentItem(
            title=title, kind="essay", source="S",
            url="https://e.com/" + title, duration="30 min", note="n",
        )],
    )


# --- the policy table --------------------------------------------------------


def test_effort_tracks_judgment_not_output_size():
    # Structuring writes a long document from an already-researched brief: little
    # judgment, lots of text. Item selection weighs candidates to emit a few integers.
    structure, select_items = policy_for("structure"), policy_for("select_items")
    assert structure.effort == "low" and select_items.effort == "medium"
    assert structure.max_tokens > select_items.max_tokens


def test_every_rail_leaves_room_for_thinking():
    # The regression this whole change exists for: no stage may be budgeted so tightly
    # that thinking alone can exhaust it.
    for stage in ("select_topics", "structure", "select_items", "rank_topics", "edition_intro"):
        assert policy_for(stage).max_tokens >= 2000, stage


def test_depth_profile_shifts_every_stage():
    for stage in ("select_topics", "structure", "select_items", "rank_topics", "edition_intro"):
        base = policy_for(stage, "balanced").effort
        assert policy_for(stage, "fast").effort == shift_effort(base, -1), stage
        assert policy_for(stage, "deep").effort == shift_effort(base, 1), stage


def test_depth_profile_reaches_research_too():
    assert research_effort_for("balanced") == "medium"
    assert research_effort_for("fast") == "low"
    assert research_effort_for("deep") == "high"


def test_shift_clamps_at_both_ends():
    assert shift_effort("low", -3) == "low"
    assert shift_effort("max", 3) == "max"


# --- truncation handling -----------------------------------------------------


def test_truncation_is_retried_with_less_thinking_and_more_room():
    slate = _TopicSlate(topics=[curator.make_topic("A", "angle")])
    client = _Client(_Resp(stop_reason="max_tokens"), _Resp(parsed=slate))

    out = curator._structured(
        client, "m", "select_topics", "sys", "user", _TopicSlate, label="topics"
    )

    assert out is slate
    first, second = client.messages.calls
    assert first["output_config"] == {"effort": "high"}
    assert second["output_config"] == {"effort": "medium"}      # one notch down
    assert second["max_tokens"] == first["max_tokens"] * 2      # doubled rail


def test_truncation_twice_raises_named_error():
    client = _Client(_Resp(stop_reason="max_tokens", output_tokens=8000))

    with pytest.raises(TruncatedError, match="max_tokens after 8000"):
        curator._structured(client, "m", "select_topics", "s", "u", _TopicSlate)

    assert len(client.messages.calls) == 2  # tried once, retried once, then gave up


def _validation_error(payload: str) -> ValidationError:
    """A real pydantic error, produced the way the SDK's parser produces one."""
    try:
        _TopicSlate.model_validate_json(payload)
    except ValidationError as exc:
        return exc
    raise AssertionError("payload unexpectedly validated")


def test_half_written_json_counts_as_truncation():
    # The other shape of a cut-off response: thinking stopped just short, leaving the
    # JSON body unfinished, and parse() raises instead of returning None.
    slate = _TopicSlate(topics=[curator.make_topic("A", "angle")])
    cut_off = _validation_error('{"topics":[{"title":"The Bog Peo')
    client = _Client(cut_off, _Resp(parsed=slate))

    assert curator._structured(client, "m", "select_topics", "s", "u", _TopicSlate) is slate
    assert client.messages.calls[1]["max_tokens"] == client.messages.calls[0]["max_tokens"] * 2


def test_schema_mismatch_is_not_treated_as_truncation():
    # Valid JSON that doesn't fit the schema is a different bug; retrying won't fix it.
    client = _Client(_validation_error('{"topics": "not a list"}'))

    with pytest.raises(RuntimeError, match="did not match the schema") as excinfo:
        curator._structured(client, "m", "select_topics", "s", "u", _TopicSlate)

    assert not isinstance(excinfo.value, TruncatedError)
    assert len(client.messages.calls) == 1


def test_refusal_is_reported_as_a_refusal_and_not_retried():
    client = _Client(_Resp(stop_reason="refusal"))

    with pytest.raises(RuntimeError, match="declined") as excinfo:
        curator._structured(client, "m", "select_topics", "s", "u", _TopicSlate)

    assert not isinstance(excinfo.value, TruncatedError)
    assert len(client.messages.calls) == 1


# --- the stages themselves ---------------------------------------------------


def test_select_topics_blames_truncation_not_the_model():
    """The 2026-09-13 outage: a cut-off response reported as 'returned no topics'."""
    client = _Client(_Resp(stop_reason="max_tokens"))

    with pytest.raises(TruncatedError) as excinfo:
        curator.select_topics(client, "m", ["past topic"], 5)

    assert "max_tokens" in str(excinfo.value)
    assert "no topics" not in str(excinfo.value)
    # ...and it asked with a budget sized for thinking, not for the answer alone.
    assert client.messages.calls[0]["max_tokens"] == 8000


def test_select_topics_still_rejects_a_genuinely_empty_slate():
    client = _Client(_Resp(parsed=_TopicSlate(topics=[])))

    with pytest.raises(RuntimeError, match="empty slate"):
        curator.select_topics(client, "m", [], 3)


def test_structuring_failure_names_the_topic_and_the_reason():
    client = _Client(_Resp(stop_reason="max_tokens"))
    topic = curator.make_topic("Bog People", "angle")

    with pytest.raises(RuntimeError, match="Bog People.*max_tokens"):
        curator.structure_deep_dive(client, "m", topic, "brief")


def test_item_selection_falls_back_instead_of_sinking_the_issue():
    client = _Client(_Resp(stop_reason="max_tokens"))
    dive = _dive("Keep me")

    out = curator.select_deep_dive(client, "m", dive, final_items=1)

    assert [i.title for i in out.items] == ["Keep me"]  # research order preserved


def test_edition_intro_ships_empty_rather_than_crashing():
    client = _Client(_Resp(stop_reason="max_tokens", text="half a sent"))

    assert curator._edition_intro(client, "m", [_dive()]) == ""
    assert len(client.messages.calls) == 2  # retried with more room first


def test_edition_intro_runs_under_its_policy():
    client = _Client(_Resp(stop_reason="end_turn", text="  A warm note.  "))

    assert curator._edition_intro(client, "m", [_dive()], depth="fast") == "A warm note."
    assert client.messages.calls[0]["output_config"] == {"effort": "low"}
