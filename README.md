# The Deep Dive

A personal weekly newsletter agent. Once a week it emails you a beautifully formatted
issue with **3 niche, obscure, intellectually interesting deep dives** — each ~2–3 hours
of curated content (documentaries, long essays, long videos, podcasts, quality articles),
written like a knowledgeable friend pointed you at the best stuff.

- **LLM:** Anthropic Claude (`claude-opus-5`)
- **Real links:** Claude curates with live **web search**, so URLs are real, not invented
- **Email:** Resend
- **Runs itself:** Railway cron, every Sunday ~8am
- **No repeats:** past topics are remembered on a persistent volume

---

## How it works

```
load history → pick 3 fresh topics → research each on the live web →
structure to JSON → render HTML → email via Resend → record topics
```

See `docs/superpowers/specs/2026-06-29-the-deep-dive-design.md` for the full design.

---

## Run it locally first

You'll need Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your keys
```

### 1. Preview the design — free, offline

No API calls, no email. Renders canned content so you can judge how it looks:

```bash
python -m deepdive.main --fake
```

This writes `out/preview.html` and opens it in your browser.

### 2. Real dry run — curates for real, but doesn't email

Uses Claude + web search to build a genuine issue, then writes `out/preview.html`
instead of sending. Costs a few cents to a couple dollars in API usage and takes a few
minutes. History is left untouched (add `--record` to also save the topics):

```bash
python -m deepdive.main --dry-run
```

### 3. Send for real

```bash
python -m deepdive.main
```

Curates, emails you, and records the topics so next week won't repeat them.

> **Resend setup:** sign up at [resend.com](https://resend.com), create an API key, and
> verify a sending domain. For a first test you can set
> `NEWSLETTER_FROM="The Deep Dive <onboarding@resend.dev>"` (Resend's sandbox sender)
> and send only to your own verified address.

---

## Deploy to Railway (runs automatically every week)

1. Push this repo to GitHub and create a new Railway project from it.
2. **Add a Volume** to the service, mounted at `/data` (this stores topic history so
   weeks don't repeat across deploys).
3. **Set environment variables** (Railway → Variables) from `.env.example`:
   - `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `NEWSLETTER_TO`, `NEWSLETTER_FROM`
   - `DATA_DIR=/data`
4. The schedule is already declared in `railway.json` as `0 8 * * 0`
   (Sunday 08:00 UTC). Railway runs the service on that cron, it builds the issue,
   emails you, and exits.

To change the day/time, edit `cronSchedule` in `railway.json` (UTC).
To test the deployed version on demand, trigger the cron service manually from the
Railway dashboard.

---

## Project layout

```
deepdive/
  config.py      env vars, validated up front
  history.py     remembers past topics (JSON on the volume)
  models.py      Pydantic shapes: ContentItem / DeepDive / Newsletter
  curator.py     selects topics, researches with web search, structures output
  renderer.py    Jinja2 -> email-safe HTML
  mailer.py      sends via Resend
  samples.py     canned content for --fake
  main.py        CLI / orchestrator
  templates/newsletter.html
railway.json     start command + weekly cron
.env.example     the variables you fill in
```

## Cost & tuning

One run a week, so cost is small (roughly cents to a couple dollars depending on how much
the model searches). Knobs via env vars: `ANTHROPIC_MODEL` (e.g. `claude-sonnet-5` to
spend less), `DEEP_DIVE_COUNT`, `NEWSLETTER_TITLE`.
