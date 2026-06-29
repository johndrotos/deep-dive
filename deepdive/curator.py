"""The brain: pick fresh topics, research them on the live web, structure the result.

Pipeline per run:
  1. select_topics()      - one structured call, no tools, avoids history.
  2. research_topic()     - streaming call WITH web search, real current links.
  3. structure_deep_dive() - one parse() call, no tools, research -> DeepDive.

Splitting "research with tools" from "structure to schema" keeps each call reliable.
"""

from __future__ import annotations

from datetime import date
from typing import List

import anthropic
from pydantic import BaseModel, Field

from .models import DeepDive, Newsletter

# Opus 4.8 supports the dynamic-filtering web search server tool.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 8}


# --- Stage 1: topic selection ------------------------------------------------


class _TopicIdea(BaseModel):
    title: str = Field(description="The topic, evocative but concrete.")
    angle: str = Field(description="One sentence on the specific angle that makes it rich.")


class _TopicSlate(BaseModel):
    topics: List[_TopicIdea]


_TOPIC_SYSTEM = """\
You are the curator of "The Deep Dive," a weekly newsletter for one intellectually \
voracious reader. Your job is to choose genuinely niche, obscure, and intellectually \
interesting topics for deep exploration.

What makes a great Deep Dive topic:
- Obscure or under-appreciated, not something they'd already know well.
- Intellectually rich: history, science, philosophy, art, technology, culture, the \
strange corners where fields meet.
- Has a real body of excellent existing content (documentaries, long essays, long \
videos, podcasts, serious articles) to draw on.
- Surprising. The reaction should be "I had no idea that was a whole world."

Avoid: overdone pop-science staples, anything generic, anything in the avoid-list, \
and topics that are merely "interesting facts" with no depth to explore."""


def select_topics(
    client: anthropic.Anthropic, model: str, history: List[str], count: int
) -> List[_TopicIdea]:
    avoid = "\n".join(f"- {t}" for t in history) if history else "(nothing yet)"
    user = (
        f"Choose {count} topics for this week's issue.\n\n"
        f"Do NOT repeat or closely overlap any of these previously covered topics:\n"
        f"{avoid}\n\n"
        f"Aim for variety across the {count} — different domains and moods. "
        f"For each, give a title and a one-sentence angle."
    )
    response = client.messages.parse(
        model=model,
        max_tokens=2000,
        system=_TOPIC_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=_TopicSlate,
    )
    slate = response.parsed_output
    if slate is None or not slate.topics:
        raise RuntimeError("Topic selection returned no topics.")
    return slate.topics[:count]


# --- Stage 2: research with live web search ----------------------------------


_RESEARCH_SYSTEM = """\
You are a brilliant, widely-read friend assembling a roughly 2-3 hour deep dive on a \
single topic for someone you respect. Use web search to find the genuinely BEST \
existing content — the stuff a knowledgeable insider would point to, not the top \
Google results.

Curate a mix of formats where they exist:
- documentaries or long video essays
- long-form written essays and serious journalism (quality outlets)
- long YouTube videos / recorded lectures / talks
- excellent podcast episodes
- the occasional canonical article, primary source, or book

Hard requirements:
- Every link must be REAL and found via search. Verify titles and sources. Never invent \
URLs. Prefer durable, reputable sources over SEO content farms and listicles.
- Aim for a total of about 2.5 hours of content across 5-8 items.
- For each item, note its approximate duration and write a short, specific, warm note on \
why it's worth their time and what to expect — like a friend handing it over, not a \
catalog entry.
- Also write an inviting introduction to the topic itself: what it is, why it's \
fascinating, the thread that ties the picks together.

Be discerning and opinionated. Quality and specificity over completeness."""


def research_topic(
    client: anthropic.Anthropic,
    model: str,
    topic: _TopicIdea,
    max_continuations: int = 4,
) -> str:
    """Run a web-search-backed research pass; return Claude's rich markdown writeup."""
    user = (
        f"Topic: {topic.title}\n"
        f"Angle: {topic.angle}\n\n"
        f"Search the web and assemble the deep dive. Produce a written brief: an "
        f"introduction to the topic, then each curated item with its title, format, "
        f"source/creator, the real URL, approximate duration, and your note on why it's "
        f"worth it. End with the rough total time."
    )
    messages = [{"role": "user", "content": user}]

    for _ in range(max_continuations + 1):
        with client.messages.stream(
            model=model,
            max_tokens=16000,
            system=_RESEARCH_SYSTEM,
            thinking={"type": "adaptive"},
            tools=[WEB_SEARCH_TOOL],
            messages=messages,
        ) as stream:
            final = stream.get_final_message()

        if final.stop_reason == "pause_turn":
            # Server tool loop hit its cap; resend to let it continue.
            messages.append({"role": "assistant", "content": final.content})
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
    client: anthropic.Anthropic, model: str, topic: _TopicIdea, research: str
) -> DeepDive:
    user = (
        f"Topic title to use: {topic.title}\n\n"
        f"Research brief:\n\n{research}\n\n"
        f"Produce the structured deep dive."
    )
    response = client.messages.parse(
        model=model,
        max_tokens=6000,
        system=_STRUCTURE_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=DeepDive,
    )
    dive = response.parsed_output
    if dive is None:
        raise RuntimeError(f"Structuring failed for topic: {topic.title}")
    return dive


# --- Orchestration -----------------------------------------------------------


_INTRO_SYSTEM = """\
You write the short editor's note for "The Deep Dive" newsletter. Warm, literate, a \
little wry. 2-4 sentences that frame the week's three deep dives and the spirit of \
intellectual wandering — without mechanically listing them."""


def _edition_intro(
    client: anthropic.Anthropic, model: str, dives: List[DeepDive]
) -> str:
    titles = "; ".join(d.title for d in dives)
    response = client.messages.create(
        model=model,
        max_tokens=600,
        system=_INTRO_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"This week's deep dives are: {titles}.\n\nWrite the note. "
                f"Return only the note text, no preamble.",
            }
        ],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def build_newsletter(
    client: anthropic.Anthropic, model: str, history: List[str], count: int
) -> Newsletter:
    """Run the full pipeline and return a finished Newsletter."""
    topics = select_topics(client, model, history, count)

    dives: List[DeepDive] = []
    for topic in topics:
        brief = research_topic(client, model, topic)
        dives.append(structure_deep_dive(client, model, topic, brief))

    intro = _edition_intro(client, model, dives)
    return Newsletter(
        edition_date=date.today().strftime("%A, %B %-d, %Y"),
        intro=intro,
        deep_dives=dives,
    )
