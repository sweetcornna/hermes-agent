"""Proactive group speech — the bot starting a conversation on its own.

⚠️  **Off by default, and enabling it is a NEW outward behaviour.**  The
corlinman implementation this was ported from set ``proactive_enabled = true``
in production, but a second switch (``group_replies_enabled = false``, the
emergency mute) silenced every group post including the proactive ones — so in
seven days of production logs the feature emitted exactly zero messages.  There
is no production behaviour to "restore" here: turning this on makes the bot
speak, unprompted, into real QQ groups for the first time.  See
``docs/migration-corlinman/B4-proactive-speech-notes.md``.

What this module is
-------------------
One resident asyncio loop per adapter.  Each beat it sleeps a *random* gap,
re-reads the live configuration, walks a fixed ladder of gates, and — if every
gate passes — runs one agent turn over the group's recent chatter and posts the
answer.  The model may answer ``SKIP`` to stay quiet; a person who glances at a
group chat does not always have something to say, and neither should this.

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
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from gateway.platforms.base import MessageEvent, MessageType
from gateway.response_filters import is_autonomous_silence_response

from . import persona_binding
from .adapter import (
    _GROUP_RECENT_MAX,
    _GROUP_SPEECH,
    group_speech_allowed,
    recent_group_messages,
    speech_key,
)
from .adapter import group_speech_muted as _adapter_group_speech_muted

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROMPT",
    "DEFAULT_TIMEZONE",
    "IDLE_RECHECK_SECS",
    "ProactiveConfig",
    "compose_prompt",
    "context_lines",
    "in_active_hours",
    "is_skip",
    "next_delay_secs",
    "now_parts",
    "proactive_loop",
    "resolve_config",
    "retrieval_query",
    "set_context_provider",
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


# ---------------------------------------------------------------------------
# Module state.  Deliberately process-wide, like the speech cap it works with:
# an adapter reconnect (or a config save that rebuilds the adapter) must not
# hand the bot a fresh daily budget.
# ---------------------------------------------------------------------------

#: ``key -> (day_str, count)`` — the per-group daily budget.
_SENT_TODAY: Dict[str, Tuple[str, int]] = {}

#: ``key -> time.monotonic()`` of the last proactive post.
_LAST_POST_MONO: Dict[str, float] = {}

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
    """Test hook — clear the daily budget and last-post clocks."""
    _SENT_TODAY.clear()
    _LAST_POST_MONO.clear()


def sent_today(key: str, day: str) -> int:
    """Posts made into ``key`` on ``day`` (0 once the day rolls over)."""
    record = _SENT_TODAY.get(key)
    return record[1] if record is not None and record[0] == day else 0


def mark_sent(key: str, day: str) -> None:
    _SENT_TODAY[key] = (day, sent_today(key, day) + 1)


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


def next_delay_secs(cfg: ProactiveConfig, rng: Optional[random.Random] = None) -> float:
    """Uniform draw over the configured gap window — irregular by design."""
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
) -> str:
    """Base prompt + optional transcript + optional snippets + the SKIP hatch.

    Context is what makes a post feel human: with it the persona can pick up
    the live topic instead of broadcasting into the void — and can bow out when
    barging in would read as botlike.
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
    parts.append(SKIP_HINT)
    return "\n\n".join(parts)


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


async def generate(adapter: Any, group: str, prompt: str) -> str:
    """Run one agent turn for a proactive post and return its text.

    Calls the gateway's message handler directly rather than
    ``handle_message`` — the autonomous lanes (cron, webhook) do the same —
    because the answer has to be inspected for the SKIP hatch *before* it can
    be delivered.  ``handle_message`` would send it for us.
    """
    handler = getattr(adapter, "_message_handler", None)
    if handler is None:
        return ""
    group_id = int(group)
    source = adapter.build_source(
        chat_id=f"g{group_id}",
        chat_name=adapter._group_display_name(group_id),
        chat_type="group",
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
            live_extra(adapter), chat_id=group_id, is_group=True
        ),
        metadata={
            "onebot_self_id": adapter.self_id,
            "onebot_group_id": group_id,
            "onebot_message_type": "group",
            # No ``onebot_at_user_id``: a proactive post pings nobody.
            "onebot_proactive": True,
        },
    )
    response = await handler(event)
    text, _ttl = adapter._unwrap_ephemeral(response)
    if not text:
        return ""
    # The same directive stripping the reply path applies, so a stray
    # ``MEDIA:``/image tag never reaches a QQ group as literal text.
    _media, text = adapter.extract_media(text)
    _images, text = adapter.extract_images(text)
    return (text or "").strip()


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
            "hours=%02d-%02d tz=%s p=%.2f ctx=%d",
            list(cfg.groups),
            cfg.min_gap_minutes,
            cfg.max_gap_minutes,
            cfg.daily_max,
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

        health = adapter.health_snapshot()
        if health.get("link_online") is not True or health.get("account_online") is False:
            continue
        if not adapter.self_id:
            continue
        # The emergency mute silences ALL group speech, proactive included.
        if group_speech_muted(adapter):
            continue
        # A person doesn't post every time they glance at their phone.
        if cfg.probability < 1.0 and random.random() >= cfg.probability:
            continue

        eligible = _eligible_groups(adapter, cfg, day, time.monotonic())
        if not eligible:
            continue
        group = random.choice(eligible)

        snippets = await _retrieve_snippets(adapter, group)
        prompt = compose_prompt(
            cfg, context_lines(adapter.instance_id, group, cfg), snippets
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

        try:
            # ``adapter.send`` is the reply path's own outbound shaping —
            # bubbles, chunking, the forward card, and recording the post into
            # the context buffer as ours.
            result = await adapter.send(f"g{group}", text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("OneBot: proactive send failed group=%s: %s", group, exc)
            continue
        if result is not None and not getattr(result, "success", True):
            logger.warning(
                "OneBot: proactive send rejected group=%s: %s",
                group,
                getattr(result, "error", ""),
            )
            continue

        key = speech_key(adapter.instance_id, group)
        mark_sent(key, day)
        _LAST_POST_MONO[key] = time.monotonic()
        # Spend from the SHARED budget, so replies and proactive posts together
        # honour the one promise made to the humans in the room.
        _GROUP_SPEECH.record(key)
        logger.info(
            "OneBot: proactive post sent group=%s chars=%d today=%d/%d",
            group,
            len(text),
            sent_today(key, day),
            cfg.daily_max,
        )
