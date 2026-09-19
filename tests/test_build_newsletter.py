"""Wiring test: build_newsletter over-provisions topics then filters to `count`."""

from deepdive import curator
from deepdive.curator import ResearchSettings, _TopicRanking, make_topic
from deepdive.models import ContentItem, DeepDive


def _dive(title):
    return DeepDive(
        title=title, dek="dek", hook="hook", estimated_time="~2 hours",
        items=[ContentItem(
            title=title, kind="essay", source="S",
            url="https://e.com/" + title, duration="30 min", note="n",
        )],
    )


class _FakeResp:
    def __init__(self, parsed):
        self.parsed_output = parsed


class _FakeMessages:
    def __init__(self, parsed):
        self._parsed = parsed

    def parse(self, **kwargs):
        return _FakeResp(self._parsed)


class _FakeClient:
    """Only the topic-rank judge call goes through here (research is monkeypatched)."""

    def __init__(self, parsed):
        self.messages = _FakeMessages(parsed)


def test_overprovisions_five_then_ships_three(monkeypatch):
    captured = {}

    def fake_select(client, model, history, count, depth="balanced", ledger=None):
        captured["candidate_count"] = count
        captured["depth"] = depth
        return [make_topic(f"T{i}", "angle") for i in range(count)]

    def fake_research(client, model, topic, settings, label, ledger=None):
        captured["ledger_reached_research"] = ledger is not None
        return _dive(topic.title)

    monkeypatch.setattr(curator, "select_topics", fake_select)
    monkeypatch.setattr(curator, "_research_and_structure", fake_research)
    monkeypatch.setattr(curator, "_edition_intro", lambda *a, **k: "intro")

    # Judge keeps indices 0,1,2 out of the 5 researched (slots are in topic order).
    client = _FakeClient(_TopicRanking(keep=[0, 1, 2]))

    nl = curator.build_newsletter(
        client, "writer", [], 3, ResearchSettings(), "judge", topic_candidates=5
    )

    assert captured["candidate_count"] == 5           # over-provisioned
    assert captured["depth"] == "balanced"            # profile reaches the stages
    assert captured["ledger_reached_research"]        # cost accounting reaches the stages
    assert len(nl.deep_dives) == 3                     # filtered to count
    assert [d.title for d in nl.deep_dives] == ["T0", "T1", "T2"]
    assert nl.intro == "intro"


def test_fixed_topics_mode_does_not_filter(monkeypatch):
    # When topics are passed in (A/B mode), research them all and ship them all.
    def fake_research(client, model, topic, settings, label, ledger=None):
        return _dive(topic.title)

    monkeypatch.setattr(curator, "_research_and_structure", fake_research)
    monkeypatch.setattr(curator, "_edition_intro", lambda *a, **k: "intro")
    # If the judge were called it would blow up (parse returns None -> fallback anyway),
    # but with 2 fixed topics and count=2 the skip-guard means it's never called.
    client = _FakeClient(None)

    fixed = [make_topic("F0", "a"), make_topic("F1", "a")]
    nl = curator.build_newsletter(
        client, "writer", [], 2, ResearchSettings(), "judge", topics=fixed
    )
    assert sorted(d.title for d in nl.deep_dives) == ["F0", "F1"]
