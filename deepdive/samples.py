"""A canned Newsletter for offline design iteration (``--fake``).

No API calls, no email — just realistic data so you can tweak the template for free.
The URLs here are plausible but illustrative; the real agent finds verified links.
"""

from __future__ import annotations

from .models import ContentItem, DeepDive, Newsletter


def sample_newsletter() -> Newsletter:
    return Newsletter(
        edition_date="Sunday, June 29, 2026",
        intro=(
            "Three doors this week, none of them where you'd expect. We start underground "
            "with the people who map caves they may never see the end of, drift into the "
            "strange afterlife of dead languages, and finish among the clockmakers who "
            "tried to bottle longitude. Pour something warm; this one's a wander."
        ),
        deep_dives=[
            DeepDive(
                title="The Cartographers of the Dark",
                dek="How a handful of obsessives map cave systems that no satellite can see.",
                hook=(
                    "Most of the planet has been photographed from orbit, but the deep "
                    "cave systems remain genuine blank spaces — surveyed by hand, in the "
                    "dark, by people squeezing through gaps the width of a mailbox. What "
                    "draws someone to spend a decade extending a single line on a map no "
                    "one will frame? It turns out the answer braids together geology, "
                    "obsession, and a very particular relationship with fear."
                ),
                estimated_time="~2.5 hours",
                items=[
                    ContentItem(
                        title="The Deepest Cave",
                        kind="documentary",
                        source="BBC Horizon",
                        url="https://example.com/deepest-cave",
                        duration="58 min",
                        note=(
                            "Start here. It follows a survey expedition into Krubera and "
                            "captures the slow, methodical madness of it better than anything "
                            "else — watch for the moment they realize the map was wrong."
                        ),
                    ),
                    ContentItem(
                        title="Blind Descent: the chapter on the sump",
                        kind="book",
                        source="James Tabor",
                        url="https://example.com/blind-descent",
                        duration="40 min read",
                        note=(
                            "The single most white-knuckle piece of cave writing I know. "
                            "You don't need the whole book — this chapter alone will stay "
                            "with you."
                        ),
                    ),
                    ContentItem(
                        title="Why we still map by hand",
                        kind="essay",
                        source="The Atavist",
                        url="https://example.com/map-by-hand",
                        duration="35 min read",
                        note=(
                            "A quieter, lovely piece on why the technology that maps "
                            "everything else fails underground, and the culture of the "
                            "people who fill the gap."
                        ),
                    ),
                    ContentItem(
                        title="A conversation with a cave surveyor",
                        kind="podcast",
                        source="The Adventure Podcast",
                        url="https://example.com/surveyor-interview",
                        duration="48 min",
                        note=(
                            "Less polished, more human — she's funny about the boredom, "
                            "which is the part the documentaries leave out."
                        ),
                    ),
                ],
            ),
            DeepDive(
                title="The Afterlives of Dead Languages",
                dek="What happens to a language after the last speaker is gone.",
                hook=(
                    "We think of language death as a clean ending, but it almost never is. "
                    "Tongues leave fossils in place names, in loan words, in the grammar of "
                    "their conquerors — and sometimes they come back. This dive sits in that "
                    "strange middle ground between extinction and revival."
                ),
                estimated_time="~2 hours",
                items=[
                    ContentItem(
                        title="The man who saved Hebrew",
                        kind="video",
                        source="NativLang",
                        url="https://example.com/hebrew-revival",
                        duration="22 min",
                        note=(
                            "A crisp, well-told account of the only fully successful language "
                            "revival in history — and how improbable it really was."
                        ),
                    ),
                    ContentItem(
                        title="Ghost words and where they hide",
                        kind="article",
                        source="Aeon",
                        url="https://example.com/ghost-words",
                        duration="25 min read",
                        note=(
                            "On the dictionary entries that describe words that were never "
                            "really words. A small, delightful rabbit hole."
                        ),
                    ),
                    ContentItem(
                        title="Recording the last speaker",
                        kind="documentary",
                        source="POV / PBS",
                        url="https://example.com/last-speaker",
                        duration="54 min",
                        note=(
                            "Sober and moving. Watch it for the ethics of the linguists as "
                            "much as the language itself."
                        ),
                    ),
                ],
            ),
            DeepDive(
                title="The Longitude Problem",
                dek="The two-century race to know where you were on an empty ocean.",
                hook=(
                    "For most of history, a ship could find its latitude easily and its "
                    "longitude almost never — and the gap killed thousands. The fix wasn't "
                    "astronomy in the end; it was a carpenter's son building a clock that "
                    "could survive the sea. It's one of the great underdog stories in the "
                    "history of science."
                ),
                estimated_time="~2.5 hours",
                items=[
                    ContentItem(
                        title="Longitude (the documentary)",
                        kind="documentary",
                        source="Granada / A&E",
                        url="https://example.com/longitude-doc",
                        duration="1.5 hr",
                        note=(
                            "Dramatized but faithful, and the device close-ups are gorgeous. "
                            "The best single starting point."
                        ),
                    ),
                    ContentItem(
                        title="Dava Sobel on the obsession of John Harrison",
                        kind="podcast",
                        source="In Our Time",
                        url="https://example.com/harrison-iot",
                        duration="50 min",
                        note=(
                            "Three historians, no script, total command of the material — "
                            "In Our Time at its best."
                        ),
                    ),
                ],
            ),
        ],
    )
