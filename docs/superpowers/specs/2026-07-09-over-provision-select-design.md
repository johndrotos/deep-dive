# Spec: Over-provision + verified selection

**Goal:** Raise curation quality and make each topic resilient to dead-link drops by
researching *more* candidates than needed, verifying them, then having the model select
the best 3-4 survivors — balanced for format diversity and the ~2.5 hr time budget.

## Pipeline change (per topic)

Before: `research (3-4 items) → structure → verify (drop dead) → done`

After: `research (N candidates) → structure → verify (drop dead) → select (best K) → done`

Selection runs **after** verification, so we always choose from links that actually work.
Over-provisioning means a dead-link drop no longer shrinks a topic below target.

## Components

1. **Research** (`_RESEARCH_SYSTEM` + `research_topic`): prompt for **N candidate items**
   (default 6), ordered best-first. More searches (`max_uses` 3→5) and a longer brief cap
   (`max_tokens` 6000→8000) to fit them. Basic search tool unchanged.
2. **Structure** (unchanged): brief → `DeepDive` with all N candidates.
3. **Verify** (unchanged): drop genuinely-dead URLs, keep bot-blocked/live.
4. **Select** (NEW — `select_deep_dive`): a cheap no-tools call that picks the best **K**
   (default 4) survivors, re-ordered into a sensible arc, balancing **format diversity**
   and the **~2.5 hr time budget**.

### Selection is hallucination-safe by construction
The select call returns **indices** of items to keep, not rewritten items. We reconstruct
`dive.items` from the *original* `ContentItem` objects at those indices — so the URLs and
notes are byte-for-byte the verified ones; selection cannot introduce a new bad URL.

**Fallbacks:** if survivors ≤ K, skip the select call (nothing to trim). If the model
returns invalid/empty indices, fall back to the first K survivors by research order.

## Config (new env knobs, same pattern as existing)
- `SEARCH_CANDIDATE_ITEMS` (N, default 6) — how many to research/verify.
- `DEEP_DIVE_ITEMS` (K, default 4) — how many make the final page.
- `SEARCH_MAX_USES` 3→5, `SEARCH_MAX_TOKENS` 6000→8000.

## Tradeoff
Quality up + dead-link-resilient; cost is a longer brief (slower) + one cheap select call
per topic. Fully bounded (basic tool — no filtering loop).

## Out of scope (v1)
- Touching up the topic hook after trimming (usually general enough; revisit if needed).
- Content-match verification (URL resolves *and* matches claimed content) — a later Goal-1
  upgrade.
