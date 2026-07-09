"""Guard tests: cheap checks that the prompt intent didn't silently revert."""

from deepdive import curator


def test_topic_system_asks_for_curation_and_depth():
    p = curator._TOPIC_SYSTEM
    assert "CURATOR" in p
    assert "EXPANSIVE" in p
    assert "two hours" in p or "2-hour" in p


def test_topic_system_dropped_the_surprise_nugget_framing():
    # The old prompt rewarded surprise-per-square-inch; that framing is gone.
    assert "I had no idea that was a whole world" not in curator._TOPIC_SYSTEM


def test_research_system_requires_honest_durations():
    assert "honest" in curator._RESEARCH_SYSTEM.lower()
