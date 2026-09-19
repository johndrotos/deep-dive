"""Cost accounting: per-stage token rollup, correct arithmetic, honest gaps.

The point of the ledger is a number you can act on, so the tests care most about
two things: that the arithmetic matches the published rate card, and that an
unknown model produces "unpriced" rather than a confident wrong figure.
"""

import threading
from types import SimpleNamespace as NS

from deepdive.curator import UsageLedger


def _usage(inp=0, out=0, cache_write=0, cache_read=0, searches=None):
    return NS(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
        server_tool_use=NS(web_search_requests=searches) if searches is not None else None,
    )


def test_prices_tokens_against_the_rate_card():
    led = UsageLedger()
    led.record("structure", "claude-sonnet-5", _usage(inp=1_000_000, out=1_000_000))

    # Sonnet 5: $2/MTok in, $10/MTok out.
    assert led.total_usd() == 12.0


def test_prices_cache_reads_and_writes_separately():
    led = UsageLedger()
    led.record("x", "claude-sonnet-5", _usage(cache_write=1_000_000, cache_read=1_000_000))

    # $2.50/MTok write + $0.20/MTok read.
    assert round(led.total_usd(), 6) == 2.70


def test_web_searches_are_billed_on_top_of_tokens():
    led = UsageLedger()
    led.record("research", "claude-sonnet-5", _usage(out=100_000, searches=23))

    # 100k output at $10/MTok = $1.00, plus 23 searches at $10/1000 = $0.23.
    assert round(led.total_usd(), 6) == 1.23


def test_unknown_model_is_counted_but_never_priced():
    led = UsageLedger()
    led.record("research", "claude-from-the-future", _usage(inp=1000, out=1000))

    row = led.rows()[0]
    assert row["input"] == 1000 and row["output"] == 1000  # tokens still counted
    assert row["usd"] is None                              # but no invented dollars
    assert led.total_usd() is None                         # and the total says so
    assert "unpriced" in led.report()


def test_a_partially_unpriced_run_does_not_report_a_confident_total():
    led = UsageLedger()
    led.record("structure", "claude-sonnet-5", _usage(out=1_000_000))
    led.record("rank_topics", "claude-from-the-future", _usage(out=1_000))

    assert led.total_usd() is None
    assert "partial" in led.report()


def test_rows_are_keyed_by_stage_and_model():
    led = UsageLedger()
    led.record("structure", "claude-sonnet-5", _usage(out=10))
    led.record("structure", "claude-sonnet-5", _usage(out=15))
    led.record("rank_topics", "claude-opus-5", _usage(out=10))

    rows = {(r["stage"], r["model"]): r for r in led.rows()}
    assert rows[("structure", "claude-sonnet-5")]["output"] == 25
    assert rows[("structure", "claude-sonnet-5")]["calls"] == 2
    # The judge runs on a pricier model; blending the two would hide that.
    assert rows[("rank_topics", "claude-opus-5")]["calls"] == 1


def test_most_expensive_stage_is_reported_first():
    led = UsageLedger()
    led.record("edition_intro", "claude-sonnet-5", _usage(out=500))
    led.record("research", "claude-sonnet-5", _usage(out=50_000, searches=20))

    assert [r["stage"] for r in led.rows()] == ["research", "edition_intro"]


def test_missing_counters_do_not_break_a_run():
    led = UsageLedger()
    led.record("x", "claude-sonnet-5", NS())  # a usage object with nothing on it
    led.record("x", "claude-sonnet-5", None)  # or none at all

    assert led.rows()[0]["calls"] == 1  # the None is ignored, the empty one counted
    assert led.total_usd() == 0.0


def test_parallel_recording_loses_nothing():
    # Topics research concurrently, so the ledger is written from several threads.
    led = UsageLedger()

    def hammer():
        for _ in range(200):
            led.record("research", "claude-sonnet-5", _usage(out=1, searches=1))

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    row = led.rows()[0]
    assert row["calls"] == 1600 and row["output"] == 1600 and row["searches"] == 1600


def test_empty_ledger_reports_cleanly():
    assert "No API usage" in UsageLedger().report()
    assert UsageLedger().total_usd() is None
