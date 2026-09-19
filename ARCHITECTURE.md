# The Deep Dive — Source of Truth

This is the holistic onboarding document for the project. If you are a new contributor
(human or AI) this should get you fully up to speed: the product vision and the business
decisions behind it, the complete curation pipeline, the evaluation pipeline, the hard-won
engineering lessons, and the current state with its known issues.

Companion docs: `PIPELINE_TIMING.md` (deep dive on where run-time goes) and
`docs/superpowers/specs/` (design specs for individual features).

---

## 1. What this is

**The Deep Dive** is a weekly newsletter agent. Once a week it:

1. Picks a few genuinely niche, intellectually-rich topics (avoiding anything covered before).
2. For each topic, researches the live web and curates ~1–2 hours of the *best existing
   content* — documentaries, long essays, serious videos, podcasts, primary sources.
3. Verifies every link actually resolves, then selects the strongest, best-rounded set.
4. Writes warm "knowledgeable friend" notes for each pick and an editor's note.
5. Renders a nicely-designed HTML email and sends it.

It is powered by the Anthropic Claude API (Python), and there is a full **evaluation
system** for measuring and tuning issue quality with real numbers.

---

## 2. Business case & vision (read this before making architectural calls)

### Where it is and where it's going

- **Today:** a **personal proof of concept.** One recipient (the owner), run manually,
  not yet deployed. The goal right now is to prove the *content is good enough that people
  would subscribe* — that is the riskiest unproven assumption.
- **Eventually:** a **multi-user product** — real subscribers, per-user preferences, a
  feedback loop, a web presence.

### The chosen strategy: personal-first, future-proofed at the seams

We deliberately did **not** build multi-user infrastructure up front. The reasoning:

**Pros of personal-first:**
- Validates the *core value* (is the curation genuinely good?) before spending effort on
  auth, signup, unsubscribe, billing, a subscriber DB, deliverability-at-scale, and a web
  app — none of which matter if the content isn't worth subscribing to.
- Lets the owner dogfood weekly and shape quality from real use.
- Avoids guessing multi-user requirements before they're understood (YAGNI).

**Cons / risks we're accepting:**
- Some single-user shortcuts are painful to unwind later — chiefly the **data model.**
  History/preferences currently live in a flat JSON file keyed to nobody.
- We are *knowingly* deferring the platform work; when subscribers arrive, there is a real
  build ahead (web app, auth, shared content library, deliverability).

### The key insight that makes personal-first safe here

**Research is user-independent.** A deep dive on "bog bodies" is the same content for one
reader or a thousand. So the eventual multi-user version is **not a rewrite** of what
exists — it's mostly a new *orchestration + delivery* layer on top. The core components
(`curator`, `verify`, `renderer`, `evaluate`) survive as-is. Multi-user becomes: maintain a
*shared library* of researched dives, and give each user a *personalized selection*.

### The seams to draw when multi-user work begins (planned, NOT yet built)

When the time comes, these cheap choices keep it additive instead of a migration:
1. **Key all user data by a user id** (even with one user) — history/preferences per-user.
2. **Move to SQLite** (one file, zero infra, but a real relational DB to grow into). This is
   the one "invest early" call — storage is the expensive-to-retrofit decision.
3. **Preferences as a first-class input** to topic selection, not hardcoded prompt text.
4. **Keep content separate from delivery** — a dive is a standalone artifact that could go
   to anyone.

Defer until actually needed: web frontend, auth/signup, unsubscribe, billing, scaled
email deliverability.

---

## 3. Mental model: how the agent pipeline works

Two nested loops, driven by two different things:

- **Outer loop — our Python code** (`curator.build_newsletter`). Picks topics, then
  dispatches each topic's research. Topics run **concurrently** (threads), so wall-clock ≈
  the slowest single topic, not the sum.
- **Inner loop — the Claude API, server-side.** Inside one research call, Claude
  autonomously searches the web, reads results, searches again, then writes a brief. We
  don't script those steps; we bound them (`max_uses`, `max_rounds`, `max_tokens`, `effort`).

The Claude API is **stateless** — each call is independent and we send the full context
every time. Curation "taste" lives in prompts + the model's judgment, not in code.

---

## 4. The curation pipeline

### Data flow

```
                         main.py  (CLI / conductor)
                            │
        ┌───────────────────┼───────────────────────────────┐
        ▼                   ▼                                ▼
   config.py           history.py                       curator.py  ── the brain
 (env settings)   (past topics, JSON)                       │
                                                            ▼
                                                        models.py
                                            (Newsletter → DeepDive → ContentItem)
                                                            │
                                    ┌───────────────────────┼───────────┐
                                    ▼                       ▼           ▼
                               renderer.py             mailer.py    evaluate.py
                            (data → HTML email)      (Resend send)   (scoring)
```

### The per-issue sequence (in `curator.build_newsletter`)

Research runs **in parallel** (`ThreadPoolExecutor`) across `TOPIC_CANDIDATES` topics, then a
judge keeps the best `DEEP_DIVE_COUNT`:

1. **Select topics** (`select_topics`) — one structured call, no web. Returns
   `TOPIC_CANDIDATES` (default 5) title+angle pairs, avoiding the last ~200 history topics.
   Over-provisions so the topic filter (step 6) can drop the thin ones. (In A/B mode, a fixed
   topic list is passed in instead via `build_newsletter(topics=...)` / `make_topic`, and
   neither over-provisioning nor the filter runs.)

2. **Research** (`research_topic` → `_stream_with_backoff` → `_stream_one_turn`) — the
   agentic step. **Streamed** (critical — see lessons below), with the **basic web-search
   tool** (`web_search_20250305`). Over-provisions: asks for `SEARCH_CANDIDATE_ITEMS`
   (default 6) candidate items, best-first. Produces a free-text brief.
   - Granular logging: each web search (`search N/max`), phase transitions
     (`entering: writing brief`), a 15s heartbeat, per-turn token/stop-reason summary.
   - `pause_turn` handling + transient-error backoff (`_stream_with_backoff`).

3. **Structure** (`structure_deep_dive`) — one structured call, no web. Brief → typed
   `DeepDive` (title, dek, hook, estimated_time, items). Preserves URLs exactly; invents
   nothing.

4. **Verify** (`verify.verify_dive`) — fetch every candidate URL concurrently and drop
   **genuinely-dead** links (404/410/5xx; YouTube via oEmbed). **Keeps** bot-blocked
   links (403/429/202 — real content that refuses automated fetch). Liveness only.

5. **Select** (`select_deep_dive`) — one structured call. From the verified survivors, pick
   the best `DEEP_DIVE_ITEMS` (default 4), balancing format diversity and a sensible arc.
   **Hallucination-safe by construction:** the model returns *indices*, and we rebuild
   `items` from the original `ContentItem` objects — selection can never introduce a new URL.

6. **Topic filter** (`rank_topics_by_richness`) — after all `TOPIC_CANDIDATES` topics are
   researched, one Opus (`EVAL_JUDGE_MODEL`) call keeps the richest `DEEP_DIVE_COUNT`,
   dropping topics whose found content is thin. **Index-based** (rebuilds from the real
   `DeepDive` objects, so it can never invent a topic) and fail-safe to the first N. Normal
   path only — the A/B fixed-topics path is never filtered.

7. **Editor's note** (`_edition_intro`) + assemble the `Newsletter`.

Then `main.py`: **render** (`renderer.render_html`) → **send** (`mailer.send`, Resend) →
**record history** (`history.record`). On a `--dry-run`/`--eval`, it writes
`out/preview.html` and `out/issue.json` instead of emailing.

### Why the pipeline is split into separate calls

Each Claude call does **one job** (pick / research / structure / select / intro). A call
that researches on the web *and* emits perfect JSON *and* balances formats would do all
three badly. Splitting keeps each reliable. Over-provisioning (step 2) + verify (4) +
select (5) together make the final page **dead-link-resilient**: dropping a bad link no
longer shrinks a topic below target, because we select from a larger verified pool.

### 4a. Effort policy: how hard each stage thinks

Current models think by default, and `max_tokens` is a hard cap on **thinking plus answer**
(§6.10 is the outage that taught us). So each stage declares two separate things in
`curator._POLICY`:

- **`effort`** — how much *judgment* the step deserves. The real dial. It does **not**
  track output size: `structure` turns an already-researched brief into a long document
  (little judgment, lots of text → `low` effort, the biggest rail), while `select_items`
  weighs a dozen candidates to emit six integers (real judgment, tiny output → `medium`
  effort, a small rail).
- **`max_tokens`** — a safety rail with thinking headroom. Set loosely, never "tuned".

| Stage | effort | rail | why |
|---|---|---|---|
| `select_topics` | `high` | 8000 | open-ended curation; the hardest judgment in the pipeline |
| research | `medium` | `SEARCH_MAX_TOKENS` | tool-driven; its rail is a real content knob (brief length) |
| `structure` | `low` | 16000 | mechanical transform of a brief; long output, little judgment |
| `select_items` | `medium` | 4000 | quality/format/arc tradeoffs → a handful of indices |
| `rank_topics` | `medium` | 4000 | same, on the Opus judge |
| `edition_intro` | `low` | 2000 | short, light writing |

`DEPTH` (`fast` / `balanced` / `deep`) shifts every row one notch along
`low → medium → high → xhigh → max`, preserving their relative shape, so cost and
thoroughness move together from a single knob.

**Truncation is named and recovered, not swallowed.** A cut-off response reaches the
caller in one of two shapes, depending on where the budget ran out, and *neither* says
"truncated" on its face:

- **no text block at all** → `parsed_output is None`, indistinguishable from "the model
  had nothing to say" (the shape that produced the 2026-09-13 traceback);
- **a half-written one** → `parse()` raises a pydantic `ValidationError`.

`curator._structured` handles both: it reads `stop_reason` for the first and, for the
second, treats a `json_invalid` error as truncation (with `output_config.format` the body
is schema-constrained server-side, so unparseable JSON means cut off, not malformed) while
letting a genuine schema mismatch through as its own error. `_with_headroom` then retries
the stage **once** a notch lower in effort with a doubled rail — the one failure here that
is mechanically recoverable.

Research streams with `thinking={"type": "adaptive", "display": "summarized"}` rather than
the `"omitted"` default: the summaries keep the wire active and surface long reasoning
phases in the log, instead of the silent gap that used to look like a stalled connection.

### 4b. What an issue costs, and where

Every run accumulates `usage` per stage in `curator.UsageLedger` and prints a costed
breakdown; `main` persists it into `data/issue.json` so the A/B harness can compare
variants on price as well as quality. Rates live in `_RATES_USD_PER_MTOK` with the date
they were checked — **a model absent from that table is counted in tokens but never
priced**, so a model swap cannot produce a confident wrong number.

Measured 2026-09-19, `claude-sonnet-5`, `DEPTH=balanced`, 5 candidate topics → 3 shipped:

| stage | calls | input | output | searches | USD |
|---|---|---|---|---|---|
| research | 5 | 409,480 | 21,010 | 23 | **$1.2591** |
| structure | 5 | 20,326 | 8,545 | 0 | $0.1261 |
| select_topics | 1 | 1,604 | 2,456 | 0 | $0.0278 |
| select_items | 5 | 6,030 | 173 | 0 | $0.0138 |
| rank_topics (Opus judge) | 1 | 2,439 | 15 | 0 | $0.0126 |
| edition_intro | 1 | 196 | 202 | 0 | $0.0024 |
| **total** | | | | | **$1.4417** |

**Research is 87% of the bill, and it is an input-token problem, not an output one.**
Search-result tokens alone are $0.82 — 57% of the issue. The 23 searches add $0.23. All
output across the entire pipeline is $0.32.

Consequences worth remembering before optimizing the wrong thing:

- `SEARCH_MAX_USES` is the dominant lever: each search costs a cent *and* drags roughly
  18k input tokens behind it. 5 → 3 saves on the order of $0.50.
- Over-provisioning has a visible price. Researching 5 topics to ship 3 discards ~$0.50
  of research per issue — deliberate (it buys the judge real choice and survives dead
  links), but now quantified rather than assumed.
- **`DEPTH` is a quality dial, not a cost lever.** It moves thinking, thinking is output
  tokens, and output is a fifth of the bill. An earlier version of the README claimed
  otherwise; the measurement disproved it.
- The Opus judge costs $0.0126. Its model choice is a quality decision with no meaningful
  cost consequence.

---

## 5. The evaluation pipeline

Quality was "eyeballed" until we built a way to **measure** it. Two tools:

### 5a. The scorecard — `evaluate.py`

Scores a single issue on two tiers of metric:

- **Objective** (computed in code, free, deterministic, fully trustworthy):
  - `link_liveness` — fraction of links that resolve (reuses `verify.check_url`).
  - `link_specificity` — fraction pointing to a specific page (not a homepage / search page).
  - `avg_distinct_formats` — format variety per topic (target ≥3).
  - `frac_topics_time_on_target` — topics whose summed duration lands in 60–180 min.
  - `frac_topics_at_item_target` — topics that hit the item count.
- **Judged** (LLM scores each topic against a rubric):
  - `note_quality`, `curation_quality`, `topic_interest`, each **0–10** (see §6 on why 0–10).
  - Judge = **Opus** (`EVAL_JUDGE_MODEL`, default `claude-opus-5`) — a *stronger* model
    than the Sonnet that writes issues, so it isn't grading its own work.
  - **Single pass per topic.** The eval calls the judge once per topic (the multi-pass runs
    you may see referenced were a one-off noise-measurement probe, not the real eval).

Run it: `python -m deepdive.main --eval` (curate + score) or
`python -m deepdive.evaluate out/issue.json` (re-score a saved issue). Saves
`out/eval-<ts>.json` with the generating settings recorded, so you build a settings→scores
history.

### 5b. The A/B experiment harness — `experiment.py`

Compares **settings** on a **fixed topic set** so score deltas are attributable to the
setting, not to random topic luck (topics vary hugely in how much good content exists).

- `experiments/golden.json` holds the fixed topics (a deliberate mix of content-rich and
  obscure, so a setting must perform across the range).
- Each `--variant` is a `ResearchSettings` override set (`"label:key=val,key=val"`); an empty
  variant is the baseline (`.env` settings). Every variant researches the **same** topics.
- Prints a **side-by-side table with deltas** vs the first variant; saves
  `out/experiment-<ts>.json`.

```
python -m deepdive.experiment --variant "" --variant "candidate_items=9,max_uses=6"
python -m deepdive.experiment --limit 2 --variant "" --variant "effort=high"   # cheaper
```

**Cost note:** each variant is a full curate + judge, run sequentially. A 4-topic ×
3-variant sweep is ~12 research + 12 judge calls — use `--limit` while exploring.

### Reading eval results honestly (lessons learned)

- **Objective > judged for trust.** Objective metrics are deterministic. Judged metrics
  carry model noise.
- **Judge noise, quantified:** on the old **1–5** scale the judge flipped a full point
  (4↔5) on *identical* input for ~⅓–½ of scorings — because true quality sits between
  integers and the scale was too coarse. Moving to **0–10** (with anchored rubric)
  essentially eliminated the flipping (identical scores across 4 passes for 2 of 3 metrics),
  making the **single-pass** eval trustworthy.
- **Control your variables.** An early A/B looked like "content-match is +2 formats!" — but
  the baseline had a broken 0-item topic that deflated it. The tooling only helps if you read
  it critically (check for anomalies, hold topics fixed, treat small judged deltas as noise).

---

## 6. Key engineering decisions & hard-won lessons

These are the non-obvious things that cost real debugging. Don't undo them without reading.

1. **Stream the research call.** A non-streaming web-search call goes silent on the wire for
   minutes → the connection is killed by a read timeout → the SDK re-runs the whole expensive
   request (token bleed). Streaming keeps the wire active (deltas + pings) so long jobs never
   time out. This was the single biggest reliability fix.

2. **Use the BASIC web-search tool, not dynamic filtering.** `web_search_20260209`
   (dynamic filtering) runs server-side result-filtering *code execution* in an **unbounded
   loop** on Sonnet 4.6 — observed 67 filtering rounds, 0 output tokens, 10+ min, never
   completing. There is no knob to cap the filtering. The basic `web_search_20250305` has no
   filtering step (results go straight to Claude) and finishes a 3-topic issue in ~90s.
   Toggle via `SEARCH_DYNAMIC_FILTERING` — but re-test before ever switching back.
   Full analysis in `PIPELINE_TIMING.md`.

3. **The brief-writing, not the searching, is the slow part.** Searches are ~20–25s total;
   generating the brief is the tail. So the speed levers are `max_tokens` / `effort`, *not*
   `max_uses`.

4. **Over-provision + select, not "just curate 4".** Research 6, verify, select 4. Makes the
   page resilient to dead-link drops and lets selection balance formats.

5. **Selection is index-based** so it can never introduce a hallucinated URL — it can only
   re-order/subset the already-verified items.

6. **Verify keeps bot-blocked links.** 403/429 are usually real content (paywalled journals,
   Bloomberg, IMDb) that refuse bots. Dropping them would lose good links; only 404/410/5xx
   and YouTube-unavailable are treated as dead.

7. **Widen judge scales to avoid rounding noise** (see §5). Coarse integer scales flip.

8. **Content-match was built and removed.** An LLM "does this page match the claim?" check
   was added, then removed: it false-positived on real links (dropped "Finding Longitude"
   because the page title was "Finding Longitude | Dava Sobel" — a publisher suffix it should
   have ignored). Lesson recorded: validate the base pipeline over several runs before
   layering features that need careful false-positive tuning.

9. **Declare every direct dependency, and cap the majors.** `requirements.txt` listed
   `anthropic>=0.113` with no upper bound and never listed `httpx`, which `verify.py`
   imports — it arrived transitively via `anthropic` 0.x. `anthropic` 1.0.0 (2026-08-20)
   switched its HTTP layer to the `httpx2` fork, so CI stopped installing `httpx` and every
   scheduled run died at import in ~1s. Three weeks of issues were missed. CI installs fresh
   each week, so an unpinned major is a scheduled outage waiting to happen.

10. **A model ID is not a configuration value, and `max_tokens` is not a tuning knob.**
    Swapping `claude-sonnet-4-6` → `claude-sonnet-5` (and `opus-4-8` → `opus-5`) flipped a
    server-side default: on the old models a request that omits `thinking` runs *without*
    thinking; on the new ones it runs adaptive thinking at `high` effort. `max_tokens` caps
    thinking **plus** answer, so every call whose budget had been trimmed to the expected
    answer (topic selection at 2000, the index picks at 500) began truncating mid-thought.
    `parse()` reports that as `parsed_output is None`, not an exception — so topic
    selection killed the 2026-09-13 issue with "returned no topics" (the model had said
    plenty), while item selection and topic ranking just silently fell back.

    Measured against the live API on 2026-09-19 (`claude-opus-5`, the topic-selection
    prompt, 12 topics of history): at `max_tokens=2000` the model spent **1999 tokens
    thinking**, returned `['thinking']` and no text block at all, and stopped on
    `max_tokens`. At 8000 the same request finished in 2610 tokens (1780 of them thinking)
    and returned all five topics; at 2000 with `thinking: {"type": "disabled"}` it finished
    in 556. The budget was never close — thinking alone needed more than the whole rail.
    The fix is structural, not a
    bigger number: **effort** is the per-stage intent dial, **`max_tokens`** is a loose rail
    with thinking headroom, and truncation is caught by name and retried (§4a). The tests
    missed all of it because the fakes returned a canned `parsed_output` with no
    `stop_reason`.

11. **Alert from the outermost layer that can see the failure.** `_alert_failure` lives
    inside `run()`, so it can only report failures that `run()` is alive to catch. Every
    other way the job can die — an import error (§6.9), a `ConfigError` that returns
    before `cfg` exists, a failed `pip install`, the job timeout, a failed history push —
    was silent, and silence looks exactly like a week nobody wrote an issue. The fix is a
    workflow-level `if: failure()` step, which fires whatever the cause because GitHub
    knows the job failed. Two details that only surfaced by *running* it rather than
    reading it: it must be the **last** step, or a later step's failure happens after the
    alert was already skipped; and Resend sits behind Cloudflare, which 403s urllib's
    default `Python-urllib/3.x` User-Agent (error 1010), so the alert silently never
    reached the API until a real User-Agent was set — an alerting path that fails quietly
    is worse than none, because you believe you are covered.

---

## 7. Configuration (all via `.env`; see `.env.example`)

**Secrets / delivery:** `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NEWSLETTER_TO`,
`NEWSLETTER_FROM`.

**Models & basics:** `ANTHROPIC_MODEL` (writer; currently `claude-sonnet-5` to save
tokens), `EVAL_JUDGE_MODEL` (judge; `claude-opus-5`), `DATA_DIR`, `NEWSLETTER_TITLE`,
`DEEP_DIVE_COUNT` (topics per issue).

**Search depth / curation (all tunable, no code change):**
| Var | Meaning |
|---|---|
| `DEPTH` | pipeline-wide effort profile: `fast` / `balanced` / `deep`. Shifts **every** stage's reasoning effort one notch (§4a), research included |
| `TOPIC_CANDIDATES` | candidate topics researched before the judge keeps the best `DEEP_DIVE_COUNT` (over-provision, default 5) |
| `SEARCH_EFFORT` | *deprecated* — pins research's per-turn effort and ignores `DEPTH`. Leave unset except to pin an A/B variant |
| `SEARCH_MAX_TOKENS` | length cap on the brief (main speed lever) |
| `SEARCH_MAX_USES` | web searches per turn |
| `SEARCH_MAX_ROUNDS` | extra `pause_turn` continuation turns (0 = one turn) |
| `SEARCH_DYNAMIC_FILTERING` | `false` = basic tool (recommended); `true` = filtering tool (loops on Sonnet) |
| `SEARCH_CANDIDATE_ITEMS` | candidates researched per topic (over-provision, default 6) |
| `DEEP_DIVE_ITEMS` | items that survive selection onto the page (default 4) |

Real secrets live only in `.env` (git-ignored). `.env.example` is placeholders only.

---

## 8. How to run (CLI — `main.py`, plus the two eval entry points)

| Command | Does |
|---|---|
| `python -m deepdive.main` | Live: curate → email → record history |
| `python -m deepdive.main --dry-run` | Curate for real, write `out/preview.html` + `out/issue.json`, no email |
| `python -m deepdive.main --eval` | Curate (no email) → score → print scorecard |
| `python -m deepdive.main --fake` | Offline canned content → preview (design iteration, no API) |
| `python -m deepdive.main --record` | With `--dry-run`, also record topics to history |
| `python -m deepdive.evaluate [out/issue.json]` | Re-score a saved issue |
| `python -m deepdive.experiment --variant ... ` | A/B settings on the fixed golden topics |

Note the venv is `.venv` (`.venv/bin/python`).

---

## 9. File map

| File | Role |
|---|---|
| `deepdive/main.py` | CLI entry / conductor; issue JSON + preview persistence |
| `deepdive/config.py` | Env config, validated up front (`Config.load`) |
| `deepdive/curator.py` | **The brain:** `ResearchSettings`, topic select, research (streaming + logging), structure, **select**, parallel orchestration, `build_newsletter`, `make_topic` |
| `deepdive/verify.py` | Link liveness verification (`check_url`, `verify_dive`) |
| `deepdive/evaluate.py` | Scorecard: objective + judged (0–10) metrics; `--eval` core |
| `deepdive/experiment.py` | A/B harness over fixed topics |
| `deepdive/models.py` | Pydantic contract: `Newsletter` → `DeepDive` → `ContentItem` |
| `deepdive/renderer.py` | `DeepDive`/`Newsletter` → email HTML (Jinja2, `templates/newsletter.html`) |
| `deepdive/mailer.py` | Resend send |
| `deepdive/history.py` | Past-topics JSON (`DATA_DIR/history.json`) so weeks don't repeat |
| `deepdive/runlog.py` | Shared timestamped logger (`[HH:MM:SS +Ns]`) |
| `deepdive/samples.py` | Canned sample newsletter for `--fake` |
| `experiments/golden.json` | Fixed topic set for A/B experiments |

---

## 10. Current state & known issues

**Working & validated:** fast parallel curation on the basic tool (~90s–2min/issue),
liveness verification, over-provision+select, the full eval + A/B tooling. A real live send
succeeded end-to-end. A validated baseline scored 100% live links and 8/10-ish judged quality.

**Delivery status:** using Resend's **sandbox sender** (`onboarding@resend.dev`), which can
only deliver to the account owner's own Gmail. The intended domain
(`johnsdeepdive.com`) is **not registered** — to send to anyone else, a real domain must be
registered and verified in Resend, then `NEWSLETTER_FROM` pointed at it.

**Deployment status:** deployed on **GitHub Actions** (`.github/workflows/weekly.yml`), not
Railway — cron `0 12 * * 0` (Sunday 08:00 EDT), secrets in repo settings, tuning env vars
inline in the workflow. History is committed back to `data/history.json` by the run itself,
which is why the repo (not a volume) is the store. `railway.json` is kept only as an
alternative host; it needs a volume at `/data` and `DATA_DIR=/data` if ever used.

**Known issues / not done:**
- **0-item structuring bug (possibly fixed, unconfirmed):** `structure_deep_dive`
  occasionally returned 0 items from a good brief → an empty topic ships. A truncated
  response is a plausible cause, and §4a now catches and retries exactly that; whether it
  was *the* cause is unverified — watch for a recurrence before closing this out.
- **Failure alerting (closed 2026-09-19):** `_alert_failure` still only covers exceptions
  raised *inside* `run()`, but `weekly.yml` now ends with an `if: failure()` step that
  emails via Resend whatever killed the job — an import error, a `ConfigError` that exits
  before the mailer exists, a failed `pip install`, the 20-minute timeout, or a failed
  history push. See §6.11.
- **Content-match:** removed (see §6.8).

---

## 11. Roadmap

1. **Quality (Goal 1) — in progress.** Have: over-provision+select, verification, the eval
   scorecard, the A/B harness, the 0–10 judge. Open ideas: research-prompt tweak for format
   diversity; the 0-item guard; possibly re-baselining effort/model with the now-trustworthy eval.
2. **Deploy (Goal 2) — mostly done.** The weekly cron runs on GitHub Actions (see §10).
   Remaining: register + verify a real domain so sending isn't limited to the owner's own
   inbox, and close the failure-alerting hole (§10). Keep Goal 3 in mind — a feedback web
   endpoint may mean a small always-on service, which is where `railway.json` would earn
   its place.
3. **Feedback (Goal 3).** Start with the cheap, serverless slice — standing preferences
   ("more history, less politics") fed into topic selection, or reply-to-train via the Gmail
   API — which doubles as the per-user preference system multi-user needs.
4. **Multi-user (Goal 4).** The web app, auth, subscriber DB, and shared content library,
   built on the proven base (see §2).
