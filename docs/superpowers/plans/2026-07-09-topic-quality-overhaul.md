# Topic Quality Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make weekly topics expansive and curated (not one-dimensional trivia) and drop topics whose *researched* content turns out thin, by rewriting the topic-selection prompt and adding an over-provision + Opus-judge topic filter.

**Architecture:** Rewrite `_TOPIC_SYSTEM` so selection asks for depth/curation, not surprise. Over-generate topics (`TOPIC_CANDIDATES`, default 5), research all in parallel as today, then a new index-based Opus judge (`rank_topics_by_richness`) keeps the best `DEEP_DIVE_COUNT` (3). Only the normal path over-provisions; the A/B fixed-topics path is untouched.

**Tech Stack:** Python 3.9, `anthropic` SDK (`client.messages.parse` structured outputs), Pydantic v2, pytest (added here as the first tests in the repo).

**Spec:** `docs/superpowers/specs/2026-07-09-topic-quality-overhaul-design.md`

## Global Constraints

- Python 3.9 compatible (repo runs on `.venv` Python 3.9). Use `from __future__ import annotations`; do not use 3.10+ syntax (`X | Y` in runtime-evaluated positions, `match`).
- Venv Python is `.venv/bin/python`. Run everything through it.
- Judge model is `EVAL_JUDGE_MODEL` (Opus, default `claude-opus-4-8`) — never the Sonnet writer.
- Topic filtering is **index-based and fail-safe**: the judge returns indices into the real dives; on any failure keep the first `keep` dives. Mirror `select_deep_dive` exactly.
- Over-provision + filter runs **only in the normal path** (`topics is None`). The fixed-topics path used by `experiment.py` must neither over-generate nor filter.
- History records only shipped dives — this is already true (`main.py` derives `topics` from `newsletter.deep_dives`); do not add recording of dropped topics.
- DRY, YAGNI, TDD, frequent commits.

---

### Task 1: Test harness + `topic_candidates` config

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_config.py`
- Modify: `deepdive/config.py:41-45` (add field) and `:107-110` (load it)
- Modify: `.env.example:24-25` (document the new var)

**Interfaces:**
- Consumes: nothing.
- Produces: `Config.topic_candidates: int` (default 5, min 1), read from env `TOPIC_CANDIDATES`. A working `pytest` invocation: `.venv/bin/python -m pytest`.

- [ ] **Step 1: Install pytest into the venv and pin it**

Create `requirements-dev.txt`:

```
pytest>=8.0
```

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pip install -r requirements-dev.txt
```

Expected: pytest installs successfully.

- [ ] **Step 2: Create the empty test package marker**

Create `tests/__init__.py` with no content (empty file).

- [ ] **Step 3: Write the failing config test**

Create `tests/test_config.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it fails**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest tests/test_config.py -v
```

Expected: FAIL — `AttributeError: 'Config' object has no attribute 'topic_candidates'`.

- [ ] **Step 5: Add the field to the `Config` dataclass**

In `deepdive/config.py`, add the field right after `deep_dive_count` (line 29):

```python
    deep_dive_count: int
    # How many candidate topics to research before the judge keeps the best
    # `deep_dive_count`. Over-provisioning lets us drop topics whose researched
    # content turns out thin. Only used in the normal (non-fixed-topics) path.
    topic_candidates: int
    newsletter_title: str
```

- [ ] **Step 6: Load it from the environment**

In `deepdive/config.py`, in the `return cls(...)` block, add right after the `deep_dive_count=deep_dive_count,` line (line 99):

```python
            deep_dive_count=deep_dive_count,
            topic_candidates=int_env("TOPIC_CANDIDATES", 5, minimum=1),
```

- [ ] **Step 7: Run the test to verify it passes**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest tests/test_config.py -v
```

Expected: PASS (3 passed).

- [ ] **Step 8: Document the var in `.env.example`**

In `.env.example`, replace lines 24-25:

```
# Number of deep dives per issue.
DEEP_DIVE_COUNT=3
```

with:

```
# Number of deep dives per issue (what actually ships).
DEEP_DIVE_COUNT=3

# Candidate topics researched before an Opus judge keeps the best DEEP_DIVE_COUNT.
# Over-provisioning lets thin topics be dropped after we see their real content.
TOPIC_CANDIDATES=5
```

- [ ] **Step 9: Commit**

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && git add requirements-dev.txt tests/__init__.py tests/test_config.py deepdive/config.py .env.example && git commit -m "Add pytest harness and TOPIC_CANDIDATES config"
```

---

### Task 2: Rewrite the topic-selection prompt + duration-honesty line

**Files:**
- Modify: `deepdive/curator.py:81-95` (`_TOPIC_SYSTEM`)
- Modify: `deepdive/curator.py:125-156` (`_RESEARCH_SYSTEM` — one added line)
- Create: `tests/test_prompts.py`

**Interfaces:**
- Consumes: nothing.
- Produces: revised `_TOPIC_SYSTEM` and `_RESEARCH_SYSTEM` string constants (same names, same module).

- [ ] **Step 1: Write a guard test for the prompt rewrite**

Create `tests/test_prompts.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest tests/test_prompts.py -v
```

Expected: FAIL (the current prompt has none of the new markers).

- [ ] **Step 3: Replace `_TOPIC_SYSTEM`**

In `deepdive/curator.py`, replace the whole `_TOPIC_SYSTEM = """..."""` block (lines 81-95) with:

```python
_TOPIC_SYSTEM = """\
You are the curator of "The Deep Dive," a weekly newsletter for one intellectually \
voracious reader. Your job is to choose topics that each open onto a genuinely rich \
2-hour intellectual journey — not clever trivia, but a slice of the world deep \
enough to get lost in.

You are a CURATOR, not a feed. The difference:
- A feed says "here's a cool thing" — a single surprising fact with nowhere to go \
once you've heard it (a fake chess-playing machine; a word for the smell of rain).
- A curator says "here's a two-hour journey into a world you didn't know had this \
much depth." The topic has real texture: history, ideas, people, open questions.

What makes a great Deep Dive topic:
- EXPANSIVE, not narrow. It should be niche because you probably haven't encountered \
it — not because it's a single hyper-specific curiosity. "The history of automata \
and the dream of mechanical life" beats "the one fake chess robot."
- DEEP enough to sustain two hours: a real body of excellent long-form content must \
exist — documentaries, serious essays and journalism, academic/long-form writing, \
substantial videos and lectures, good podcasts. Abundance of high-quality \
documentaries and serious literature is the strongest sign of a rich topic.
- CURATED in its framing. The best topics have a shape. For example (illustrations, \
not a checklist — invent your own):
    - start with one person, object, or event and expand outward into its whole world
    - a throughline: how several cultures or eras tackled the same deep question
    - a guided descent into a living field of research
- Intellectually rich: history, science, philosophy, art, technology, culture, and \
the strange places where fields meet.

Avoid: single-fact curiosities with no room to explore ("huh, neat" and you're done); \
overdone pop-science staples; anything generic; anything in the avoid-list.

The reaction you want is not "huh, weird" but "I had no idea there was THIS MUCH here"."""
```

- [ ] **Step 4: Add the duration-honesty line to `_RESEARCH_SYSTEM`**

In `deepdive/curator.py`, inside `_RESEARCH_SYSTEM`, find the bullet that currently reads (around line 150-152):

```python
- For each item, note its approximate duration and write a short, specific, warm note on \
why it's worth their time and what to expect — like a friend handing it over, not a \
catalog entry.
```

Replace it with (adds one honest-duration sentence):

```python
- For each item, note its approximate duration and write a short, specific, warm note on \
why it's worth their time and what to expect — like a friend handing it over, not a \
catalog entry. Estimate durations HONESTLY from the actual content: judge an article's \
read-time by its real length (don't inflate a <1000-word piece to "25 min"), and don't \
count a video's full runtime if much of it is off-topic.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest tests/test_prompts.py -v
```

Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && git add deepdive/curator.py tests/test_prompts.py && git commit -m "Rewrite topic-selection prompt for depth/curation; require honest durations"
```

---

### Task 3: `rank_topics_by_richness` — the Opus topic judge

**Files:**
- Modify: `deepdive/curator.py` — add `_TOPIC_RANK_SYSTEM`, `_TopicRanking`, and `rank_topics_by_richness` in the orchestration section (after `select_deep_dive`, before `_INTRO_SYSTEM`, around line 476)
- Create: `tests/test_rank_topics.py`

**Interfaces:**
- Consumes: `deepdive.models.DeepDive`, `deepdive.models.ContentItem`.
- Produces:
  - `rank_topics_by_richness(client: anthropic.Anthropic, judge_model: str, dives: List[DeepDive], keep: int, label: str = "") -> List[DeepDive]` — returns exactly `min(keep, len(dives))` dives (the kept ones, best-first). Skips the API call and returns `dives` unchanged when `len(dives) <= keep`. On any exception / empty / unusable output, returns `dives[:keep]`.
  - `_TopicRanking` (Pydantic `BaseModel` with `keep: List[int]`).
  - `_TOPIC_RANK_SYSTEM` (str).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rank_topics.py`:

```python
"""Unit tests for the post-research topic richness filter."""

import pytest

from deepdive import curator
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest tests/test_rank_topics.py -v
```

Expected: FAIL — `ImportError: cannot import name 'rank_topics_by_richness'`.

- [ ] **Step 3: Implement the ranker**

In `deepdive/curator.py`, add this block immediately after `select_deep_dive` returns (after line 475, before the `# --- Orchestration ---` / `_INTRO_SYSTEM` section):

```python
# --- Stage 5: rank whole topics by researched richness -----------------------


_TOPIC_RANK_SYSTEM = """\
You are the editor of "The Deep Dive," choosing which of several fully-researched \
topics are strong enough to run this week. Each topic below shows its framing and the \
real content that research actually found for it.

Keep the topics that form the richest, most substantial ~2-hour journeys. Reward, in \
order:
1. DEPTH & SUBSTANCE — genuinely enough excellent material for two hours, not one thin \
idea padded out. An abundance of serious long-form (documentaries, academic/serious \
writing, substantial videos and lectures, real podcast episodes) is the strongest signal.
2. CURATION — a coherent throughline, a topic with real texture and places to go, not a \
single-fact curiosity ("huh, neat" and you're done).
3. VARIETY across the kept set — different domains and moods.

Drop the thin ones: topics whose found content is sparse, mostly one format, padded, or \
only tangentially on-topic. Return ONLY the indices of the topics to keep, best-first."""


class _TopicRanking(BaseModel):
    keep: List[int] = Field(
        description="0-based indices of the topics to keep, best-first."
    )


def rank_topics_by_richness(
    client: anthropic.Anthropic,
    judge_model: str,
    dives: List[DeepDive],
    keep: int,
    label: str = "",
) -> List[DeepDive]:
    """Keep the ``keep`` richest researched topics, judged by a stronger (Opus) model.

    Ranking is by INDEX into ``dives`` and we rebuild from the original DeepDive objects,
    so the judge can only subset/reorder — never invent a topic (same safety property as
    ``select_deep_dive``). Skips the call when there aren't more topics than we need, and
    falls back to the first ``keep`` on any failure.
    """
    if len(dives) <= keep:
        return dives  # nothing to trim

    listing = "\n".join(
        f"[{i}] {d.title} — {d.dek}\n"
        f"     {' '.join((d.hook or '').split())[:300]}\n"
        f"     found content:\n" + "\n".join(
            f"       - ({it.kind}, {it.duration}) {it.title} — {it.source}"
            for it in d.items
        )
        for i, d in enumerate(dives)
    )
    user = (
        f"Researched topics:\n{listing}\n\n"
        f"Keep the best {keep}, ordered best-first. Return their indices."
    )

    ranking = None
    try:
        response = client.messages.parse(
            model=judge_model,
            max_tokens=500,
            system=_TOPIC_RANK_SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_format=_TopicRanking,
        )
        ranking = response.parsed_output
    except Exception as exc:  # noqa: BLE001 - fall back to research order on any failure
        _log(f"  topic-rank call failed ({type(exc).__name__}); keeping first {keep}")

    kept: List[DeepDive] = []
    if ranking is not None:
        seen = set()
        for idx in ranking.keep:
            if 0 <= idx < len(dives) and idx not in seen:
                seen.add(idx)
                kept.append(dives[idx])
            if len(kept) >= keep:
                break
    if not kept:
        kept = dives[:keep]  # fallback: research/return order

    dropped = [d.title for d in dives if d not in kept]
    if dropped:
        _log(f"  topic filter: kept {len(kept)} of {len(dives)}; dropped: {'; '.join(dropped)}")
    return kept
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest tests/test_rank_topics.py -v
```

Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && git add deepdive/curator.py tests/test_rank_topics.py && git commit -m "Add rank_topics_by_richness: Opus judge filters researched topics by depth"
```

---

### Task 4: Wire over-provision + filter into the pipeline

**Files:**
- Modify: `deepdive/curator.py:531-592` (`build_newsletter` signature + body)
- Modify: `deepdive/main.py:84-86` (pass new args) and `:78-82` (status line)
- Modify: `deepdive/experiment.py:122-124` (pass `judge_model` positionally)
- Modify: `ARCHITECTURE.md` (§4 pipeline, §7 config table)
- Create/extend: `tests/test_build_newsletter.py`

**Interfaces:**
- Consumes: `select_topics`, `_research_and_structure`, `rank_topics_by_richness`, `_edition_intro` (all in `curator`); `Config.topic_candidates`, `Config.eval_judge_model`.
- Produces: new `build_newsletter` signature:
  `build_newsletter(client, model, history, count, settings, judge_model, topics=None, topic_candidates=None) -> Newsletter`
  where `count` is the number that ships and `topic_candidates` (defaulting to `count`) is how many to research in the normal path. Fixed-topics mode (`topics` given) ignores `topic_candidates` and does not filter.

- [ ] **Step 1: Write the failing wiring test**

Create `tests/test_build_newsletter.py`:

```python
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

    def fake_select(client, model, history, count):
        captured["candidate_count"] = count
        return [make_topic(f"T{i}", "angle") for i in range(count)]

    def fake_research(client, model, topic, settings, label):
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
    assert len(nl.deep_dives) == 3                     # filtered to count
    assert [d.title for d in nl.deep_dives] == ["T0", "T1", "T2"]
    assert nl.intro == "intro"


def test_fixed_topics_mode_does_not_filter(monkeypatch):
    # When topics are passed in (A/B mode), research them all and ship them all.
    def fake_research(client, model, topic, settings, label):
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest tests/test_build_newsletter.py -v
```

Expected: FAIL — `TypeError: build_newsletter() ... 'judge_model'` (signature doesn't accept it yet).

- [ ] **Step 3: Update the `build_newsletter` signature and docstring**

In `deepdive/curator.py`, replace the signature (lines 531-538) and docstring:

```python
def build_newsletter(
    client: anthropic.Anthropic,
    model: str,
    history: List[str],
    count: int,
    settings: "ResearchSettings",
    judge_model: str,
    topics: "Optional[List[_TopicIdea]]" = None,
    topic_candidates: "Optional[int]" = None,
) -> Newsletter:
    """Run the full pipeline and return a finished Newsletter.

    ``count`` is how many deep dives ship. In the normal path we research
    ``topic_candidates`` topics (default ``count``; set higher to over-provision) and an
    Opus ``judge_model`` keeps the richest ``count``. If ``topics`` is given (fixed-topics
    mode, for A/B experiments), those are researched verbatim and none are filtered.
    """
```

- [ ] **Step 4: Over-provision at selection (and record the normal-path flag)**

In `build_newsletter`, replace the topic-selection block (lines 545-551) with this — note `topics_selected_fresh`, which Step 5 uses to gate filtering:

```python
    topics_selected_fresh = topics is None
    if topics_selected_fresh:
        n_candidates = topic_candidates or count
        topics = select_topics(client, model, history, n_candidates)
        _log("  Topics chosen:")
    else:
        _log("  Topics (fixed):")
    for topic in topics:
        _log(f"    - {topic.title}")
```

- [ ] **Step 5: Filter after the parallel research block**

In `build_newsletter`, find the lines (currently 582-587):

```python
    dives = [d for d in slots if d is not None]
    if not dives:
        raise RuntimeError("All topics failed to research:\n" + "\n".join(errors))

    _log("  Writing the editor's note...")
    intro = _edition_intro(client, model, dives)
```

Replace with (insert the filter between them):

```python
    dives = [d for d in slots if d is not None]
    if not dives:
        raise RuntimeError("All topics failed to research:\n" + "\n".join(errors))

    # Normal path over-provisions topics; keep only the richest `count`. Fixed-topics
    # mode (A/B) holds its set constant, so skip filtering there.
    if topics_selected_fresh and len(dives) > count:
        _log(f"  Judging {len(dives)} researched topics; keeping the best {count}...")
        dives = rank_topics_by_richness(client, judge_model, dives, count)

    _log("  Writing the editor's note...")
    intro = _edition_intro(client, model, dives)
```

- [ ] **Step 6: Run the wiring tests to verify they pass**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest tests/test_build_newsletter.py -v
```

Expected: PASS (2 passed).

- [ ] **Step 7: Update the `main.py` call site and status line**

In `deepdive/main.py`, replace the `build_newsletter` call (lines 84-86):

```python
    newsletter = curator.build_newsletter(
        client, cfg.model, past, cfg.deep_dive_count, settings,
        cfg.eval_judge_model, topic_candidates=cfg.topic_candidates,
    )
```

Then update the "Curation:" status line. Find this existing block:

```python
    print(
        f"  Curation: research {settings.candidate_items} candidates per topic, "
        f"verify, then select the best {settings.final_items}."
    )
```

and replace it with:

```python
    print(
        f"  Curation: research {cfg.topic_candidates} topics, judge-select the best "
        f"{cfg.deep_dive_count}; per topic research {settings.candidate_items} "
        f"candidates, verify, then select the best {settings.final_items}."
    )
```

- [ ] **Step 8: Update the `experiment.py` call site**

In `deepdive/experiment.py`, replace the `build_newsletter` call (lines 122-124) so it passes `judge_model` and no `topic_candidates` (fixed-topics mode never over-provisions):

```python
        newsletter = curator.build_newsletter(
            client, cfg.model, [], len(topics), settings, cfg.eval_judge_model,
            topics=topics,
        )
```

- [ ] **Step 9: Verify the full suite still passes and imports are clean**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m pytest -v && .venv/bin/python -c "import deepdive.main, deepdive.experiment, deepdive.curator"
```

Expected: all tests PASS; import line prints nothing (no import error).

- [ ] **Step 10: Update `ARCHITECTURE.md`**

In `ARCHITECTURE.md` §4 ("The per-issue sequence"), add a step after topic selection and before per-topic research describing over-provisioning, and add a final "topic filter" step. Specifically, update step 1 to note it now returns `TOPIC_CANDIDATES` topics, and add after the per-topic steps:

```
7. **Topic filter** (`rank_topics_by_richness`) — after all candidate topics are fully
   researched, an Opus judge keeps the richest `DEEP_DIVE_COUNT`, dropping topics whose
   found content is thin. Index-based (can't invent a topic); fail-safe to the first N.
   Normal path only — the A/B fixed-topics path is never filtered.
```

In §7's config table, add a row:

```
| `TOPIC_CANDIDATES` | candidate topics researched before the judge keeps the best `DEEP_DIVE_COUNT` (over-provision, default 5) |
```

- [ ] **Step 11: Commit**

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && git add deepdive/curator.py deepdive/main.py deepdive/experiment.py ARCHITECTURE.md tests/test_build_newsletter.py && git commit -m "Wire topic over-provision + judge filter into build_newsletter"
```

---

### Task 5: End-to-end verification (dry run)

**Files:** none (verification only).

**Interfaces:**
- Consumes: the whole pipeline.
- Produces: evidence the feature works against the real API.

- [ ] **Step 1: Run a dry run with over-provisioning explicit**

Run (writes preview + JSON, no email):

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && TOPIC_CANDIDATES=5 DEEP_DIVE_COUNT=3 .venv/bin/python -m deepdive.main --dry-run --no-open 2>&1 | tee out/dryrun-topicfilter.log
```

Expected in the log:
- "Curation: research 5 topics, judge-select the best 3 ..."
- 5 topics listed under "Topics chosen:"
- "Researching 5 topics in parallel..."
- A "Judging 5 researched topics; keeping the best 3..." line and a "topic filter: kept 3 of 5; dropped: ..." line
- Exactly 3 dives in the final "    - <title> (N items)" summary
- "Wrote preview: out/preview.html"

- [ ] **Step 2: Confirm the shipped issue has 3 dives**

Run:

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -c "import json; d=json.load(open('out/issue.json')); print('dives:', len(d['deep_dives'])); [print(' -', x['title']) for x in d['deep_dives']]"
```

Expected: `dives: 3` and three titles that read as expansive/curated (not one-dimensional trivia).

- [ ] **Step 3: Sanity-check topic quality by eye**

Open `out/preview.html` (or read the titles/heks from Step 2). Confirm the chosen topics look like 2-hour journeys with a curated shape, and that the dropped topics (from the log) were the thinner ones. If topics still look one-dimensional, that's a prompt-tuning follow-up — note it, don't block.

- [ ] **Step 4: Regression — experiment harness still runs fixed topics unfiltered**

Run a minimal 1-variant, limited A/B to confirm the signature change didn't break it (uses the golden set; keep `--limit` small to control cost):

```bash
cd "/Users/johndrotos/Desktop/Deep Dive" && .venv/bin/python -m deepdive.experiment --limit 2 --variant "" 2>&1 | tail -30
```

Expected: it researches exactly the 2 fixed golden topics, prints a scores table, and never logs a "topic filter:" line.

---

## Notes for the executor

- **No new runtime dependencies.** `requirements-dev.txt` (pytest) is dev-only.
- **`count` vs `topic_candidates`:** `count` is what ships (`DEEP_DIVE_COUNT`); `topic_candidates` is what's researched (`TOPIC_CANDIDATES`). Keep them distinct everywhere.
- **Fixed-topics mode is sacred:** never over-provision or filter when `topics` is passed — the A/B harness relies on a constant topic set.
- **Cost:** Task 5's dry run and the A/B regression make real API calls (~5 research calls + 1 Opus judge for the dry run). That's expected and intended as the end-to-end proof.
