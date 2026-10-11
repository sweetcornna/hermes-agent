"""QQ 语音通话 — a *call-shaped* voice session, built out of voice messages.

Read this before anything else: what this is NOT
------------------------------------------------
This is **not** a real-time audio call, and nothing here joins QQ's own call
stack.  The OneBot v11 surface exposes no call signalling, no RTP, and no
media channel — its whole action surface is messages, plus the group AI voice
actions (``send_group_ai_record`` / ``get_ai_record`` /
``get_ai_characters``) and ``send_poke``.  A module claiming to "join a QQ
voice call" over this bridge would be fiction.

What it actually is
-------------------
A **conversational session over voice messages**.  Both sides trade voice
bubbles quickly inside an explicit, bounded session that has call semantics:
an invite, a ring, an accept or a decline, an active period, and a hangup —
by either side, by going quiet, or by hitting a cap.  The value is not the
transport (it is still voice bubbles); it is the *framing*: while a call is
up, the bot answers in voice, promptly, and it knows when the call is over
instead of drifting on forever.

Why the state machine knows nothing about the transport
-------------------------------------------------------
Everything in here is pure: no asyncio, no I/O, no wall clock.  Transitions
take ``now`` as a parameter and return a :class:`CallDecision` describing
*what the caller should do next and why* — speak this line, ring (poke +
speak), or do nothing.  The only place a decision meets the wire is
:func:`perform`, which takes the speaking capability as an **injected
callable**.

Two things follow from that, and both are the point:

* the tests are fully offline and run on a frozen clock — no sleeping, no
  sockets, no backend;
* if a backend ever *does* expose a native call action, it slots in
  underneath :func:`perform` without touching the session layer.

Deliberate divergences and known gaps, recorded rather than hidden
------------------------------------------------------------------
* The brief for this module named the invite parameter ``initiator``.  It is
  ``peer`` here: the ``voice_call_allowed_users`` narrowing filter has to key
  on *the human on the other end of the call*, and "initiator" is ambiguous
  for an outbound invite (we initiate it; the allowlist cares about who we
  are calling).  One name, one meaning, in both entry points.
* **There is no "the user asked for a call" classifier.**  :func:`offer` is
  called explicitly by whatever detects the request.  Of the three
  classifiers here, a "wants a call" regex is the one whose false positive
  rings a real person's phone unprompted, and guessing at its vocabulary is
  exactly the kind of guess this module refuses to make elsewhere.
* **The resident sweeper and the adapter wiring live in a later subtask.**
  This module decides; nothing here performs on its own schedule.  Caps
  therefore fire when someone calls :func:`sweep` — after each turn and on a
  timer is the intended cadence.
* ``voice_call_*`` keys are **YAML-only**, with no ``ONEBOT_VOICE_CALL_*``
  environment form, for the same reason ``proactive_*`` has none (see
  ``plugins/platforms/onebot/README.md`` and decision B4-5 in
  ``docs/migration-corlinman/B4-proactive-speech-notes.md``): the switch
  deciding whether the bot talks to real people lives in exactly one place,
  and an env var is read once at process start so it could never hot-apply.
  They must be added to ``adapter._PRIVATE_YAML_KEYS`` or the merge drops
  them silently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# The registry key is the adapter's own speech key, on purpose.  ``proactive``
# shares it too: a second keying scheme silently forks the per-chat state (see
# the note at ``proactive.py`` about a second implementation being a second
# behaviour), so this module imports the one that exists instead of building
# ``f"{instance_id}:{chat_id}"`` a third time.
from .adapter import speech_key

# The coercers are imported, not copied.  ``resolve_config`` below mirrors
# ``proactive.resolve_config`` down to the ``get`` shim, and two copies of
# "what counts as true" is how a config key ends up meaning one thing in one
# feature and something else in its sibling.
from .proactive import (
    _as_bool,
    _as_float,
    _as_int,
    _parse_groups as _parse_ids,
    live_extra,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CallAction",
    "CallConfig",
    "CallDecision",
    "CallIntent",
    "CallSession",
    "CallState",
    "EndReason",
    "Expiry",
    "Refusal",
    "accept",
    "active_sessions",
    "classify_intent",
    "decline",
    "expiry_reason",
    "hangup",
    "invite",
    "live_config",
    "note_local_turn",
    "note_remote_turn",
    "offer",
    "on_inbound",
    "perform",
    "reset_state",
    "resolve_config",
    "snapshot",
    "speech_key",
    "sweep",
    "user_allowed",
]


# ---------------------------------------------------------------------------
# Vocabulary the bot speaks.  Chinese first — this bot's users write Chinese —
# and short, because every one of these goes out as a voice bubble.
# ---------------------------------------------------------------------------

#: Spoken with the poke when we place a call.
RING_LINE = "我给你打个电话，现在方便说话吗"

#: They picked up the call we placed — the caller speaks first.
ACCEPT_OUTGOING_LINE = "喂，能听见吗"

#: We picked up the call they asked for.
ACCEPT_INCOMING_LINE = "在的，你说"

#: They turned our call down.  Say one graceful line and stop.
DECLINE_REMOTE_LINE = "行，那你先忙"

#: We turn their request down.
DECLINE_LOCAL_LINE = "现在不太方便，回头我找你"


class CallState(str, Enum):
    """Where a chat's call is.

    ``IDLE`` is not stored — it is what :func:`snapshot` reports for a chat
    with no session, and what a refusal decision carries when there is
    nothing to refuse against.  ``ENDED`` is terminal and always carries an
    :class:`EndReason`; ended sessions leave the registry immediately, so a
    stale corpse can never block the next invite.
    """

    IDLE = "idle"
    RINGING = "ringing"
    INCOMING = "incoming"
    ACTIVE = "active"
    ENDED = "ended"


class EndReason(str, Enum):
    """Why a session ended.  Every terminal path names one."""

    DECLINED = "declined"
    HANGUP_LOCAL = "hangup_local"
    HANGUP_REMOTE = "hangup_remote"
    RING_TIMEOUT = "ring_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    MAX_DURATION = "max_duration"
    MAX_TURNS = "max_turns"
    ERROR = "error"
    #: The feature was switched off while a call was up.  Not in the original
    #: list, and needed: hot-disabling must not leave a zombie session behind
    #: that the (now absent) sweeper would never collect.
    DISABLED = "disabled"


#: Sign-off spoken on each ending.  ``DISABLED`` is deliberately silent: the
#: operator just turned the feature off, and manufacturing one more voice
#: bubble on the way out is the opposite of obeying that switch.
_END_LINES: Dict[EndReason, str] = {
    EndReason.HANGUP_LOCAL: "那我先挂啦，回头再聊",
    EndReason.HANGUP_REMOTE: "好，那先这样，拜拜",
    EndReason.RING_TIMEOUT: "那你先忙，我回头再找你",
    EndReason.IDLE_TIMEOUT: "你好像去忙了，那我先挂啦",
    EndReason.MAX_DURATION: "聊了挺久了，先到这儿吧",
    EndReason.MAX_TURNS: "先聊到这儿吧，回头接着说",
    EndReason.ERROR: "这边好像出了点问题，先挂了，等下再说",
    EndReason.DECLINED: DECLINE_LOCAL_LINE,
    EndReason.DISABLED: "",
}


class CallAction(str, Enum):
    """What the caller should put on the wire.  Keep the caller dumb."""

    #: Nothing goes out.  Every refusal lands here.
    NOTHING = "nothing"
    #: Send ``decision.line`` as one voice bubble.
    SPEAK = "speak"
    #: The phone-ringing gesture: a 戳一戳 poke *and* ``decision.line``.
    RING = "ring"


class Refusal(str, Enum):
    """Why a transition did not happen.  ``ok=False`` always carries one."""

    DISABLED = "disabled"
    GROUPS_DISABLED = "groups_disabled"
    NOT_ALLOWED = "not_allowed"
    ALREADY_LIVE = "already_live"
    NO_SESSION = "no_session"
    WRONG_STATE = "wrong_state"
    #: Inbound text that is not a recognisable answer.  The caller should let
    #: the message fall through to normal handling.
    UNCLEAR = "unclear"
    #: A blank turn (sticker-only, recall, media-only).  Nothing was said.
    NO_CONTENT = "no_content"


class CallIntent(str, Enum):
    """What a human's words mean for a call."""

    NONE = "none"
    ACCEPT = "accept"
    DECLINE = "decline"
    HANGUP = "hangup"


# ---------------------------------------------------------------------------
# Configuration.  Flat ``voice_call_*`` keys off ``PlatformConfig.extra``,
# resolved fresh on every read so a saved config hot-applies — exactly the
# shape ``proactive.resolve_config`` uses, including "``None`` means off".
# ---------------------------------------------------------------------------

DEFAULT_RING_TIMEOUT_SECS = 60.0
DEFAULT_IDLE_TIMEOUT_SECS = 180.0
DEFAULT_MAX_DURATION_SECS = 900.0
DEFAULT_MAX_TURNS = 40

#: Floor for every duration.  There is deliberately no "0 means unlimited"
#: escape: a call that never ends on its own is precisely the failure this
#: module exists to prevent, and a zero ring timeout would end a call in the
#: same breath that started it.
MIN_TIMEOUT_SECS = 1.0


@dataclass(frozen=True)
class CallConfig:
    """Resolved ``voice_call_*`` settings.  ``None`` elsewhere means "off"."""

    ring_timeout_secs: float = DEFAULT_RING_TIMEOUT_SECS
    idle_timeout_secs: float = DEFAULT_IDLE_TIMEOUT_SECS
    max_duration_secs: float = DEFAULT_MAX_DURATION_SECS
    max_turns: int = DEFAULT_MAX_TURNS
    #: A call in a group is loud and disruptive — every bubble lands in front
    #: of everyone.  The operator opts in deliberately or not at all.
    allow_groups: bool = False
    #: Empty means "anyone the adapter already lets talk to it".  This filter
    #: only ever *narrows*: it is consulted in addition to the adapter's own
    #: allowlist, never instead of it, so it cannot widen access.
    allowed_users: Tuple[str, ...] = ()


def _clamp_secs(value: float) -> float:
    """Floor a duration at :data:`MIN_TIMEOUT_SECS`.

    ``max(MIN, value)`` also swallows NaN (``nan > MIN`` is false), so a
    ``voice_call_idle_timeout_secs: nan`` cannot produce a deadline that no
    comparison ever satisfies.
    """
    return max(MIN_TIMEOUT_SECS, value)


def resolve_config(extra: Any) -> Optional[CallConfig]:
    """Read the flat ``voice_call_*`` keys; ``None`` when the feature is off.

    Ships **off**.  Turning it on is a new outward behaviour: the bot starts
    placing and answering call-shaped voice sessions with real people.

    Resolve this on every read rather than caching it — that is the whole
    hot-apply mechanism, the same one ``proactive`` relies on.
    """
    # Same shim as ``proactive.resolve_config``: accept a mapping or any
    # attribute-bearing settings object.
    get = (
        extra.get if hasattr(extra, "get") else (lambda k, d=None: getattr(extra, k, d))
    )
    if not _as_bool(get("voice_call_enabled", None), False):
        return None

    return CallConfig(
        ring_timeout_secs=_clamp_secs(
            _as_float(
                get("voice_call_ring_timeout_secs", None), DEFAULT_RING_TIMEOUT_SECS
            )
        ),
        idle_timeout_secs=_clamp_secs(
            _as_float(
                get("voice_call_idle_timeout_secs", None), DEFAULT_IDLE_TIMEOUT_SECS
            )
        ),
        max_duration_secs=_clamp_secs(
            _as_float(
                get("voice_call_max_duration_secs", None), DEFAULT_MAX_DURATION_SECS
            )
        ),
        max_turns=max(1, _as_int(get("voice_call_max_turns", None), DEFAULT_MAX_TURNS)),
        allow_groups=_as_bool(get("voice_call_allow_groups", None), False),
        # ``_parse_ids`` is ``proactive._parse_groups``: ``[1, "2"]`` and
        # ``"1,2"`` both become ``("1", "2")``, ids compared as text.  Reused
        # rather than re-written so a QQ number is parsed identically
        # wherever this plugin reads a list of ids.
        allowed_users=_parse_ids(get("voice_call_allowed_users", None)),
    )


def live_config(adapter: Any) -> Optional[CallConfig]:
    """Re-resolve off the adapter's *live* settings mapping.

    Reuses ``proactive.live_extra`` — it already knows that
    ``PlatformConfig.extra`` is the object an in-place reconcile mutates,
    while ``adapter._extra`` is only the copy taken at construction.
    """
    return resolve_config(live_extra(adapter))


def user_allowed(cfg: CallConfig, user_id: str) -> bool:
    """Whether ``user_id`` survives the ``voice_call_allowed_users`` filter.

    An empty allowlist allows everyone *the adapter already admitted* — this
    function is never the only gate, and it can only subtract.  An unknown or
    blank id against a non-empty allowlist is refused: an allowlist we cannot
    evaluate is an allowlist that did not pass.
    """
    if not cfg.allowed_users:
        return True
    return str(user_id or "").strip() in cfg.allowed_users


# ---------------------------------------------------------------------------
# Intent parsing.
#
# The bias is asymmetric and deliberate: **ambiguous input must never be read
# as accept.**  Starting to talk at somebody who did not agree is the
# expensive error; making them repeat themselves is cheap.  So:
#
# * ACCEPT requires the *whole* utterance to be agreement — every
#   punctuation-separated clause matches;
# * DECLINE and HANGUP need only *one* clause to match, because ending or not
#   starting is the safe direction.
#
# Every pattern is anchored over a clause, never a substring sweep — the same
# protection ``proactive.is_skip`` has, where ``"skip今天不聊"`` must not read
# as ``SKIP``.  A keyword buried in an unrelated sentence matches nothing.
# ---------------------------------------------------------------------------

#: Clause boundaries.  Whitespace is NOT a boundary: "not now", "hang up" and
#: "gotta go" are single answers that happen to contain a space.
_CLAUSE_SPLIT_RE = re.compile(r"[，,。.！!？?~～、;；:：…]+")

#: Trimmed off each clause's ends before matching.
_TRIM_CHARS = " \t\r\n·「」【】《》\"'“”‘’()（）[]"

#: Interjections that may lead an answer without changing it.  Note that none
#: of them is an answer *on its own* — a bare "嗯" stays ambiguous.
_LEAD = r"(?:嗯+|那|那就|哦+|噢+|欸|诶|well|ok\s+then)?\s*"

#: Sentence-final particles that may trail an answer.
_TAIL = r"(?:吧|啊|呀|哇|哦|噢|嘞|咯|喽|了|的)?\s*"

_ACCEPT_CORE = (
    r"(?:接(?:听|吧|通|电话)?"
    r"|好(?:的|啊|呀|吧|哇|嘞)?"
    r"|可以(?:啊|呀)?"
    r"|行(?:啊|呀)?"
    r"|来吧|开始吧|说吧|聊吧|打吧|打过来"
    r"|喂"
    r"|在(?:的|呢)?"
    r"|没问题"
    r"|ok(?:ay)?|yes|yep|yeah|sure|fine"
    r"|pick\s*up|go\s*ahead|i'?m\s*here)"
)

_DECLINE_CORE = (
    r"(?:我?(?:现在)?(?:真的)?"
    r"(?:不(?:用|了|行|想|接|聊|方便|太方便|太行)?"
    r"|拒绝|算了|没空|没时间|不在|在忙|有点忙|忙着|忙)"
    r"|(?:待会|等会|等下|一会|回头|改天|下次|下回)(?:儿)?(?:再)?(?:说|聊)?"
    r"|no(?:pe)?|not\s*now|later|maybe\s*later|busy|can'?t|cannot|nah)"
)

_HANGUP_CORE = (
    r"(?:先?挂(?:了|吧|断|电话)?"
    r"|先这样(?:吧)?|就这样(?:吧)?|先到这(?:儿|里)?(?:吧)?"
    r"|不聊了|别聊了|结束吧"
    r"|拜拜?|白白|再见|回聊"
    r"|(?:回头|下次|改天|下回)(?:再)?(?:聊|说)"
    r"|我?先忙(?:去)?(?:了)?|我?去忙(?:了)?"
    r"|bye(?:\s*bye)?|goodbye|hang\s*up|gotta\s*go|talk\s*later|ttyl"
    r"|see\s*you|cya)"
)


def _clause_re(core: str) -> "re.Pattern[str]":
    return re.compile(rf"^{_LEAD}{core}{_TAIL}$", re.IGNORECASE)


_ACCEPT_RE = _clause_re(_ACCEPT_CORE)
_DECLINE_RE = _clause_re(_DECLINE_CORE)
_HANGUP_RE = _clause_re(_HANGUP_CORE)

#: A clause that is *only* interjection ("嗯", "那", "哦").  Dropped before
#: classifying, so "嗯，可以" reads as agreement while a bare "嗯" — which
#: carries no content at all — stays ambiguous.
_FILLER_RE = re.compile(rf"^{_LEAD}$")


def _clauses(text: str) -> List[str]:
    """Split an utterance into trimmed, non-empty clauses."""
    stripped = (text or "").strip()
    if not stripped:
        return []
    out: List[str] = []
    for piece in _CLAUSE_SPLIT_RE.split(stripped):
        clause = piece.strip().strip(_TRIM_CHARS).strip()
        if clause:
            out.append(clause)
    return out


def classify_intent(text: str) -> CallIntent:
    """Read a human's words as a call intent — conservatively.

    Checked in the order ``HANGUP → DECLINE → ACCEPT``; the order is the
    contract and the tests pin it.  Ending beats agreeing, so "好，挂了" is a
    hangup rather than an accept, and a message that is only *partly*
    agreement ("好，我先忙完这个") is :data:`CallIntent.NONE` — the caller
    should ask again rather than start talking.
    """
    clauses = [c for c in _clauses(text) if not _FILLER_RE.match(c)]
    if not clauses:
        return CallIntent.NONE
    if any(_HANGUP_RE.match(c) for c in clauses):
        return CallIntent.HANGUP
    if any(_DECLINE_RE.match(c) for c in clauses):
        return CallIntent.DECLINE
    if all(_ACCEPT_RE.match(c) for c in clauses):
        return CallIntent.ACCEPT
    return CallIntent.NONE


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSession:
    """One chat's call.  All timestamps are monotonic seconds, injected.

    Three separate anchors, because three deadlines must not interfere:

    * ``started_at`` — when the phone started ringing; the ring deadline;
    * ``answered_at`` — when it went :data:`CallState.ACTIVE`; the max-duration
      deadline, and it **never moves**, so talking cannot outlast the cap;
    * ``last_remote_at`` — when the *other side* last said something; the idle
      deadline.  Our own speech deliberately does not refresh it, or the bot
      could hold a call open by talking to a wall.
    """

    key: str
    chat_id: str
    is_group: bool
    peer: str
    #: ``True`` when we placed the call (``invite``), ``False`` when they
    #: asked for it (``offer``).  Survives into ``ACTIVE``, where the state
    #: no longer says which way the call went; read by logs and snapshots.
    outgoing: bool
    state: CallState
    started_at: float
    answered_at: Optional[float] = None
    last_remote_at: float = 0.0
    #: One voice bubble from *either* side.  ``voice_call_max_turns`` counts
    #: bubbles, not exchanges.
    turns: int = 0
    ended_at: Optional[float] = None
    end_reason: Optional[EndReason] = None


@dataclass(frozen=True)
class CallDecision:
    """What happened, what to do about it, and why.

    Returned by every transition — never a bare boolean.  A caller that only
    looks at ``action`` and ``line`` is a correct caller.
    """

    ok: bool
    action: CallAction
    state: CallState
    line: str = ""
    refusal: Optional[Refusal] = None
    end_reason: Optional[EndReason] = None
    #: The session this decision is about.  For a terminal decision it is the
    #: ``ENDED`` copy, which the registry no longer holds.
    session: Optional[CallSession] = None


@dataclass(frozen=True)
class Expiry:
    """One session that just crossed a deadline, and what to do about it."""

    key: str
    reason: EndReason
    decision: CallDecision


#: ``speech_key(instance_id, chat_id) -> CallSession``.  Module-level and
#: process-wide, like ``proactive``'s budget: an adapter reconnect or a config
#: save that rebuilds the adapter must not silently drop a live call.  Only
#: live sessions live here — ending one removes it.
_SESSIONS: Dict[str, CallSession] = {}


def reset_state() -> None:
    """Test hook — drop every session.

    ``adapter._reset_module_state`` should call this alongside
    ``proactive.reset_state()``; wiring that up belongs to the subtask that
    owns ``adapter.py``.  Until then, test files reset this module directly.
    """
    _SESSIONS.clear()


def snapshot(key: str) -> Optional[CallSession]:
    """The live session for ``key``, or ``None`` (i.e. :data:`CallState.IDLE`)."""
    return _SESSIONS.get(key)


def active_sessions() -> Tuple[CallSession, ...]:
    """Every live session — ringing, incoming or active — ordered by key."""
    return tuple(_SESSIONS[k] for k in sorted(_SESSIONS))


def _refuse(
    reason: Refusal, state: CallState, session: Optional[CallSession] = None
) -> CallDecision:
    return CallDecision(
        ok=False,
        action=CallAction.NOTHING,
        state=state,
        refusal=reason,
        session=session,
    )


def _end(
    key: str, session: CallSession, reason: EndReason, now: float, line: str
) -> CallDecision:
    """Terminate ``session``, remove it, and describe the sign-off."""
    ended = replace(session, state=CallState.ENDED, ended_at=now, end_reason=reason)
    _SESSIONS.pop(key, None)
    logger.info(
        "OneBot: voice call ended key=%s reason=%s turns=%d secs=%.0f",
        key,
        reason.value,
        ended.turns,
        now
        - (ended.answered_at if ended.answered_at is not None else ended.started_at),
    )
    return CallDecision(
        ok=True,
        action=CallAction.SPEAK if line else CallAction.NOTHING,
        state=CallState.ENDED,
        line=line,
        end_reason=reason,
        session=ended,
    )


def _start(
    key: str,
    *,
    chat_id: str,
    is_group: bool,
    peer: str,
    now: float,
    cfg: Optional[CallConfig],
    outgoing: bool,
) -> CallDecision:
    """Shared gate ladder and session construction for invite/offer.

    The ladder order is the contract and the tests pin it: **disabled →
    groups → allowlist → already live**.  Cheapest and broadest first, and
    the "a session already exists" answer is a refusal, never an exception —
    a live call is a normal thing to run into, not an error.
    """
    if cfg is None:
        return _refuse(Refusal.DISABLED, CallState.IDLE)
    if is_group and not cfg.allow_groups:
        return _refuse(Refusal.GROUPS_DISABLED, CallState.IDLE)
    if not user_allowed(cfg, peer):
        return _refuse(Refusal.NOT_ALLOWED, CallState.IDLE)
    existing = _SESSIONS.get(key)
    if existing is not None:
        return _refuse(Refusal.ALREADY_LIVE, existing.state, existing)

    state = CallState.RINGING if outgoing else CallState.INCOMING
    session = CallSession(
        key=key,
        chat_id=chat_id,
        is_group=is_group,
        peer=str(peer or ""),
        outgoing=outgoing,
        state=state,
        started_at=now,
        last_remote_at=now,
    )
    _SESSIONS[key] = session
    logger.info(
        "OneBot: voice call %s key=%s chat=%s group=%s peer=%s",
        "invited" if outgoing else "requested",
        key,
        chat_id,
        is_group,
        peer or "-",
    )
    return CallDecision(
        ok=True,
        # Placing a call rings; being asked for one does not — we answer that
        # with an explicit accept/decline, and speaking twice would be noise.
        action=CallAction.RING if outgoing else CallAction.NOTHING,
        state=state,
        line=RING_LINE if outgoing else "",
        session=session,
    )


def invite(
    key: str,
    *,
    chat_id: str,
    is_group: bool,
    peer: str,
    now: float,
    cfg: Optional[CallConfig],
) -> CallDecision:
    """We place a call: :data:`CallState.RINGING`, awaiting their answer.

    ``peer`` is the QQ uin of the person we are calling — the id the
    ``voice_call_allowed_users`` filter is evaluated against.  In a group
    there may be no single peer; pass the member who prompted the call, or
    leave the allowlist empty (a non-empty allowlist plus an unknown peer is
    a refusal, by design).
    """
    return _start(
        key,
        chat_id=chat_id,
        is_group=is_group,
        peer=peer,
        now=now,
        cfg=cfg,
        outgoing=True,
    )


def offer(
    key: str,
    *,
    chat_id: str,
    is_group: bool,
    peer: str,
    now: float,
    cfg: Optional[CallConfig],
) -> CallDecision:
    """They ask to talk: :data:`CallState.INCOMING`, awaiting *our* answer.

    Nothing goes on the wire yet — the answer is an explicit :func:`accept`
    or :func:`decline`, and that is the bubble they hear.
    """
    return _start(
        key,
        chat_id=chat_id,
        is_group=is_group,
        peer=peer,
        now=now,
        cfg=cfg,
        outgoing=False,
    )


def accept(key: str, now: float) -> CallDecision:
    """Answer a ringing or incoming call: → :data:`CallState.ACTIVE`.

    One function for both directions, because the state machine's answer is
    the same; only the line differs.  ``answered_at`` is stamped here and
    never moves again — it is the max-duration anchor.
    """
    session = _SESSIONS.get(key)
    if session is None:
        return _refuse(Refusal.NO_SESSION, CallState.IDLE)
    if session.state not in (CallState.RINGING, CallState.INCOMING):
        return _refuse(Refusal.WRONG_STATE, session.state, session)

    line = ACCEPT_OUTGOING_LINE if session.outgoing else ACCEPT_INCOMING_LINE
    active = replace(
        session, state=CallState.ACTIVE, answered_at=now, last_remote_at=now
    )
    _SESSIONS[key] = active
    logger.info(
        "OneBot: voice call connected key=%s outgoing=%s", key, session.outgoing
    )
    return CallDecision(
        ok=True,
        action=CallAction.SPEAK,
        state=CallState.ACTIVE,
        line=line,
        session=active,
    )


def decline(key: str, now: float) -> CallDecision:
    """Turn a ringing or incoming call down: → ``ENDED(declined)``.

    Which side said no is read off ``outgoing``: they turned *our* call down,
    or we turned *their* request down.  Two different lines, one transition.
    """
    session = _SESSIONS.get(key)
    if session is None:
        return _refuse(Refusal.NO_SESSION, CallState.IDLE)
    if session.state not in (CallState.RINGING, CallState.INCOMING):
        return _refuse(Refusal.WRONG_STATE, session.state, session)
    line = DECLINE_REMOTE_LINE if session.outgoing else DECLINE_LOCAL_LINE
    return _end(key, session, EndReason.DECLINED, now, line)


def hangup(key: str, reason: EndReason, now: float) -> CallDecision:
    """End a session for ``reason``, from any live state.

    Legal while still ringing: cancelling an unanswered call is a hangup too.
    """
    session = _SESSIONS.get(key)
    if session is None:
        return _refuse(Refusal.NO_SESSION, CallState.IDLE)
    return _end(key, session, reason, now, _END_LINES.get(reason, ""))


def note_remote_turn(key: str, text: str, now: float) -> CallDecision:
    """They said something while the call is up.

    Refreshes the idle deadline and counts a turn.  Blank text (a sticker, a
    recall, a media-only bubble) still refreshes the deadline — they are
    plainly still there — but is not a turn, because there was nothing said
    to answer.  ``proactive._visible_buffer`` drops blanks for the mirror
    image of this reason.
    """
    session = _SESSIONS.get(key)
    if session is None:
        return _refuse(Refusal.NO_SESSION, CallState.IDLE)
    if session.state is not CallState.ACTIVE:
        return _refuse(Refusal.WRONG_STATE, session.state, session)
    spoke = bool((text or "").strip())
    updated = replace(
        session,
        last_remote_at=now,
        turns=session.turns + (1 if spoke else 0),
    )
    _SESSIONS[key] = updated
    return CallDecision(
        ok=True, action=CallAction.NOTHING, state=CallState.ACTIVE, session=updated
    )


def note_local_turn(key: str, text: str, now: float) -> CallDecision:
    """We said something while the call is up.

    Counts a turn and nothing else.  It deliberately does **not** touch
    ``last_remote_at``: the idle timer measures *their* silence, and a bot
    that refreshed it by speaking would keep a dead call alive forever.

    ``now`` is unused today and stays in the signature on purpose — every
    transition in this module takes the clock the same way, and a caller that
    has to remember which ones don't is a caller that will pass the wrong
    thing to the ones that do.
    """
    session = _SESSIONS.get(key)
    if session is None:
        return _refuse(Refusal.NO_SESSION, CallState.IDLE)
    if session.state is not CallState.ACTIVE:
        return _refuse(Refusal.WRONG_STATE, session.state, session)
    if not (text or "").strip():
        return _refuse(Refusal.NO_CONTENT, CallState.ACTIVE, session)
    updated = replace(session, turns=session.turns + 1)
    _SESSIONS[key] = updated
    return CallDecision(
        ok=True, action=CallAction.NOTHING, state=CallState.ACTIVE, session=updated
    )


def on_inbound(key: str, text: str, now: float) -> CallDecision:
    """Route one inbound message through the call, if there is one.

    The single call the adapter makes on every inbound message. The state
    decides what a word means, and that mapping is the reason this lives here
    rather than in the wiring:

    * **RINGING** — we are waiting for their answer, so this *is* the answer:
      accept, or decline (a hangup word to an unanswered ring is a decline).
    * **INCOMING** — *we* owe the answer, so their words are not one.  Accept
      vocabulary means nothing here; decline/hangup vocabulary is them
      withdrawing the request, which ends it.
    * **ACTIVE** — hangup vocabulary ends the call as ``hangup_remote``;
      everything else is simply the conversation, and counts as their turn.

    A :data:`Refusal.NO_SESSION` or :data:`Refusal.UNCLEAR` answer means "not
    mine" — let the message fall through to normal handling.
    """
    session = _SESSIONS.get(key)
    if session is None:
        return _refuse(Refusal.NO_SESSION, CallState.IDLE)
    intent = classify_intent(text)

    if session.state is CallState.RINGING:
        if intent is CallIntent.ACCEPT:
            return accept(key, now)
        if intent in (CallIntent.DECLINE, CallIntent.HANGUP):
            return decline(key, now)
        return _refuse(Refusal.UNCLEAR, session.state, session)

    if session.state is CallState.INCOMING:
        if intent in (CallIntent.DECLINE, CallIntent.HANGUP):
            return decline(key, now)
        return _refuse(Refusal.UNCLEAR, session.state, session)

    if intent is CallIntent.HANGUP:
        return hangup(key, EndReason.HANGUP_REMOTE, now)
    return note_remote_turn(key, text, now)


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


def expiry_reason(
    session: CallSession, now: float, cfg: Optional[CallConfig]
) -> Optional[EndReason]:
    """Which deadline ``session`` has crossed at ``now``, if any.

    Pure, and separated from :func:`sweep` so the deadline arithmetic can be
    tested without touching the registry.  A deadline fires at ``now >=
    deadline``: one tick before is alive, one tick past is dead.

    When several have crossed at once the order is
    ``MAX_DURATION → MAX_TURNS → IDLE_TIMEOUT`` — the operator's hard walls
    are reported ahead of the soft one, so a log says "you set a 15-minute
    cap" rather than "they went quiet". The order is the contract; the tests
    pin it.
    """
    if cfg is None:
        # The feature was switched off underneath a live call.  Collect it
        # rather than leaving it for a sweeper that no longer runs.
        return EndReason.DISABLED
    if session.state in (CallState.RINGING, CallState.INCOMING):
        if now >= session.started_at + cfg.ring_timeout_secs:
            return EndReason.RING_TIMEOUT
        return None
    if session.state is CallState.ACTIVE:
        answered = (
            session.answered_at
            if session.answered_at is not None
            else session.started_at
        )
        if now >= answered + cfg.max_duration_secs:
            return EndReason.MAX_DURATION
        if session.turns >= cfg.max_turns:
            return EndReason.MAX_TURNS
        if now >= session.last_remote_at + cfg.idle_timeout_secs:
            return EndReason.IDLE_TIMEOUT
    return None


def sweep(
    now: float, cfg: Optional[CallConfig], *, instance_id: Optional[str] = None
) -> List[Expiry]:
    """End every session that has crossed a deadline; return what to do.

    This is what the future resident loop calls on a timer, and what the
    wiring should also call right after each turn so a cap does not wait out
    the tick. With no sessions it is a no-op.

    ``instance_id`` narrows the sweep to one adapter's sessions. Keys are
    built by :func:`speech_key` as ``f"{instance_id}:{chat_id}"``, so the
    prefix is exact. It matters as soon as two OneBot instances are
    configured: each owns a sweeper, each resolves its own ``voice_call_*``
    config, and an unfiltered sweep would judge the other instance's calls by
    the wrong timeouts.
    """
    prefix = None if instance_id is None else f"{instance_id}:"
    expired: List[Expiry] = []
    for key in sorted(_SESSIONS):
        if prefix is not None and not key.startswith(prefix):
            continue
        session = _SESSIONS[key]
        reason = expiry_reason(session, now, cfg)
        if reason is None:
            continue
        decision = _end(key, session, reason, now, _END_LINES.get(reason, ""))
        expired.append(Expiry(key=key, reason=reason, decision=decision))
    return expired


# ---------------------------------------------------------------------------
# The one place a decision meets the wire
# ---------------------------------------------------------------------------

#: Speak one line as a voice bubble.  The adapter-wiring subtask injects the
#: OneBot voice sender here; this module never imports it, which is what
#: keeps the state machine transport-free and the tests offline.
SpeakFn = Callable[[str], Awaitable[Any]]

#: Send a 戳一戳 nudge — the "phone is ringing" gesture.
PokeFn = Callable[[], Awaitable[Any]]


async def perform(
    decision: CallDecision,
    *,
    speak: SpeakFn,
    poke: Optional[PokeFn] = None,
) -> bool:
    """Carry out ``decision`` through injected callables. ``True`` if we spoke.

    Poke first, then speak: the phone rings before the voice arrives. A poke
    failure is logged and swallowed — not every backend implements
    ``send_poke``, and a missing nudge must not swallow the words that follow
    it. A speak failure propagates, because a call whose voice does not go
    out is a call the caller has to end.
    """
    if decision.action is CallAction.NOTHING or not decision.line:
        return False
    if decision.action is CallAction.RING and poke is not None:
        try:
            await poke()
        except Exception as exc:  # noqa: BLE001 — the nudge is decoration
            logger.debug("OneBot: voice call poke failed: %s", exc)
    await speak(decision.line)
    return True
