"""Recent group chatter, handed to the reply lane as ``channel_context``.

Why this exists
---------------
A QQ group is a room, not a thread.  Someone asks "这个报错怎么修", three
people answer, and only the fourth message says "格兰 你觉得呢" — the one
message the router hands the model.  Every other adapter in this tree already
solves that: Discord scans backwards to its own last post
(``_fetch_channel_context``), Slack backfills the thread, the relay renders
its host's buffer, and all three land in ``MessageEvent.channel_context``,
which ``run.py`` prepends ahead of a ``[New message]`` marker before the turn
is built.  The OneBot lane was the only one that never filled the field, so
the persona answered "你觉得呢" without knowing what 呢 pointed at.

Two local facts make the gap worse here than elsewhere:

* ``group_sessions_per_user`` defaults to True, so a group's transcript is
  split one session per speaker — 张三's history has never contained a line
  李四 wrote, however long the bot has been in the room;
* the bot only answers when addressed, so the messages it does *not* answer
  are exactly the ones carrying the topic.

Where the data comes from
-------------------------
No network call and no new state.  ``adapter.record_group_message`` already
feeds a bounded per-group ring buffer (30 rows, 200 chars each) *before* the
reply gate, on the stated grounds that "a persona should see the whole room,
not only the messages it chose to answer".  This module renders a window of
that buffer.  The proactive lane reads the same buffer through
``proactive.context_lines``, so both lanes see one room rather than two.

The window
----------
Backwards from the newest entry, stopping at the bot's own last post, capped
at :func:`limit_from_extra` lines.  Our own message is the partition for the
reason Discord uses it: everything older was already visible to the model as
its own turn, everything newer is not.

That partition is also what stops the block repeating itself.  A line shown
as context in one turn sits *before* the reply that turn produced, so the
next scan halts before reaching it — no bookkeeping, no dedup pass, and
nothing to get out of step after a restart (the buffer is RAM-only, so a
restart simply yields a shorter window, never a duplicated one).

Rendering deliberately matches the trigger message's own shape: ``run.py``
prefixes the current message with ``[display name] `` in a shared group
session, so context lines are written the same way and the model reads one
transcript instead of two stacked formats.  QQ display names are set by their
owners, so they go through the same neutraliser that call site uses — an
embedded newline in a nickname would otherwise let a group member open a fake
section inside content the model reads every turn.

Off switch: ``group_context_messages: 0``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "CONTEXT_HEADER",
    "DEFAULT_LIMIT",
    "ENV_KEY",
    "EXTRA_KEY",
    "LINE_CHARS",
    "limit_from_extra",
    "render_context",
    "select_window",
]

#: Sibling of Discord's ``[Recent channel messages]``.  ``run.py`` writes the
#: ``[New message]`` marker that closes the block, so this header only has to
#: open it.
CONTEXT_HEADER = "[Recent group messages]"

EXTRA_KEY = "group_context_messages"
ENV_KEY = "ONEBOT_GROUP_CONTEXT_MESSAGES"

#: Lines, not characters.  The ring buffer holds 30; a dozen is roughly one
#: screen of QQ chat, enough to carry a topic without burying the message the
#: bot was actually asked to answer.
DEFAULT_LIMIT = 12

#: Per line.  ``_GROUP_RECENT_TEXT_CHARS`` already caps what the buffer
#: stores; this is a second, independent cap so raising that one cannot
#: silently multiply the size of every prompt.
LINE_CHARS = 200

#: Buffer rows are ``(stamp, sender, text, is_self)``.
Entry = Tuple[float, str, str, bool]


def limit_from_extra(extra: Mapping[str, Any]) -> int:
    """How many context lines to offer; ``0`` turns the block off entirely.

    ``extra`` first so an in-place config reconcile takes effect without a
    restart, env second, default last — the precedence every other knob in
    this adapter uses.
    """
    raw: Any = None
    if isinstance(extra, Mapping):
        raw = extra.get(EXTRA_KEY)
    if raw is None:
        raw = os.getenv(ENV_KEY)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_LIMIT
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning("OneBot: ignoring non-numeric %s=%r", EXTRA_KEY, raw)
        return DEFAULT_LIMIT


def select_window(entries: Sequence[Entry], limit: int) -> List[Entry]:
    """The chatter since the bot last spoke, oldest first, at most *limit*.

    *entries* must NOT already contain the triggering message: the caller
    snapshots the buffer before recording it, because that message is about
    to be the turn itself and would otherwise be shown twice.
    """
    if limit <= 0:
        return []
    window: List[Entry] = []
    for entry in reversed(entries):
        if entry[3]:  # our own post — everything older is already in history
            break
        if not (entry[2] or "").strip():
            continue
        window.append(entry)
        if len(window) >= limit:
            break
    window.reverse()
    return window


def render_context(
    entries: Sequence[Entry], limit: int, *, sender_id: Optional[str] = None
) -> Optional[str]:
    """Recent chatter plus trusted envelope sender identity for this turn.

    Without sender identity, return ``None`` on an empty path, so the caller
    can assign it straight onto ``MessageEvent.channel_context`` — ``run.py``
    tests that field for truthiness and an empty string would be equivalent,
    but only ``None`` says "there was nothing" rather than "there was a block
    and it rendered blank".
    """
    lines = [CONTEXT_HEADER]
    for _stamp, sender, text, _is_self in select_window(entries, limit):
        body = _inert(text, LINE_CHARS)
        if not body:
            continue
        lines.append(f"[{_inert(sender, 64) or '某人'}] {body}")
    history = "\n".join(lines) if len(lines) > 1 else ""
    # Shared group sessions cannot identify a person from a mutable nickname.
    # Keep envelope identity beside the current user turn, never in the cached
    # system prefix or command text. Historic quoted IDs remain just history.
    sender = str(sender_id or "")
    if sender.isascii() and sender.isdigit():
        current_sender = f"[Current QQ sender ID: {sender}]"
        return f"{history}\n\n{current_sender}" if history else current_sender
    return history or None


def _inert(value: Any, max_chars: int) -> str:
    """One line of untrusted text, newlines collapsed, length capped."""
    try:
        from gateway.session import neutralize_untrusted_inline_text
    except Exception:  # noqa: BLE001 — context is decorative; never fail a turn
        return " ".join(str(value or "").split())[:max_chars]
    return neutralize_untrusted_inline_text(value or "", max_chars=max_chars)
