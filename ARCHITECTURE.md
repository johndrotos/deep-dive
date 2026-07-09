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

For each of `DEEP_DIVE_COUNT` topics, run **in parallel** (`ThreadPoolExecutor`):

1. **Select topics** (`select_topics`) — one structured call, no web. Returns title+angle
   pairs, avoiding the last ~200 history topics. (In A/B mode, a fixed topic list is passed
   in instead via `build_newsletter(topics=...)` / `make_topic`.)

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

6. **Editor's note** (`_edition_intro`) + assemble the `Newsletter`.

Then `main.py`: **render** (`renderer.render_html`) → **send** (`mailer.send`, Resend) →
**record history** (`history.record`). On a `--dry-run`/`--eval`, it writes
`out/preview.html` and `out/issue.json` instead of emailing.

### Why the pipeline is split into separate calls

Each Claude call does **one job** (pick / research / structure / select / intro). A call
that researches on the web *and* emits perfect JSON *and* balances formats would do all
three badly. Splitting keeps each reliable. Over-provisioning (step 2) + verify (4) +
select (5) together make the final page **dead-link-resilient**: dropping a bad link no
longer shrinks a topic below target, because we select from a larger verified pool.

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
  - Judge = **Opus** (`EVAL_JUDGE_MODEL`, default `claude-opus-4-8`) — a *stronger* model
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

---

## 7. Configuration (all via `.env`; see `.env.example`)

**Secrets / delivery:** `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NEWSLETTER_TO`,
`NEWSLETTER_FROM`.

**Models & basics:** `ANTHROPIC_MODEL` (writer; currently `claude-sonnet-4-6` to save
tokens), `EVAL_JUDGE_MODEL` (judge; `claude-opus-4-8`), `DATA_DIR`, `NEWSLETTER_TITLE`,
`DEEP_DIVE_COUNT` (topics per issue).

**Search depth / curation (all tunable, no code change):**
| Var | Meaning |
|---|---|
| `SEARCH_EFFORT` | reasoning effort per turn (low/medium/high) |
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

**Known issues / not done:**
- **0-item structuring bug (open):** `structure_deep_dive` occasionally returns 0 items from
  a good brief → an empty topic ships. Not yet fixed (a retry guard is the intended fix).
- **Not deployed:** runs locally only; no Railway, no cron.
- **Content-match:** removed (see §6.8).

---

## 11. Roadmap

1. **Quality (Goal 1) — in progress.** Have: over-provision+select, verification, the eval
   scorecard, the A/B harness, the 0–10 judge. Open ideas: research-prompt tweak for format
   diversity; the 0-item guard; possibly re-baselining effort/model with the now-trustworthy eval.
2. **Deploy (Goal 2).** Register + verify a domain; Railway with a persistent volume for
   history and a weekly cron; failure alerting. Design it with Goal 3 in mind (a feedback
   web endpoint may mean deploying a small always-on service, not just a cron).
3. **Feedback (Goal 3).** Start with the cheap, serverless slice — standing preferences
   ("more history, less politics") fed into topic selection, or reply-to-train via the Gmail
   API — which doubles as the per-user preference system multi-user needs.
4. **Multi-user (Goal 4).** The web app, auth, subscriber DB, and shared content library,
   built on the proven base (see §2).
