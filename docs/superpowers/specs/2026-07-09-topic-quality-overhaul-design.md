# Topic Quality Overhaul — Design Spec

**Date:** 2026-07-09
**Status:** Approved design, pending spec review → implementation plan
**Companion docs:** `ARCHITECTURE.md` (§4 curation pipeline, §6 lessons), `PIPELINE_TIMING.md`

---

## 1. Problem

Topics currently feel **one-dimensional and too small for a proper deep dive.** Looking at
a real issue:

- **"The Turk" (fake mechanical chess player)** — weak. Too specific: a single-fact
  curiosity ("someone fooled everyone for 84 years") with nowhere to go once you've heard
  it. The long YouTube "documentary" was mostly chess analysis; the "25-minute read"
  article was under 1000 words. There is not two hours of real material behind it.
- **"The smell of rain / petrichor"** — passable only because the underlying *chemistry*
  is genuinely interesting; strip that out and the topic is thin.
- **"Byzantine timekeeping / Orthros"** — the strongest, but still at risk of returning
  too little material; would be richer expanded toward *monastic life and how those orders
  interacted with society.*

### Root cause

The current `_TOPIC_SYSTEM` prompt **actively rewards the failure mode.** It asks for
topics that are *"niche, obscure"* where *"the reaction should be 'I had no idea that was a
whole world.'"* That optimizes for **surprise-per-square-inch** — the "cool Instagram post"
quality — and rewards a narrow trivia nugget like The Turk over an expansive, curated topic.
It never asks the load-bearing question: *does this topic have two hours of real, substantial
material behind it?*

Two distinct problems are tangled together:

1. **Topic shape** — too specific, one-dimensional, not curated. (A *prompt* problem: we ask
   for the wrong thing.)
2. **Content sufficiency** — even a decent topic may not return two hours of substantial
   material, and nothing in the pipeline checks. (A *signal* problem.)

---

## 2. Goals & non-goals

**Goals**
- Topics that open onto a genuinely rich ~2-hour intellectual journey, curated (with a
  shape/throughline), and *expansive* — niche because unexpected, not because hyper-specific.
- A mechanism that catches and drops topics whose *researched* content turns out thin.

**Non-goals (YAGNI for this change)**
- No changes to the A/B experiment harness's fixed-topics path (it must hold topics
  constant, so it neither over-generates nor filters).
- No changes to rendering, mailing, verification, or the item-level over-provision+select.
- No new deployment/scheduling work.

---

## 3. The three changes

### 3.1 Rewrite the topic-selection prompt (fixes *topic shape*)

Replace `_TOPIC_SYSTEM` in `curator.py`. Key shifts: reframe from "surprising nugget" to
"curated expansive journey," state the depth requirement explicitly, and give curated-angle
patterns as *illustrations* (not a rigid menu — a menu produces formulaic output). The Turk
and rain-smell are used as anti-examples; "history of automata" is the reshaped good version.

```
You are the curator of "The Deep Dive," a weekly newsletter for one intellectually
voracious reader. Your job is to choose topics that each open onto a genuinely rich
2-hour intellectual journey — not clever trivia, but a slice of the world deep
enough to get lost in.

You are a CURATOR, not a feed. The difference:
- A feed says "here's a cool thing" — a single surprising fact with nowhere to go
  once you've heard it (a fake chess-playing machine; a word for the smell of rain).
- A curator says "here's a two-hour journey into a world you didn't know had this
  much depth." The topic has real texture: history, ideas, people, open questions.

What makes a great Deep Dive topic:
- EXPANSIVE, not narrow. It should be niche because you probably haven't encountered
  it — not because it's a single hyper-specific curiosity. "The history of automata
  and the dream of mechanical life" beats "the one fake chess robot."
- DEEP enough to sustain two hours: a real body of excellent long-form content must
  exist — documentaries, serious essays and journalism, academic/long-form writing,
  substantial videos and lectures, good podcasts. Abundance of high-quality
  documentaries and serious literature is the strongest sign of a rich topic.
- CURATED in its framing. The best topics have a shape. For example (illustrations,
  not a checklist — invent your own):
    · start with one person, object, or event and expand outward into its whole world
    · a throughline: how several cultures or eras tackled the same deep question
    · a guided descent into a living field of research
- Intellectually rich: history, science, philosophy, art, technology, culture, and
  the strange places where fields meet.

Avoid: single-fact curiosities with no room to explore ("huh, neat" and you're done);
overdone pop-science staples; anything generic; anything in the avoid-list.

The reaction you want is not "huh, weird" but "I had no idea there was THIS MUCH here."
```

### 3.2 Over-provision + judge-filter the topics (fixes *content sufficiency*)

Only in the normal path (`topics is None`). The fixed-topics path used by `experiment.py`
is untouched.

```
select_topics → returns TOPIC_CANDIDATES topics (new config, default 5)
      ↓
research all 5 in parallel   ← existing _research_and_structure per topic
   (research → structure → verify → select-items), unchanged
      ↓
rank_topics_by_richness (NEW)  ← one Opus judge call
   reads the 5 finished DeepDives, ranks by depth / substance / curation /
   documentary-academic abundance, returns indices of the top DEEP_DIVE_COUNT (3)
      ↓
keep the top 3 → editor's note → render → send
```

**New function** `rank_topics_by_richness(client, judge_model, dives, keep) -> List[DeepDive]`
in `curator.py`, modeled on `select_deep_dive`:

- **Judge model:** `EVAL_JUDGE_MODEL` (Opus, default `claude-opus-4-8`) — consistent with
  the eval philosophy: a stronger model that isn't grading its own writing. `build_newsletter`
  gains a `judge_model` parameter (threaded from `cfg.eval_judge_model` in `main.py`); the
  A/B harness passes its own value or the default.
- **Index-based & hallucination-safe:** the judge returns *indices* into the 5 dives; we
  rebuild the kept list from the real `DeepDive` objects (identical safety property to
  `select_deep_dive` — it can only subset/reorder, never invent).
- **Fail-safe:** on any exception, malformed output, or too-few indices, fall back to
  keeping the first `keep` dives (research/return order). Log the fallback.
- **Skip guard:** if `len(dives) <= keep` (i.e. `TOPIC_CANDIDATES <= DEEP_DIVE_COUNT`), skip
  the judge call entirely and keep all — same pattern as `select_deep_dive`.
- **Input to the judge:** per-dive summary — title, dek/hook, and the kept items
  (kind, duration, source, title) — enough to assess substance without dumping full notes.

**New prompt** `_TOPIC_RANK_SYSTEM`: rank the researched dives by which form the richest,
most substantial, best-curated ~2-hour experiences; reward genuine depth, an abundance of
serious long-form (documentaries, academic/serious writing, substantial videos and
podcasts), and a coherent curated throughline; penalize one-dimensional topics, thin or
padded content, and single-fact curiosities. Return the indices of the top `keep`.

**Config:** new `TOPIC_CANDIDATES` env var → `Config.topic_candidates: int`, default **5**,
minimum 1 (parallels `DEEP_DIVE_COUNT`/`SEARCH_CANDIDATE_ITEMS`). Threaded into
`build_newsletter`. Add to `.env.example` and to `ARCHITECTURE.md` §7 config table.

**Where the filter runs:** after the parallel `ThreadPoolExecutor` block in
`build_newsletter` produces the finished dives, before `_edition_intro`. The editor's note
and everything downstream see only the surviving 3.

### 3.3 Duration honesty (small supporting change)

Part of the Turk failure was a <1000-word article labeled "25 min read." Add one line to
the research prompt (`_RESEARCH_SYSTEM`), where durations are first produced, instructing
honest durations: estimate real length/runtime from the actual content; do not inflate a
short article's read-time or count a mostly-off-topic video's full length. This keeps padded
durations from fooling the richness judge. (`_STRUCTURE_SYSTEM` already says to preserve the
brief's values, so it needs no change.)

---

## 4. Decisions (resolved)

- **History records only the 3 that shipped.** Already true for free: `main.py` derives the
  recorded `topics` from `newsletter.deep_dives`, which contains only the survivors. The
  dropped 2 never enter it, so they can resurface another week rather than being burned
  forever. No code change needed for this; noted so it isn't "fixed" by accident.
- **Post-research, judge-only filtering** (chosen over pre-research and over objective/hybrid
  signals): the failure mode is *substance*, which a link-count or self-reported duration
  misses, so we pay to research more and let a strong judge read the real dives.
- **Judge model = Opus** (`EVAL_JUDGE_MODEL`), not the Sonnet writer.

### Cost & timing (stated plainly)

- Researching **5 topics instead of 3 ≈ +67% research cost** every week, plus **one Opus
  judge call** per issue.
- **Wall-clock ≈ unchanged.** Research is already parallel, so total time is ~the slowest of
  5 topics instead of the slowest of 3 (a small increase in expectation), plus the one judge
  call (~a few seconds) and the item-selection on 5 rather than 3 dives.

---

## 5. File-by-file changes

| File | Change |
|---|---|
| `deepdive/curator.py` | Rewrite `_TOPIC_SYSTEM` (3.1); add `_TOPIC_RANK_SYSTEM` + `rank_topics_by_richness` (3.2); add `judge_model` param to `build_newsletter` and call the filter after the parallel block; make `select_topics` request `topic_candidates`; small duration-honesty line in `_RESEARCH_SYSTEM` (3.3) |
| `deepdive/config.py` | Add `topic_candidates` field + `TOPIC_CANDIDATES` env (default 5, min 1) |
| `deepdive/main.py` | Pass `cfg.topic_candidates` and `cfg.eval_judge_model` into `build_newsletter`; update the "Curation:" status line to mention research-N-keep-M |
| `deepdive/experiment.py` | Ensure fixed-topics path still bypasses over-generation/filter; pass a `judge_model` (default) so the signature change is satisfied |
| `.env.example` | Document `TOPIC_CANDIDATES` |
| `ARCHITECTURE.md` | Update §4 pipeline (add topic over-provision+select step), §7 config table, §11 roadmap note |

---

## 6. Testing & verification

- **Unit-level (fast, cheap — per working-style):** test `rank_topics_by_richness` in
  isolation with stubbed judge responses: (a) normal case returns the requested indices in
  order; (b) malformed/empty output falls back to first `keep`; (c) skip guard when
  `candidates <= keep` returns all without calling the judge; (d) out-of-range/duplicate
  indices are handled like `select_deep_dive`.
- **End-to-end:** one `--dry-run` with `TOPIC_CANDIDATES=5`, `DEEP_DIVE_COUNT=3`. Confirm from
  `out/dryrun.log`: 5 topics chosen, 5 researched in parallel, one richness-rank line, 3
  survivors shipped, and `out/preview.html` renders 3 dives. Verify the log shows the judge
  call and the dropped titles.
- **Prompt sanity:** eyeball that the 5 chosen topics read as expansive/curated (not
  Turk-style nuggets), and that the 3 survivors are the more substantial ones.
- **Regression:** `experiment.py` with a fixed golden set still researches exactly those
  topics and does not invoke the topic filter.

---

## 7. Open risks

- **Judge cost/latency** is now on the weekly critical path (one extra Opus call). Acceptable;
  it's a few seconds and one call.
- **Judge could be fooled by padded durations** — mitigated by 3.3 (honest durations) and by
  the judge reading item kinds/sources, not just claimed minutes.
- **Chronically thin topics** may be regenerated and re-dropped occasionally (mild wasted
  research) because we don't record the dropped 2. Accepted trade-off (keeps the pool wide).
