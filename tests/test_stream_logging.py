"""Research-stream logging: announce phase CHANGES, and measure progress honestly.

A cited brief arrives as one `text` block per citation (33 in one observed turn), so
announcing every block start buried the useful lines. And the API reports usage only in
a single `message_delta` at the end of the turn, so there is no live token count to
report — progress is counted in characters streamed instead.
"""

from types import SimpleNamespace as NS

import pytest

from deepdive import curator
from deepdive.curator import ResearchSettings


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(curator, "_log", lines.append)
    return lines


def _state():
    return {"phase": "starting", "chars": 0}


def _start(block_type, **kw):
    return NS(type="content_block_start", content_block=NS(type=block_type, **kw))


def _delta(**kw):
    return NS(type="content_block_delta", delta=NS(**kw))


def _feed(events, state=None, settings=None):
    state = state if state is not None else _state()
    settings = settings or ResearchSettings()
    searches = 0
    for ev in events:
        searches = curator._handle_stream_event(ev, None, state, searches, settings, "[1/1]")
    return state


def test_repeated_text_blocks_are_announced_once(logged):
    # The citation-split case: 33 text blocks in a row, one line about it.
    _feed([_start("text") for _ in range(33)])

    assert logged == ["  [1/1]     · entering: writing brief"]


def test_each_real_phase_change_is_announced(logged):
    _feed([
        _start("thinking"),
        _start("text"), _start("text"),
        _start("thinking"),
        _start("text"),
    ])

    assert [l.split("entering: ")[1] for l in logged] == [
        "thinking", "writing brief", "thinking", "writing brief",
    ]


def test_web_search_start_is_never_announced(logged):
    # Searches are logged in full, with their query, when the block completes.
    _feed([_start("server_tool_use", name="web_search")])

    assert logged == []


def test_web_search_still_separates_two_text_runs(logged):
    # A search between two briefs must not be swallowed by the dedup: the phase still
    # changes, so the second "writing brief" is announced.
    _feed([
        _start("text"),
        _start("server_tool_use", name="web_search"),
        _start("text"),
    ])

    assert [l.split("entering: ")[1] for l in logged] == ["writing brief", "writing brief"]


def test_progress_counts_characters_from_every_delta_kind():
    state = _feed([
        _delta(text="hello "),
        _delta(thinking="pondering"),
        _delta(partial_json='{"a":1}'),
        _delta(text=""),          # empty deltas contribute nothing
        NS(type="message_stop"),  # unrelated events are ignored
    ])

    assert state["chars"] == len("hello ") + len("pondering") + len('{"a":1}')


def test_heartbeat_reports_characters_not_tokens(logged, monkeypatch):
    import threading

    stop = threading.Event()
    state = {"phase": "writing brief", "chars": 12345}
    # wait() returns False once (so the body runs), then True to end the loop.
    ticks = iter([False, True])
    monkeypatch.setattr(stop, "wait", lambda _timeout: next(ticks))

    curator._heartbeat(stop, state, "[1/1]")

    assert len(logged) == 1
    assert "phase='writing brief'" in logged[0]
    assert "12,345 chars streamed" in logged[0]
