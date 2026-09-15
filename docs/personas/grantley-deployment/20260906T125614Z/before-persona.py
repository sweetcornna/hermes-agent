"""Persona document loading, cache-safe splitting, and ``{{persona.*}}`` rendering.

This module is where the migration's central architectural decision lives.

The source prompt body (``assets/grantley.md``, byte-exact from corlinman)
embeds live mutable state directly in the system prompt under the
``## 此刻的我（实时状态）`` heading::

    - 心情：{{persona.mood}}
    - 精神状态：{{persona.fatigue}}
    ...

In hermes that is a prompt-cache violation. ``AGENTS.md`` is unambiguous:

    Per-conversation prompt caching is sacred. […] Anything that mutates past
    context, swaps toolsets, or rebuilds the system prompt mid-conversation
    invalidates that cache and multiplies the user's cost. We do not do it.

    […] a system prompt that is byte-stable for the life of a conversation.

Mood, fatigue and the life-state fields change on a timescale far shorter
than a conversation, so interpolating them into the system prompt would
guarantee a cache miss on every turn that a decay tick or a life beat landed
between.

Resolution: **split the document, don't edit it.**

:func:`split_persona_document` partitions the byte-exact source at the
``## 此刻的我（实时状态）`` heading into two halves:

* :attr:`PersonaDocument.stable` — everything else, with the volatile section
  replaced by :data:`LIVE_STATE_POINTER`, a fixed string. This is byte-stable
  for the life of a conversation *by construction*: it contains no
  placeholder, so no state change can alter it. It is what goes into the
  system-prompt layer (``SOUL.md`` / profile identity).
* :attr:`PersonaDocument.volatile` — the section itself, still carrying its
  own framing prose, rendered per-turn with live values and delivered
  through the cache-safe user-message sidecar
  (``MemoryProvider.prefetch()`` → ``<memory-context>`` → ``api_content``).

The character text is never rewritten: the split is mechanical and the
on-disk asset stays byte-identical to corlinman's copy (asserted by test).
The only authored addition is :data:`LIVE_STATE_POINTER`, which is framework
scaffolding rather than character content — without it the model has no idea
the state block riding on each user message describes *itself*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .state import (
    PersonaState,
    RECENT_TOPICS_VISIBLE,
    bucket_fatigue,
    format_topics,
)

#: Persona slug. Doubles as the seed-pack filename stem, the plugin_db key
#: scope, and the ``persona_id`` an adapter binds to a channel.
PERSONA_ID: str = "grantley"

#: Heading of the volatile section inside the ported prompt body. Matched
#: exactly — if a future asset edit renames it, the split fails loudly (see
#: :func:`split_persona_document`) rather than silently baking live state
#: into the cached prefix.
VOLATILE_SECTION_HEADING: str = "## 此刻的我（实时状态）"

#: The stable stub that replaces the volatile section in the system prompt.
#: Authored for this port (NOT character content from corlinman) — it is the
#: pointer that makes the sidecar legible to the model. It must never contain
#: a placeholder or any value that varies within a conversation.
LIVE_STATE_POINTER: str = (
    "## 此刻的我（实时状态）\n"
    "\n"
    "你的实时状态（心情 / 精神状态 / 最近在聊 / 现在在做 / 人在哪 / 身边有谁 / "
    "状态 / 当前剧情线）不写在这里。\n"
    "每一轮对话都会在最新一条消息里附带一个 `<persona-state>` 块，"
    "那里面才是你**当前**的真实状态——以最新的一个为准。\n"
    "说话时自然带上，不要逐条复述、不要当成清单念出来。空着的字段就当它不存在。"
)

#: ``{{persona.<key>}}`` — the placeholder shape used by the source prompt.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*persona\.([A-Za-z0-9_]+)\s*\}\}")

#: Directory holding the byte-exact ported assets.
ASSETS_DIR: Path = Path(__file__).resolve().parent / "assets"

#: The ported system-prompt body. Byte-identical to corlinman's
#: ``corlinman_server/persona/default_grantley.md`` (the repo copy, which says
#: ``web_search`` — the wire tool name hermes actually dispatches).
PROMPT_ASSET_PATH: Path = ASSETS_DIR / "grantley.md"

#: Tag wrapping the per-turn state block in the user-message sidecar.
STATE_BLOCK_TAG: str = "persona-state"


class PersonaAssetError(RuntimeError):
    """Raised when a ported asset is missing or structurally unrecognised."""


@dataclass(frozen=True)
class PersonaDocument:
    """The persona prompt body, split along the cache boundary.

    Attributes
    ----------
    stable
        Cache-safe. Contains no placeholder. Goes into the system prompt.
    volatile
        The live-state section template, still holding its ``{{persona.*}}``
        placeholders. Rendered per turn into the user-message sidecar.
    source
        The unmodified document, kept so callers can assert byte-equality
        with the corlinman original.
    """

    stable: str
    volatile: str
    source: str

    def placeholder_keys(self) -> list[str]:
        """Return the placeholder keys the volatile section interpolates."""
        seen: dict[str, None] = {}
        for match in _PLACEHOLDER_RE.finditer(self.volatile):
            seen.setdefault(match.group(1), None)
        return list(seen)

    def assert_cache_safe(self) -> None:
        """Raise if the stable half still carries a placeholder.

        This is the invariant the whole design rests on: a placeholder in
        the system prompt means a per-turn cache miss. Cheap to check, so
        we check it on every load rather than only in tests.
        """
        leaked = _PLACEHOLDER_RE.findall(self.stable)
        if leaked:
            raise PersonaAssetError(
                "stable persona prompt still contains volatile placeholders "
                f"{sorted(set(leaked))!r}; that would invalidate the prompt "
                "cache on every state change"
            )


def split_persona_document(text: str) -> PersonaDocument:
    """Split *text* at :data:`VOLATILE_SECTION_HEADING`.

    The volatile section runs from its heading up to (but excluding) the
    next top-level ``## `` heading, or end-of-document. It is replaced in the
    stable half by :data:`LIVE_STATE_POINTER`.

    Raises :class:`PersonaAssetError` when the heading is absent — a silent
    fallback here would put live state back into the cached prefix, which is
    exactly the failure this port exists to prevent.
    """
    lines = text.split("\n")
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == VOLATILE_SECTION_HEADING:
            start = index
            break
    if start is None:
        raise PersonaAssetError(
            f"persona document has no {VOLATILE_SECTION_HEADING!r} section; "
            "cannot determine which part is volatile"
        )

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break

    volatile = "\n".join(lines[start:end]).strip("\n")
    stable_lines = lines[:start] + LIVE_STATE_POINTER.split("\n") + [""] + lines[end:]
    doc = PersonaDocument(
        stable="\n".join(stable_lines),
        volatile=volatile,
        source=text,
    )
    doc.assert_cache_safe()
    return doc


def load_persona_document(path: Path | None = None) -> PersonaDocument:
    """Load and split the ported prompt body."""
    target = Path(path) if path is not None else PROMPT_ASSET_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise PersonaAssetError(f"cannot read persona prompt {target}: {exc}") from exc
    return split_persona_document(text)


def resolve_placeholder(state: PersonaState, key: str) -> str:
    """Resolve one ``{{persona.<key>}}`` against *state*.

    Rules ported from ``corlinman_persona/placeholders.py``:

    * ``mood`` — the raw mood string.
    * ``fatigue`` — a **categorical bucket label**, never the float. The
      ``[0.0, 1.0]`` range is an implementation detail and does not belong
      in a prompt.
    * ``recent_topics`` — the freshest
      :data:`~plugins.grantley.state.RECENT_TOPICS_VISIBLE`, newest-first.
    * anything else — looked up in ``state_json`` (this is how the flat
      ``life_*`` mirror keys resolve). Missing keys yield ``""`` rather than
      raising, so a typo in a template cannot kill a render.
    """
    if key == "mood":
        return str(state.mood or "")
    if key == "fatigue":
        return bucket_fatigue(state.fatigue)
    if key == "recent_topics":
        return format_topics(list(state.recent_topics), RECENT_TOPICS_VISIBLE)
    raw = state.state_json.get(key)
    if raw is None:
        return ""
    return str(raw)


def render_placeholders(template: str, state: PersonaState) -> str:
    """Substitute every ``{{persona.*}}`` in *template* against *state*."""
    return _PLACEHOLDER_RE.sub(
        lambda m: resolve_placeholder(state, m.group(1)), template
    )


def render_state_block(
    doc: PersonaDocument,
    state: PersonaState,
    *,
    extra_lines: list[str] | None = None,
) -> str:
    """Render the per-turn sidecar block.

    Wrapped in ``<persona-state>`` so the model can tell this apart from the
    frozen identity in the system prompt, and so a stale copy further back in
    the transcript is visibly superseded by the newest one.

    *extra_lines* carries derived signals (life nudges, decayed life events).
    They ride here rather than in the system prompt for the same reason
    everything else does: they change between turns.
    """
    body = render_placeholders(doc.volatile, state)
    parts = [body]
    if extra_lines:
        parts.append("\n".join(str(line) for line in extra_lines if str(line).strip()))
    inner = "\n\n".join(p for p in parts if p.strip())
    return f"<{STATE_BLOCK_TAG}>\n{inner}\n</{STATE_BLOCK_TAG}>"


def describe_placeholders(state: PersonaState) -> Mapping[str, Any]:
    """Return every placeholder key/value pair — used by the CLI + tests."""
    doc = load_persona_document()
    return {key: resolve_placeholder(state, key) for key in doc.placeholder_keys()}


__all__ = [
    "ASSETS_DIR",
    "LIVE_STATE_POINTER",
    "PERSONA_ID",
    "PROMPT_ASSET_PATH",
    "STATE_BLOCK_TAG",
    "VOLATILE_SECTION_HEADING",
    "PersonaAssetError",
    "PersonaDocument",
    "describe_placeholders",
    "load_persona_document",
    "render_placeholders",
    "render_state_block",
    "resolve_placeholder",
    "split_persona_document",
]
