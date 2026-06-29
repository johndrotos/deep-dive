"""Structured data the curator produces and the renderer consumes."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class ContentItem(BaseModel):
    """A single piece of curated content within a deep dive."""

    title: str = Field(description="The title of the piece.")
    kind: str = Field(
        description="One of: documentary, essay, video, podcast, article, book, lecture, "
        "interactive."
    )
    source: str = Field(
        description="The outlet, author, channel, or creator — e.g. 'The New Yorker', "
        "'Veritasium', 'Dan Carlin'."
    )
    url: str = Field(description="A real, working URL to the content.")
    duration: str = Field(
        description="Approximate time to consume, e.g. '45 min', '1.5 hr', '20 min read'."
    )
    note: str = Field(
        description="1-3 sentences in a warm, knowledgeable-friend voice: why this is "
        "worth their time and what to expect. Specific, never generic."
    )


class DeepDive(BaseModel):
    """One topic's curated ~2-3 hour exploration."""

    title: str = Field(description="The topic title, evocative but clear.")
    dek: str = Field(
        description="A short standfirst/subtitle of one sentence that sharpens the angle."
    )
    hook: str = Field(
        description="One or two paragraphs introducing the topic the way a brilliant, "
        "well-read friend would — what makes it fascinating and why you should care. "
        "No clichés, no 'in this newsletter'."
    )
    estimated_time: str = Field(description="Total time across all items, e.g. '~2.5 hours'.")
    items: List[ContentItem] = Field(description="The curated pieces, in a sensible order.")


class Newsletter(BaseModel):
    """A complete issue."""

    edition_date: str = Field(description="Human-readable date, e.g. 'Sunday, June 29, 2026'.")
    intro: str = Field(
        description="A short, warm editor's note (2-4 sentences) framing this week's three "
        "deep dives without summarizing them mechanically."
    )
    deep_dives: List[DeepDive]
