"""Proactive speech — the bot starting a conversation on its own.

⚠️  **Off by default, and enabling any lane here is a NEW outward behaviour.**
The corlinman implementation this was ported from set ``proactive_enabled =
true`` in production, but a second switch (``group_replies_enabled = false``,
the emergency mute) silenced every group post including the proactive ones — so
in seven days of production logs the feature emitted exactly zero messages.
There is no production behaviour to "restore" here: turning this on makes the
bot speak, unprompted, into real QQ groups for the first time.  See
``docs/migration-corlinman/B4-proactive-speech-notes.md``.

The two lanes added on top of that one are newer still and ship off for the
same reason, only more so:

* **主动语音** (``proactive_voice_*``) — the same post, delivered as a voice
  bubble instead of text.  A reader can skim past a wall of text; a voice
  message has to be played, so the same sentence costs the room more.
* **主动语音通话** (``proactive_call_*``) — the bot deciding, on its own, to
  place a call-shaped voice session with a specific person.  This is the most
  intrusive thing in this plugin: it pokes a real person's phone and then
  talks at them, with nobody having asked.  It is gated hardest, it refuses to
  guess who to call, and it stays off unless *both* it and the underlying
  ``voice_call_*`` subsystem are switched on.

What this module is
-------------------
Two resident asyncio loops per adapter.  Each beat sleeps a *random* gap,
re-reads the live configuration, walks a fixed ladder of gates, and — if every
gate passes — runs one agent turn and delivers the answer.  The model may
answer ``SKIP`` to stay quiet; a person who glances at a group chat does not
always have something to say, and neither should this.

The gate ladder, in order (the order is the contract; the tests pin it):

1. **enabled** — resolved fresh every beat, so a config change hot-applies
   without restarting the channel;
2. **active hours**, evaluated in an *explicit* timezone (never the process
   zone — the target host runs ``Asia/Tokyo`` while the business day is Beijing
   time, and an implicit fallback there is a one-hour silent drift);
3. **health** — the WebSocket up *and* the QQ account not known-offline;
4. **identity** — we know our own uin;
5. **the emergency mute** — ``group_replies_enabled`` silences *all* group
   speech, proactive included.  Both the live config value and the reactive
   path's own router flag must be on, so the two lanes can never disagree
   about whether the bot is muted;
6. **probability** — a person doesn't post every time they glance at a phone;
7. **per-group eligibility** — daily budget, minimum gap since our last post,
   the *shared* speech cap, and "somebody has spoken since we last did".

Modality is **not** a gate.  Whether a post goes out as text or as a voice
bubble is decided *after* gate 7, next to retrieval, precisely so it can never
change *which* group hears from us — only how this one post is delivered.  It
is an offer per beat, the same shape ``ONEBOT_STICKER_PROBABILITY`` has on the
reply path, and it books exactly the same budgets as a text post because it is
still the bot speaking.

The call lane (:func:`proactive_call_loop`) walks its own ladder, and gates
3, 4 and 6 are the *same functions* — :func:`link_healthy`,
:func:`identity_known`, :func:`passes_probability` — rather than a second copy
of the same three ideas.  Where it differs, it differs on purpose:

* **private chats only**, never a group;
* a second master switch: ``voice_call.resolve_config`` must also be enabled,
  and ``voice_call_allowed_users`` still applies.  This lane narrows the call
  subsystem's audience; it never widens it;
* its own daily budget and its own last-call clock, because the targets and
  the cost are different from a group post's;
* its own, narrower active-hours default (10–22 rather than 9–23) — calling
  somebody at 09:00 is not the same as posting in a group at 09:00;
* the emergency mute is **not** consulted.  ``group_replies_enabled`` is a
  group-speech switch everywhere else in this plugin (``adapter.send``,
  ``voice.speak`` and gate 5 above all scope it to groups), and giving it a
  fourth, wider meaning here would mean flipping the group panic switch
  silently disables an unrelated private-chat feature.  Recorded as a
  deliberate choice, not an oversight: ``proactive_call_enabled`` and
  ``voice_call_enabled`` are this lane's two switches.

Three things are deliberately shared with the reactive reply path rather than
reimplemented, because a second implementation is a second behaviour:

* the **speech cap** (``adapter.group_speech_allowed`` → one process-wide
  :class:`~.rate_limit.SlidingWindowCounter`).  Two independent counters would
  quietly double how much the bot says in a group;
* the **recent-chatter buffer** (``adapter.recent_group_messages``), which
  holds inbound member messages *and* the bot's own posts.  Both uses matter:
  it is the context the persona reads, and it is how the loop knows the bot
  spoke last (piling a second message on silence reads as spam);
* the **outbound shaping** — the post goes out through ``adapter.send()``, the
  same call the reply path uses, so ``[MSG_BREAK]`` bubbles, chunking, the
  merged-forward card and the self-recording all behave identically.  Building
  a private send path here is how ``[MSG_BREAK]`` ends up printed literally in
  a QQ message.

The voice lanes obey the same rule one level down: they go out through
:func:`plugins.platforms.onebot.voice.speak`, never through
``adapter.send_voice``.  ``send_voice`` reaches the wire via
``_send_attachment``, which skips the content policy and the self-recording
(``adapter.py:2424`` → ``:2272``), and a proactive voice post that never lands
in the recent-chatter buffer is a bot that answers itself out loud on the next
beat.  ``speak`` is the one place those guarantees live.

Retrieval (the one deliberate divergence)
-----------------------------------------
The source folded up to three snippets from the gateway's ``kb.sqlite``
document corpus into the prompt.  Hermes has no such corpus and this port does
not invent one.  What it does instead:

* the proactive turn is an ordinary agent turn, so Grantley's
  :class:`~plugins.grantley.memory_provider.GrantleyMemoryProvider` injects
  live persona state and salient life events exactly as it does for a reply;
* the composed prompt keeps the source's line telling the model it may call its
  memory tools before deciding what to say — pull-based recall by the model
  instead of push-based retrieval by the channel loop;
* the snippet-folding code and its "query on other people's words only, never
  our own" rule are ported intact behind :func:`set_context_provider`, which
  defaults to *unset*.  Nothing calls it today; it exists so a corpus can be
  attached later without re-deriving this logic.

That gap is recorded, not hidden: with no provider set, no ``资料库`` section
is emitted.

Known gaps in the voice lanes, named rather than hidden
-------------------------------------------------------
* :func:`generate` strips ``MEDIA:``/image directives and drops the media
  (B4 §8 gap 4).  Neither voice lane changes that, and neither is harmed by
  it: a voice bubble is audio the *provider* synthesizes from the words, so a
  model-chosen image was never going to ride along, and the stripped text is
  exactly what should be spoken.
* Nothing here sweeps an unanswered call.  :func:`proactive_call_loop` places
  the call; ending it on a ring timeout is the adapter's resident sweeper
  (``OneBotAdapter._call_sweeper_loop`` → ``voice_call.sweep``).  Both key the
  session with ``speech_key(instance_id, chat_id)`` on the same bare uin, so
  the two agree — but this lane depends on that sweeper running, and without
  it an unanswered call would leave a ``RINGING`` session that refuses the
  next invite with ``ALREADY_LIVE``.  That degrades in the safe direction (the
  bot calls somebody at most once and then stops), which is why it is a gap
  rather than a hazard.
* :func:`_place_call` does **not** route through ``adapter.start_voice_call``,
  which does exist.  It performs its own invite with the canned
  ``voice_call.RING_LINE``, and this lane's whole point is that the persona
  writes the opening line.  The reasoning, and what *is* reused instead, is
  recorded on :func:`_place_call`.
* Starting and stopping :func:`proactive_call_loop` belongs to the adapter's
  lifecycle wiring (``OneBotAdapter._start_proactive_call_loop``, created in
  ``connect()`` and cancelled in ``disconnect()``).  Like the text lane it is
  started even when the feature is off, so flipping the YAML switch
  hot-applies on the next beat instead of needing a restart.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from gateway.platforms.base import MessageEvent, MessageType
from gateway.response_filters import is_autonomous_silence_response

from . import persona_binding, proactive_judge
from .adapter import (
    _GROUP_RECENT_MAX,
    _GROUP_SPEECH,
    format_chat_id,
    group_speech_allowed,
    is_muted_send_result,
    is_policy_blocked_send_result,
    parse_chat_id,
    recent_group_messages,
    speech_key,
    split_bubbles,
)
from .adapter import group_speech_muted as _adapter_group_speech_muted

logger = logging.getLogger(__name__)

__all__ = [
    "CALL_SKIP_HINT",
    "DEFAULT_CALL_PROMPT",
    "DEFAULT_PROMPT",
    "DEFAULT_TIMEZONE",
    "IDLE_RECHECK_SECS",
    "ProactiveCallConfig",
    "ProactiveConfig",
    "ProactiveVoiceConfig",
    "VOICE_HINT",
    "VoiceVerdict",
    "called_today",
    "compose_call_prompt",
    "compose_prompt",
    "context_lines",
    "flatten_bubbles",
    "generate",
    "generate_private",
    "identity_known",
    "in_active_hours",
    "is_skip",
    "judge_room",
    "link_healthy",
    "live_call_config",
    "live_config",
    "live_voice_config",
    "mark_called",
    "next_delay_secs",
    "now_parts",
    "passes_probability",
    "proactive_call_loop",
    "proactive_loop",
    "resolve_call_config",
    "resolve_config",
    "resolve_voice_config",
    "retrieval_query",
    "set_context_provider",
    "voice_hint",
]


# ---------------------------------------------------------------------------
# Prompt vocabulary (verbatim from the source implementation)
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = (
    "现在你想在群里主动说点什么。结合你当前的状态、正在做的事或最近想到的话题，"
    "用你自己的口吻发一条简短自然的群聊消息（一两句话即可）。"
    "不要刻意打招呼，不要自我介绍，也不要每次都用相似的开头。"
)

#: The escape hatch.  Without it the bot always says *something*, which is the
#: single most botlike thing it can do.
SKIP_HINT = (
    "如果你看完之后觉得现在不适合插话（话题不便加入、群里正忙着聊别的、"
    "或者确实没什么想说的），就只回复 SKIP，不要输出其他内容。"
)

_SKIP_RE = re.compile(r"^\[?\s*skip\s*\]?[。.!！]?$", re.IGNORECASE)

#: Told to the model about its own lines in the transcript, and used as the
#: rendered label for them.  The persona must be able to tell its own messages
#: apart or it re-answers the newest human message every single beat.
SELF_LABEL = "你自己"

_CONTEXT_SUFFIX = (
    "\n记录里「{label}」开头的行是你之前发过的消息——不要重复、复述"
    "或换个说法再发一遍，也不要再次回答你已经回答过的问题。"
    "\n如果上面的聊天还在进行中且话题合适，可以自然地接话；"
    "如果话题已经过去了，就另起一个轻松的话头。不要复述别人刚说过的内容。"
    "\n需要的话可以先回忆一下你自己的相关记忆，再决定说什么。"
).format(label=SELF_LABEL)

#: Told to the model when this particular beat will be *heard* rather than
#: read.  Additive: the three constants above are pinned verbatim by tests and
#: are not touched — this is one more paragraph, appended only on a voice beat.
#:
#: It exists because a model writing for a chat window writes for eyes.
#: Markdown becomes "asterisk asterisk", a URL becomes forty spoken characters
#: of punctuation, and an emoji-only reply becomes silence.  ``spoken_script``
#: in ``voice.py`` cleans up what it can, but it cannot shorten a paragraph or
#: turn a link into a sentence — that has to be asked for up front.
VOICE_HINT = (
    "这条消息会以语音的形式发出去——对方是听到它，不是看到它。"
    "所以只写能直接读出口的话：不要 markdown、不要网址或链接、"
    "不要颜文字或只发表情，句子短一些、一口气说得完，"
    "整条控制在 {max_chars} 字以内。"
)


def voice_hint(max_chars: int) -> str:
    """:data:`VOICE_HINT` with the character budget filled in.

    The number is told to the model as well as enforced afterwards.  The
    enforcement alone would silently demote every long answer back to text;
    saying the limit out loud is what lets the model actually hit it.
    """
    return VOICE_HINT.format(max_chars=max_chars)


#: The opening line of a call the bot decided to place.  Ported in spirit from
#: ``voice_call.RING_LINE`` ("我给你打个电话，现在方便说话吗") but written by
#: the persona instead of canned: a call nobody asked for needs a reason, and a
#: fixed string is the one thing guaranteed not to have one.
DEFAULT_CALL_PROMPT = (
    "你现在想主动给这个人打个语音电话。说一句开场白——就是电话刚接通时你会开口的第一句话，"
    "顺带说清楚你这会儿为什么想起找他。自然一点，不用寒暄，一两句就够。"
)

#: The call lane's escape hatch.  :data:`SKIP_HINT` is worded for a group
#: ("话题不便加入"), which is not the question being asked here — the question
#: is whether to ring somebody at all, and that deserves its own wording.
#: :func:`is_skip` reads both answers identically.
CALL_SKIP_HINT = (
    "如果你觉得现在并不适合给他打这个电话（没什么要紧的事、时间不合适、"
    "或者你根本不想打），就只回复 SKIP，不要输出其他内容。"
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: How often the resident loop re-checks the live config while the feature is
#: OFF.  This is what makes enabling it hot-apply: the loop is always running,
#: it just idles.
IDLE_RECHECK_SECS = 60.0

#: Explicit by default and never "whatever zone the process happens to be in".
#: The production host is ``Asia/Tokyo``; the configured 9–23 active window is
#: Beijing office hours.  Leaving this implicit is a one-hour drift that shows
#: up only as the bot talking at the wrong time of day.
DEFAULT_TIMEZONE = "Asia/Shanghai"

#: Snippets folded into the prompt / per-snippet char cap (see the module
#: docstring — no provider is wired by default).
RETRIEVAL_TOP_K = 3
RETRIEVAL_SNIPPET_CHARS = 300

#: Per-message char cap inside the rendered transcript, and how much recent
#: human chatter a retrieval query may carry.  Both bound the prompt so a busy
#: group cannot turn one beat into a kilobyte of context.
CONTEXT_LINE_CHARS = 200
RETRIEVAL_QUERY_MESSAGES = 8
RETRIEVAL_QUERY_CHARS = 300

#: ``user_id`` stamped on the synthetic event.  It lands in the session key, so
#: the proactive lane keeps its own conversation thread per group instead of
#: barging into whatever session a human is mid-way through.
PROACTIVE_SENDER_ID = "proactive"

#: Character budget told to the model for a proactive call's opening line.
#: A constant rather than a config key on purpose: unlike a group post, a call
#: has no text form to degrade to — an over-long opening line is either spoken
#: or the call does not happen — so a *configurable* ceiling here would be a
#: switch whose only failure mode is silently cancelling calls.  Shorter than
#: the group lane's 120 because this is the first thing somebody hears after
#: their phone buzzes, and it is a greeting, not a monologue.
_CALL_LINE_MAX_CHARS = 60


# ---------------------------------------------------------------------------
# Module state.  Deliberately process-wide, like the speech cap it works with:
# an adapter reconnect (or a config save that rebuilds the adapter) must not
# hand the bot a fresh daily budget.
# ---------------------------------------------------------------------------

#: ``key -> (day_str, count)`` — the per-group daily budget.
_SENT_TODAY: Dict[str, Tuple[str, int]] = {}

#: ``key -> time.monotonic()`` of the last proactive post.
_LAST_POST_MONO: Dict[str, float] = {}

#: The call lane's own budget, in its own two dicts.  Deliberately **not** the
#: dicts above, even though both are keyed by :func:`speech_key`: the group
#: lane's keys carry group ids and this one's carry user uins, which live in
#: the same numeric space, and one shared dict would let a group post silently
#: eat a person's call allowance (or the reverse) on a collision.  The two also
#: mean different things — one is "how much the bot said in a room", the other
#: is "how many times it rang a phone".
_CALLS_TODAY: Dict[str, Tuple[str, int]] = {}
_LAST_CALL_MONO: Dict[str, float] = {}

#: Optional retrieval hook: ``(query, top_k) -> awaitable[sequence[str]]``.
ContextProvider = Callable[[str, int], Awaitable[Sequence[str]]]
_context_provider: Optional[ContextProvider] = None


def set_context_provider(provider: Optional[ContextProvider]) -> None:
    """Attach (or clear) the optional background-snippet retrieval hook.

    Unset by default — see the module docstring.  A provider that raises or
    hangs must never block a post, so the loop treats it as best-effort.
    """
    global _context_provider
    _context_provider = provider


def reset_state() -> None:
    """Test hook — clear the daily budgets and the last-post/last-call clocks."""
    _SENT_TODAY.clear()
    _LAST_POST_MONO.clear()
    _CALLS_TODAY.clear()
    _LAST_CALL_MONO.clear()


def sent_today(key: str, day: str) -> int:
    """Posts made into ``key`` on ``day`` (0 once the day rolls over)."""
    record = _SENT_TODAY.get(key)
    return record[1] if record is not None and record[0] == day else 0


def mark_sent(key: str, day: str) -> None:
    _SENT_TODAY[key] = (day, sent_today(key, day) + 1)


def sent_today_total(instance_id: str, groups: Sequence[str], day: str) -> int:
    """Posts made on ``day`` across ``groups`` of one instance."""
    return sum(sent_today(speech_key(instance_id, group), day) for group in groups)


def called_today(key: str, day: str) -> int:
    """Calls placed to ``key`` on ``day`` (0 once the day rolls over)."""
    record = _CALLS_TODAY.get(key)
    return record[1] if record is not None and record[0] == day else 0


def mark_called(key: str, day: str) -> None:
    _CALLS_TODAY[key] = (day, called_today(key, day) + 1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProactiveConfig:
    """Resolved ``proactive_*`` settings.  ``None`` elsewhere means "off"."""

    groups: Tuple[str, ...]
    min_gap_minutes: float
    max_gap_minutes: float
    daily_max: int
    active_start_hour: int
    active_end_hour: int
    prompt: str
    probability: float = 1.0
    timezone: str = DEFAULT_TIMEZONE
    context_messages: int = _GROUP_RECENT_MAX
    #: Posts per day across every target group together; ``0`` means only the
    #: per-group ``daily_max`` applies.
    daily_max_total: int = 0


@dataclass(frozen=True)
class ProactiveVoiceConfig:
    """Resolved ``proactive_voice_*`` settings.  ``None`` elsewhere means "off".

    This is a *modality*, not a feature: it never decides whether the bot
    speaks, only whether this particular post is a voice bubble.  Everything
    about which group, how often and how much is still :class:`ProactiveConfig`.
    """

    probability: float = 0.0
    max_chars: int = 120
    #: Empty means "whatever ``text_to_speech_tool`` is configured to use" —
    #: passed through as ``None`` so the tool picks its own default rather
    #: than being handed the empty string as a provider name.
    provider: str = ""


@dataclass(frozen=True)
class ProactiveCallConfig:
    """Resolved ``proactive_call_*`` settings.  ``None`` elsewhere means "off"."""

    #: QQ uins.  **Empty means the lane is off** — never "call anyone".
    targets: Tuple[str, ...]
    daily_max: int = 1
    min_gap_minutes: float = 240.0
    #: Narrower than the group lane's 9–23 on purpose.  A group post at 09:00
    #: is a notification somebody reads when they get to it; a call at 09:00 is
    #: a phone going off.
    active_start_hour: int = 10
    active_end_hour: int = 22
    probability: float = 1.0
    prompt: str = DEFAULT_CALL_PROMPT
    timezone: str = DEFAULT_TIMEZONE

    @property
    def max_gap_minutes(self) -> float:
        """The far edge of the pacing window, so :func:`next_delay_secs` works.

        There is deliberately no ``proactive_call_max_gap_minutes`` key: this
        lane's cadence is already bounded by ``proactive_call_daily_max`` and
        by the min gap, and one more dial would be one more thing that can be
        set to "every minute".  The ×4 is the same widening
        :func:`resolve_config` applies when the group lane's max gap is unset,
        for the same reason — human pacing is a window, not a metronome.
        """
        return self.min_gap_minutes * 4


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_groups(raw: Any) -> Tuple[str, ...]:
    """``[1, "2"]`` / ``"1,2"`` → ``("1", "2")``.  Ids are compared as text."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(v).strip() for v in raw if str(v).strip())
    return tuple(
        p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip()
    )


def resolve_config(
    extra: Any, group_whitelist: Optional[frozenset]
) -> Optional[ProactiveConfig]:
    """Read the flat ``proactive_*`` keys; ``None`` when the feature is off.

    Two different empties, two different answers:

    * ``proactive_groups`` **unset or empty** falls back to ``group_whitelist``
      — the natural reading of "speak in my whitelisted groups";
    * ``proactive_groups`` **set but nothing survives the whitelist filter**
      stays off.  The operator asked to narrow the target; answering that by
      broadening to every whitelisted group is the opposite of what they said.

    Enabled with no resolvable target logs a warning and stays off: guessing at
    a target here means guessing at which real people get messaged.
    """
    get = extra.get if hasattr(extra, "get") else (lambda k, d=None: getattr(extra, k, d))
    if not _as_bool(get("proactive_enabled", None), False):
        return None

    requested = _parse_groups(get("proactive_groups", None))
    groups = requested
    if requested and group_whitelist is not None:
        # The whitelist is the hard gate everywhere else — an @mention cannot
        # bypass it, so proactive speech certainly must not.  It is the last
        # barrier between a config typo and a message in a stranger's group.
        groups = tuple(g for g in requested if g in group_whitelist)
        outside = [g for g in requested if g not in group_whitelist]
        if outside:
            logger.warning(
                "OneBot: proactive_groups outside group_whitelist ignored: %s",
                outside,
            )
        if not groups:
            # DELIBERATE FIX to the source implementation, not a port of it.
            # There, an explicit target list that filtered down to nothing fell
            # through to "no groups ⇒ use the whole whitelist", which contradicts
            # that same function's own docstring ("Enabled with no resolvable
            # target ... stays off (never spam-guess)").  It is also fail-open in
            # the worst direction: an operator narrowing the bot down to one
            # group, and mistyping the id, would get unprompted speech in EVERY
            # whitelisted group instead of none.  Safe to correct because the
            # feature never ran in production, so there is no behaviour to stay
            # bug-compatible with.
            logger.warning(
                "OneBot: every requested proactive_groups entry (%s) is outside "
                "group_whitelist — proactive speech stays OFF rather than "
                "falling back to the whole whitelist",
                list(requested),
            )
            return None
    if not groups and group_whitelist:
        groups = tuple(sorted(group_whitelist))
    if not groups:
        logger.warning(
            "OneBot: proactive_enabled but no target groups "
            "(set proactive_groups or group_whitelist) — staying silent"
        )
        return None

    min_gap = _as_float(get("proactive_min_gap_minutes", None), 45.0)
    max_gap = _as_float(get("proactive_max_gap_minutes", None), 0.0)
    if max_gap < min_gap:
        # Human pacing is a wide window, not a metronome.
        max_gap = min_gap * 4

    probability = _as_float(get("proactive_probability", None), 1.0)
    probability = min(1.0, max(0.0, probability))

    raw_context = get("proactive_context_messages", None)
    context_messages = (
        _GROUP_RECENT_MAX if raw_context is None else _as_int(raw_context, _GROUP_RECENT_MAX)
    )

    timezone_name = str(get("proactive_timezone", None) or "").strip() or DEFAULT_TIMEZONE
    prompt = str(get("proactive_prompt", None) or "").strip() or DEFAULT_PROMPT

    return ProactiveConfig(
        groups=groups,
        min_gap_minutes=min_gap,
        max_gap_minutes=max_gap,
        daily_max=max(1, _as_int(get("proactive_daily_max", None), 4)),
        active_start_hour=_as_int(get("proactive_active_start_hour", None), 9),
        active_end_hour=_as_int(get("proactive_active_end_hour", None), 23),
        prompt=prompt,
        probability=probability,
        timezone=timezone_name,
        context_messages=max(0, context_messages),
        daily_max_total=max(0, _as_int(get("proactive_daily_max_total", None), 0)),
    )


def live_extra(adapter: Any) -> Dict[str, Any]:
    """The adapter's *live* settings mapping.

    ``PlatformConfig.extra`` is the object an in-place config reconcile would
    mutate; ``adapter._extra`` is the copy taken at construction.  Reading the
    live one first is what lets a saved config take effect on the next beat
    instead of on the next restart.
    """
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    if isinstance(extra, dict):
        return extra
    fallback = getattr(adapter, "_extra", None)
    return fallback if isinstance(fallback, dict) else {}


def live_config(adapter: Any) -> Optional[ProactiveConfig]:
    """Re-resolve the proactive config off the adapter's live settings."""
    return resolve_config(live_extra(adapter), getattr(adapter, "group_whitelist", None))


def resolve_voice_config(extra: Any) -> Optional[ProactiveVoiceConfig]:
    """Read the flat ``proactive_voice_*`` keys; ``None`` when the lane is off.

    Ships off, and ``proactive_voice_probability`` defaults to ``0.0`` on top
    of that — so even an operator who flips ``proactive_voice_enabled`` without
    reading further gets no voice until they say how often they want one.  Two
    accidental keystrokes, not one, before the bot starts talking out loud.
    """
    get = (
        extra.get if hasattr(extra, "get") else (lambda k, d=None: getattr(extra, k, d))
    )
    if not _as_bool(get("proactive_voice_enabled", None), False):
        return None
    probability = min(
        1.0, max(0.0, _as_float(get("proactive_voice_probability", None), 0.0))
    )
    # No "0 means unlimited" escape, for the reason ``voice_call.MIN_TIMEOUT_SECS``
    # records about its own floor: the one value that would make this knob
    # dangerous is the one an operator is most likely to type by accident.
    max_chars = max(1, _as_int(get("proactive_voice_max_chars", None), 120))
    return ProactiveVoiceConfig(
        probability=probability,
        max_chars=max_chars,
        provider=str(get("proactive_voice_provider", None) or "").strip(),
    )


def live_voice_config(adapter: Any) -> Optional[ProactiveVoiceConfig]:
    """Re-resolve the voice modality off the adapter's live settings."""
    return resolve_voice_config(live_extra(adapter))


def _private_targets(raw: Any, group_whitelist: frozenset) -> Tuple[str, ...]:
    """Parse ``proactive_call_targets`` into private-chat uins, dropping the rest.

    Two entries are refused, loudly, and both refusals only ever narrow:

    * anything that parses as a **group** (``g123``, ``group:123``).  This lane
      places private calls; a call-shaped voice session in a group puts every
      bubble in front of everybody, which ``voice_call.allow_groups`` exists to
      make an explicit, separate decision;
    * a bare id that is also a **whitelisted group** id.  Group ids and uins
      share one numeric space and nothing in the wire format tells them apart,
      so this is the only signal available that an operator pasted the wrong
      number.  A uin that happens to equal a whitelisted group id costs one
      missed call; the other direction costs a call placed into a group.

    Duplicates collapse, order is preserved: the operator's list order is the
    order the lane considers targets in.
    """
    targets: List[str] = []
    for entry in _parse_groups(raw):
        try:
            is_group, target = parse_chat_id(entry)
        except ValueError:
            logger.warning(
                "OneBot: proactive_call_targets entry %r is not a usable QQ id "
                "— ignored",
                entry,
            )
            continue
        if is_group:
            logger.warning(
                "OneBot: proactive_call_targets entry %r names a GROUP — ignored; "
                "this lane places private calls only",
                entry,
            )
            continue
        text = str(target)
        if text in group_whitelist:
            logger.warning(
                "OneBot: proactive_call_targets entry %r is a whitelisted GROUP id, "
                "not a person — ignored rather than called",
                entry,
            )
            continue
        targets.append(text)
    return tuple(dict.fromkeys(targets))


def resolve_call_config(extra: Any) -> Optional[ProactiveCallConfig]:
    """Read the flat ``proactive_call_*`` keys; ``None`` when the lane is off.

    Ships off.  Turning it on is the most outward behaviour in this plugin:
    the bot begins ringing real people's phones on its own initiative.

    ``proactive_call_targets`` empty — unset, blank, or emptied by the filters
    in :func:`_private_targets` — keeps the lane off, and there is deliberately
    **no fallback to "anyone we are allowed to talk to"**.  This is the same
    refusal :func:`resolve_config` makes about groups, for the same reason
    stated there: guessing at a target here means guessing at which real people
    get contacted.  Unlike the group lane there is not even a whitelist to fall
    back to, which makes the guess worse rather than more excusable.

    ``proactive_timezone`` is shared with the group lane rather than given a
    ``proactive_call_timezone`` twin: one bot in one place has one local hour,
    and two zone keys is a drift that only ever shows up as the bot phoning
    somebody an hour early.
    """
    get = (
        extra.get if hasattr(extra, "get") else (lambda k, d=None: getattr(extra, k, d))
    )
    if not _as_bool(get("proactive_call_enabled", None), False):
        return None

    whitelist = frozenset(_parse_groups(get("group_whitelist", None)))
    targets = _private_targets(get("proactive_call_targets", None), whitelist)
    if not targets:
        logger.warning(
            "OneBot: proactive_call_enabled but no usable proactive_call_targets "
            "— the call lane stays OFF rather than guessing who to call"
        )
        return None

    probability = min(
        1.0, max(0.0, _as_float(get("proactive_call_probability", None), 1.0))
    )
    timezone_name = (
        str(get("proactive_timezone", None) or "").strip() or DEFAULT_TIMEZONE
    )
    prompt = (
        str(get("proactive_call_prompt", None) or "").strip() or DEFAULT_CALL_PROMPT
    )
    return ProactiveCallConfig(
        targets=targets,
        daily_max=max(1, _as_int(get("proactive_call_daily_max", None), 1)),
        min_gap_minutes=max(
            0.0, _as_float(get("proactive_call_min_gap_minutes", None), 240.0)
        ),
        active_start_hour=_as_int(get("proactive_call_active_start_hour", None), 10),
        active_end_hour=_as_int(get("proactive_call_active_end_hour", None), 22),
        probability=probability,
        prompt=prompt,
        timezone=timezone_name,
    )


def live_call_config(adapter: Any) -> Optional[ProactiveCallConfig]:
    """Re-resolve the call lane off the adapter's live settings."""
    return resolve_call_config(live_extra(adapter))


def group_speech_muted(adapter: Any) -> bool:
    """``True`` when the emergency mute is silencing group speech.

    Two sources have to agree, and either one being off means muted: the live
    config value (so flipping the mute hot-applies) and the router flag the
    *reactive* path actually obeys (so the two lanes can never end up in a
    state where replies are muted but the bot is still talking).

    The rule itself moved to :func:`plugins.platforms.onebot.adapter.
    group_speech_muted` when D44 put the same gate on the outbound path.  It
    is re-exported here unchanged, because three lanes deciding "am I muted"
    from two implementations is the disagreement this function exists to
    prevent.
    """
    return _adapter_group_speech_muted(adapter)


def speech_window(adapter: Any) -> Tuple[float, int]:
    """``(window_secs, max_messages)`` for the shared cap, read live."""
    extra = live_extra(adapter)
    if "group_rate_limit_window_minutes" in extra or "group_rate_limit_max_messages" in extra:
        window = max(0.0, _as_float(extra.get("group_rate_limit_window_minutes"), 0.0)) * 60.0
        return window, max(0, _as_int(extra.get("group_rate_limit_max_messages"), 0))
    return (
        float(getattr(adapter, "group_window_secs", 0.0) or 0.0),
        int(getattr(adapter, "group_window_max", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def now_parts(tz_name: str) -> Optional[Tuple[str, int]]:
    """``(YYYY-MM-DD, hour)`` in ``tz_name``, or ``None`` if there is no clock.

    A bad zone name falls back to :data:`DEFAULT_TIMEZONE` — loudly — and never
    to the process zone.  Returning ``None`` (no usable tz database at all)
    makes the caller skip the beat: staying silent beats posting at an hour
    nobody asked for.
    """
    from zoneinfo import ZoneInfo  # stdlib; imported here to keep import cost off startup

    candidates = [tz_name] if tz_name else []
    if DEFAULT_TIMEZONE not in candidates:
        candidates.append(DEFAULT_TIMEZONE)
    for index, candidate in enumerate(candidates):
        try:
            now = datetime.now(ZoneInfo(candidate))
        except Exception:  # noqa: BLE001 — bad name, or no tzdata installed
            logger.warning(
                "OneBot: proactive timezone %r is unusable%s",
                candidate,
                " — falling back to " + DEFAULT_TIMEZONE if index == 0 else "",
            )
            continue
        return now.strftime("%Y-%m-%d"), now.hour
    logger.error(
        "OneBot: no usable timezone database — skipping the proactive beat "
        "rather than guessing the local hour"
    )
    return None


def in_active_hours(hour: int, start: int, end: int) -> bool:
    """``hour`` inside the ``[start, end)`` window.

    ``start == end`` degenerates to always-on; ``start > end`` wraps overnight
    (e.g. 22 → 2).
    """
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def next_delay_secs(cfg: Any, rng: Optional[random.Random] = None) -> float:
    """Uniform draw over the configured gap window — irregular by design.

    Takes anything with ``min_gap_minutes`` / ``max_gap_minutes``, which is
    both :class:`ProactiveConfig` and :class:`ProactiveCallConfig`.  One
    pacing implementation for both loops: a second one is a second cadence
    that drifts the first time either is tuned.
    """
    source = rng or random
    return source.uniform(cfg.min_gap_minutes * 60.0, cfg.max_gap_minutes * 60.0)


async def sleep_or_cancel(cancel: asyncio.Event, secs: float) -> bool:
    """Cancel-aware sleep.  ``True`` means "cancelled, stop looping"."""
    try:
        await asyncio.wait_for(cancel.wait(), timeout=max(secs, 1.0))
    except (asyncio.TimeoutError, TimeoutError):
        return False
    return True


# ---------------------------------------------------------------------------
# Gates shared by both loops
#
# Extracted rather than copied.  Two lanes asking "is the link up" from two
# functions is two answers the day either one is corrected — the same argument
# the module docstring makes about the speech cap and the outbound path, one
# level down.  Each of these is exactly one rung of the documented ladder, so
# the ladder stays readable at both call sites.
# ---------------------------------------------------------------------------


def link_healthy(adapter: Any) -> bool:
    """Gate 3 — the WebSocket is up and the QQ account is not known-offline.

    ``account_online`` is compared against ``False`` rather than truth-tested:
    ``None`` means "the backend has not told us yet", and refusing to speak
    until a backend that never reports status says something would silence the
    bot permanently on those builds.
    """
    health = adapter.health_snapshot()
    return (
        health.get("link_online") is True and health.get("account_online") is not False
    )


def identity_known(adapter: Any) -> bool:
    """Gate 4 — we know our own uin."""
    return bool(adapter.self_id)


def passes_probability(probability: float, rng: Optional[random.Random] = None) -> bool:
    """Gate 6 — a person doesn't act every time they glance at their phone.

    ``probability >= 1.0`` short-circuits *without* drawing, so the common
    "always" configuration does not consume the shared RNG stream and a test
    that seeds it stays reproducible.
    """
    if probability >= 1.0:
        return True
    source = rng or random
    return source.random() < probability


# ---------------------------------------------------------------------------
# Context, retrieval and prompt
# ---------------------------------------------------------------------------


def _visible_buffer(instance_id: str, group: str) -> List[Tuple[float, str, str, bool]]:
    """The recent-chatter buffer with blank entries dropped.

    Stickers, recalls and media-only posts land in the buffer as empty text.
    They are noise in the transcript, and — worse — a blank inbound entry
    would read as "a human spoke last" and unblock a post nobody prompted.
    """
    return [
        entry for entry in recent_group_messages(instance_id, group) if (entry[2] or "").strip()
    ]


def last_message_is_self(instance_id: str, group: str) -> bool:
    """``True`` when the newest non-blank buffered message is the bot's own."""
    buffer = _visible_buffer(instance_id, group)
    return bool(buffer) and bool(buffer[-1][3])


def context_lines(instance_id: str, group: str, cfg: ProactiveConfig) -> List[str]:
    """Render the recent chatter as ``[HH:MM] sender: text`` lines."""
    if cfg.context_messages <= 0:
        return []
    buffer = _visible_buffer(instance_id, group)
    if not buffer:
        return []
    tz = None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(cfg.timezone)
    except Exception:  # noqa: BLE001 — timestamps are cosmetic; never fail a beat
        tz = None
    lines: List[str] = []
    for stamp, sender, text, is_self in buffer[-cfg.context_messages:]:
        clock = datetime.fromtimestamp(stamp, tz).strftime("%H:%M")
        label = SELF_LABEL if is_self else (sender or "某人")
        lines.append(f"[{clock}] {label}: {text.strip()[:CONTEXT_LINE_CHARS]}")
    return lines


def retrieval_query(instance_id: str, group: str) -> str:
    """Free-text query built from the group's recent **human** chatter.

    Our own posts are excluded on purpose: querying with the bot's own words
    retrieves the bot's own words, and the persona ends up talking to itself.
    Capped so a busy buffer cannot become a kilobyte-long query.
    """
    buffer = _visible_buffer(instance_id, group)
    if not buffer:
        return ""
    texts = [text for (_stamp, _sender, text, is_self) in buffer if not is_self]
    return " ".join(texts[-RETRIEVAL_QUERY_MESSAGES:])[:RETRIEVAL_QUERY_CHARS]


def compose_prompt(
    cfg: ProactiveConfig,
    lines: List[str],
    snippets: Optional[Sequence[str]] = None,
    *,
    voice_max_chars: Optional[int] = None,
) -> str:
    """Base prompt + optional transcript + optional snippets + the SKIP hatch.

    Context is what makes a post feel human: with it the persona can pick up
    the live topic instead of broadcasting into the void — and can bow out when
    barging in would read as botlike.

    ``voice_max_chars`` set means this beat will be *heard*: :func:`voice_hint`
    is appended just before the SKIP hatch, so "how to write it" sits next to
    "or don't".  ``None`` — the default, and every text beat — produces a
    byte-identical prompt to the one this function has always produced.
    """
    parts: List[str] = []
    if lines:
        parts.append(
            "以下是这个群最近的聊天记录（越靠下越新），供你参考语境：\n" + "\n".join(lines)
        )
    kept = [
        text.strip()[:RETRIEVAL_SNIPPET_CHARS]
        for text in (snippets or [])
        if text and text.strip()
    ][:RETRIEVAL_TOP_K]
    if kept:
        parts.append(
            "你的资料库里有几段可能和最近话题相关的内容（背景参考，"
            "别整段照搬，也别提到资料库本身）：\n" + "\n".join(f"- {s}" for s in kept)
        )
    parts.append(cfg.prompt + _CONTEXT_SUFFIX if lines else cfg.prompt)
    if voice_max_chars is not None:
        parts.append(voice_hint(voice_max_chars))
    parts.append(SKIP_HINT)
    return "\n\n".join(parts)


def compose_call_prompt(cfg: ProactiveCallConfig, max_chars: int) -> str:
    """The opening line's prompt: what to say + it is spoken + the SKIP hatch.

    No transcript.  The recent-chatter buffer is group-only
    (``adapter.record_group_message`` never sees a DM), so there is nothing
    honest to render here, and inventing a private-chat buffer to fill the gap
    would be a new store of other people's messages that nothing asked for.
    The persona's own memory is what carries the context, exactly as the module
    docstring describes for the group lane.
    """
    return "\n\n".join([cfg.prompt, voice_hint(max_chars), CALL_SKIP_HINT])


def flatten_bubbles(text: str) -> str:
    """The one-bubble form of a reply the persona wrote with ``[MSG_BREAK]``.

    A voice message *is* one bubble — there is no second audio file to carry
    the second half of a rhythm — so the marker has to go.  The flattening is
    the one ``adapter.send`` already performs when it records a post into the
    recent-chatter buffer (``" ".join(bubbles)``, ``adapter.py:2320``), reused
    rather than re-chosen: two answers to "what does a bubbled reply look like
    flattened" is how the spoken words and the buffered transcript end up
    disagreeing about what the bot just said.

    Not doing this is also how the literal string ``[MSG_BREAK]`` reaches a TTS
    provider and gets read out as "left square bracket M S G".

    One deliberate difference from ``adapter.send``'s own line: where it falls
    back to the raw ``content`` when nothing survives the split, this returns
    ``""``.  The only inputs that reach that branch are blank text and a reply
    that is *nothing but* markers, and for those the raw fallback hands the
    synthesizer the exact string this function exists to remove.  An empty
    script is read as "no voice this beat" and the post degrades to text,
    which is the honest answer for a reply with no words in it.
    """
    return " ".join(split_bubbles(text))


def is_skip(text: str) -> bool:
    """``True`` when the model chose to stay quiet.

    Accepts the source's ``SKIP`` vocabulary *and* hermes' own autonomous-lane
    silence markers (``[SILENT]`` / ``NO_REPLY``), which cron and the webhook
    lane already teach models to emit.  A model that has learned either idiom
    gets the same escape hatch.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    if _SKIP_RE.match(stripped):
        return True
    return is_autonomous_silence_response(stripped)


# ---------------------------------------------------------------------------
# One turn
# ---------------------------------------------------------------------------


async def _run_turn(
    adapter: Any, *, is_group: bool, target: int, chat_name: str, prompt: str
) -> str:
    """Run one agent turn on the bot's own initiative and return its text.

    Calls the gateway's message handler directly rather than
    ``handle_message`` — the autonomous lanes (cron, webhook) do the same —
    because the answer has to be inspected for the SKIP hatch *before* it can
    be delivered.  ``handle_message`` would send it for us.

    One function for both lanes, because everything that makes a proactive turn
    *proactive* — the dedicated sender slot, the disarmed slash-command path,
    the per-channel persona frame, the ``onebot_proactive`` marker — is the same
    whether the answer will be posted in a group or spoken down a phone.  Two
    copies would be two places to forget ``allow_gateway_control=False``.
    """
    handler = getattr(adapter, "_message_handler", None)
    if handler is None:
        return ""
    source = adapter.build_source(
        chat_id=format_chat_id(is_group, target),
        chat_name=chat_name,
        chat_type="group" if is_group else "dm",
        user_id=PROACTIVE_SENDER_ID,
        user_name=PROACTIVE_SENDER_ID,
    )
    event = MessageEvent(
        text=prompt,
        message_type=MessageType.TEXT,
        user_id=PROACTIVE_SENDER_ID,
        user_name=PROACTIVE_SENDER_ID,
        source=source,
        # Our own prompt text must never be read as an operator command: a
        # prompt that happens to start with "/" would otherwise be dispatched
        # as a gateway slash command.
        allow_gateway_control=False,
        # The same per-channel persona frame the reply lane sets, through the
        # same resolver.  A proactive post is PURE persona output — no user
        # message carries the framing for it — so this lane is the one where
        # the missing binding actually showed (00-PLAN.md §17/§18).
        channel_prompt=persona_binding.channel_prompt(
            live_extra(adapter), chat_id=target, is_group=is_group
        ),
        metadata={
            "onebot_self_id": adapter.self_id,
            "onebot_group_id": target if is_group else None,
            "onebot_message_type": "group" if is_group else "private",
            # No ``onebot_at_user_id``: a proactive post pings nobody.
            "onebot_proactive": True,
        },
    )
    response = await handler(event)
    text, _ttl = adapter._unwrap_ephemeral(response)
    if not text:
        return ""
    # The same directive stripping the reply path applies, so a stray
    # ``MEDIA:``/image tag never reaches a QQ group as literal text.  Recorded
    # gap (B4 §8): the media itself is dropped, not delivered.  Neither voice
    # lane is affected — a voice bubble is synthesized from the words, so
    # there was never a place for a model-chosen file to ride along.
    _media, text = adapter.extract_media(text)
    _images, text = adapter.extract_images(text)
    return (text or "").strip()


async def generate(adapter: Any, group: str, prompt: str) -> str:
    """Run one agent turn for a proactive group post and return its text."""
    group_id = int(group)
    return await _run_turn(
        adapter,
        is_group=True,
        target=group_id,
        chat_name=adapter._group_display_name(group_id),
        prompt=prompt,
    )


async def generate_private(adapter: Any, peer: str, prompt: str) -> str:
    """Run one agent turn for a proactive private turn and return its text.

    ``chat_name`` is the bare uin: unlike groups, the adapter keeps no nickname
    cache for peers, and blocking a beat on a ``get_stranger_info`` round trip
    to prettify a log line is a worse trade than the plain number.

    The sender slot is :data:`PROACTIVE_SENDER_ID`, the same one the group lane
    uses and *not* the peer's own uin.  That keeps the call's opening line out
    of whatever DM session the person is mid-way through — the reason B4-2
    gives for the group lane applies here with more force, because in a DM the
    session it would barge into is theirs.
    """
    _is_group, uin = parse_chat_id(peer)
    return await _run_turn(
        adapter,
        is_group=False,
        target=uin,
        chat_name=str(uin),
        prompt=prompt,
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _eligible_groups(
    adapter: Any, cfg: ProactiveConfig, day: str, now_mono: float
) -> List[str]:
    """Groups that pass every per-group gate, in configuration order."""
    instance_id = adapter.instance_id
    window_secs, window_max = speech_window(adapter)
    eligible: List[str] = []
    for group in cfg.groups:
        key = speech_key(instance_id, group)
        if sent_today(key, day) >= cfg.daily_max:
            continue
        last = _LAST_POST_MONO.get(key)
        if last is not None and (now_mono - last) < cfg.min_gap_minutes * 60.0:
            continue
        # Peek, never consume: the budget is spent when a message actually
        # goes out, not when we consider one.
        if not group_speech_allowed(
            instance_id, group, window_secs, window_max, record=False
        ):
            continue
        # Nobody has spoken since our last post — a second message on top of
        # silence reads as spam.  Wait for a human.
        if last_message_is_self(instance_id, group):
            continue
        eligible.append(group)
    return eligible


class VoiceVerdict(str, Enum):
    """What the voice modality did with one post — the degrade matrix.

    Three answers rather than a boolean, because two of the three failures
    must be handled in opposite directions:

    * :data:`SPOKEN` — a voice bubble went out.  Book the budget, done.
    * :data:`REFUSED` — the mute or the Tencent content policy said *do not
      speak here*.  There is no text fallback: re-sending the same words
      through ``adapter.send`` runs them past the same two gates and gets the
      same answer, so a "fallback" would either be a no-op that logs a scary
      error or — if the gates ever diverged — a way to launder a refusal into
      a delivered message.  The beat ends, and nothing is booked.
    * :data:`UNAVAILABLE` — synthesis was not possible this beat (no provider,
      a provider error, an oversized script, a transport failure on the record
      segment).  Nothing was refused and nothing was delivered, so the post
      falls back to the text lane and the beat continues.  Losing a post
      because a TTS provider was down would be a worse bug than the missing
      voice.
    """

    SPOKEN = "spoken"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


async def _post_voice(
    adapter: Any, group: str, text: str, cfg: ProactiveVoiceConfig
) -> VoiceVerdict:
    """Try to deliver one proactive post as a voice bubble.

    Goes through :func:`plugins.platforms.onebot.voice.speak`, which owns the
    ordered gate list (mute → content policy → connection → synthesize → vet →
    ``record`` segment → self-recording → cleanup).  ``adapter.send_voice``
    would skip two of those; see the module docstring.

    The import is local because ``voice_call`` imports *this* module, and the
    two voice modules are heavy at import time (a TTS toolchain, the content
    policy tables) for a lane that ships off.
    """
    from . import voice  # noqa: PLC0415 — see the docstring

    flat = flatten_bubbles(text)
    if not flat:
        return VoiceVerdict.UNAVAILABLE
    # The cap counts the FLATTENED text, not the composed reply.  ``[MSG_BREAK]``
    # is eleven characters of syntax that will never be heard, so counting it
    # would make the ceiling depend on punctuation the listener cannot perceive
    # — two replies of the same spoken length would land on opposite sides of
    # the line purely because one was written in two bubbles.
    if len(flat) > cfg.max_chars:
        logger.info(
            "OneBot: proactive post is %d chars, over proactive_voice_max_chars=%d "
            "— sending it as text instead group=%s",
            len(flat),
            cfg.max_chars,
            group,
        )
        return VoiceVerdict.UNAVAILABLE
    try:
        outcome = await voice.speak(
            adapter, f"g{group}", flat, provider=cfg.provider or None
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a broken TTS stack loses the voice, not the post
        logger.warning(
            "OneBot: proactive voice post raised group=%s: %s — falling back to text",
            group,
            exc,
        )
        return VoiceVerdict.UNAVAILABLE
    if outcome.spoke:
        return VoiceVerdict.SPOKEN
    failure = outcome.failure
    if failure is not None and (
        is_muted_send_result(failure) or is_policy_blocked_send_result(failure)
    ):
        logger.info(
            "OneBot: proactive voice post refused group=%s (%s) — NOT retrying as text",
            group,
            outcome.reason or "",
        )
        return VoiceVerdict.REFUSED
    logger.warning(
        "OneBot: proactive voice post unavailable group=%s (%s) — falling back to text",
        group,
        outcome.reason or "",
    )
    return VoiceVerdict.UNAVAILABLE


async def _post_text(adapter: Any, group: str, text: str) -> bool:
    """Deliver one proactive post as text.  ``True`` when it went out.

    ``adapter.send`` is the reply path's own outbound shaping — bubbles,
    chunking, the forward card, and recording the post into the context buffer
    as ours.
    """
    try:
        result = await adapter.send(f"g{group}", text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("OneBot: proactive send failed group=%s: %s", group, exc)
        return False
    if result is not None and not getattr(result, "success", True):
        logger.warning(
            "OneBot: proactive send rejected group=%s: %s",
            group,
            getattr(result, "error", ""),
        )
        return False
    return True


async def judge_room(adapter: Any, group: str, cfg: ProactiveConfig) -> "proactive_judge.JudgeVerdict":
    """Ask the pre-judge whether the room wants an unprompted post now.

    The judge always reads the full buffer, independent of
    ``proactive_context_messages``: that knob trims what the persona is shown,
    and a judge blind to the room could only ever say no.
    """
    settings = proactive_judge.resolve_settings(live_extra(adapter))
    buffer = _visible_buffer(adapter.instance_id, group)
    lines = context_lines(adapter.instance_id, group, replace(cfg, context_messages=_GROUP_RECENT_MAX))
    idle = (time.time() - buffer[-1][0]) / 60.0 if buffer else None
    try:
        from zoneinfo import ZoneInfo

        now_label = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001 — the label is context, not a gate
        now_label = datetime.now().strftime("%Y-%m-%d %H:%M")
    return await proactive_judge.judge(
        lines,
        self_label=SELF_LABEL,
        now_label=now_label,
        idle_minutes=idle,
        settings=settings,
    )


async def _retrieve_snippets(adapter: Any, group: str) -> List[str]:
    """Best-effort background snippets.  Unset by default; never fatal."""
    provider = _context_provider
    if provider is None:
        return []
    query = retrieval_query(adapter.instance_id, group)
    if not query:
        return []
    try:
        return list(await provider(query, RETRIEVAL_TOP_K) or [])
    except Exception as exc:  # noqa: BLE001 — retrieval must not block a post
        logger.warning("OneBot: proactive retrieval failed group=%s: %s", group, exc)
        return []


async def proactive_loop(
    adapter: Any,
    cancel: asyncio.Event,
    cfg: Optional[ProactiveConfig] = None,
) -> None:
    """Speak in the configured groups at a human pace, forever.

    The loop is resident even while the feature is OFF: ``cfg`` is only the
    *initial* resolve, and every beat re-reads the adapter's live settings.
    That is what makes enabling, disabling and retuning ``proactive_*``
    hot-apply without a channel restart — and it is why the idle beat is a
    cheap 60-second re-check rather than an early return.

    Nothing in here is allowed to crash the channel: a failed turn, a rejected
    send or a broken retrieval logs and skips to the next beat.
    """
    if cfg is not None:
        logger.info(
            "OneBot: proactive loop started groups=%s gap=%.0f-%.0fmin daily_max=%d "
            "daily_max_total=%d hours=%02d-%02d tz=%s p=%.2f ctx=%d",
            list(cfg.groups),
            cfg.min_gap_minutes,
            cfg.max_gap_minutes,
            cfg.daily_max,
            cfg.daily_max_total,
            cfg.active_start_hour,
            cfg.active_end_hour,
            cfg.timezone,
            cfg.probability,
            cfg.context_messages,
        )
    else:
        logger.info(
            "OneBot: proactive loop idle (disabled) instance=%s", adapter.instance_id
        )

    was_enabled = cfg is not None
    while not cancel.is_set():
        pre = live_config(adapter)
        delay = next_delay_secs(pre) if pre is not None else IDLE_RECHECK_SECS
        if await sleep_or_cancel(cancel, delay):
            break

        # Re-resolve AFTER the (long) sleep — the config may have been saved
        # while we slept, and that save should not wait for the next restart.
        cfg = live_config(adapter)
        if (cfg is not None) != was_enabled:
            was_enabled = cfg is not None
            logger.info(
                "OneBot: proactive %s (hot-applied) instance=%s",
                "enabled" if was_enabled else "disabled",
                adapter.instance_id,
            )
        if cfg is None:
            continue

        parts = now_parts(cfg.timezone)
        if parts is None:
            continue
        day, hour = parts
        if not in_active_hours(hour, cfg.active_start_hour, cfg.active_end_hour):
            continue

        if not link_healthy(adapter):
            continue
        if not identity_known(adapter):
            continue
        # The emergency mute silences ALL group speech, proactive included.
        if group_speech_muted(adapter):
            continue
        # A person doesn't post every time they glance at their phone.
        if not passes_probability(cfg.probability):
            continue

        # The overall allowance is checked before any group is picked, so a
        # spent day costs no judge call.
        if cfg.daily_max_total and (
            sent_today_total(adapter.instance_id, cfg.groups, day) >= cfg.daily_max_total
        ):
            continue

        eligible = _eligible_groups(adapter, cfg, day, time.monotonic())
        if not eligible:
            continue
        group = random.choice(eligible)

        # A separate model reads the room before the persona is asked to
        # speak: an in-character turn told "say something" rarely says SKIP.
        verdict = await judge_room(adapter, group, cfg)
        if not verdict.speak:
            logger.info(
                "OneBot: proactive post held by the pre-judge group=%s cause=%s reason=%s",
                group,
                verdict.cause or "judged",
                verdict.reason,
            )
            continue

        # Modality, decided here and nowhere earlier.  Sitting AFTER gate 7
        # is the contract: a voice roll must never be able to change which
        # group hears from us, only how this one post is delivered — the same
        # shape ``ONEBOT_STICKER_PROBABILITY`` has on the reply path, an offer
        # per turn rather than a rung of the ladder.
        voice_cfg = live_voice_config(adapter)
        as_voice = voice_cfg is not None and passes_probability(voice_cfg.probability)

        snippets = await _retrieve_snippets(adapter, group)
        prompt = compose_prompt(
            cfg,
            context_lines(adapter.instance_id, group, cfg),
            snippets,
            # Tell the model it is about to be heard, not read — before it
            # writes, because no amount of post-processing turns a URL into a
            # sentence.
            voice_max_chars=voice_cfg.max_chars if as_voice and voice_cfg else None,
        )
        try:
            text = await generate(adapter, group, prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — skip this beat, keep living
            logger.warning("OneBot: proactive turn failed group=%s: %s", group, exc)
            continue
        if not text:
            continue
        if is_skip(text):
            logger.info("OneBot: proactive post skipped by the model group=%s", group)
            continue

        spoke = False
        if as_voice and voice_cfg is not None:
            verdict = await _post_voice(adapter, group, text, voice_cfg)
            if verdict is VoiceVerdict.REFUSED:
                # "Do not speak here", not "speak differently" — see
                # :class:`VoiceVerdict`.  Nothing went out and nothing is booked.
                continue
            spoke = verdict is VoiceVerdict.SPOKEN
        if not spoke and not await _post_text(adapter, group, text):
            continue

        key = speech_key(adapter.instance_id, group)
        # Exactly the same three budgets whichever modality delivered it.  A
        # voice post is still the bot speaking in that room; giving it its own
        # allowance would double how much the bot says while every counter
        # still read as if it had not.  Booked ONCE, after one delivery — a
        # voice attempt that degraded to text delivered one message, not two.
        mark_sent(key, day)
        _LAST_POST_MONO[key] = time.monotonic()
        # Spend from the SHARED budget, so replies and proactive posts together
        # honour the one promise made to the humans in the room.
        _GROUP_SPEECH.record(key)
        logger.info(
            "OneBot: proactive post sent group=%s mode=%s chars=%d today=%d/%d",
            group,
            "voice" if spoke else "text",
            len(text),
            sent_today(key, day),
            cfg.daily_max,
        )


# ---------------------------------------------------------------------------
# 主动语音通话 — the bot placing a call nobody asked for
# ---------------------------------------------------------------------------


def _eligible_call_targets(
    adapter: Any,
    cfg: ProactiveCallConfig,
    call_cfg: Any,
    day: str,
    now_mono: float,
) -> List[str]:
    """Targets that pass every per-target gate, in configuration order.

    The mirror image of :func:`_eligible_groups`, and deliberately shaped like
    it: daily budget, minimum gap since we last rang this person, then the two
    questions only the call subsystem can answer.

    ``voice_call.user_allowed`` is consulted *in addition to* this lane's own
    target list, never instead of it.  Both must say yes, so this lane can only
    ever narrow the set of people the call subsystem already admits.

    The live-session check is a **peek**, exactly like the ``record=False``
    speech-cap peek in :func:`_eligible_groups`: ``voice_call.invite`` is still
    the authority and runs its own ladder a moment later.  Asking first only
    avoids spending an agent turn composing an opening line for a call that
    cannot be placed.
    """
    from . import voice_call  # noqa: PLC0415 — voice_call imports this module

    instance_id = adapter.instance_id
    eligible: List[str] = []
    for peer in cfg.targets:
        key = speech_key(instance_id, peer)
        if called_today(key, day) >= cfg.daily_max:
            continue
        last = _LAST_CALL_MONO.get(key)
        if last is not None and (now_mono - last) < cfg.min_gap_minutes * 60.0:
            continue
        if not voice_call.user_allowed(call_cfg, peer):
            logger.debug(
                "OneBot: proactive call target %s is outside voice_call_allowed_users",
                peer,
            )
            continue
        if voice_call.snapshot(key) is not None:
            continue
        eligible.append(peer)
    return eligible


async def _place_call(
    adapter: Any,
    cfg: ProactiveCallConfig,
    call_cfg: Any,
    peer: str,
    day: str,
    line: str,
) -> bool:
    """Ring ``peer`` and speak ``line``.  ``True`` when the voice went out.

    Why not ``adapter.start_voice_call``
    ------------------------------------
    It exists, and this lane deliberately does not use it: it performs the
    invite it built, and what it speaks is the canned ``voice_call.RING_LINE``.
    Routing through it would make ``proactive_call_prompt`` dead config and
    every unprompted call the same sentence — and the whole difference between
    a call the state machine placed on somebody's behalf and one the bot
    decided to place is that the second one owes the person a reason.

    So this uses the *same* pieces that wrapper uses — ``voice_call.invite``,
    ``voice_call.perform``, and the adapter's own ``_call_poke`` — and only
    substitutes the line.  ``CallDecision`` is frozen, so that substitution is
    a copy rather than a mutation of the state machine's own value.

    The one thing not shared is ``adapter._call_speak``, and that is this
    lane's own policy rather than an oversight: ``_call_speak`` degrades a line
    the voice path could not carry into a **text message**, which is right for
    a call somebody asked for (they are waiting for an answer) and wrong for
    one they did not — "poke, then an unsolicited DM" is not a quieter version
    of a call the person never wanted.  No voice here means no call, and the
    session is collected on the spot.
    """
    from . import voice, voice_call  # noqa: PLC0415 — voice_call imports this module

    key = speech_key(adapter.instance_id, peer)
    decision = voice_call.invite(
        key,
        chat_id=peer,
        is_group=False,
        peer=peer,
        now=time.monotonic(),
        cfg=call_cfg,
    )
    if not decision.ok:
        logger.info(
            "OneBot: proactive call refused by the call layer peer=%s reason=%s",
            peer,
            decision.refusal.value if decision.refusal else "unknown",
        )
        return False

    # Booked BEFORE the wire, not after.  ``perform`` pokes first and speaks
    # second, so by the time anything can fail a real phone has already
    # buzzed — the budget counts intrusions, not successes.  A lane that only
    # charged for successes would poke somebody once per beat for as long as
    # the TTS provider stayed down.
    mark_called(key, day)
    _LAST_CALL_MONO[key] = time.monotonic()

    # ``invite`` returns ``action=RING`` carrying ``voice_call.RING_LINE``; the
    # action (poke, then speak) is exactly what we want and only the words
    # change.  ``CallDecision`` is frozen, so this is a copy, not a mutation of
    # the state machine's own value.
    decision = replace(decision, line=line)

    async def _speak(spoken: str) -> Any:
        outcome = await voice.speak(adapter, peer, spoken)
        if not outcome.spoke:
            # ``perform`` documents that a speak failure propagates: a call
            # whose voice does not go out is a call the caller has to end.
            raise RuntimeError(outcome.reason or "the voice bubble did not go out")
        return outcome

    async def _poke() -> Any:
        # The adapter's own nudge, not a second ``SendPoke`` call site: it
        # already knows the group/private split and the 10-second timeout, and
        # ``perform`` treats a failed poke as decoration either way.
        return await adapter._call_poke(peer, peer)  # noqa: SLF001 — one plugin

    try:
        await voice_call.perform(decision, speak=_speak, poke=_poke)
    except asyncio.CancelledError:
        voice_call.hangup(key, voice_call.EndReason.ERROR, time.monotonic())
        raise
    except Exception as exc:  # noqa: BLE001 — a failed call must not kill the loop
        logger.warning("OneBot: proactive call failed peer=%s: %s", peer, exc)
        # Clear the session so the target is not left permanently RINGING, and
        # do NOT perform the sign-off line: speaking an apology through the
        # path that just failed to speak is a second failure, not a courtesy.
        voice_call.hangup(key, voice_call.EndReason.ERROR, time.monotonic())
        return False

    logger.info(
        "OneBot: proactive call placed peer=%s chars=%d today=%d/%d",
        peer,
        len(line),
        called_today(key, day),
        cfg.daily_max,
    )
    return True


async def _call_beat(adapter: Any, cfg: ProactiveCallConfig) -> None:
    """One pass of the call lane's gate ladder, then at most one call.

    The ladder, in order — and gates 3, 4 and 6 are the *same functions* the
    group loop walks, not a second reading of the same three ideas:

    1. **the call subsystem** — ``voice_call_enabled``.  This lane cannot place
       a call if the thing that models calls is off, and it is the cheapest
       question available;
    2. **active hours**, in the shared explicit timezone but this lane's own,
       narrower window;
    3. **health** — :func:`link_healthy`;
    4. **identity** — :func:`identity_known`;
    5. **probability** — :func:`passes_probability`;
    6. **per-target eligibility** — :func:`_eligible_call_targets`.

    There is no emergency-mute rung: ``group_replies_enabled`` is a group
    switch everywhere else in this plugin and this lane never touches a group.
    See the module docstring — recorded as a decision, not an omission.
    """
    from . import voice_call  # noqa: PLC0415 — voice_call imports this module

    call_cfg = voice_call.resolve_config(live_extra(adapter))
    if call_cfg is None:
        logger.debug(
            "OneBot: proactive_call_enabled but voice_call_enabled is off — "
            "the call lane stays silent"
        )
        return

    parts = now_parts(cfg.timezone)
    if parts is None:
        return
    day, hour = parts
    if not in_active_hours(hour, cfg.active_start_hour, cfg.active_end_hour):
        return
    if not link_healthy(adapter):
        return
    if not identity_known(adapter):
        return
    if not passes_probability(cfg.probability):
        return

    eligible = _eligible_call_targets(adapter, cfg, call_cfg, day, time.monotonic())
    if not eligible:
        return
    peer = random.choice(eligible)

    prompt = compose_call_prompt(cfg, _CALL_LINE_MAX_CHARS)
    try:
        line = await generate_private(adapter, peer, prompt)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — skip this beat, keep living
        logger.warning("OneBot: proactive call turn failed peer=%s: %s", peer, exc)
        return
    if not line:
        return
    if is_skip(line):
        logger.info("OneBot: proactive call skipped by the model peer=%s", peer)
        return

    await _place_call(adapter, cfg, call_cfg, peer, day, line)


async def proactive_call_loop(
    adapter: Any,
    cancel: asyncio.Event,
    cfg: Optional[ProactiveCallConfig] = None,
) -> None:
    """Place a call, on the bot's own initiative, at a human pace, forever.

    Structurally the twin of :func:`proactive_loop`, and for the same reasons:
    the loop is resident even while the lane is OFF, ``cfg`` is only the
    *initial* resolve, and every beat re-reads the adapter's live settings so
    enabling, disabling and retuning ``proactive_call_*`` hot-apply without a
    channel restart.  A disabled lane idles on the same cheap 60-second
    re-check rather than returning early.

    Nothing in here is allowed to crash the channel: a failed turn, a refused
    invite or a silent TTS provider logs and skips to the next beat.
    """
    if cfg is not None:
        logger.info(
            "OneBot: proactive CALL loop started targets=%s gap>=%.0fmin daily_max=%d "
            "hours=%02d-%02d tz=%s p=%.2f",
            list(cfg.targets),
            cfg.min_gap_minutes,
            cfg.daily_max,
            cfg.active_start_hour,
            cfg.active_end_hour,
            cfg.timezone,
            cfg.probability,
        )
    else:
        logger.info(
            "OneBot: proactive call loop idle (disabled) instance=%s",
            adapter.instance_id,
        )

    was_enabled = cfg is not None
    while not cancel.is_set():
        pre = live_call_config(adapter)
        delay = next_delay_secs(pre) if pre is not None else IDLE_RECHECK_SECS
        if await sleep_or_cancel(cancel, delay):
            break

        # Re-resolve AFTER the (long) sleep — the config may have been saved
        # while we slept, and that save should not wait for the next restart.
        cfg = live_call_config(adapter)
        if (cfg is not None) != was_enabled:
            was_enabled = cfg is not None
            logger.info(
                "OneBot: proactive calls %s (hot-applied) instance=%s",
                "enabled" if was_enabled else "disabled",
                adapter.instance_id,
            )
        if cfg is None:
            continue

        try:
            await _call_beat(adapter, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — skip this beat, keep living
            logger.warning("OneBot: proactive call beat failed: %s", exc)
