# Copyright (C) 2026 Sam Struthers
# SPDX-License-Identifier: AGPL-3.0-or-later
# This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

"""The shapes of the coach's answers, as JSON schemas, and how they become text.

Every request to a language model asks for JSON of a fixed shape (a *structured
output*): a note is ``{"explanation": ..., "line_ideas": [...]}``, a commentary
window is a list of ``{"move", "comment"}`` pairs, and so on. Providers that
support it enforce the shape while the model writes (Ollama ``format``, the
OpenAI ``response_format``, Anthropic ``output_config.format``), so answers are
parsed instead of scraped out of free text with regular expressions — the
parsing step used to lose whole lines whenever a model drifted from the format.

Every parser here still accepts the old plain-text formats, because a provider
may refuse the schema (the request is then repeated without it) and because the
test stand-ins answer in plain text.

The schemas are deliberately small (every property required, no optional
fields, no nesting beyond one list of pairs) so that small local models follow
them and the OpenAI "strict" mode accepts them.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_json(text: str | None) -> dict | None:
    """The JSON object in a model answer, tolerating code fences and chatter around it ('' → None)."""
    if not text:
        return None
    candidates = [_FENCE.sub("", text.strip())]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _obj(name: str, /, **props: dict) -> dict:
    return {"title": name, "type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def _str(description: str) -> dict:
    return {"type": "string", "description": description}


def _strings(description: str) -> dict:
    return {"type": "array", "description": description, "items": {"type": "string"}}


_IDEAS = {
    "type": "array",
    "description": "One entry per candidate move you were asked about: the move exactly as written in the "
                   "evidence and one short sentence on what it achieves for the side playing it.",
    "items": _obj("idea", move=_str("the move, e.g. Nf3 or ...c5, exactly as written in the evidence"),
                  idea=_str("one short sentence")),
}

IDEAS = _obj("ideas", line_ideas=_IDEAS)

NOTE = _obj(
    "note",
    explanation=_str("the note itself: two or three short paragraphs separated by a blank line, each one to "
                     "three sentences; no headings or bullet points"),
    line_ideas=_IDEAS,
)

WINDOW = _obj(
    "commentary",
    lines={
        "type": "array",
        "description": "one entry for EVERY move listed, in order",
        "items": _obj("line", move=_str("the move label exactly as given, e.g. 11. axb4 or 11... Qc7"),
                      comment=_str("1-2 sentences naming the side")),
    },
)

CHAPTER = _obj(
    "chapter",
    title=_str("3-7 words naming what this chapter was about"),
    summary=_str("2-3 sentences"),
)

POSITION = _obj(
    "position",
    assessment=_str("the assessment and main plans from the coached side's perspective, then why the "
                    "engine's top move makes sense; two or three short paragraphs separated by a blank line"),
    line_ideas=_IDEAS,
)

REVIEW = _obj(
    "review",
    summary=_str("2-4 sentences: the opening by name, the result exactly as the RESULT fact says, and who "
                 "stood better when and why"),
    story=_strings("3-6 points, one per phase or turning point, each starting with the move range, e.g. "
                   "'Moves 8-12: White expands on the queenside with b4 and a3 while Black ...'"),
    went_well=_strings("1-2 points on what the player did well in THIS game, each naming the move or fact"),
    takeaways=_strings("2-4 concrete, transferable lessons from THIS game, each naming the player's own "
                       "move it comes from, e.g. 'Move 31 (Qf5): ...'"),
)


def profile_schema(time_class: str | None = None) -> dict:
    """The progress review: one list of bullets per section of prompts.PROFILE_SECTIONS."""
    props = {"intro": _str("one or two sentences on where the player stands; no numbers they already see")}
    if not time_class:
        props["time_controls"] = _strings("one bullet per time control in the statistics")
    props.update(
        weaknesses=_strings("2-3 bullets, the most persistent first: the weakness in a few words, then the "
                            "evidence (how often, in which games)"),
        strengths=_strings("1-2 bullets, only what the statistics or reviews show"),
        progress=_strings("1-2 bullets on what has improved and what has not between the earlier games and "
                          "the recent ones, only from the 'Progress over time' statistics; empty if absent"),
        openings=_strings("1-2 bullets: the opening that goes best and the one that goes worst, with how "
                          "many games"),
        train=_strings("2-3 concrete things to practise, one line each"),
    )
    return _obj("profile", **props)


# ------------------------------------------------------------------ from JSON to the texts the app uses

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _text(value: Any) -> str:
    """Tidy model text: whitespace collapsed within a paragraph, blank lines between paragraphs kept."""
    if not isinstance(value, str | int | float):
        return ""
    paragraphs = (" ".join(part.split()) for part in _PARAGRAPH_BREAK.split(str(value)))
    return "\n\n".join(part for part in paragraphs if part)


def _bullets(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out = []
    for v in values:
        t = re.sub(r"^\s*(?:[-•]|\*\s|\d+[.)])\s*", "", _text(v)).strip()
        if t:
            out.append(f"- {t}")
    return out


def ideas_from(data: dict) -> dict[str, str]:
    """{move: idea} from a note's or position's `line_ideas` (also accepts a plain {move: idea} object)."""
    raw = data.get("line_ideas")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        raw = [{"move": k, "idea": v} for k, v in raw.items()]
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        move = _text(item.get("move")).strip().lstrip(".… ").strip("*_`")
        idea = _text(item.get("idea"))
        if move and idea:
            out[move] = idea
    return out


def note_from(data: dict) -> tuple[str, dict[str, str]]:
    """(explanation, {move: idea}) from a NOTE or POSITION answer."""
    text = _text(data.get("explanation") or data.get("assessment") or data.get("note"))
    return text, ideas_from(data)


def window_lines(data: dict) -> list[str]:
    """A WINDOW answer as the plain '11. axb4: comment' lines the commentary parser understands."""
    out = []
    for item in data.get("lines") if isinstance(data.get("lines"), list) else []:
        if isinstance(item, dict):
            move, comment = _text(item.get("move")).strip("*_ "), _text(item.get("comment"))
            if move and comment:
                out.append(f"{move}: {comment}")
    return out


def chapter_from(data: dict) -> tuple[str, str]:
    return _text(data.get("title")).strip("*_\"' ")[:80], _text(data.get("summary"))


REVIEW_HEADINGS = (("summary", "Summary"), ("story", "How the game unfolded"),
                   ("went_well", "What went well"), ("takeaways", "Key takeaways"))


def review_markdown(text: str) -> str:
    """A REVIEW answer rendered with the headings review_check expects; plain text is returned unchanged."""
    data = parse_json(text)
    if not data or not any(k in data for k, _ in REVIEW_HEADINGS):
        return text
    parts = []
    for key, heading in REVIEW_HEADINGS:
        body = _text(data.get(key)) if key == "summary" else "\n".join(_bullets(data.get(key)))
        if body:
            parts += [f"## {heading}", body, ""]
    return "\n".join(parts).strip()


def profile_markdown(text: str, time_class: str | None = None) -> str:
    """A profile answer rendered with the headings of prompts.PROFILE_SECTIONS; plain text unchanged."""
    from .prompts import profile_sections  # late import: prompts imports this module
    data = parse_json(text)
    if not data or ("intro" not in data and not any(k in data for k, _, _ in profile_sections(time_class))):
        return text
    parts = [_text(data.get("intro"))] if _text(data.get("intro")) else []
    for key, title, _ in profile_sections(time_class):
        bullets = _bullets(data.get(key))
        if bullets:
            parts += ["", f"### {title}", *bullets]
    return "\n".join(parts).strip()


__all__ = ["CHAPTER", "IDEAS", "NOTE", "POSITION", "REVIEW", "REVIEW_HEADINGS", "WINDOW", "chapter_from", "ideas_from",
           "note_from", "parse_json", "profile_markdown", "profile_schema", "review_markdown", "window_lines"]
