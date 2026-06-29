"""Render a Newsletter into email-safe HTML via Jinja2."""

from __future__ import annotations

import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import Newsletter

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

# Small label + accent per content kind, shown as a badge on each item.
_KIND_STYLES = {
    "documentary": ("Documentary", "#7c4a2d"),
    "video": ("Video", "#7c4a2d"),
    "lecture": ("Lecture", "#5a4a7c"),
    "essay": ("Essay", "#2d5a4a"),
    "article": ("Article", "#2d5a4a"),
    "podcast": ("Podcast", "#7c2d4a"),
    "book": ("Book", "#4a4a4a"),
    "interactive": ("Interactive", "#2d4a7c"),
}


def _badge(kind: str) -> dict:
    label, color = _KIND_STYLES.get(kind.strip().lower(), (kind.title(), "#4a4a4a"))
    return {"label": label, "color": color}


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["badge"] = _badge
    return env


def render_html(newsletter: Newsletter, title: str) -> str:
    template = _environment().get_template("newsletter.html")
    return template.render(nl=newsletter, title=title)


def subject_line(newsletter: Newsletter, title: str) -> str:
    """A subject that previews the topics, e.g. 'The Deep Dive — A, B & C'."""
    titles = [d.title for d in newsletter.deep_dives]
    if len(titles) > 1:
        joined = ", ".join(titles[:-1]) + " & " + titles[-1]
    else:
        joined = titles[0] if titles else ""
    return f"{title} — {joined}"
