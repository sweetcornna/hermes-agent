"""Reply gating — *should we answer this message at all?*

This module is the single place with an opinion about whether an inbound
OneBot message earns a turn.  The **order** of the checks is itself the
contract; several of the tests exist only to pin it down:

1. Resolve the @mention targets.  A non-zero ``self_id`` on the event wins
   over the configured seed list, so a NapCat account switch needs no
   config edit.
2. Flatten the text (``raw_message`` when the backend supplies it, else the
   segments).
3. Private chat: always dispatch.
4. Group chain, in order:
   a. ``group_replies_enabled`` master switch — checked BEFORE mention and
      keyword, so it really is an emergency mute (an @ cannot punch through).
   b. Group whitelist — a hard gate.  **An @mention does not bypass it**,
      and an *empty* set means "no group is allowed", which is different
      from ``None`` ("no whitelist configured").
   c. Explicit summons = @mention or a slash command.
   d. Reply policy + per-group cooldown for everything else.
5. Drop empty text (pure sticker, recall placeholder).
6. Token-bucket rate limits — deliberately AFTER the gate, so filtered
   messages never consume anyone's budget.

The group *speech cap* (a hard N-per-M-minutes budget shared with any
proactive speaking) is NOT here: it belongs after slash-command handling
and immediately before the model call, so operator commands are never
locked out and a capped group never burns an LLM call.  See
``adapter.py``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional

from .protocol import (
    MessageEvent,
    MessageType,
    is_mentioned,
    segments_to_text,
)
from .rate_limit import TokenBucket

__all__ = [
    "Binding",
    "ChannelRouter",
    "GroupKeywords",
    "RoutedRequest",
    "looks_like_command",
    "parse_group_keywords",
]


#: ``{"<group_id>": ["keyword", ...]}``.  Keys are strings because JSON
#: object keys are; values are case-insensitive substring matches against
#: the flattened message text.
GroupKeywords = Dict[str, List[str]]


#: A leading slash plus a command-shaped first token.  Mirrors what the
#: Hermes gateway will treat as a command (``MessageEvent.is_command()``),
#: but requires a word character after the slash so a bare "/" or a pasted
#: path fragment is not mistaken for a summons.
_COMMAND_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9_\-]*(?:\s|$)")


def looks_like_command(text: str) -> bool:
    """Whether *text* reads as a slash command.

    Group semantics: a slash command is an explicit summons, exactly like an
    @mention — it bypasses the keyword filter and the cooldown.  Without
    this, typing ``/status`` in a group would be silently swallowed by the
    keyword gate, which is how the ported gate behaves if you drop the
    command check on the floor.
    """
    return bool(_COMMAND_RE.match((text or "").lstrip()))


def parse_group_keywords(raw: str) -> GroupKeywords:
    """Parse the ``ONEBOT_GROUP_KEYWORDS`` JSON map.

    Empty / whitespace input returns an empty map.  Raises
    :class:`json.JSONDecodeError` for a non-object payload or a non-array
    value so a typo fails loudly at config load instead of silently
    disabling every keyword.
    """
    if not (raw or "").strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("expected JSON object at top level", raw, 0)
    out: GroupKeywords = {}
    for k, v in parsed.items():
        if not isinstance(v, list):
            raise json.JSONDecodeError(f"value for key {k!r} must be an array", raw, 0)
        out[str(k)] = [str(item) for item in v]
    return out


@dataclass(frozen=True)
class Binding:
    """Where a routed message came from, in transport-neutral terms.

    ``thread`` is the conversation: the group id for group chat, the peer's
    uin for a DM.  ``sender`` is always the message author.
    """

    channel: str
    account: str
    thread: str
    sender: str
    is_group: bool

    @classmethod
    def group(cls, self_id: int, group_id: int, user_id: int) -> "Binding":
        return cls("onebot", str(self_id), str(group_id), str(user_id), True)

    @classmethod
    def private(cls, self_id: int, user_id: int) -> "Binding":
        return cls("onebot", str(self_id), str(user_id), str(user_id), False)

    def session_key(self) -> str:
        """Stable conversation key (rate-limit maps, logs, dedup)."""
        kind = "group" if self.is_group else "dm"
        return f"{self.channel}:{kind}:{self.thread}"


@dataclass
class RoutedRequest:
    """A message that passed the gate and deserves a turn."""

    binding: Binding
    content: str
    message_id: Optional[str] = None
    timestamp: int = 0
    mentioned: bool = False
    is_command: bool = False

    @property
    def session_key(self) -> str:
        return self.binding.session_key()


@dataclass
class ChannelRouter:
    """Gate state.  Cheap to rebuild — cooldown clocks are the only history."""

    group_keywords: GroupKeywords = field(default_factory=dict)

    group_replies_enabled: bool = False
    """Master switch for group dispatch.

    ``False`` drops every group message BEFORE the mention/keyword checks
    (private chat is unaffected).

    **The default is False on purpose.**  The deployment this was ported for
    runs with the whole group pipeline configured — whitelist, keyword,
    rate limits — and this switch off, so the bot is silent in groups and
    answers only DMs.  Defaulting to False reproduces that exactly; turning
    groups on is a one-line config change.
    """

    group_whitelist: Optional[FrozenSet[str]] = None
    """Hard allow-list of group ids.

    ``None`` ⇒ no whitelist (every group passes this gate).  A set — even an
    EMPTY one — means only listed groups are ever answered.  @mentions do
    not bypass it: this is a security boundary, not a politeness rule.
    """

    group_reply_policy: str = "mention_or_keyword"
    """How a non-mention group message can still qualify.

    * ``"mention_or_keyword"`` (default) — reply to @mentions, or to messages
      matching an EXPLICITLY configured keyword list for that group.  No
      keyword list ⇒ mention-only.  This is the human-shaped default: a
      person does not answer every message in a group.
    * ``"all"`` — legacy dispatch-all: a group with no keyword list gets a
      reply to every message.
    """

    group_reply_cooldown_secs: float = 0.0
    """Minimum gap between NON-mention replies in one group.  ``0`` disables.

    An explicit summons always answers and RESETS the clock — a human
    replies when called.
    """

    self_ids: List[int] = field(default_factory=list)
    """Fallback @mention targets used only until a live ``self_id`` is seen."""

    group_limiter: Optional[TokenBucket] = None
    sender_limiter: Optional[TokenBucket] = None
    rate_limit_hook: Optional[Callable[[str, str], None]] = None
    """Observation hook fired on every silent rate-limit drop: ``(channel,
    reason)`` with reason in ``group`` / ``sender``."""

    _last_group_reply_mono: Dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def with_rate_limits(
        self, group: Optional[TokenBucket], sender: Optional[TokenBucket]
    ) -> "ChannelRouter":
        """Attach per-group / per-sender buckets (either may be ``None``)."""
        self.group_limiter = group
        self.sender_limiter = sender
        return self

    def with_rate_limit_hook(
        self, hook: Callable[[str, str], None]
    ) -> "ChannelRouter":
        self.rate_limit_hook = hook
        return self

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def dispatch(self, event: MessageEvent) -> Optional[RoutedRequest]:
        """Return a :class:`RoutedRequest`, or ``None`` to stay silent.

        Every drop is silent: callers log at DEBUG if they want visibility,
        and rate-limit drops additionally fire :attr:`rate_limit_hook`.
        """
        # The event names the account that received it.  Prefer that live
        # value so a stale configured id cannot keep acting as a mention
        # target after an account switch; fall back to the seed list only
        # for malformed / legacy zero ids.
        mention_targets = [event.self_id] if event.self_id > 0 else self.self_ids

        text = _flatten_and_trim(event.message, event.raw_message)
        mentioned = any(is_mentioned(event.message, sid) for sid in mention_targets)
        is_command = looks_like_command(text)

        if event.message_type == MessageType.PRIVATE:
            binding = Binding.private(event.self_id, event.user_id)
        elif event.message_type == MessageType.GROUP:
            if not self.group_replies_enabled:
                return None
            group_id = event.group_id
            if group_id is None:
                return None
            if (
                self.group_whitelist is not None
                and str(group_id) not in self.group_whitelist
            ):
                return None
            explicit = mentioned or is_command
            if not self._group_reply_allowed(group_id, text, explicit):
                return None
            binding = Binding.group(event.self_id, group_id, event.user_id)
        else:  # pragma: no cover — MessageType is a closed enum
            return None

        # Pure sticker / recall placeholder — nothing to answer.
        if not text.strip():
            return None

        # Rate limits run last so a filtered message never spends tokens.
        # Per-group first: cheaper and lower cardinality.
        if self.group_limiter is not None:
            if not self.group_limiter.check(f"{binding.channel}:{binding.thread}"):
                self._fire_hook(binding.channel, "group")
                return None
        if self.sender_limiter is not None:
            key = f"{binding.channel}:{binding.thread}:{binding.sender}"
            if not self.sender_limiter.check(key):
                self._fire_hook(binding.channel, "sender")
                return None

        return RoutedRequest(
            binding=binding,
            content=text,
            message_id=str(event.message_id),
            timestamp=event.time,
            mentioned=mentioned,
            is_command=is_command,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fire_hook(self, channel: str, reason: str) -> None:
        if self.rate_limit_hook is not None:
            try:
                self.rate_limit_hook(channel, reason)
            except Exception:  # noqa: BLE001 — observation must not gate chat
                pass

    def _group_reply_allowed(
        self, group_id: int, text: str, explicit: bool
    ) -> bool:
        """Reply gating for a group that already cleared the whitelist."""
        gid = str(group_id)
        now = time.monotonic()
        if explicit:
            self._last_group_reply_mono[gid] = now
            return True
        if self.group_reply_policy == "all":
            matched = self._keyword_match(gid, text)
        else:
            # mention_or_keyword: keywords must be explicitly configured —
            # a missing or empty list means mention-only.
            kws = self.group_keywords.get(gid) or []
            lower = text.lower()
            matched = any(kw.lower() in lower for kw in kws)
        if not matched:
            return False
        if self.group_reply_cooldown_secs > 0:
            last = self._last_group_reply_mono.get(gid)
            if last is not None and (now - last) < self.group_reply_cooldown_secs:
                return False
        self._last_group_reply_mono[gid] = now
        return True

    def _keyword_match(self, gid: str, text: str) -> bool:
        """Legacy ``all`` policy: no keyword list configured ⇒ match all."""
        kws = self.group_keywords.get(gid)
        if not kws:
            return True
        lower = text.lower()
        return any(kw.lower() in lower for kw in kws)


def _flatten_and_trim(segments: Iterable[Any], raw: str) -> str:
    """Prefer the backend's ``raw_message``; otherwise flatten the segments.

    ``raw_message`` is the CQ-code rendering the backend already produced.
    Using it keeps keyword matching identical to what a human sees in the
    client.  The segment fallback still renders ``at`` as ``@<qq> `` so an
    address never hides from the keyword gate.
    """
    if raw:
        return raw
    return segments_to_text(segments)
