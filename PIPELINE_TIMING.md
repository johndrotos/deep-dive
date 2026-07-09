# The Deep Dive — Pipeline Steps & Timing

A step-by-step map of what happens during one run, how long each step *should* take,
and where the time actually goes. Written from **measured** timestamps (the `+Ns`
counters in `out/dryrun.log`), not guesses. Model: `claude-sonnet-4-6`, settings
`effort=medium, max_tokens=6000, max_uses=3, max_rounds=0`.

---

## 1. The two loops (recap)

There are two nested loops, driven by two different things:

- **Outer loop — our Python code.** Picks topics, then dispatches each topic's research.
  As of the parallelism change, the topics run **concurrently**, so the outer loop's
  wall-clock is ≈ the *slowest single topic*, not the sum.
- **Inner loop — the Claude API, server-side.** Inside one research call, Claude
  autonomously searches the web, waits for the tool to filter results, then writes the
  brief. We don't control this loop's steps; we only bound it (`max_uses`, `max_rounds`,
  `max_tokens`, `effort`).

---

## 2. The steps, in order, with expected timing

Times below are per-run wall-clock. "Per topic" steps run in parallel across topics.

| # | Step | Function | What it does | Typical time | Bounded by |
|---|------|----------|--------------|--------------|------------|
| 1 | **Select topics** | `select_topics` | One plain API call (no web). Returns N topic titles + angles, avoiding history. | **3–5 s** | one short completion |
| 2 | **Research** *(per topic, parallel)* | `research_topic` → `_stream_one_turn` | The agentic web-search turn. Breaks into 2a/2b below. | **~1.5–6+ min**, highly variable | `max_uses`, `max_tokens`, `effort`, and the tool's filtering |
| 2a | &nbsp;&nbsp;↳ Web searches | (inside 2) | Up to `max_uses` searches; each returns 7–10 results. | **15–30 s total** (~2–6 s each) | `max_uses` |
| 2b | &nbsp;&nbsp;↳ **Filter + write brief** | (inside 2) | Server-side dynamic-filtering code execution on the results, **then** generating the brief text. **This is the slow, variable part.** | **60 s → 370 s+** | the tool's filtering + `max_tokens` |
| 3 | **Structure** *(per topic, parallel)* | `structure_deep_dive` | One API call (no web). Turns the free-text brief into the strict `DeepDive` schema (title, items, URLs). | **15–30 s** | one medium completion |
| 4 | **Editor's note** | `_edition_intro` | One short API call (no web) framing the issue. | **3–10 s** | one short completion |

**Total wall-clock (parallel)** ≈ step 1 + (slowest topic's 2 + 3) + step 4.
- Best case: ~3 min.
- Realistic: 4–6 min.
- Bad-luck case: 8+ min, entirely because one topic's step 2b spikes.

---

## 3. Where the time actually goes — measured

From the 2-topic run (`out/dryrun.log`), topic "Cryonics":

```
+  4s   research starts
+ 23s   all 3 searches done          ← searching took ~19 s
+148s   turn complete (4087 tokens)  ← step 2b took ~125 s
+173s   structured & done            ← structuring took ~25 s
```

**Searching is cheap (~20 s). Step 2b — filter + write brief — is ~85% of a topic's
time.** The other topic in the same run ("Synoptic Problem") finished searching at +27s
and then never completed step 2b in 370s+ before we killed it.

### Why step 2b is slow *and* unpredictable — now CONFIRMED with phase logging

Originally inferred from the `max_tokens` math; since **confirmed directly** by
phase-level logging (the `· entering: <phase>` lines and the 15 s heartbeat). For a
single search returning 9 results, the log showed:

```
+9s   search done, 9 results
+9s   · entering: result filtering (code execution)   ← round 1
+13s  · entering: result filtering (code execution)   ← round 2
+15s  …still working: phase='result filtering (code execution)', ~0 output tokens
+17s  · entering: result filtering (code execution)   ← round 3
+19s  · entering: writing brief                        ← text finally starts
+29s  turn complete (768 tokens)
```

Confirmed facts:

- The tail **is** the `web_search_20260209` dynamic-filtering **code execution** — and it
  runs in **multiple rounds** per search (~4 cycles for one search above), each a few seconds.
- The heartbeat proves it's **not** text generation: during filtering, `output tokens` stayed
  at **~0**. Text ("writing brief") only starts *after* filtering finishes.
- It doesn't count against `max_tokens`, so it isn't capped — a topic can run well past the
  ~3 min text ceiling. That's why two topics with identical settings finished 125 s vs. 370 s+
  apart: **more searches × more results × more filtering rounds = a longer, variable tail.**

---

## 4. The specific incident (topic 1 "never completed")

Topic 1 finished its 3 searches at +27s, then sat in step 2b for 370s+ with no output.
It was **not hung in our code** and **not text-generating** (that would have capped out at
~3 min). It was waiting on the server-side dynamic-filtering code execution for that
topic's search results, which ran far longer than topic 2's. Left alone it would either
have eventually completed or hit the client `timeout=600s` and raised — we killed it first.

---

## 5. Instrumentation (the black box is now open)

The logging now makes step 2b fully visible:

- **`· entering: <phase>`** on every content block — search / search results / result
  filtering (code execution) / filter output / thinking / writing brief. This shows the
  exact sequence and how many filtering rounds run.
- **A 15 s heartbeat** — a background timer that fires *even during a silent server-side
  step*, printing the current phase and cumulative output-token count. This is what lets us
  distinguish "stuck filtering" (tokens flat at ~0) from "slowly writing text" (tokens rising).
- **Live output-token count** from `message_delta` events.

All log lines carry a `[HH:MM:SS +Ns]` timestamp, so gaps between phases are directly readable.

---

## 6. Levers — what actually changes the timing

Ranked by impact on the slow part (step 2b):

| Lever | Effect | Trade-off |
|-------|--------|-----------|
| **Web-search tool variant** | Switching `web_search_20260209` (dynamic filtering) → basic `web_search_20250305` removes the server-side code-execution filtering tail entirely. Likely the biggest, most predictable speedup. | Slightly less pre-filtered results; Claude sifts them itself. Quality impact needs a test. |
| **`max_tokens`** (brief length) | Directly caps the text-generation half of 2b. Lower = shorter briefs, faster. | Shorter briefs → less material for structuring. |
| **`effort`** | `low` vs `medium` changes reasoning depth in 2b. | Lower can mean shallower curation. |
| **`max_uses`** (searches) | Caps step **2a** only — which is already only ~20 s. **Low impact on total time.** | Trimming it barely helps speed; it mainly affects link coverage. |
| **Parallelism** *(done)* | Turns total time from *sum of topics* into *slowest topic*. Already in place. | None — pure win. |
| **`max_rounds`** | Each extra round is a whole additional turn (another 2a+2b). Keep at 0 for speed. | Higher = deeper digs, much slower. |

**Key correction to an earlier assumption:** trimming `max_uses` was expected to speed
things up, but the timestamps show searching is already cheap. The real levers are the
**web-search tool variant** and **`max_tokens`**, not the search count.

---

## 7. Recommended next experiment

To get a fast, predictable run: try the **basic `web_search_20250305`** variant (removes
the filtering tail) at `effort=low, max_tokens=4000`, and add `code_execution`-block
logging so step 2b is no longer a black box. Then compare link quality against the
dynamic-filtering runs to decide whether the filtering is worth its cost.
