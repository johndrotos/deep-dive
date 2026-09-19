"""The brain: pick fresh topics, research them on the live web, structure the result.

Pipeline per run:
  1. select_topics()      - one structured call, no tools, avoids history.
  2. research_topic()     - non-streaming call WITH web search, real current links.
  3. structure_deep_dive() - one parse() call, no tools, research -> DeepDive.

Splitting "research with tools" from "structure to schema" keeps each call reliable.

Every stage runs under an explicit effort policy (see "Effort policy" below). Current
models think by default, and `max_tokens` caps thinking *plus* answer, so each stage
declares how hard it should think and keeps a rail loose enough to survive it.

Research streams with `display: "summarized"` thinking: the summaries keep the wire
active and make the long server-side phases visible in the log, instead of the silent
gap that used to trip read timeouts.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import List, Optional

import anthropic
from pydantic import BaseModel, Field, ValidationError

from . import verify
from .models import DeepDive, Newsletter
from .runlog import log as _log

# Transient server-side conditions worth retrying with backoff. An overload fires
# before meaningful generation, so retrying it costs ~nothing in tokens.
_RETRYABLE = (
    anthropic.APITimeoutError,
    anthropic.InternalServerError,  # 5xx, includes 529 overloaded
    anthropic.APIConnectionError,
)
_BACKOFF_SECONDS = (5, 15, 40)


# --- Effort policy: how hard each stage thinks -------------------------------
#
# `max_tokens` is a hard cap on thinking PLUS answer text, and current models (Sonnet 5,
# Opus 5) think by default when `thinking` is omitted. A budget trimmed to the expected
# answer therefore truncates mid-thought, and `parse()` reports that as an empty result
# rather than an error. Two knobs, never to be conflated:
#
#   effort     - how much JUDGMENT the step deserves. The real dial. Set per stage.
#   max_tokens - a safety rail with thinking headroom. Set loosely; never "tuned".
#
# Effort does not track output size. `structure` turns an already-researched brief into
# a long document (little judgment, lots of text -> low effort, big rail), while
# `select_items` weighs a dozen candidates to emit six integers (real judgment, tiny
# output -> medium effort, small rail).

_EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max")

# One pipeline-wide profile shifts every stage by a notch, keeping their relative shape.
_DEPTH_SHIFT = {"fast": -1, "balanced": 0, "deep": 1}
DEPTHS = tuple(_DEPTH_SHIFT)
DEFAULT_DEPTH = "balanced"


class StagePolicy(BaseModel):
    """The effort one stage runs at, plus the rail that catches a runaway."""

    effort: str
    max_tokens: int


_POLICY = {
    "select_topics": StagePolicy(effort="high", max_tokens=8000),
    "structure": StagePolicy(effort="low", max_tokens=16000),
    "select_items": StagePolicy(effort="medium", max_tokens=4000),
    "rank_topics": StagePolicy(effort="medium", max_tokens=4000),
    "edition_intro": StagePolicy(effort="low", max_tokens=2000),
}

# Research is the one stage whose rail is a real content knob (how long a brief may be),
# so it keeps its own max_tokens on ResearchSettings; only its effort comes from here.
_RESEARCH_EFFORT = "medium"


def shift_effort(effort: str, notches: int) -> str:
    """Move an effort level along the ladder, clamped at both ends."""
    start = _EFFORT_LADDER.index(effort) if effort in _EFFORT_LADDER else 1
    return _EFFORT_LADDER[max(0, min(len(_EFFORT_LADDER) - 1, start + notches))]


def policy_for(stage: str, depth: str = DEFAULT_DEPTH) -> StagePolicy:
    """The stage's policy, shifted by the pipeline-wide depth profile."""
    base = _POLICY[stage]
    shifted = shift_effort(base.effort, _DEPTH_SHIFT.get(depth, 0))
    return base.model_copy(update={"effort": shifted})


def research_effort_for(depth: str = DEFAULT_DEPTH) -> str:
    """Research's per-turn effort under a given depth profile."""
    return shift_effort(_RESEARCH_EFFORT, _DEPTH_SHIFT.get(depth, 0))


class TruncatedError(RuntimeError):
    """A response hit `max_tokens` before finishing - recoverable by giving it room."""


def _no_output_error(stage: str, response) -> Exception:
    """Turn an empty structured response into an error that says WHY it was empty."""
    stop = getattr(response, "stop_reason", None)
    if stop == "max_tokens":
        used = getattr(getattr(response, "usage", None), "output_tokens", "?")
        return TruncatedError(f"{stage}: hit max_tokens after {used} output tokens")
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        return RuntimeError(f"{stage}: model declined ({details})")
    return RuntimeError(f"{stage}: no parseable output (stop_reason={stop})")


def _invalid_json_error(stage: str, exc: ValidationError) -> Exception:
    """Classify a `parse()` ValidationError.

    Truncation has two shapes, depending on where the budget ran out: no text block at
    all (``parsed_output is None``, handled above) or a half-written one, which `parse()`
    raises on. `output_config.format` constrains the body to the schema server-side, so
    JSON that doesn't parse means it was cut off mid-value, not malformed.
    """
    if any(err.get("type") == "json_invalid" for err in exc.errors()):
        return TruncatedError(f"{stage}: response JSON was cut off mid-value")
    return RuntimeError(f"{stage}: response did not match the schema ({exc})")


def _with_headroom(stage: str, depth: str, label: str, attempt):
    """Run a stage, retrying once with more room if the model ran out of budget.

    ``attempt(policy)`` makes the call and raises ``TruncatedError`` when the response was
    cut off. Truncation is the one failure here that is mechanically recoverable: drop a
    notch of effort (so it thinks less) and double the rail, then try once more. Anything
    else - a refusal, an unparseable body - propagates with its reason attached.
    """
    policy = policy_for(stage, depth)
    try:
        return attempt(policy)
    except TruncatedError as exc:
        relaxed = StagePolicy(
            effort=shift_effort(policy.effort, -1), max_tokens=policy.max_tokens * 2
        )
        _log(
            f"  {label}   {exc}; retrying at effort={relaxed.effort}, "
            f"max_tokens={relaxed.max_tokens}"
        )
        return attempt(relaxed)


def _structured(
    client: anthropic.Anthropic,
    model: str,
    stage: str,
    system: str,
    user: str,
    output_format,
    depth: str = DEFAULT_DEPTH,
    label: str = "",
):
    """One structured-output call under its stage policy, with truncation named.

A cut-off response reaches us two ways - ``parsed_output is None`` when no text block
    was emitted at all, or a ``ValidationError`` when a half-written one was - and neither
    says "truncated" on its face. Left unclassified, both are indistinguishable from a
    model that had nothing to say, which is how a silent change in model defaults killed
    the 2026-09-13 issue under the message "Topic selection returned no topics."
    """

    def attempt(policy: StagePolicy):
        try:
            response = client.messages.parse(
                model=model,
                max_tokens=policy.max_tokens,
                output_config={"effort": policy.effort},
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output_format,
            )
        except ValidationError as exc:
            raise _invalid_json_error(stage, exc) from exc
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise _no_output_error(stage, response)
        return parsed

    return _with_headroom(stage, depth, label, attempt)



# Two web-search server-tool variants:
#   dynamic — writes+runs code to filter results before Claude reads them (thorough, but
#             on some models runs away in an unbounded filtering loop);
#   basic   — results go straight to Claude, no filtering step (fast, predictable).
_WEB_SEARCH_DYNAMIC = "web_search_20260209"
_WEB_SEARCH_BASIC = "web_search_20250305"


class ResearchSettings(BaseModel):
    """Search-depth knobs (from .env). Lower = faster/cheaper; raise for deeper digs.

    ``depth`` is the pipeline-wide effort profile: it shifts every stage's policy a
    notch (see "Effort policy"), including research's own ``effort`` unless a caller
    overrides that explicitly (an A/B variant, or SEARCH_EFFORT).

    Together these put a hard, predictable ceiling on how long one topic can research:
    at most (max_rounds + 1) streamed turns, each doing at most max_uses web searches.
    """

    depth: str = DEFAULT_DEPTH      # fast | balanced | deep — shifts EVERY stage's effort
    effort: str = "medium"          # research's own per-turn effort (see policy_for)
    max_tokens: int = 8000          # length cap on the written brief
    max_uses: int = 5               # web searches allowed PER streamed turn
    max_rounds: int = 1             # extra pause_turn continuation rounds (0 = one turn)
    dynamic_filtering: bool = False  # True = newer filtering tool; False = basic tool
    candidate_items: int = 6        # how many items research proposes (over-provision)
    final_items: int = 4            # how many survive selection onto the page

    def web_search_tool(self) -> dict:
        tool_type = _WEB_SEARCH_DYNAMIC if self.dynamic_filtering else _WEB_SEARCH_BASIC
        return {"type": tool_type, "name": "web_search", "max_uses": self.max_uses}


# --- Stage 1: topic selection ------------------------------------------------


class _TopicIdea(BaseModel):
    title: str = Field(description="The topic, evocative but concrete.")
    angle: str = Field(description="One sentence on the specific angle that makes it rich.")


class _TopicSlate(BaseModel):
    topics: List[_TopicIdea]


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


def select_topics(
    client: anthropic.Anthropic,
    model: str,
    history: List[str],
    count: int,
    depth: str = DEFAULT_DEPTH,
) -> List[_TopicIdea]:
    avoid = "\n".join(f"- {t}" for t in history) if history else "(nothing yet)"
    user = (
        f"Choose {count} topics for this week's issue.\n\n"
        f"Do NOT repeat or closely overlap any of these previously covered topics:\n"
        f"{avoid}\n\n"
        f"Aim for variety across the {count} — different domains and moods. "
        f"For each, give a title and a one-sentence angle."
    )
    slate = _structured(
        client, model, "select_topics", _TOPIC_SYSTEM, user, _TopicSlate,
        depth=depth, label="topics",
    )
    if not slate.topics:
        raise RuntimeError("Topic selection returned an empty slate.")
    return slate.topics[:count]


# --- Stage 2: research with live web search ----------------------------------


_RESEARCH_SYSTEM = """\
You are a brilliant, widely-read friend assembling a 1-2 hour deep dive on a \
single topic for someone you respect. Use web search to find the genuinely BEST \
existing content — the stuff a knowledgeable insider would point to, not the top \
Google results.

Favor a FEW substantial, longer-form pieces over many short ones:
- documentaries or long video essays
- long-form written essays and serious journalism (quality outlets)
- long YouTube videos / recorded lectures / talks
- excellent podcast episodes
- the occasional canonical article, primary source, or book

Hard requirements:
- Propose a shortlist of strong candidate items (the exact number is specified per \
request), ORDERED BEST-FIRST — most essential first. A later step verifies the links and \
picks the final set, so give it good options to choose from, but never pad with weak \
picks: a strong shortlist beats a long mediocre one.
- Every link must be REAL and confirmed via search: point only to a specific page you \
actually found in results (an exact article, video, or episode URL). NEVER invent a URL, \
guess a slug, link a bare homepage/channel as a stand-in, or paste a search-results \
page. If you cannot confirm a specific working URL for an item, drop that item rather \
than pad. Prefer durable, reputable sources over SEO content farms and listicles.
- Aim for a mix of formats (documentary, essay, video, podcast, article) so the final \
selection can be well-rounded, not all one kind.
- For each item, note its approximate duration and write a short, specific, warm note on \
why it's worth their time and what to expect — like a friend handing it over, not a \
catalog entry. Estimate durations HONESTLY from the actual content: judge an article's \
read-time by its real length (don't inflate a <1000-word piece to "25 min"), and don't \
count a video's full runtime if much of it is off-topic.
- Also write an inviting introduction to the topic itself: what it is, why it's \
fascinating, the thread that ties the picks together.

Be discerning and opinionated. Every candidate should be one you'd genuinely recommend."""


def _is_overloaded(exc: Exception) -> bool:
    """True for a transient 'Overloaded' (529) surfaced as a generic APIStatusError."""
    return isinstance(exc, anthropic.APIStatusError) and (
        getattr(exc, "status_code", None) == 529 or "overload" in str(exc).lower()
    )


def _log_tool_block(block, searches: int, settings: "ResearchSettings", label: str) -> int:
    """Print a completed web-search block or its result; return the running search count."""
    btype = getattr(block, "type", None)
    if btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
        searches += 1
        inp = getattr(block, "input", None) or {}
        query = inp.get("query", "") if isinstance(inp, dict) else ""
        _log(f'  {label}   search {searches}/{settings.max_uses}: "{query}"')
    elif btype == "web_search_tool_result":
        content = getattr(block, "content", None)
        if isinstance(content, list):
            _log(f"  {label}       -> {len(content)} results")
        else:
            code = getattr(content, "error_code", None) or getattr(content, "type", None)
            _log(f"  {label}       -> search returned: {code}")
    return searches


def _phase_name(block) -> str:
    """Human-readable name for a content block, used to label which phase we're in."""
    btype = getattr(block, "type", None)
    if btype == "server_tool_use":
        name = getattr(block, "name", "")
        if name == "web_search":
            return "web search"
        if name in ("code_execution", "bash_code_execution"):
            return "result filtering (code execution)"
        return f"server tool: {name}"
    if btype == "web_search_tool_result":
        return "search results"
    if btype in ("bash_code_execution_tool_result", "code_execution_tool_result"):
        return "filter output"
    if btype == "thinking":
        return "thinking"
    if btype == "text":
        return "writing brief"
    return btype or "unknown block"


def _heartbeat(stop_event: threading.Event, state: dict, label: str) -> None:
    """Fire every 15s even during a silent server-side step, reporting the live phase.

    This is what de-black-boxes the long gap: if the stream goes quiet (e.g. the model is
    running result-filtering code server-side), no events arrive — but this timer still
    prints which phase we entered last and whether the model is still emitting, so we can
    tell "stuck filtering" from "slowly writing text."

    Progress is measured in characters streamed, not tokens: the API reports usage in a
    single `message_delta` at the very END of the turn, so a live token count is not
    available at all (verified against the wire — the in-flight snapshot stays at 1).
    """
    while not stop_event.wait(15):
        _log(
            f"  {label}   …still working: phase='{state['phase']}', "
            f"~{state['chars']:,} chars streamed so far"
        )


def _handle_stream_event(event, stream, state, searches, settings, label) -> int:
    """Process one stream event for logging; return the running search count."""
    etype = event.type
    if etype == "content_block_start":
        phase = _phase_name(event.content_block)
        # Announce a phase only when it CHANGES. A cited brief is split into one text
        # block per citation — 33 in one observed turn — and announcing every block
        # start buried the searches and turn summaries under identical lines.
        if phase != state["phase"] and phase != "web search":
            # Searches are logged in full (with query) at stop, hence the exclusion.
            _log(f"  {label}     · entering: {phase}")
        state["phase"] = phase
    elif etype == "content_block_delta":
        # The only live progress signal there is; see _heartbeat.
        delta = getattr(event, "delta", None)
        for attr in ("text", "thinking", "partial_json"):
            chunk = getattr(delta, attr, None)
            if chunk:
                state["chars"] += len(chunk)
                break
    elif etype == "content_block_stop":
        try:
            blocks = stream.current_message_snapshot.content
        except Exception:  # noqa: BLE001 - snapshot best-effort for logging
            blocks = []
        if event.index < len(blocks):
            searches = _log_tool_block(blocks[event.index], searches, settings, label)
    return searches


def _stream_one_turn(client, model, messages, settings, label, container):
    """Stream a single research turn with granular, phase-level logging.

    We iterate the stream events so we can show what Claude is doing inside the turn:
    every content block is announced as it *starts* (search / filtering / thinking /
    writing brief), each completed ``web_search`` block prints its query against the
    ``max_uses`` cap, and a background heartbeat reports the live phase + output-token
    count every 15s so even a silent server-side step is visible. A one-line summary
    closes out the turn.
    """
    extra = {"container": container} if container else {}
    searches = 0
    state = {"phase": "starting", "chars": 0}
    stop_event = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat, args=(stop_event, state, label), daemon=True
    )
    heartbeat.start()
    try:
        with client.messages.stream(
            model=model,
            max_tokens=settings.max_tokens,
            system=_RESEARCH_SYSTEM,
            # Summarized (not the "omitted" default): thinking blocks arrive with real
            # text, so a long reasoning phase shows up in the log and on the wire as
            # progress rather than as a stall.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": settings.effort},
            tools=[settings.web_search_tool()],
            messages=messages,
            **extra,
        ) as stream:
            for event in stream:
                searches = _handle_stream_event(
                    event, stream, state, searches, settings, label
                )
            final = stream.get_final_message()
    finally:
        stop_event.set()

    out_tokens = getattr(getattr(final, "usage", None), "output_tokens", "?")
    _log(
        f"  {label}   turn complete: {searches} search(es), "
        f"{out_tokens} output tokens, stop_reason={final.stop_reason}"
    )
    return final


def _stream_with_backoff(client, model, messages, settings, label, container=None):
    """One streamed research turn, retried with backoff on transient server errors.

    Streaming keeps the wire active so long jobs don't trip the read timeout; this loop
    additionally absorbs overload/5xx blips that can arrive mid-stream. Retries are cheap
    because these errors fire before substantial token generation.

    ``container`` carries the code-execution container id when resuming a ``pause_turn``:
    the dynamic-filtering web search tool runs code execution under the hood, and the API
    requires the same container be passed back to continue a paused turn.
    """
    last_exc = None
    for attempt in range(len(_BACKOFF_SECONDS) + 1):
        try:
            return _stream_one_turn(client, model, messages, settings, label, container)
        except Exception as exc:  # noqa: BLE001 - re-raised below if not retryable
            if not (isinstance(exc, _RETRYABLE) or _is_overloaded(exc)):
                raise
            last_exc = exc
            if attempt < len(_BACKOFF_SECONDS):
                wait = _BACKOFF_SECONDS[attempt]
                _log(
                    f"  {label}   transient API error ({type(exc).__name__}), "
                    f"attempt {attempt + 1}/{len(_BACKOFF_SECONDS)} — retrying in {wait}s..."
                )
                time.sleep(wait)
    raise RuntimeError(f"Research stream failed after retries: {last_exc}")


def research_topic(
    client: anthropic.Anthropic,
    model: str,
    topic: _TopicIdea,
    settings: "ResearchSettings",
    label: str = "",
) -> str:
    """Run a web-search-backed research pass; return Claude's rich markdown writeup.

    Streamed (with summarized thinking) so the connection stays active for the several
    minutes a web-search-heavy request can take: streaming sends incremental deltas and
    periodic keep-alive pings, so the read timeout never trips even on a long job. A
    non-streaming version goes silent on the wire until the whole response is done, which
    trips the read timeout and then re-runs the expensive request on every retry. The
    ``pause_turn`` loop continues the server-side web search tool when it hits its
    per-response iteration cap.
    """
    user = (
        f"Topic: {topic.title}\n"
        f"Angle: {topic.angle}\n\n"
        f"Search the web and assemble the deep dive. Propose {settings.candidate_items} "
        f"candidate items, ordered best-first. Produce a written brief: an introduction to "
        f"the topic, then each candidate item with its title, format, source/creator, the "
        f"real URL, approximate duration, and your note on why it's worth it."
    )
    messages = [{"role": "user", "content": user}]

    final = None
    container = None  # code-execution container id, carried across pause_turn rounds
    total_rounds = settings.max_rounds + 1
    for round_i in range(total_rounds):
        if total_rounds > 1:
            _log(f"  {label}   round {round_i + 1}/{total_rounds}...")
        final = _stream_with_backoff(client, model, messages, settings, label, container)
        if final.stop_reason == "pause_turn":
            # Server tool loop hit its cap; resend the assistant content (no extra
            # "continue" message — the API resumes from the trailing server_tool_use)
            # and pass the container back so its pending tool use can complete.
            _log(
                f"  {label}   paused (hit tool-iteration cap); continuing for another round"
                if round_i + 1 < total_rounds
                else f"  {label}   paused, but out of rounds — using what we have"
            )
            messages.append({"role": "assistant", "content": final.content})
            container = getattr(getattr(final, "container", None), "id", None) or container
            continue
        break

    text = "\n".join(b.text for b in final.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"Research returned no text for topic: {topic.title}")
    return text


# --- Stage 3: structure into a DeepDive --------------------------------------


_STRUCTURE_SYSTEM = """\
You convert a research brief into a clean, structured deep dive. Preserve the curator's \
voice and the real URLs exactly. Do not invent items or links that aren't in the brief. \
Order the items into a sensible reading/watching arc. Keep notes warm and specific."""


def structure_deep_dive(
    client: anthropic.Anthropic,
    model: str,
    topic: _TopicIdea,
    research: str,
    depth: str = DEFAULT_DEPTH,
    label: str = "",
) -> DeepDive:
    user = (
        f"Topic title to use: {topic.title}\n\n"
        f"Research brief:\n\n{research}\n\n"
        f"Produce the structured deep dive."
    )
    try:
        return _structured(
            client, model, "structure", _STRUCTURE_SYSTEM, user, DeepDive,
            depth=depth, label=label,
        )
    except RuntimeError as exc:
        raise RuntimeError(f"Structuring failed for topic {topic.title!r}: {exc}") from exc


# --- Stage 4: select the best items from the verified candidates -------------


_SELECT_SYSTEM = """\
You are the curator finalizing a Deep Dive. From a list of VERIFIED candidate items \
(their links already resolve), choose the best ones that together form a well-rounded \
1-2 hour exploration. Optimize for, in order: (1) quality — pick the genuinely best \
pieces; (2) a MIX of formats — don't return all podcasts or all videos; (3) a sensible \
reading/watching arc across the ones you keep. Return ONLY the indices of the items to \
keep, ordered as they should appear on the page."""


class _Selection(BaseModel):
    keep: List[int] = Field(
        description="0-based indices of the items to keep, in final page order (best/"
        "most essential first)."
    )


def select_deep_dive(
    client: anthropic.Anthropic,
    model: str,
    dive: DeepDive,
    final_items: int,
    label: str = "",
    depth: str = DEFAULT_DEPTH,
) -> DeepDive:
    """Pick the best ``final_items`` from a verified DeepDive's candidates.

    Selection is by INDEX into the existing items, and we rebuild ``items`` from the
    original ContentItem objects — so the chosen links/notes are the exact verified ones
    and selection can never introduce a new (possibly hallucinated) URL. Falls back to the
    research order if the model returns nothing usable; skips the call entirely when there
    aren't more candidates than we need.
    """
    items = dive.items
    if len(items) <= final_items:
        return dive  # nothing to trim — keep all survivors

    listing = "\n".join(
        f"[{i}] ({it.kind}, {it.duration}) {it.title} — {it.source}\n"
        f"     {' '.join((it.note or '').split())[:200]}"
        for i, it in enumerate(items)
    )
    user = (
        f"Topic: {dive.title}\n\n"
        f"Verified candidate items:\n{listing}\n\n"
        f"Choose the best {final_items} to keep, ordered for the page. Return their indices."
    )
    sel = None
    try:
        sel = _structured(
            client, model, "select_items", _SELECT_SYSTEM, user, _Selection,
            depth=depth, label=label,
        )
    except Exception as exc:  # noqa: BLE001 - fall back to research order on any failure
        _log(f"  {label}   selection call failed ({type(exc).__name__}: {exc}); keeping first {final_items}")

    kept: List = []
    if sel is not None:
        seen = set()
        for idx in sel.keep:
            if 0 <= idx < len(items) and idx not in seen:
                seen.add(idx)
                kept.append(items[idx])
            if len(kept) >= final_items:
                break
    if not kept:
        kept = items[:final_items]  # fallback: research's best-first ordering

    _log(f"  {label}   selected {len(kept)} of {len(items)} candidates for the page")
    return dive.model_copy(update={"items": kept})


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
    depth: str = DEFAULT_DEPTH,
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
        ranking = _structured(
            client, judge_model, "rank_topics", _TOPIC_RANK_SYSTEM, user, _TopicRanking,
            depth=depth, label=label,
        )
    except Exception as exc:  # noqa: BLE001 - fall back to research order on any failure
        _log(f"  {label}   topic-rank call failed ({type(exc).__name__}: {exc}); keeping first {keep}")

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

    kept_ids = {id(d) for d in kept}
    dropped = [d.title for d in dives if id(d) not in kept_ids]
    if dropped:
        _log(f"  {label}   topic filter: kept {len(kept)} of {len(dives)}; dropped: {'; '.join(dropped)}")
    return kept


# --- Orchestration -----------------------------------------------------------


_INTRO_SYSTEM = """\
You write the short editor's note for "The Deep Dive" newsletter. Warm, literate, a \
little wry. 2-4 sentences that frame the week's three deep dives and the spirit of \
intellectual wandering — without mechanically listing them."""


def _edition_intro(
    client: anthropic.Anthropic,
    model: str,
    dives: List[DeepDive],
    depth: str = DEFAULT_DEPTH,
) -> str:
    titles = "; ".join(d.title for d in dives)

    def attempt(policy: StagePolicy) -> str:
        response = client.messages.create(
            model=model,
            max_tokens=policy.max_tokens,
            output_config={"effort": policy.effort},
            system=_INTRO_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"This week's deep dives are: {titles}.\n\nWrite the note. "
                    f"Return only the note text, no preamble.",
                }
            ],
        )
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise TruncatedError("edition_intro: hit max_tokens before finishing the note")
        return "".join(b.text for b in response.content if b.type == "text").strip()

    try:
        return _with_headroom("edition_intro", depth, "intro", attempt)
    except TruncatedError as exc:
        # The note is the one piece the issue can ship without; don't sink a whole
        # newsletter over it.
        _log(f"  intro still truncated after retry ({exc}); shipping without a note")
        return ""


def _research_and_structure(
    client: anthropic.Anthropic,
    model: str,
    topic: _TopicIdea,
    settings: "ResearchSettings",
    label: str,
) -> DeepDive:
    """Full per-topic pipeline (research -> structure). One unit of parallel work."""
    _log(f"  {label} researching '{topic.title}'...")
    brief = research_topic(client, model, topic, settings, label)
    _log(f"  {label}   structuring brief into a deep dive...")
    dive = structure_deep_dive(client, model, topic, brief, settings.depth, label)
    _log(f"  {label}   verifying {len(dive.items)} candidate links...")
    dive = verify.verify_dive(dive, label)
    _log(f"  {label}   selecting best {settings.final_items} of {len(dive.items)}...")
    dive = select_deep_dive(
        client, model, dive, settings.final_items, label, settings.depth
    )
    _log(f"  {label} done: {topic.title} ({len(dive.items)} items)")
    return dive


def make_topic(title: str, angle: str) -> "_TopicIdea":
    """Build a topic from a title + angle — used to feed a fixed set (e.g. an A/B run)."""
    return _TopicIdea(title=title, angle=angle)


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
    topics_selected_fresh = topics is None
    if topics_selected_fresh:
        n_candidates = topic_candidates or count
        topics = select_topics(client, model, history, n_candidates, settings.depth)
        _log("  Topics chosen:")
    else:
        _log("  Topics (fixed):")
    for topic in topics:
        _log(f"    - {topic.title}")

    # Topics are independent, stateless API calls, and each spends almost all its time
    # waiting on the network — so research them concurrently. The wall-clock becomes
    # ~the slowest topic instead of the sum of all three (the SDK releases the GIL
    # during I/O, so threads give near-linear speedup here). Results are slotted back in
    # topic order; a topic that fails is logged and skipped rather than sinking the issue.
    total = len(topics)
    slots: List[Optional[DeepDive]] = [None] * total
    errors: List[str] = []
    _log(f"  Researching {total} topics in parallel...")
    with ThreadPoolExecutor(max_workers=total) as pool:
        futures = {
            pool.submit(
                _research_and_structure,
                client,
                model,
                topic,
                settings,
                f"[{i}/{total}]",
            ): i
            for i, topic in enumerate(topics, 1)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                slots[i - 1] = future.result()
            except Exception as exc:  # noqa: BLE001 - keep the other topics' results
                errors.append(f"{topics[i - 1].title}: {exc}")
                _log(f"  [{i}/{total}] FAILED: {exc}")

    dives = [d for d in slots if d is not None]
    if not dives:
        raise RuntimeError("All topics failed to research:\n" + "\n".join(errors))

    # Normal path over-provisions topics; keep only the richest `count`. Fixed-topics
    # mode (A/B) holds its set constant, so skip filtering there.
    if topics_selected_fresh and len(dives) > count:
        _log(f"  Judging {len(dives)} researched topics; keeping the best {count}...")
        dives = rank_topics_by_richness(
            client, judge_model, dives, count, depth=settings.depth
        )

    _log("  Writing the editor's note...")
    intro = _edition_intro(client, model, dives, settings.depth)
    return Newsletter(
        edition_date=date.today().strftime("%A, %B %-d, %Y"),
        intro=intro,
        deep_dives=dives,
    )
