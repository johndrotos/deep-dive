# The Deep Dive — Design

A personal weekly newsletter agent. Once a week it emails the owner a beautifully
formatted newsletter containing **3 niche, obscure, intellectually interesting topic
deep dives**. Each deep dive is ~2–3 hours of curated content (documentaries, long-form
essays, long YouTube videos, podcasts, quality-outlet articles), written in the voice of
a knowledgeable friend pointing you at the best stuff — not a listicle.

## Decisions (confirmed with owner)

- **LLM:** Anthropic Claude API, model `claude-opus-4-8`.
- **Real links:** Claude uses the live **web search** server tool while curating, so URLs
  are real and current rather than hallucinated.
- **Email delivery:** Resend.
- **Schedule:** Sunday ~08:00, weekly, via Railway cron.
- **Topic memory:** a small JSON file on a persistent Railway **Volume** (`/data`),
  so weeks never repeat. Local fallback to `./data`.

## Architecture

One Python app run as a **one-shot process** by Railway cron (not a long-running server).
Each run:

```
load history → select 3 fresh topics → research each (web search) →
structure to JSON → render HTML → send via Resend → record topics
```

Modules (each small and isolated):

| Module | Responsibility |
|---|---|
| `deepdive/config.py` | Load + validate env vars; fail loudly if required ones are missing. |
| `deepdive/history.py` | Read/write past topics JSON at `DATA_DIR/history.json`. Prevents repeats. |
| `deepdive/models.py` | Pydantic models: `ContentItem`, `DeepDive`, `Newsletter`. |
| `deepdive/curator.py` | The brain. Topic selection + per-topic web-search research + structuring. |
| `deepdive/renderer.py` + `templates/newsletter.html` | Jinja2 → editorial, email-safe HTML. |
| `deepdive/mailer.py` | Send the HTML via Resend. |
| `deepdive/samples.py` | Canned `Newsletter` for offline `--fake` testing. |
| `deepdive/main.py` | Orchestrator + CLI (`--dry-run`, `--fake`, `--record`). |

## Curation pipeline (per run)

1. **Select topics** — one structured Claude call (no tools) returns 3 niche topics +
   a one-line angle each, explicitly avoiding everything in the history list and obvious
   overdone subjects.
2. **Research each topic** — a streaming Claude call with the `web_search` tool and
   adaptive thinking. Claude finds the genuinely best mixed-format content and writes
   warm, specific notes. Handles `pause_turn` continuations.
3. **Structure each topic** — a `messages.parse()` call (no tools, Pydantic schema)
   turns the research into a clean `DeepDive`.

Splitting research (tools) from structuring (schema) keeps each call reliable.

## Local testing

- `python -m deepdive.main --fake` — renders canned content to `out/preview.html` and
  opens it in the browser. **No API calls, no email** — for iterating on the design.
- `python -m deepdive.main --dry-run` — runs the *real* curation (Claude + web search)
  but writes `out/preview.html` and opens it instead of emailing. Does not touch history
  unless `--record` is also passed.
- `python -m deepdive.main` — the production path: curate, email, record history.

## Deployment (Railway)

- `railway.json` declares the start command and the weekly cron `0 8 * * 0`.
- Attach a Volume mounted at `/data`; set `DATA_DIR=/data`.
- Fill in env vars (see `.env.example`).

## Env vars

| Var | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | — | Claude API key. |
| `RESEND_API_KEY` | yes | — | Resend API key. |
| `NEWSLETTER_TO` | yes | — | Recipient (owner's email). |
| `NEWSLETTER_FROM` | yes | — | Verified sender, e.g. `The Deep Dive <dd@yourdomain.com>`. |
| `ANTHROPIC_MODEL` | no | `claude-opus-4-8` | Curation model. |
| `DATA_DIR` | no | `./data` | Where history.json lives (set `/data` on Railway). |
| `DEEP_DIVE_COUNT` | no | `3` | Deep dives per issue. |
| `NEWSLETTER_TITLE` | no | `The Deep Dive` | Masthead title. |
