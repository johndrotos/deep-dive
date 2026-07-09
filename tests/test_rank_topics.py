"""Unit tests for the post-research topic richness filter."""

from deepdive.curator import rank_topics_by_richness, _TopicRanking
from deepdive.models import ContentItem, DeepDive


def _item(title="X"):
    return ContentItem(
        title=title, kind="essay", source="Src",
        url="https://example.com/" + title, duration="30 min",
        note="A note.",
    )


def _dive(title):
    return DeepDive(
        title=title, dek="dek", hook="hook",
        estimated_time="~2 hours", items=[_item(title)],
    )


class _FakeResp:
    def __init__(self, parsed):
        self.parsed_output = parsed


class _FakeMessages:
    """Records calls; returns a canned parsed output or raises."""

    def __init__(self, parsed=None, exc=None):
        self._parsed = parsed
        self._exc = exc
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._parsed)


class _FakeClient:
    def __init__(self, parsed=None, exc=None):
        self.messages = _FakeMessages(parsed=parsed, exc=exc)


def test_skip_guard_returns_all_without_calling_judge():
    dives = [_dive("A"), _dive("B"), _dive("C")]
    client = _FakeClient(exc=AssertionError("judge must not be called"))
    result = rank_topics_by_richness(client, "judge", dives, keep=3)
    assert result == dives
    assert client.messages.calls == 0


def test_keeps_the_indices_the_judge_returns_in_order():
    dives = [_dive(t) for t in ("A", "B", "C", "D", "E")]
    client = _FakeClient(parsed=_TopicRanking(keep=[3, 1, 4]))
    result = rank_topics_by_richness(client, "judge", dives, keep=3)
    assert [d.title for d in result] == ["D", "B", "E"]


def test_empty_ranking_falls_back_to_first_keep():
    dives = [_dive(t) for t in ("A", "B", "C", "D", "E")]
    client = _FakeClient(parsed=_TopicRanking(keep=[]))
    result = rank_topics_by_richness(client, "judge", dives, keep=3)
    assert [d.title for d in result] == ["A", "B", "C"]


def test_exception_falls_back_to_first_keep():
    dives = [_dive(t) for t in ("A", "B", "C", "D", "E")]
    client = _FakeClient(exc=RuntimeError("boom"))
    result = rank_topics_by_richness(client, "judge", dives, keep=3)
    assert [d.title for d in result] == ["A", "B", "C"]


def test_out_of_range_and_duplicate_indices_are_sanitized():
    dives = [_dive(t) for t in ("A", "B", "C", "D", "E")]
    # 99 is out of range and ignored; the duplicate 1 is deduped; stops at keep=2.
    client = _FakeClient(parsed=_TopicRanking(keep=[99, 1, 1, 0, 4]))
    result = rank_topics_by_richness(client, "judge", dives, keep=2)
    assert [d.title for d in result] == ["B", "A"]
