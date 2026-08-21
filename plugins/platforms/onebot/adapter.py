"""QQ platform adapter speaking OneBot v11 (NapCat / Lagrange / go-cqhttp).

**This is not the same thing as ``gateway/platforms/qqbot/``.**  That adapter
speaks Tencent's *official* QQ Bot API v2 (``api.sgroup.qq.com``, appid +
secret).  This one speaks the community OneBot v11 protocol against a local
NapCat instance driving a normal QQ account.  Two different protocols, two
different platform names — ``qqbot`` vs ``onebot`` — on purpose.

Topology: Hermes dials OUT to a forward WebSocket the backend already
exposes (``ws://127.0.0.1:3001`` by default).  Nothing binds a port, and the
adapter never writes the backend's configuration — on a shared box another
service may own that config, and two writers fighting over it can drop the
live QQ service.  Read the ws URL and token, consume, leave it alone.

Inbound gating lives in :mod:`.router`; the wire vocabulary in
:mod:`.protocol`; the socket, reconnect ladder and ``echo`` correlation in
:mod:`.client`.  What is left here is the Hermes contract: the four abstract
methods, the capability class attributes, and the translation between the
OneBot vocabulary and Hermes' ``MessageEvent`` / ``SendResult``.

Chat-id format (what ``deliver=onebot:<id>`` and ``send_message`` take):

* ``g<group_id>`` — a QQ group, e.g. ``g183287894``
* ``<user_id>``   — a QQ user (DM), e.g. ``2104743984``

The ``g`` prefix (rather than ``group:<id>``) is load-bearing:
``DeliveryTarget.parse`` splits delivery strings on ``:``, so a colon inside
the chat id would be parsed as a thread id and the message would go to the
wrong place — silently, because that parser degrades to LOCAL instead of
raising.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SEND_ERROR_KINDS,
    cache_audio_from_url,
    cache_image_from_url,
    classify_send_error,
    validate_media_delivery_path,
)
from gateway.platforms.helpers import MessageDeduplicator, strip_markdown

# The Tencent-facing content policy, shared with the QZone tools.  It lives in
# ``plugins/qzone`` because that is where it landed when it was ported, but it
# is transport-neutral (pure stdlib, no QZone imports) and the QQ *account* is
# the thing it protects — the same account both plugins speak as.  Importing it
# here is cheap: ``plugins.qzone.__init__`` does all its real work inside
# ``register()``, so this pulls in nothing but the rule table.
from plugins.qzone import policy as content_policy

from . import persona_binding, protocol, sticker
from .client import (
    OneBotClient,
    OneBotConfig,
    OneBotConfigError,
    OneBotTransportError,
    set_live_client,
)
from .rate_limit import SlidingWindowCounter, TokenBucket
from .router import ChannelRouter, parse_group_keywords, looks_like_command

try:  # optional: scope-aware secret reads (multi-profile gateways)
    from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
    from agent.secret_scope import get_secret as _scoped_get_secret
except Exception:  # pragma: no cover - agent package always ships with hermes
    _UnscopedSecretError = RuntimeError  # type: ignore[assignment]
    _scoped_get_secret = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLATFORM_NAME = "onebot"
PLATFORM_LABEL = "QQ (OneBot v11 / NapCat)"

#: NapCat's practical per-message ceiling is ~4500-5000 characters; keep a
#: safety margin so we never discover the real limit in production.
MAX_MESSAGE_LENGTH = 3800

#: Above this, a reply stops being chat and becomes a considered answer — the
#: WHOLE reply folds into one merged-forward ("聊天记录") card the reader taps
#: to expand, instead of being split into bubbles and capped.  Measured on the
#: entire finished body, not per bubble: it is the reply's character that
#: decides its shape, and that is not a property of any one fragment.
FORWARD_TEXT_THRESHOLD = 1000

#: A forward card cannot carry an @mention, so in a group we post this line
#: (with the @) first — otherwise the person who asked never gets a ping.
FORWARD_LEAD_TEXT = "回复较长，已折叠成聊天记录，点开查看 ↓"


@dataclass(frozen=True)
class _ForwardDeliveryResult:
    """Outcome of a card attempt, including any separately delivered lead."""

    success: bool
    failure: Optional[SendResult]
    lead_sent: bool


#: Persona-style bubble separator: one logical reply, several chat bubbles.
BUBBLE_SEPARATOR = "[MSG_BREAK]"
BUBBLE_GAP_SECS = 0.3

#: How many separate chat bubbles one reply may occupy.  Several short bubbles
#: read as a person typing; eight in a row read as a bot flooding the window,
#: and QQ users experience that as spam regardless of what the text says.  The
#: default matches the persona's natural daily-chat range (one to three
#: bubbles). The model does not reliably obey that, so the transport enforces
#: it. ``<= 0`` disables the cap (escape hatch).
DEFAULT_MAX_BUBBLES_PER_REPLY = 3

#: Inline attachments are shipped as ``base64://`` because the backend
#: usually runs in a different container and cannot read our filesystem.
#: The ceiling is 8 MiB rather than the 30 MiB the source implementations
#: used: base64 inflates ~4/3, the frame is buffered in RAM on both sides,
#: and the target host has well under 200 MB of headroom.  Larger files fall
#: back to a literal path, which works only when the backend shares our
#: filesystem — that is the honest failure mode, and it is logged.
BASE64_MAX_BYTES = 8 * 1024 * 1024

#: ``upload_*_file`` only answers once the file has reached Tencent, which
#: legitimately takes a while.
UPLOAD_RESPONSE_TIMEOUT = 120.0

#: Health watcher: how often to look, and how long without a frame before we
#: call the link lost.  A healthy backend heartbeats every ~30 s.
HEALTH_PROBE_SECS = 30.0
HEALTH_LOST_SECS = 120.0

#: Default per-adapter turn concurrency.  The upstream implementation used 8;
#: 2 matches the 2 vCPU / ~190 MB-free box this was ported for.  Raise with
#: ``ONEBOT_MAX_CONCURRENCY`` on bigger hardware.
DEFAULT_MAX_CONCURRENCY = 2

#: Audio containers ``mimetypes`` does not know but QQ voice messages use.
_AUDIO_EXTS = {
    ".mp3",
    ".wav",
    ".ogg",
    ".amr",
    ".silk",
    ".m4a",
    ".flac",
    ".aac",
    ".opus",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".3gp"}


# ---------------------------------------------------------------------------
# Process-wide state shared with anything else that speaks for this bot
# ---------------------------------------------------------------------------
#
# The group speech cap is a promise to the humans in a group ("at most N
# messages per M minutes"), and it has to hold across BOTH reactive replies
# and any proactive speaking added later, and across an adapter restart —
# otherwise a reconnect (or a config save) silently hands the bot a fresh
# budget.  Module level is the cheapest place that satisfies both; it is lost
# on process restart, which is acceptable and documented.

_GROUP_SPEECH = SlidingWindowCounter()

#: Recent group chatter, including the bot's own posts, read by
#: :mod:`.proactive`.  Fed BEFORE the reply gate: a persona should see the
#: whole room, not only the messages it happened to answer.
_GROUP_RECENT: Dict[str, Deque[Tuple[float, str, str, bool]]] = {}
_GROUP_RECENT_MAX = 30

#: Per-message char cap in that buffer.  It is prompt input, so a single
#: pasted wall of text must not be able to dominate a proactive turn.
_GROUP_RECENT_TEXT_CHARS = 200


def speech_key(instance_id: str, group_id: Any) -> str:
    """Key for the shared per-group speech budget."""
    return f"{instance_id}:{group_id}"


def group_speech_allowed(
    instance_id: str,
    group_id: Any,
    window_secs: float,
    max_messages: int,
    *,
    record: bool = True,
) -> bool:
    """Check (and by default consume) one unit of a group's speech budget.

    Exposed at module scope so a later proactive-speaking job shares exactly
    this budget instead of inventing a second one.
    """
    return _GROUP_SPEECH.allow(
        speech_key(instance_id, group_id), window_secs, max_messages, record=record
    )


def record_group_message(
    instance_id: str, group_id: Any, sender: str, text: str, is_self: bool
) -> None:
    """Append to the bounded per-group context buffer.

    Blank entries (stickers, recalls, media-only posts) are dropped rather
    than stored: they are noise in a rendered transcript, and a blank *inbound*
    entry would read as "a human just spoke" to the proactive loop's
    anti-spam check.  Text is capped because this buffer is prompt input.
    """
    text = (text or "").strip()
    if not text:
        return
    text = text[:_GROUP_RECENT_TEXT_CHARS]
    key = speech_key(instance_id, group_id)
    buf = _GROUP_RECENT.get(key)
    if buf is None:
        buf = deque(maxlen=_GROUP_RECENT_MAX)
        _GROUP_RECENT[key] = buf
    buf.append((time.time(), sender, text, is_self))


def recent_group_messages(
    instance_id: str, group_id: Any
) -> List[Tuple[float, str, str, bool]]:
    """Snapshot of the per-group context buffer (oldest first)."""
    return list(_GROUP_RECENT.get(speech_key(instance_id, group_id), ()))


def _gh_live_extra(adapter: Any) -> Dict[str, Any]:
    """The adapter's *live* settings mapping, for group-history resolution.

    Same precedence rule as :func:`plugins.platforms.onebot.proactive.live_extra`
    — ``config.extra`` (what an in-place config reconcile mutates) before
    ``_extra`` (the copy taken at construction).  Duplicated here rather than
    imported because ``.proactive`` imports this module.
    """
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    if isinstance(extra, dict):
        return extra
    fallback = getattr(adapter, "_extra", None)
    return fallback if isinstance(fallback, dict) else {}


# ---------------------------------------------------------------------------
# The emergency mute (D44)
# ---------------------------------------------------------------------------
#
# ``group_replies_enabled`` reads, to documentation and to operator intuition,
# as the master switch for group speech — in the source system it silenced ALL
# group output, monitor digests included, which is precisely why corlinman's
# ``qunjlu`` digest never reached a group in seven days of production.  In this
# port it was only ever consumed on the INBOUND side (the router's reply gate)
# and by the proactive loop, so anything that reached ``send()`` from the other
# direction — a cron job delivering to ``onebot:g<id>``, a model calling
# ``send_message``, a media upload — went out while the operator believed the
# bot was muted.  That is the failure that matters: the switch is pressed in an
# incident, and the incident keeps talking.  See 00-PLAN.md §19.
#
# The gate below closes it on the outbound side.  It is deliberately NOT a
# replacement for D2's structural suppression of the QQ monitors (``deliver=
# "local"`` + an empty toolset): structural suppression does not depend on any
# runtime config being read correctly, which makes it the stronger guarantee.
# The two stack (D45).

#: Marker key set on ``SendResult.raw_response`` (and on the standalone-send
#: result dict) when a send was refused by the mute.  A caller that needs to
#: tell "muted" from "the send failed" branches on this, not on the message.
MUTED_MARKER = "onebot_group_muted"

#: Stable machine-readable reason string carried alongside the marker.
MUTED_REASON = "group_replies_enabled=false"

#: Marker key set on ``SendResult.raw_response`` when a send was refused by the
#: Tencent content policy.  Same role as :data:`MUTED_MARKER` and deliberately
#: the same shape, so a caller can tell "refused by a local policy" from "the
#: send failed" without parsing the message text.
POLICY_MARKER = "onebot_policy_blocked"

#: Stable machine-readable reason string carried alongside the policy marker.
POLICY_REASON = "tencent_content_policy"


def group_speech_muted(adapter: Any) -> bool:
    """``True`` when the emergency mute is silencing group speech.

    Two sources have to agree and either one being off means muted: the live
    config value (so flipping the mute hot-applies without a restart) and the
    router flag the *reactive* path actually obeys (so the two lanes can never
    end up in a state where replies are muted but the bot still talks).  This
    is B4's deliberately stricter form, kept as-is and now shared with the
    outbound path so all three lanes read one function.
    """
    router_flag = bool(
        getattr(getattr(adapter, "router", None), "group_replies_enabled", False)
    )
    extra = _gh_live_extra(adapter)
    live_flag = (
        _as_bool(extra.get("group_replies_enabled"), router_flag)
        if "group_replies_enabled" in extra
        else router_flag
    )
    return not (router_flag and live_flag)


def muted_send_result(target: Any) -> SendResult:
    """The ``SendResult`` a muted group send returns.  Logs once, here.

    Why ``error_kind`` is neither ``forbidden`` nor ``not_found``, which are
    the two that *describe* this best: both are in
    ``gateway.dead_targets._DEAD_ERROR_KINDS``, so returning either would let
    the delivery layer record the group as permanently unreachable.  A mute is
    an operator toggle; marking the target dead would outlive un-muting and
    turn a reversible switch into a sticky one.  ``unknown`` is the honest
    remaining value — this is a local policy refusal, not a platform error,
    and the shared vocabulary has no word for that.  ``retryable`` is False
    and the message text is deliberately free of every substring
    ``classify_send_error`` matches on, so no retry ladder and no dead-target
    classifier can reinterpret it.
    """
    logger.warning(
        "OneBot: group speech is muted (%s) — refusing to post to group %s",
        MUTED_REASON,
        target,
    )
    return SendResult(
        success=False,
        error=(
            "OneBot: group speech is muted "
            f"({MUTED_REASON}); nothing was sent to group {target}"
        ),
        error_kind="unknown",
        retryable=False,
        raw_response={MUTED_MARKER: True, "reason": MUTED_REASON, "group_id": target},
    )


def is_muted_send_result(result: Any) -> bool:
    """Whether *result* is a refusal by the mute rather than a send failure.

    Accepts both a :class:`SendResult` and the plain dict shape
    :func:`_standalone_send` returns, so out-of-process callers can ask the
    same question.
    """
    if isinstance(result, dict):
        return bool(result.get(MUTED_MARKER))
    raw = getattr(result, "raw_response", None)
    return bool(isinstance(raw, dict) and raw.get(MUTED_MARKER))


def policy_blocked_send_result(
    target: Any, is_group: bool, decision: Any
) -> SendResult:
    """The ``SendResult`` a content-policy refusal returns.  Logs once, here.

    Deliberately the same shape as :func:`muted_send_result`, for the same
    reasons spelled out there, which apply verbatim:

    * ``error_kind`` is ``"unknown"``, **not** ``forbidden``/``not_found``.
      Those two are the members of ``gateway.dead_targets._DEAD_ERROR_KINDS``,
      so either one would let the delivery layer record this chat as
      permanently unreachable — turning a single refused *message* into a
      sticky dead *target* that outlives the sentence that tripped it.  A
      policy refusal is a local decision about one body of text, not a
      statement about whether the peer is reachable.
    * ``retryable`` is False: re-sending the same text produces the same
      verdict, deterministically (the classifier is a fixed rule table).
    * the message text avoids every substring ``classify_send_error`` matches
      on, so neither the retry ladder nor the dead-target classifier can
      re-read this refusal as a platform failure.  ``tests/gateway/
      test_onebot_content_gate.py`` asserts that by calling the real
      classifier rather than trusting the wording.

    Only the three audit fields from :func:`policy.policy_error_payload` go
    into ``raw_response`` — category codes, rule ids, ruleset version.  None of
    them contains the offending text, so this is safe to log, matching how
    ``plugins/qzone`` surfaces the same refusal and corlinman's
    ``_log_tencent_block`` audit record.
    """
    payload = content_policy.policy_error_payload(decision)
    where = f"group {target}" if is_group else f"user {target}"
    logger.warning(
        "OneBot: Tencent content policy refused an outbound message to %s "
        "(categories=%s rules=%s ruleset=%s)",
        where,
        payload.get("category_codes"),
        payload.get("rule_ids"),
        payload.get("ruleset_version"),
    )
    return SendResult(
        success=False,
        error=(
            "OneBot: Tencent content policy refused this message "
            f"({POLICY_REASON}); nothing was sent to {where}"
        ),
        error_kind="unknown",
        retryable=False,
        raw_response={
            POLICY_MARKER: True,
            "reason": POLICY_REASON,
            ("group_id" if is_group else "user_id"): target,
            **payload,
        },
    )


def is_policy_blocked_send_result(result: Any) -> bool:
    """Whether *result* is a content-policy refusal rather than a send failure.

    Mirrors :func:`is_muted_send_result`, including accepting the plain dict
    shape, so callers can ask the same question of either lane.
    """
    if isinstance(result, dict):
        return bool(result.get(POLICY_MARKER))
    raw = getattr(result, "raw_response", None)
    return bool(isinstance(raw, dict) and raw.get(POLICY_MARKER))


# ---------------------------------------------------------------------------
# Hiding hermes' own system messages on QQ
# ---------------------------------------------------------------------------
#
# QQ is a persona channel.  Between two lines of a conversation the reader
# suddenly gets ``💾 Self-improvement review: User profile updated`` — hermes
# talking about its own bookkeeping, in a chat window where everything else is
# a person speaking.  There is no thread to hide it in and no collapsed
# "system" lane the way Discord has; it lands as a chat bubble like any other,
# and it reads as the mask slipping.
#
# The gateway already knows which of its sends are *not* conversation: every
# lifecycle / status emitter routes its metadata through
# ``gateway/run.py::_non_conversational_metadata``, which stamps
# ``non_conversational: True``.  That marker was Discord-only (Discord uses it
# to keep status bumps out of the conversation history it replays to the
# model); it now also covers this platform, so the classification is made once,
# at the emitter, by the code that knows what it is sending — not guessed here
# from the text.
#
# That distinction is the whole point.  Matching on ``💾``/``⚠️`` prefixes was
# the obvious alternative and it is the wrong one: the persona writes emoji,
# so a text-shaped filter eventually eats a real reply.  Reading the emitter's
# own flag cannot: a persona reply is never marked.
#
# Two marked categories are still delivered — see
# :data:`_ALWAYS_DELIVER_PATTERNS`.

#: Metadata key the gateway stamps on lifecycle/status sends.
NON_CONVERSATIONAL_METADATA_KEY = "non_conversational"

#: Marker key set on ``SendResult.raw_response`` when a send was dropped as a
#: hermes system message.  Same shape and same role as :data:`MUTED_MARKER`
#: and :data:`POLICY_MARKER`: a caller tells "suppressed by local policy" from
#: "the send failed" by branching on this, never by parsing the message.
SUPPRESSED_MARKER = "onebot_system_message_suppressed"

#: Stable machine-readable reason string carried alongside the marker.
SUPPRESSED_REASON = "suppress_system_messages=true"

#: The carve-outs: marked messages that are delivered anyway.
#:
#: These regexes are safe in a way a general text filter is not, because they
#: are only ever consulted for a message the *gateway* already flagged as
#: non-conversational.  A persona reply never carries that flag, so nothing
#: the persona writes can reach this table at all — the emoji-prefix problem
#: that rules out matching on text alone does not apply here.
#:
#: 1. The update prompt.  ``_watch_update_progress`` asks a question and then
#:    *blocks* on the answer.  This adapter implements no ``send_update_prompt``,
#:    so the button lane never fires here and this text fallback is the only
#:    form the question takes on QQ.  Swallowing it strands ``hermes update``
#:    forever, waiting for a reply to a question nobody was shown.
#: 2. The session-database alarm.  ``messages may not be persisted`` is data
#:    loss in progress; hiding it means the operator finds out by noticing
#:    history is gone.
#:
#: Approval requests ("⚠️ Dangerous command requires approval") need no entry:
#: ``_approval_notify_sync`` passes its thread metadata straight through and
#: never routes it via ``_non_conversational_metadata``, so an approval is not
#: marked in the first place and this gate never sees it.  That is checked by
#: a test rather than trusted.
_ALWAYS_DELIVER_PATTERNS = (
    re.compile(r"^\s*⚕\s*\*{0,2}\s*Update needs your input", re.IGNORECASE),
    re.compile(r"^\s*⚠️?\s*\*{0,2}\s*Session database\b", re.IGNORECASE),
)


def system_messages_suppressed(adapter: Any) -> bool:
    """``True`` when hermes' own status messages must not reach QQ.

    Reads the *live* settings mapping first (what an in-place config reconcile
    mutates) so flipping the switch hot-applies without a gateway restart,
    exactly like :func:`group_speech_muted`, and falls back to the value
    resolved at construction.
    """
    extra = _gh_live_extra(adapter)
    if "suppress_system_messages" in extra:
        return _as_bool(
            extra.get("suppress_system_messages"),
            bool(getattr(adapter, "suppress_system_messages", True)),
        )
    return bool(getattr(adapter, "suppress_system_messages", True))


def is_suppressible_system_message(metadata: Any, content: Any) -> bool:
    """Whether this send is a hermes system message QQ should not see."""
    if not isinstance(metadata, dict):
        return False
    if not metadata.get(NON_CONVERSATIONAL_METADATA_KEY):
        return False
    text = str(content or "")
    return not any(rx.search(text) for rx in _ALWAYS_DELIVER_PATTERNS)


def suppressed_send_result(chat_id: Any, content: Any) -> SendResult:
    """The ``SendResult`` a suppressed system message returns.  Logs once, here.

    Dropped is not silent: the line below is the record that a message existed
    and did not go out, at ``info`` because it is a routine, expected
    consequence of an operator setting rather than a fault.  It intentionally
    does not log any of the suppressed body: lifecycle/status text may include
    user or task data and the whole reason this message does not reach QQ is
    that it is internal.

    ``error_kind`` is ``"unknown"`` for the reason spelled out at length in
    :func:`muted_send_result`, which applies verbatim: ``forbidden`` and
    ``not_found`` are the two members of ``gateway.dead_targets._DEAD_ERROR_KINDS``,
    so either would let the delivery layer record this chat as permanently
    unreachable — turning a display preference into a dead target that
    outlives it.  ``retryable`` is False (retrying reaches the same verdict),
    and the wording avoids every substring ``classify_send_error`` matches on.
    """
    logger.info(
        "OneBot: hiding a hermes system message from %s (%s)",
        chat_id,
        SUPPRESSED_REASON,
    )
    return SendResult(
        success=False,
        error=(
            "OneBot: hermes system messages are hidden on this platform "
            f"({SUPPRESSED_REASON}); nothing was sent to {chat_id}"
        ),
        error_kind="unknown",
        retryable=False,
        raw_response={
            SUPPRESSED_MARKER: True,
            "reason": SUPPRESSED_REASON,
            "chat_id": str(chat_id),
        },
    )


def is_suppressed_send_result(result: Any) -> bool:
    """Whether *result* is a suppressed system message rather than a failure.

    Mirrors :func:`is_muted_send_result` and
    :func:`is_policy_blocked_send_result`, including accepting the plain dict
    shape, so callers can ask the same question of any of the three.
    """
    if isinstance(result, dict):
        return bool(result.get(SUPPRESSED_MARKER))
    raw = getattr(result, "raw_response", None)
    return bool(isinstance(raw, dict) and raw.get(SUPPRESSED_MARKER))


def _reset_module_state() -> None:
    """Test hook — clear every process-wide buffer, this module's and the
    proactive loop's daily budget, so state cannot leak between tests."""
    _GROUP_SPEECH._events.clear()  # noqa: SLF001 — test-only reach-in
    _GROUP_RECENT.clear()
    from . import proactive as _proactive  # local: .proactive imports this module

    _proactive.reset_state()
    persona_binding.reset_state()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Scope-aware credential read with the default-profile fallback.

    Secondary profiles construct adapters inside a profile secret scope, and
    a scoped miss must NOT borrow another profile's value out of the
    environment.  The default profile constructs unscoped, where a bare
    scoped read raises — there ``os.environ`` is that profile's own value.
    """
    if _scoped_get_secret is None:  # pragma: no cover - defensive
        return os.getenv(name, default)
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def _as_bool(value: Any, default: bool = False) -> bool:
    """Parse a config/env truthy value.  Unset / blank keeps *default*."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_id_list(raw: Any) -> List[int]:
    """Parse a comma-separated / list-shaped set of numeric ids."""
    if raw is None:
        return []
    items: Sequence[Any]
    if isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        items = [p for p in str(raw).replace(";", ",").split(",")]
    out: List[int] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            out.append(int(text))
        except ValueError:
            logger.warning("OneBot: ignoring non-numeric id %r", text)
    return out


def parse_whitelist(raw: Any) -> Optional[frozenset]:
    """Parse a group whitelist, preserving the unset/empty distinction.

    ``None`` (key absent) means *no whitelist* — every group passes the gate.
    An empty string or empty list means *an empty whitelist* — no group is
    ever answered.  Collapsing the two would silently open every group the
    bot is in, so the distinction is deliberate and tested.
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(str(v).strip() for v in raw if str(v).strip())
    return frozenset(
        p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip()
    )


def parse_chat_id(chat_id: Any) -> Tuple[bool, int]:
    """``"g123"`` → ``(True, 123)``; ``"456"`` → ``(False, 456)``.

    Also accepts the more verbose ``group:123`` / ``private:456`` /
    ``user:456`` spellings a human might type by hand.  Raises
    :class:`ValueError` for anything else — a mistyped target must fail
    loudly here rather than deliver to the wrong QQ number.
    """
    text = str(chat_id or "").strip()
    if not text:
        raise ValueError("empty chat_id")
    lowered = text.lower()
    for prefix in ("group:", "group/", "g:"):
        if lowered.startswith(prefix):
            return True, int(text[len(prefix) :].strip())
    for prefix in ("private:", "user:", "dm:", "p:", "u:"):
        if lowered.startswith(prefix):
            return False, int(text[len(prefix) :].strip())
    if lowered[0] == "g" and lowered[1:].isdigit():
        return True, int(text[1:])
    if text.isdigit():
        return False, int(text)
    raise ValueError(f"unrecognized OneBot chat id: {chat_id!r}")


def format_chat_id(is_group: bool, target: Any) -> str:
    """Canonical chat id for a group / user."""
    return f"g{target}" if is_group else str(target)


def split_bubbles(content: str) -> List[str]:
    """Split a reply on the persona bubble marker, dropping empty bubbles."""
    if BUBBLE_SEPARATOR not in content:
        stripped = content.strip()
        return [stripped] if stripped else []
    return [part.strip() for part in content.split(BUBBLE_SEPARATOR) if part.strip()]


def cap_bubbles(bubbles: List[str], limit: int) -> List[str]:
    """Clamp a bubble list to *limit* entries by MERGING the overflow.

    The overflow is folded into the last surviving bubble, joined with a
    newline — never dropped.  A reader who receives half a thought is worse
    off than one who receives a slightly longer message: truncation loses
    content the sender believes was delivered, and the bot has no way to know
    it was cut.  So the cap is about how many *notifications* a reply fires,
    not about how much text it carries.

    ``limit <= 0`` disables the cap.  Applied to the ``split_bubbles`` result
    and BEFORE ``chunk_text``: the merged bubble is still subject to the
    per-message length ceiling, which is a protocol constraint this cap has no
    business overriding.

    Only the degraded chat fallback reaches this.  With cards enabled, a reply
    that crosses either ``forward_threshold`` or the bubble limit is delivered
    as one merged-forward card, so ``send`` preserves each bubble as a node
    instead of merging the overflow.
    """
    if limit <= 0 or len(bubbles) <= limit:
        return list(bubbles)
    head = list(bubbles[: limit - 1])
    tail = [b for b in bubbles[limit - 1 :] if b]
    head.append("\n".join(tail))
    return head


def chunk_text(
    body: str, limit: int = MAX_MESSAGE_LENGTH, *, prefix_overhead: int = 16
) -> List[str]:
    """Split *body* into ``<= limit``-char chunks on natural boundaries.

    Multi-chunk output is prefixed ``(n/N)\\n`` so the reader knows a message
    continues.  Boundary preference: paragraph → line → sentence (ASCII and
    CJK) → hard cut, always taking the LATEST boundary inside the budget and
    never cutting before half the budget (which would produce a spray of tiny
    messages).  Unlike truncation, every character survives.
    """
    if len(body) <= limit:
        return [body]
    effective = max(limit - prefix_overhead, limit // 2)
    half = effective // 2
    chunks: List[str] = []
    remaining = body
    while remaining:
        if len(remaining) <= effective:
            chunks.append(remaining)
            break
        window = remaining[:effective]
        cut = window.rfind("\n\n")
        if cut < half:
            cut = window.rfind("\n")
        if cut < half:
            cut = max(
                window.rfind(". "),
                window.rfind("。"),
                window.rfind("！"),
                window.rfind("？"),
            )
            if cut > 0:
                cut += 1  # keep the punctuation with its sentence
        if cut < half:
            cut = effective  # hard cut
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if len(chunks) == 1:
        return chunks
    n = len(chunks)
    return [f"({i + 1}/{n})\n{c}" for i, c in enumerate(chunks)]


#: OneBot / NapCat retcodes whose meaning ``classify_send_error`` cannot know
#: from the message text alone.  Everything else falls through to the shared
#: classifier so this adapter speaks the same failure vocabulary as the rest
#: of the gateway (it drives dead-target detection).
_RETCODE_HINTS: Dict[int, str] = {
    1401: "forbidden",  # unauthorized — bad or missing access token
    1403: "forbidden",  # forbidden
    1404: "not_found",  # unknown action / unknown target
    10003: "bad_format",  # NapCat: invalid parameter
    10004: "forbidden",  # NapCat: insufficient permission
}


def classify_onebot_error(
    retcode: Optional[int], message: str = "", exc: Optional[BaseException] = None
) -> str:
    """Map a OneBot failure onto the shared :data:`SEND_ERROR_KINDS`.

    Tries the shared classifier first (it knows the phrasing every messaging
    API uses), then a small retcode table for the OneBot-specific numbers,
    then a few QQ-specific Chinese phrases, and finally ``unknown`` — never
    a guess that would make a hard failure look benign.
    """
    blob_parts = [message or ""]
    if retcode is not None:
        blob_parts.append(f"retcode={retcode}")
    blob = " ".join(p for p in blob_parts if p)
    kind = classify_send_error(exc, blob)
    if kind != "unknown":
        return kind
    if retcode is not None and retcode in _RETCODE_HINTS:
        return _RETCODE_HINTS[retcode]
    lowered = (message or "").lower()
    # QQ / NapCat speak Chinese for the cases that matter most: being kicked
    # from a group, being muted, or a target that no longer exists.
    if any(s in message for s in ("不存在", "未找到", "没有找到")):
        return "not_found"
    if any(
        s in message for s in ("禁言", "无权", "权限", "拒绝", "被限制", "不是群成员")
    ):
        return "forbidden"
    if any(s in message for s in ("频繁", "过快", "限制发言")):
        return "rate_limited"
    if "too long" in lowered or "消息过长" in message:
        return "too_long"
    if any(s in lowered for s in ("timeout", "timed out", "connection", "closed")):
        return "transient"
    return "unknown"


def guess_media_kind(path: str) -> str:
    """Classify a local file as ``image`` / ``audio`` / ``video`` / ``document``.

    Extension first, because ``mimetypes`` does not know ``.silk`` or ``.amr``
    — the two containers QQ voice messages actually use.
    """
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    mime, _ = mimetypes.guess_type(path)
    if mime:
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
    return "document"


def encode_base64_file(path: str, max_bytes: Optional[int] = None) -> Optional[str]:
    """Return a ``base64://…`` payload, or ``None`` when the file is too big.

    ``None`` is not an error: the caller falls back to handing the backend a
    literal path, which works when it shares our filesystem.

    The ceiling is resolved at call time (not bound as a default) so it stays
    one knob rather than a value frozen at import.
    """
    if max_bytes is None:
        max_bytes = BASE64_MAX_BYTES
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        logger.warning("OneBot: cannot stat attachment: %s", exc)
        return None
    if size > max_bytes:
        logger.warning(
            "OneBot: attachment is %d bytes (> %d) — falling back to a literal "
            "path, which only works if the OneBot backend shares this filesystem",
            size,
            max_bytes,
        )
        return None
    try:
        with open(path, "rb") as fh:
            return "base64://" + base64.b64encode(fh.read()).decode("ascii")
    except OSError as exc:
        logger.warning("OneBot: cannot read attachment: %s", exc)
        return None


def _response_ok(resp: Dict[str, Any]) -> bool:
    """OneBot ``retcode`` 0 = done, 1 = accepted-async; anything else failed."""
    retcode = resp.get("retcode")
    if retcode is None:
        return str(resp.get("status", "")).lower() in {"ok", "async"}
    return retcode in (0, 1)


def _response_error(resp: Dict[str, Any]) -> str:
    """Human-readable detail from a failed OneBot response envelope."""
    msg = resp.get("message") or resp.get("wording") or resp.get("msg") or ""
    return f"{msg} (retcode={resp.get('retcode')})".strip()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OneBotAdapter(BasePlatformAdapter):
    """Hermes platform adapter for QQ over OneBot v11."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    #: We chunk in :meth:`send`, so the gateway must hand us the FULL payload.
    #: With this False, ``gateway/delivery.py`` truncates anything over
    #: ``MAX_PLATFORM_OUTPUT`` (4000 chars) before it ever reaches us, which
    #: silently mangles long scheduled reports.
    splits_long_messages = True

    #: QQ renders neither markdown nor fenced code blocks.
    supports_code_blocks = False
    supports_status_text = False

    def __init__(self, config: PlatformConfig, **kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        extra = dict(getattr(config, "extra", {}) or {})
        self._extra = extra

        self.instance_id = str(extra.get("instance_id") or "default")
        self.ws_url = str(extra.get("ws_url") or os.getenv("ONEBOT_WS_URL", "")).strip()
        self.access_token = (
            extra.get("access_token") or _get_secret("ONEBOT_ACCESS_TOKEN", "") or ""
        ).strip()
        self.token_in_query = _as_bool(
            extra.get("token_in_query", os.getenv("ONEBOT_TOKEN_IN_QUERY")), True
        )
        self.self_ids = parse_id_list(
            extra.get("self_ids", os.getenv("ONEBOT_SELF_IDS"))
        )

        # ---- direct-message gate ------------------------------------------
        # Default True: unlike the group master switch below, private chat
        # was dispatched unconditionally before this key existed, and that
        # is the behaviour every deployment already depends on — corlinman's
        # production QQ account replies to DMs today. Defaulting True with
        # the key unset reproduces that exactly. Flip to False for a
        # receive-only observation window (e.g. a group-archive smoke test)
        # run while another bot instance is still answering this account's
        # private chats, so hermes never double-replies to a real person.
        self.direct_replies_enabled = _as_bool(
            extra.get(
                "direct_replies_enabled", os.getenv("ONEBOT_DIRECT_REPLIES_ENABLED")
            ),
            True,
        )

        # ---- group gates -------------------------------------------------
        # The master switch defaults to False: see the module README. The
        # whole group pipeline below is configured and tested, but a
        # deployment only starts talking in groups when someone flips this.
        self.group_replies_enabled = _as_bool(
            extra.get(
                "group_replies_enabled", os.getenv("ONEBOT_GROUP_REPLIES_ENABLED")
            ),
            False,
        )
        whitelist_raw = extra.get("group_whitelist", _sentinel := object())
        if whitelist_raw is _sentinel:
            whitelist_raw = os.environ.get("ONEBOT_GROUP_WHITELIST")
        self.group_whitelist = parse_whitelist(whitelist_raw)
        self.group_keywords = _load_group_keywords(extra)
        self.group_reply_policy = str(
            extra.get("group_reply_policy")
            or os.getenv("ONEBOT_GROUP_REPLY_POLICY")
            or "mention_or_keyword"
        ).strip()
        self.group_reply_cooldown_secs = _as_float(
            extra.get(
                "group_reply_cooldown_secs",
                os.getenv("ONEBOT_GROUP_REPLY_COOLDOWN_SECS"),
            ),
            0.0,
        )
        self.group_window_secs = (
            _as_float(
                extra.get(
                    "group_rate_limit_window_minutes",
                    os.getenv("ONEBOT_GROUP_RATE_LIMIT_WINDOW_MINUTES"),
                ),
                0.0,
            )
            * 60.0
        )
        self.group_window_max = _as_int(
            extra.get(
                "group_rate_limit_max_messages",
                os.getenv("ONEBOT_GROUP_RATE_LIMIT_MAX_MESSAGES"),
            ),
            0,
        )

        group_per_min = _as_int(
            extra.get(
                "rate_limit_group_per_min",
                os.getenv("ONEBOT_RATE_LIMIT_GROUP_PER_MIN"),
            ),
            0,
        )
        sender_per_min = _as_int(
            extra.get(
                "rate_limit_sender_per_min",
                os.getenv("ONEBOT_RATE_LIMIT_SENDER_PER_MIN"),
            ),
            0,
        )
        self.router = ChannelRouter(
            group_keywords=self.group_keywords,
            group_replies_enabled=self.group_replies_enabled,
            direct_replies_enabled=self.direct_replies_enabled,
            group_whitelist=self.group_whitelist,
            group_reply_policy=self.group_reply_policy,
            group_reply_cooldown_secs=self.group_reply_cooldown_secs,
            self_ids=list(self.self_ids),
        ).with_rate_limits(
            TokenBucket.per_minute(group_per_min) if group_per_min > 0 else None,
            TokenBucket.per_minute(sender_per_min) if sender_per_min > 0 else None,
        )
        self.router.rate_limit_hook = self._on_rate_limited

        # ---- outbound behaviour -----------------------------------------
        self.reply_with_mention = _as_bool(
            extra.get("reply_with_mention", os.getenv("ONEBOT_REPLY_WITH_MENTION")),
            True,
        )
        self.max_bubbles_per_reply = _as_int(
            extra.get(
                "max_bubbles_per_reply", os.getenv("ONEBOT_MAX_BUBBLES_PER_REPLY")
            ),
            DEFAULT_MAX_BUBBLES_PER_REPLY,
        )
        self.forward_threshold = _as_int(
            extra.get("forward_threshold", os.getenv("ONEBOT_FORWARD_THRESHOLD")),
            FORWARD_TEXT_THRESHOLD,
        )
        # Read through the sticker module so the adapter and the persona
        # frame that offers the menu resolve ONE precedence rule.  If they
        # drifted, the model could be shown a menu the adapter then refuses
        # to honour — a silently broken feature rather than a loud one.
        self.sticker_probability = sticker.probability_from_extra(extra)
        self.sticker_dir = sticker.dir_from_extra(extra)
        self.wait_for_send_ack = _as_bool(
            extra.get("wait_for_send_ack", os.getenv("ONEBOT_WAIT_FOR_SEND_ACK")),
            True,
        )
        self.typing_indicator = _as_bool(
            extra.get("typing_indicator", os.getenv("ONEBOT_TYPING_INDICATOR")), True
        )
        # ---- hermes' own system messages ---------------------------------
        # Default True, and deliberately NOT the "preserve today's behaviour"
        # default the DM gate above uses.  QQ is the one platform hermes
        # drives as a person rather than as a bot: a status bubble between two
        # lines of conversation is the mask slipping, which is a defect on
        # this channel specifically and not on Discord or Telegram.  The cost
        # of the default is bounded — every dropped message is still logged
        # (``suppressed_send_result``), the two categories where silence would
        # actually cost something are delivered regardless
        # (``_ALWAYS_DELIVER_PATTERNS``), and approval requests are never
        # marked non-conversational in the first place.  Set false to get the
        # status bubbles back.
        self.suppress_system_messages = _as_bool(
            extra.get(
                "suppress_system_messages",
                os.getenv("ONEBOT_SUPPRESS_SYSTEM_MESSAGES"),
            ),
            True,
        )
        self.max_concurrency = max(
            1,
            _as_int(
                extra.get("max_concurrency", os.getenv("ONEBOT_MAX_CONCURRENCY")),
                DEFAULT_MAX_CONCURRENCY,
            ),
        )
        self.health_probe_secs = _as_float(
            extra.get("health_probe_secs", os.getenv("ONEBOT_HEALTH_PROBE_S")),
            HEALTH_PROBE_SECS,
        )
        self.health_lost_secs = _as_float(
            extra.get("health_lost_secs", os.getenv("ONEBOT_HEALTH_LOST_S")),
            HEALTH_LOST_SECS,
        )

        # ---- runtime state ----------------------------------------------
        self._client: Optional[OneBotClient] = None
        self._dispatch_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._proactive_task: Optional[asyncio.Task] = None
        self._proactive_cancel: Optional[asyncio.Event] = None
        #: Persistent group-message archive (D3).  ``None`` until ``connect()``
        #: resolves a config that switches it on; see :mod:`.group_history`.
        self._history_writer: Optional[Any] = None
        self._turn_tasks: set = set()
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._dedup = MessageDeduplicator()
        self._group_names: Dict[int, str] = {}
        self._bot_nickname = str(extra.get("bot_nickname") or "").strip()
        self._account_online: Optional[bool] = None
        self._link_online = False

        logger.info(
            "OneBot adapter initialised: url=%s groups=%s whitelist=%s "
            "keywords=%d policy=%s cap=%d/%.0fs",
            self.ws_url or "<unset>",
            "enabled" if self.group_replies_enabled else "MUTED",
            "off" if self.group_whitelist is None else len(self.group_whitelist),
            len(self.group_keywords),
            self.group_reply_policy,
            self.group_window_max,
            self.group_window_secs,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def self_id(self) -> Optional[int]:
        """The bot's own uin: live value first, configured seed as fallback."""
        if self._client is not None and self._client.last_self_id:
            return self._client.last_self_id
        return self.self_ids[0] if self.self_ids else None

    def health_snapshot(self) -> Dict[str, Any]:
        """Two different questions, two different answers.

        ``link_online`` is "is the WebSocket up"; ``account_online`` is "is
        the QQ account actually logged in".  They diverge exactly when the
        account gets kicked offline — the socket keeps heartbeating while the
        bot silently stops being able to say anything.
        """
        client = self._client
        return {
            "platform": PLATFORM_NAME,
            "url": self.ws_url,
            "link_online": bool(client is not None and client.connected),
            "account_online": self._account_online,
            "last_event_at_ms": client.last_event_at_ms if client else None,
            "inbound_dropped": client.inbound_dropped_count if client else 0,
            "outbound_queue_depth": client.outbound_queue_depth if client else 0,
            "self_id": self.self_id,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Dial the OneBot WebSocket and start the dispatch + health loops.

        Returns ``False`` only for problems retrying cannot fix (no URL, no
        ``websockets``, an outright rejected token).  A backend that is merely
        down returns ``True``: the client's reconnect ladder is the right
        place to wait it out, and failing the whole platform would mean an
        operator has to restart the gateway by hand after a NapCat restart.
        """
        try:
            import websockets  # noqa: F401,PLC0415
        except ImportError:
            logger.error(
                "OneBot: the 'websockets' package is required (pip install websockets)"
            )
            return False
        if not self.ws_url:
            logger.error(
                "OneBot: ONEBOT_WS_URL (or platforms.onebot.extra.ws_url) is required"
            )
            return False

        try:
            client = OneBotClient(
                OneBotConfig(
                    url=self.ws_url,
                    access_token=self.access_token or None,
                    self_ids=list(self.self_ids),
                    token_in_query=self.token_in_query,
                ),
                on_self_id=self._on_self_id,
            )
        except OneBotConfigError as exc:
            logger.error("OneBot: %s", exc)
            return False

        auth_failed = await self._preflight()
        if auth_failed:
            # A rejected handshake is not something the ladder can fix, and
            # the two tokens on a NapCat box (WebUI vs OneBot) are easy to
            # mix up — say so loudly instead of reconnecting forever.
            self._set_fatal_error(
                "onebot_auth_rejected",
                (
                    f"OneBot rejected the handshake at {self.ws_url}. Check "
                    "ONEBOT_ACCESS_TOKEN — the OneBot access token is NOT the "
                    "same secret as the NapCat WebUI token."
                ),
                retryable=False,
            )
            return False

        self._client = client
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._running = True
        await client.connect()
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name="onebot-dispatch"
        )
        self._health_task = asyncio.create_task(
            self._health_loop(), name="onebot-health"
        )
        self._start_proactive_loop()
        self._start_group_history()
        # Publish for the synchronous tool layer (tools/onebot_client.py) so
        # QQ-borrowing tools reuse this connection instead of opening a
        # second one against the same backend.
        set_live_client(client, asyncio.get_running_loop())
        self._mark_connected()
        logger.info("OneBot: adapter connected (%s)", self.ws_url)
        return True

    async def _preflight(self) -> bool:
        """Probe the handshake once.  ``True`` means "auth was rejected".

        Anything else (unreachable, timeout, TLS) returns ``False`` — those
        are the ladder's problem, not a fatal misconfiguration.
        """
        import websockets  # noqa: PLC0415

        uri = self.ws_url
        headers: List[Tuple[str, str]] = []
        if self.access_token:
            headers.append(("Authorization", f"Bearer {self.access_token}"))
            if self.token_in_query:
                from urllib.parse import quote

                sep = "&" if "?" in uri else "?"
                uri = f"{uri}{sep}access_token={quote(self.access_token)}"
        try:
            async with websockets.connect(
                uri,
                additional_headers=headers or None,
                open_timeout=10,
                close_timeout=5,
            ):
                return False
        except Exception as exc:  # noqa: BLE001 — classified by shape below
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                return True
            logger.warning(
                "OneBot: preflight to %s did not complete (%s: %s) — the "
                "reconnect ladder will keep trying",
                self.ws_url,
                type(exc).__name__,
                exc,
            )
            return False

    def _start_proactive_loop(self) -> None:
        """Spawn the resident proactive-speech loop.

        Always started, even when ``proactive_enabled`` is false: the loop
        idles on a 60-second re-check of the live config, which is what lets an
        operator turn the feature on (or off) without restarting the channel.
        An idle beat is one dict read, so a permanently disabled deployment
        pays essentially nothing for it.

        Imported lazily because :mod:`.proactive` imports this module for the
        shared speech budget and context buffer.
        """
        if self._proactive_task is not None and not self._proactive_task.done():
            # ``connect()`` also runs on reconnect.  A second loop would be a
            # second speaker: both would draw their own gaps and the group
            # would get roughly twice the messages it was promised.
            return
        self._proactive_cancel = asyncio.Event()
        try:
            from . import proactive as _proactive
        except Exception:  # noqa: BLE001 — a broken optional loop must not kill the channel
            logger.exception(
                "OneBot: proactive speech unavailable — continuing without it"
            )
            return
        self._proactive_task = asyncio.create_task(
            _proactive.proactive_loop(
                self,
                self._proactive_cancel,
                _proactive.live_config(self),
            ),
            name="onebot-proactive",
        )

    def _start_group_history(self) -> None:
        """Start the persistent group-message archive, if it is switched on.

        This is the writer for ``qq_group_history.sqlite`` — the store the
        three migrated QQ monitors read and that nothing in this port used to
        write (00-PLAN.md §19 / §21, D46).  Off unless
        ``group_history_enabled`` says otherwise.

        Guarded against ``connect()``'s reconnect path exactly like
        :meth:`_start_proactive_loop`: a second writer would be a second
        thread appending the same messages, i.e. duplicate rows in every
        digest.  Imported lazily and wrapped, because a broken optional
        archive must never cost the channel its QQ connection.
        """
        if self._history_writer is not None and self._history_writer.running:
            return
        try:
            from . import group_history as _gh

            config = _gh.resolve_config(
                _gh_live_extra(self), getattr(self, "group_whitelist", None)
            )
            if config is None:
                self._history_writer = None
                return
            writer = _gh.GroupHistoryWriter(config)
            if not writer.start():
                self._history_writer = None
                return
            self._history_writer = writer
        except Exception:  # noqa: BLE001 — archiving is never load-bearing
            logger.exception(
                "OneBot: group history archiving unavailable — continuing without it"
            )
            self._history_writer = None

    def _record_group_history(
        self, event: protocol.MessageEvent, sender_name: str
    ) -> None:
        """Archive one inbound group message.  Best-effort, never raises.

        Mirrors corlinman's own capture point
        (``corlinman-channels/service.py`` ``_qq_dispatch_loop``, L2694-2716):
        called BEFORE the router gate, so the digest sees the whole room —
        including the messages the bot would never have answered — and with
        the same field conventions, so rows written here and rows corlinman
        wrote are indistinguishable to the monitors' reader:

        * ``sender_name`` is blanked when it is merely the user id repeated,
          because the digest renderer prints ``name(id)`` and would otherwise
          emit ``123(123)``;
        * the text is ``segments_to_text(...) or raw_message`` — segments
          first, which is corlinman's order for the *archive* (the in-memory
          proactive buffer above deliberately prefers ``raw_message``).
        """
        writer = self._history_writer
        if writer is None:
            return
        try:
            writer.record(
                instance_id=self.instance_id,
                group_id=event.group_id,
                sender_user_id=event.user_id,
                sender_name="" if sender_name == str(event.user_id) else sender_name,
                message_id=event.message_id,
                event_time_ms=int(event.time) * 1000 if event.time else None,
                text=protocol.segments_to_text(event.message) or event.raw_message,
            )
        except Exception:  # noqa: BLE001 — an archive miss is not a reply miss
            logger.exception("OneBot: group history capture failed")

    async def _stop_group_history(self) -> None:
        """Flush and stop the archive writer without blocking the event loop.

        ``close()`` joins a thread that may be mid-fsync, so it runs in the
        default executor.  A writer that will not stop is left as the daemon
        thread it already is rather than delaying shutdown.
        """
        writer = self._history_writer
        self._history_writer = None
        if writer is None:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(writer.close), timeout=15.0)
        except Exception:  # noqa: BLE001 — includes TimeoutError; shutdown is best-effort
            # The thread is a daemon, so an unfinished join cannot hold up
            # process exit; at worst the last un-committed batch is lost.
            logger.warning("OneBot: group history writer did not shut down cleanly")

    async def disconnect(self) -> None:
        """Stop the loops and close the socket."""
        self._running = False
        set_live_client(None, None)
        if self._proactive_cancel is not None:
            # Ask first, cancel second: a beat that is mid-send should finish
            # writing rather than leave half a message in the group.
            self._proactive_cancel.set()
        for task in (self._dispatch_task, self._health_task, self._proactive_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._dispatch_task = None
        self._health_task = None
        self._proactive_task = None
        self._proactive_cancel = None
        for task in list(self._turn_tasks):
            if not task.done():
                task.cancel()
        self._turn_tasks.clear()
        # After the turn tasks, before the socket: nothing can still be
        # recording by now, so this flush really is the last word.
        await self._stop_group_history()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass
            self._client = None
        self._mark_disconnected()
        logger.info("OneBot: adapter disconnected")

    def _on_self_id(self, self_id: int) -> None:
        """Observer fired when the backend reveals (or changes) the bot uin."""
        logger.info("OneBot: bot account is %s", self_id)

    def _on_rate_limited(self, channel: str, reason: str) -> None:
        logger.info("OneBot: dropped an inbound message (%s rate limit)", reason)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def _health_loop(self) -> None:
        """Watch link liveness and QQ-account liveness separately."""
        while self._running:
            try:
                await asyncio.sleep(self.health_probe_secs)
            except asyncio.CancelledError:
                return
            client = self._client
            if client is None:
                continue
            now_ms = int(time.time() * 1000)
            last = client.last_event_at_ms
            link_online = bool(
                client.connected
                and last is not None
                and (now_ms - last) < self.health_lost_secs * 1000
            )
            if link_online != self._link_online:
                self._link_online = link_online
                logger.log(
                    logging.INFO if link_online else logging.WARNING,
                    "OneBot: link %s (%s)",
                    "online" if link_online else "silent",
                    self.ws_url,
                )
            account_online = client.last_status_online
            if account_online != self._account_online:
                self._account_online = account_online
                if account_online is False:
                    logger.warning(
                        "OneBot: the QQ account appears to be OFFLINE while the "
                        "WebSocket is up — the bot was most likely kicked and "
                        "needs a QR re-login on the backend"
                    )
                elif account_online:
                    logger.info("OneBot: QQ account is online")

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        """Consume inbound events; one turn per accepted message."""
        client = self._client
        if client is None:  # pragma: no cover — connect() sets it
            return
        try:
            async for event in client.inbound():
                if not self._running:
                    break
                try:
                    await self._on_message_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — one bad event must not stop the loop
                    logger.exception("OneBot: failed to handle an inbound event")
        except asyncio.CancelledError:
            return

    async def _on_message_event(self, event: protocol.MessageEvent) -> None:
        """Gate one inbound message and, if it survives, run a turn."""
        # Backends configured with ``reportSelfMessage`` echo our own posts
        # back to us; answering those is an infinite loop.
        if event.self_id and event.user_id == event.self_id:
            return
        if self._dedup.is_duplicate(f"{event.message_type}:{event.message_id}"):
            return

        is_group = event.message_type == protocol.MessageType.GROUP
        sender_name = (
            event.sender.display_name() if event.sender else str(event.user_id)
        )

        if is_group and event.group_id is not None:
            # Feed the context buffer BEFORE the gate: a persona should see
            # the whole room, not only the messages it chose to answer.
            record_group_message(
                self.instance_id,
                event.group_id,
                sender_name,
                event.raw_message or protocol.segments_to_text(event.message),
                False,
            )
            # ...and the PERSISTENT archive, for the same reason and at the
            # same point.  The two are complements, not duplicates: the buffer
            # above is 30 rows of 200 chars in RAM, sized to be a proactive
            # turn's prompt context and lost on restart; this one survives
            # restarts and holds the 24h window three cron monitors summarise.
            self._record_group_history(event, sender_name)

        routed = self.router.dispatch(event)
        if routed is None:
            return

        # Hard speech cap — a budget, not a politeness rule. Checked AFTER
        # the command short-circuit (operator tooling must never be locked
        # out) and BEFORE the model call (a capped group must not burn one).
        # An @mention does NOT bypass it.
        if is_group and event.group_id is not None and not routed.is_command:
            if not group_speech_allowed(
                self.instance_id,
                event.group_id,
                self.group_window_secs,
                self.group_window_max,
            ):
                logger.info(
                    "OneBot: group %s hit its speech cap (%d per %.0fs)",
                    event.group_id,
                    self.group_window_max,
                    self.group_window_secs,
                )
                self._on_rate_limited(PLATFORM_NAME, "group_window")
                return

        hermes_event = await self._build_message_event(event, routed.content)
        if self._semaphore is None:  # pragma: no cover — connect() sets it
            await self.handle_message(hermes_event)
            return
        # Acquire BEFORE spawning so backpressure reaches the WebSocket
        # reader instead of fanning out unbounded tasks.
        await self._semaphore.acquire()
        task = asyncio.create_task(self._run_turn(hermes_event))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _run_turn(self, event: MessageEvent) -> None:
        try:
            await self.handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("OneBot: turn failed")
        finally:
            if self._semaphore is not None:
                self._semaphore.release()

    async def _build_message_event(
        self, event: protocol.MessageEvent, routed_text: str
    ) -> MessageEvent:
        """Translate an OneBot message into the Hermes event shape."""
        is_group = event.message_type == protocol.MessageType.GROUP
        self_id = event.self_id or (self.self_id or 0)
        # Routing matched against the raw CQ text; the model gets a cleaned
        # version without the bot's own @mention (mentions of other people
        # stay — they are conversational content).
        text = protocol.strip_self_mention(event.message, self_id)
        if not text.strip():
            text = routed_text.strip()

        media_urls, media_types = await self._download_media(event)
        chat_id = format_chat_id(
            is_group, event.group_id if is_group else event.user_id
        )
        sender_name = event.sender.display_name() if event.sender else ""
        chat_name = (
            self._group_display_name(event.group_id)
            if is_group
            else (sender_name or str(event.user_id))
        )
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type="group" if is_group else "dm",
            user_id=str(event.user_id),
            user_name=sender_name or str(event.user_id),
            message_id=str(event.message_id),
        )
        message_type = MessageType.TEXT
        if media_types:
            first = media_types[0]
            if first.startswith("image/"):
                message_type = MessageType.PHOTO
            elif first.startswith("audio/"):
                message_type = MessageType.VOICE
            elif first.startswith("video/"):
                message_type = MessageType.VIDEO
            else:
                message_type = MessageType.DOCUMENT
        return MessageEvent(
            text=text,
            message_type=message_type,
            user_id=str(event.user_id),
            user_name=sender_name or str(event.user_id),
            source=source,
            raw_message=event,
            message_id=str(event.message_id),
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=protocol.reply_target(event.message),
            timestamp=(
                datetime.fromtimestamp(event.time, tz=timezone.utc)
                if event.time
                else datetime.now(tz=timezone.utc)
            ),
            # Per-channel persona framing (00-PLAN.md §18).  The gateway folds
            # this into ``combined_ephemeral`` at API-call time
            # (``gateway/run.py:5211``); ``None`` simply leaves it unset.  The
            # proactive lane resolves it through the SAME function, so the
            # persona is framed identically whether it answers or speaks first.
            channel_prompt=persona_binding.channel_prompt(
                _gh_live_extra(self),
                chat_id=event.group_id if is_group else event.user_id,
                is_group=is_group,
            ),
            metadata={
                "onebot_self_id": self_id,
                "onebot_group_id": event.group_id,
                # The uin to @ when replying in a group.
                "onebot_at_user_id": str(event.user_id),
                "onebot_message_type": event.message_type.value,
            },
        )

    async def _download_media(
        self, event: protocol.MessageEvent
    ) -> Tuple[List[str], List[str]]:
        """Cache inbound attachments locally so vision / file tools can read them.

        Failures are logged and skipped: a broken image must not cost the
        user their message.
        """
        media_urls: List[str] = []
        media_types: List[str] = []
        for ref in protocol.segments_to_media(event.message):
            try:
                if ref.kind == "image":
                    ext = Path(ref.file_name or "").suffix or ".jpg"
                    path = await cache_image_from_url(ref.url, ext=ext)
                    media_types.append(
                        "image/jpeg"
                        if ext in (".jpg", ".jpeg")
                        else f"image/{ext.lstrip('.')}"
                    )
                elif ref.kind == "audio":
                    ext = Path(ref.file_name or "").suffix or ".amr"
                    path = await cache_audio_from_url(ref.url, ext=ext)
                    media_types.append(f"audio/{ext.lstrip('.')}")
                else:
                    path = await self._cache_generic(ref)
                    if path is None:
                        continue
                    media_types.append(
                        "video/mp4"
                        if ref.kind == "video"
                        else "application/octet-stream"
                    )
                media_urls.append(path)
            except Exception as exc:  # noqa: BLE001 —媒体缺失 must not drop the text
                logger.warning(
                    "OneBot: could not cache a %s attachment (%s)", ref.kind, exc
                )
        return media_urls, media_types

    async def _cache_generic(self, ref: protocol.MediaRef) -> Optional[str]:
        """Download a video / document segment into the document cache."""
        from gateway.platforms.base import cache_document_from_bytes
        from tools.url_safety import create_ssrf_safe_async_client, is_safe_url

        if not is_safe_url(ref.url):
            logger.warning("OneBot: refusing to fetch an unsafe media URL")
            return None
        async with create_ssrf_safe_async_client(
            timeout=30.0, follow_redirects=True
        ) as client:
            resp = await client.get(ref.url)
            resp.raise_for_status()
            data = resp.content
        return cache_document_from_bytes(data, ref.file_name or "qq_attachment.bin")

    def _group_display_name(self, group_id: Optional[int]) -> str:
        """Cached group name, refreshed in the background on a cache miss.

        Never blocks the inbound path on an API round trip — a message must
        not wait on cosmetics.
        """
        if group_id is None:
            return "QQ group"
        cached = self._group_names.get(group_id)
        if cached:
            return cached
        if self._client is not None and self._running:
            task = asyncio.create_task(self._refresh_group_name(group_id))
            self._turn_tasks.add(task)
            task.add_done_callback(self._turn_tasks.discard)
        return f"QQ group {group_id}"

    async def _refresh_group_name(self, group_id: int) -> None:
        try:
            info = await self._call(
                protocol.RawAction("get_group_info", {"group_id": group_id}),
                timeout=10.0,
            )
        except Exception:  # noqa: BLE001 — cosmetic
            return
        data = info.get("data") if isinstance(info, dict) else None
        if isinstance(data, dict) and data.get("group_name"):
            self._group_names[group_id] = str(data["group_name"])

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def _call(
        self, action: protocol.Action, *, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        if self._client is None:
            raise OneBotTransportError("OneBot adapter is not connected")
        return await self._client.call_action(action, timeout=timeout)

    async def _send_segments(
        self,
        is_group: bool,
        target: int,
        segments: List[protocol.MessageSegment],
    ) -> Tuple[bool, Optional[str], Optional[SendResult]]:
        """Send one message; return ``(ok, message_id, failure_result)``.

        When the backend never echoes (some builds do not), a timeout is
        treated as an optimistic success rather than a failure: reporting a
        delivered message as failed would make the gateway retry and
        double-post, and would poison dead-target detection.
        """
        if is_group and group_speech_muted(self):
            # Defence in depth.  Every caller above already checks, but this is
            # the single choke point every text/inline-media send passes
            # through, so a path added later cannot quietly route around the
            # mute.
            return False, None, muted_send_result(target)
        action: protocol.Action = (
            protocol.SendGroupMsg(group_id=target, message=segments)
            if is_group
            else protocol.SendPrivateMsg(user_id=target, message=segments)
        )
        if not self.wait_for_send_ack:
            if self._client is None:
                return (
                    False,
                    None,
                    SendResult(
                        success=False,
                        error="OneBot adapter is not connected",
                        error_kind="transient",
                        retryable=True,
                    ),
                )
            await self._client.send_action(action)
            return True, None, None
        try:
            resp = await self._call(action)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "OneBot: no send acknowledgement within the timeout — assuming "
                "delivery (the backend may not echo responses)"
            )
            return True, None, None
        except OneBotTransportError as exc:
            return (
                False,
                None,
                SendResult(
                    success=False,
                    error=str(exc),
                    error_kind=classify_onebot_error(None, str(exc), exc),
                    retryable=True,
                ),
            )
        if not _response_ok(resp):
            detail = _response_error(resp)
            kind = classify_onebot_error(resp.get("retcode"), detail)
            return (
                False,
                None,
                SendResult(
                    success=False,
                    error=f"OneBot rejected the message: {detail}",
                    error_kind=kind,
                    raw_response=resp,
                    retryable=kind in ("transient", "rate_limited"),
                ),
            )
        data = resp.get("data")
        mid = None
        if isinstance(data, dict) and data.get("message_id") is not None:
            mid = str(data["message_id"])
        return True, mid, None

    def _lead_segments(
        self,
        is_group: bool,
        at_user_id: Optional[str],
        reply_to: Optional[str],
    ) -> List[protocol.MessageSegment]:
        """Prefix segments for the FIRST outgoing message only.

        Only the first chunk carries the @mention and the quote: repeating
        them on every chunk pings the recipient N times, which QQ clients
        render as spam and Tencent's anti-spam may throttle.
        """
        segments: List[protocol.MessageSegment] = []
        if reply_to:
            segments.append(protocol.ReplySegment(id=str(reply_to)))
        if is_group and at_user_id and self.reply_with_mention:
            segments.append(protocol.AtSegment(qq=str(at_user_id)))
        return segments

    def _sticker_segment(self, slugs: Sequence[str]) -> Optional[protocol.ImageSegment]:
        """Resolve the model's sticker choice into one inline image segment.

        ``None`` whenever the reply should go out exactly as it would have
        before this feature existed — which is every failure mode there is:
        the feature is off, the model named a sticker that does not exist, or
        the file has gone missing on the deployed box.  A sticker is a
        flourish; nothing about it may cost the reader the actual answer, so
        every branch here degrades to silence rather than to an error.

        The probability and asset directory are resolved from live adapter
        settings so this agrees with ``persona_binding.channel_prompt`` when
        an in-place config reconcile changes them. Without that, the menu
        could advertise a sticker after the adapter had been switched off, or
        the adapter could emit one after the menu disappeared.

        ``sticker_probability <= 0`` is checked FIRST and is a hard off
        switch, not merely "never offer the menu".  The two are different
        under hallucination: a model that has seen the menu on earlier turns
        can emit the marker on a turn where it was not offered, and an
        operator who set the knob to zero means "no stickers", not "no
        stickers unless the model insists".

        Only the first valid slug is honoured.  A model that writes three
        markers gets one image, because the point of the whole feature is one
        small flourish and a wall of stickers is the noise this outbound path
        has been repeatedly reshaped to prevent.
        """
        live_extra = _gh_live_extra(self)
        if sticker.probability_from_extra(live_extra) <= 0:
            return None
        for slug in slugs:
            path = sticker.sticker_path(slug, sticker.dir_from_extra(live_extra))
            if path is None:
                continue
            # Same reference form the attachment path has always used:
            # ``base64://`` inline, with a literal path only as the
            # oversize fallback.  See ``encode_base64_file`` — the backend
            # usually runs in its own container and cannot read our disk.
            payload = encode_base64_file(path)
            if payload is None:
                logger.info(
                    "OneBot: sticker %r could not be inlined — sending a "
                    "literal path, which needs a backend sharing this disk",
                    slug,
                )
            return protocol.ImageSegment(file=payload or path)
        return None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver a reply, splitting bubbles / chunks / forward cards.

        The single outbound choke point: both the reactive reply lane and the
        proactive lane (``proactive.py``) reach the wire through here, so this
        is where outbound shaping and the outbound gates live.

        Shape of one send:

        1. The emergency mute (groups only) — an operator decision, checked
           before anything is computed or connected.
        2. Strip markdown: QQ renders none of it.
        3. The Tencent content policy, on the whole stripped body, groups and
           DMs alike.  Fails closed.
        4. Route the WHOLE reply.  A reply longer than ``forward_threshold``
           OR split into more than ``max_bubbles_per_reply`` bubbles goes out
           as ONE merged-forward card — one notification, opened on demand,
           with the ``[MSG_BREAK]`` rhythm preserved as nodes.
        5. Chat otherwise.  Bubbles within both limits go out as written,
           each chunked at the message ceiling.  ``cap_bubbles`` is only the
           degraded fallback when cards are disabled or a card is rejected.
        6. Only the first outgoing message carries the quote + @mention.
        """
        metadata = metadata or {}
        # Hermes' own status messages, hidden before anything else happens.
        # First because a message that is not going out should not be parsed,
        # moderated, split, or measured against the mute — and because the
        # mute logs a warning per refusal, which would turn every hidden
        # heartbeat into an alarm about a group nobody tried to post to.
        if is_suppressible_system_message(
            metadata, content
        ) and system_messages_suppressed(self):
            return suppressed_send_result(chat_id, content)
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError as exc:
            return SendResult(success=False, error=str(exc), error_kind="not_found")
        # The emergency mute, checked BEFORE the connection state: "muted" is
        # a decision, "not connected" is a transient the caller would retry,
        # and a muted send must not be retried into existence.  DMs are never
        # affected — the mute is about speaking in rooms full of people.
        if is_group and group_speech_muted(self):
            return muted_send_result(target)

        # ── Outbound shaping and the content gate ───────────────────────────
        # Order here is load-bearing: mute → strip_markdown → content policy →
        # split_bubbles → chunk_text.
        #
        # QQ clients render no markdown, so an unstripped reply shows literal
        # ``**bold**`` / ``- item`` / ``### heading`` to the person reading it.
        # Eight adapters already call this helper (the *other* QQ platform,
        # ``gateway/platforms/qqbot``, among them); this one did not.  Note
        # that the ``supports_code_blocks = False`` class attribute below is
        # NOT this — it is read only by ``gateway/run.py``'s progress output
        # and never reaches this method.
        content = strip_markdown(content or "")

        # ── Lift the sticker markers out of the body ───────────────────────
        # Between ``strip_markdown`` and the content policy, deliberately.
        #
        # After stripping, because a marker that arrived wrapped in markdown
        # emphasis has to be unwrapped before it is recognisable.
        #
        # BEFORE the policy, because ``[STICKER:thumbs-up]`` is machine
        # syntax the reader never sees.  The gate's whole job is to audit the
        # sentence a human will read; auditing a string with control tokens
        # still embedded in it means auditing something nobody receives, and
        # the tokens could split a phrase the rule table is watching for
        # (exactly the straddling problem the comment below describes for
        # bubbles).  Stripping first also means a refused reply is refused on
        # its real text, with the sticker going nowhere either — the marker
        # is carried in a local, and nothing downstream of a refusal reads it.
        content, sticker_slugs = sticker.extract_markers(content)

        # The policy audits the text AFTER stripping, and audits it WHOLE.
        #
        # After stripping, because the stripped string is what actually leaves
        # the process — auditing the pre-strip text would be auditing something
        # nobody ever receives.  Whole, because ``split_bubbles`` and
        # ``chunk_text`` below cut the body at markers and length boundaries
        # that have nothing to do with meaning: a phrase the rule table catches
        # can straddle such a cut, and per-bubble or per-chunk auditing would
        # let both halves through individually.
        #
        # The source system audits at two different points on its two lanes
        # (the proactive lane audits pre-normalization, the reply lane audits
        # the post-normalization body).  Both lanes funnel through this one
        # method here, so wiring the gate at this single choke point makes the
        # two consistent.  That is a deliberate improvement over the source's
        # inconsistency, not an accidental deviation from it.
        #
        # No ``is_group`` condition, unlike the mute above.  The mute is about
        # speaking in a room full of people; the freeze risk this policy exists
        # to contain is account-level, and Tencent's risk control does not care
        # whether the sentence was typed into a group or a DM.
        #
        # TODO(media): the image/file send paths (``send_media`` and the
        # inline-image branch) do not reach this method, so nothing moderates
        # them.  The source system gates those with ``policy.moderate_media``;
        # porting that is out of scope for this change and is tracked as a
        # known gap.
        policy_cfg = content_policy.resolve_config(None)
        try:
            decision = content_policy.moderate_text(content, policy_cfg).decision
        except Exception:  # noqa: BLE001 — fail closed, never fail open
            # An exploding classifier must not become an open gate: the whole
            # point of this module is that it is the last thing standing
            # between a bad sentence and a frozen account.
            decision = content_policy.classifier_failure_decision(content)
        if not decision.allowed:
            return policy_blocked_send_result(target, is_group, decision)

        # Both gates sit BEFORE the connection check for the reason the mute
        # comment above gives: "not connected" is a retryable transient, and
        # reporting it for text that will be refused anyway would have the
        # caller retry a message that must never go out.  Decisions first,
        # transients second.
        if self._client is None:
            return SendResult(
                success=False,
                error="OneBot adapter is not connected",
                error_kind="transient",
                retryable=True,
            )

        at_user_id = metadata.get("onebot_at_user_id") if is_group else None
        bubbles = split_bubbles(content or "")
        sent_ids: List[str] = []

        # ── Route the reply: one card, or a handful of bubbles ─────────────
        #
        # The choice is made ONCE, on the whole finished reply, before any
        # splitting — because it is a question about what KIND of reply this
        # is, and that is a property of the whole thing rather than of any
        # fragment of it.
        #
        # D88: a whole reply is a card when it is too long OR too fragmented.
        # A cap merge makes the final bubble into a bloated slab and loses the
        # persona's intentional ``[MSG_BREAK]`` rhythm, while a forward card
        # preserves every bubble as its own node and emits one notification.
        # ``forward_threshold <= 0`` explicitly disables cards; only that
        # degraded path, or a non-terminal forward rejection, reaches the
        # cap merge below.
        delivered_as_card = False
        cards_enabled = self.forward_threshold > 0
        too_long = len(content) > self.forward_threshold
        too_many_bubbles = (
            self.max_bubbles_per_reply > 0 and len(bubbles) > self.max_bubbles_per_reply
        )
        needs_card = cards_enabled and (too_long or too_many_bubbles)
        needs_capped_fallback = not cards_enabled
        lead_already_sent = False
        if needs_card:
            forward_result = await self._deliver_forward(
                is_group, target, bubbles or [content], at_user_id, reply_to
            )
            lead_already_sent = forward_result.lead_sent
            if forward_result.success:
                delivered_as_card = True
            elif (
                forward_result.failure is not None
                and forward_result.failure.error_kind in ("forbidden", "not_found")
            ):
                # The target is gone — chunking will not help.
                return forward_result.failure
            else:
                # Card rejected for any other reason: preserve the answer on
                # the bounded chat fallback rather than lose it.
                logger.info("OneBot: forward card rejected — falling back to chunks")
                needs_capped_fallback = True

        if not delivered_as_card:
            if needs_capped_fallback:
                # Capped before the per-bubble loop, so protocol length
                # splitting still applies to an overflow merged into one body.
                bubbles = cap_bubbles(bubbles, self.max_bubbles_per_reply)

            # ── The sticker the model asked for ───────────────────────────
            # Resolved only from inside this branch, which settles three
            # things at once:
            #
            # * only the CHAT lane can carry one.  Reaching here means the
            #   card path was not taken, so "no sticker on a folded answer"
            #   needs no condition of its own — a considered answer the
            #   reader taps to expand is not a place for a cartoon tiger;
            # * it is downstream of the content gate, and the marker was
            #   already stripped upstream of it, so the policy audited
            #   exactly the text a human ends up reading — no more (the
            #   marker is machine syntax) and no less;
            # * it is downstream of the routing decision above, which
            #   measured character count and bubble count on the stripped
            #   text.  A sticker therefore cannot tip a reply into a card,
            #   and cannot change how many messages fire.
            sticker_segment = self._sticker_segment(sticker_slugs)
            # Materialised before the loop purely so "the last message" is
            # knowable while the first one is being built.  ``chunk_text``
            # already returns a list, so this changes no value.
            planned = [chunk_text(b, self.MAX_MESSAGE_LENGTH) for b in bubbles]
            last_bubble = len(planned) - 1
            # A group card lead already carried the quote and @mention.  If the
            # card itself is then rejected, the chat fallback starts after that
            # lead instead of pinging and quoting the recipient a second time.
            first = not lead_already_sent
            for bubble_index, chunks in enumerate(planned):
                for chunk_index, chunk in enumerate(chunks):
                    segments = self._lead_segments(
                        is_group,
                        at_user_id if first else None,
                        reply_to if first else None,
                    )
                    segments.append(
                        protocol.TextSegment(
                            text=(" " if segments and is_group else "") + chunk
                        )
                    )
                    carries_sticker = (
                        sticker_segment is not None
                        and bubble_index == last_bubble
                        and chunk_index == len(chunks) - 1
                    )
                    if carries_sticker:
                        segments.append(sticker_segment)
                    ok, mid, failure = await self._send_segments(
                        is_group, target, segments
                    )
                    if (
                        not ok
                        and carries_sticker
                        and failure is not None
                        and failure.error_kind not in ("forbidden", "not_found")
                    ):
                        # A backend can reject an otherwise valid text message
                        # only because its inline image handling is disabled or
                        # flaky. Retry this final bubble once without the
                        # optional image so a failed flourish never loses the
                        # actual reply. Permanent target errors are not retried.
                        plain_segments = [
                            segment
                            for segment in segments
                            if not isinstance(segment, protocol.ImageSegment)
                        ]
                        ok, mid, failure = await self._send_segments(
                            is_group, target, plain_segments
                        )
                    if not ok and failure is not None:
                        return failure
                    if mid:
                        sent_ids.append(mid)
                    first = False
                if len(bubbles) > 1:
                    await asyncio.sleep(BUBBLE_GAP_SECS)

            if sticker_segment is not None and not planned:
                # A reply that was NOTHING but a sticker marker.  The text is
                # empty, so the loop above sent nothing and the image has no
                # bubble to ride on — but the persona did choose to answer,
                # and dropping it silently would look like the bot ignored
                # the message.  One image, no empty text bubble beside it:
                # QQ renders a zero-length message as a blank grey box.
                segments = self._lead_segments(is_group, at_user_id, reply_to)
                segments.append(sticker_segment)
                ok, mid, failure = await self._send_segments(is_group, target, segments)
                if not ok and failure is not None:
                    return failure
                if mid:
                    sent_ids.append(mid)

        if is_group:
            # Store the flattened bubbles, not the raw body: a literal
            # ``[MSG_BREAK]`` in the buffer would be fed straight back into the
            # next proactive prompt as if the bot had typed it.
            record_group_message(
                self.instance_id,
                target,
                self._bot_nickname or "bot",
                " ".join(bubbles) if bubbles else content,
                True,
            )
        return SendResult(
            success=True,
            message_id=sent_ids[-1] if sent_ids else None,
            continuation_message_ids=tuple(sent_ids[:-1]),
            raw_response={"message_ids": sent_ids} if sent_ids else None,
        )

    async def _deliver_forward(
        self,
        is_group: bool,
        target: int,
        bodies: List[str],
        at_user_id: Optional[str],
        reply_to: Optional[str],
    ) -> _ForwardDeliveryResult:
        """Fold a long reply into ONE merged-forward card.

        *bodies* is one entry per persona bubble, and each becomes its own
        node inside the card.  A card node renders as a separate line under
        the sender's name, so the ``[MSG_BREAK]`` rhythm the persona wrote
        survives INSIDE the card — the reader sees a conversation, not one
        undifferentiated slab — while the recipient's client still fires a
        single notification for the whole thing.  ``messages`` is a list on
        both actions and ``action_to_wire`` serializes every node, so this
        needs no protocol work.

        In a group the card cannot carry the @mention, so a short lead line
        goes out first — otherwise the person who asked never gets pinged.
        The result records whether that separate message succeeded so a card
        rejection can fall back to chat without repeating its lead segments.
        """
        lead_sent = False
        if is_group and at_user_id:
            lead = self._lead_segments(True, at_user_id, reply_to)
            lead.append(protocol.TextSegment(text=f" {FORWARD_LEAD_TEXT}"))
            ok, _mid, failure = await self._send_segments(True, target, lead)
            if not ok:
                return _ForwardDeliveryResult(False, failure, lead_sent=False)
            lead_sent = True
        nodes = [
            protocol.ForwardNode(
                name=self._bot_nickname or "Hermes",
                uin=str(self.self_id or target),
                content=[protocol.TextSegment(text=body)],
            )
            for body in bodies
        ]
        action: protocol.Action = (
            protocol.SendGroupForwardMsg(group_id=target, messages=nodes)
            if is_group
            else protocol.SendPrivateForwardMsg(user_id=target, messages=nodes)
        )
        try:
            resp = await self._call(action)
        except (asyncio.TimeoutError, TimeoutError):
            return _ForwardDeliveryResult(False, None, lead_sent)
        except OneBotTransportError as exc:
            return _ForwardDeliveryResult(
                False,
                SendResult(
                    success=False,
                    error=str(exc),
                    error_kind=classify_onebot_error(None, str(exc), exc),
                    retryable=True,
                ),
                lead_sent,
            )
        if _response_ok(resp):
            return _ForwardDeliveryResult(True, None, lead_sent)
        detail = _response_error(resp)
        return _ForwardDeliveryResult(
            False,
            SendResult(
                success=False,
                error=f"OneBot rejected the forward card: {detail}",
                error_kind=classify_onebot_error(resp.get("retcode"), detail),
                raw_response=resp,
            ),
            lead_sent,
        )

    # ------------------------------------------------------------------
    # Outbound media
    # ------------------------------------------------------------------

    async def _send_attachment(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        *,
        kind: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Route one local file by kind: image / voice inline, else upload.

        Images and voice notes go INLINE as ``base64://`` segments so they
        appear in the conversation; everything else goes through the group /
        private file area, which is where QQ users expect documents.
        """
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError as exc:
            return SendResult(success=False, error=str(exc), error_kind="not_found")
        # A picture posted into a muted group is still the bot talking in that
        # group.  Gated here rather than only in ``send()`` because the file
        # and inline-media paths do not go through it.
        if is_group and group_speech_muted(self):
            return muted_send_result(target)
        safe_path = validate_media_delivery_path(file_path)
        if not safe_path:
            return SendResult(
                success=False,
                error="attachment path rejected by media-delivery policy",
                error_kind="forbidden",
            )
        media_kind = kind or guess_media_kind(safe_path)
        payload = encode_base64_file(safe_path)

        if media_kind in ("image", "audio"):
            segments = self._lead_segments(
                is_group, (metadata or {}).get("onebot_at_user_id"), reply_to
            )
            file_ref = payload or safe_path
            if media_kind == "image":
                segments.append(protocol.ImageSegment(file=file_ref))
            else:
                segments.append(protocol.RecordSegment(file=file_ref))
            if caption:
                # QQ renders a voice message as its own bubble — a caption
                # alongside it would be dropped, so send it as text first.
                cap_segments = self._lead_segments(
                    is_group, (metadata or {}).get("onebot_at_user_id"), reply_to
                )
                cap_segments.append(
                    protocol.TextSegment(
                        text=(" " if cap_segments and is_group else "") + caption
                    )
                )
                await self._send_segments(is_group, target, cap_segments)
            ok, mid, failure = await self._send_segments(is_group, target, segments)
            if not ok and failure is not None:
                return failure
            return SendResult(success=True, message_id=mid)

        name = file_name or Path(safe_path).name
        action: protocol.Action = (
            protocol.UploadGroupFile(
                group_id=target, file=payload or safe_path, name=name
            )
            if is_group
            else protocol.UploadPrivateFile(
                user_id=target, file=payload or safe_path, name=name
            )
        )
        try:
            resp = await self._call(action, timeout=UPLOAD_RESPONSE_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            return SendResult(
                success=False,
                error=f"OneBot file upload timed out after {UPLOAD_RESPONSE_TIMEOUT:.0f}s",
                error_kind=classify_onebot_error(None, "timeout", exc),
                retryable=True,
            )
        except OneBotTransportError as exc:
            return SendResult(
                success=False,
                error=str(exc),
                error_kind=classify_onebot_error(None, str(exc), exc),
                retryable=True,
            )
        if not _response_ok(resp):
            detail = _response_error(resp)
            return SendResult(
                success=False,
                error=f"OneBot rejected the file upload: {detail}",
                error_kind=classify_onebot_error(resp.get("retcode"), detail),
                raw_response=resp,
            )
        if caption:
            await self.send(chat_id, caption, reply_to=reply_to, metadata=metadata)
        return SendResult(success=True, raw_response=resp)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_attachment(
            chat_id,
            image_path,
            caption,
            kind="image",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a remote image by URL — the backend fetches it itself."""
        if not str(image_url).lower().startswith(("http://", "https://")):
            return await self.send_image_file(
                chat_id, image_url, caption, reply_to, metadata
            )
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError as exc:
            return SendResult(success=False, error=str(exc), error_kind="not_found")
        if is_group and group_speech_muted(self):
            return muted_send_result(target)
        segments = self._lead_segments(
            is_group, (metadata or {}).get("onebot_at_user_id"), reply_to
        )
        if caption:
            segments.append(
                protocol.TextSegment(
                    text=(" " if segments and is_group else "") + caption
                )
            )
        segments.append(protocol.ImageSegment(url=str(image_url)))
        ok, mid, failure = await self._send_segments(is_group, target, segments)
        if not ok and failure is not None:
            return failure
        return SendResult(success=True, message_id=mid)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_attachment(
            chat_id,
            audio_path,
            caption,
            kind="audio",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_attachment(
            chat_id,
            video_path,
            caption,
            kind="video",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_attachment(
            chat_id,
            file_path,
            caption,
            kind="document",
            file_name=file_name,
            reply_to=reply_to,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata: Optional[Dict] = None) -> None:
        """DM-only "对方正在输入…".  QQ groups do not render a typing state.

        Any failure is swallowed: a non-NapCat backend answers "unsupported
        action", and a missing typing bubble must never block the reply.
        """
        if not self.typing_indicator or self._client is None:
            return
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError:
            return
        if is_group:
            return
        try:
            await self._client.send_action(
                protocol.SetInputStatus(user_id=target, event_type=1)
            )
        except Exception:  # noqa: BLE001
            pass

    async def stop_typing(self, chat_id: str) -> None:
        if not self.typing_indicator or self._client is None:
            return
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError:
            return
        if is_group:
            return
        try:
            await self._client.send_action(
                protocol.SetInputStatus(user_id=target, event_type=0)
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Directory / info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Name + type for a chat.  Falls back to the id when lookup fails."""
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError:
            return {"name": str(chat_id), "type": "dm", "id": str(chat_id)}
        fallback = {
            "name": f"QQ group {target}" if is_group else str(target),
            "type": "group" if is_group else "dm",
            "id": format_chat_id(is_group, target),
        }
        if self._client is None:
            return fallback
        action = (
            protocol.RawAction("get_group_info", {"group_id": target})
            if is_group
            else protocol.RawAction("get_stranger_info", {"user_id": target})
        )
        try:
            resp = await self._call(action, timeout=10.0)
        except Exception:  # noqa: BLE001 — info is best-effort
            return fallback
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            return fallback
        name = data.get("group_name") if is_group else data.get("nickname")
        if name:
            fallback["name"] = str(name)
            if is_group:
                self._group_names[target] = str(name)
        if is_group and data.get("member_count") is not None:
            fallback["member_count"] = data["member_count"]
        return fallback

    async def list_channels(self) -> Optional[List[Dict[str, Any]]]:
        """Enumerate groups and friends for the channel directory.

        Returns ``None`` (not ``[]``) when the link is down so the directory
        keeps whatever it already knew instead of wiping every known target.
        """
        if self._client is None or not self._client.connected:
            return None
        channels: List[Dict[str, Any]] = []
        try:
            resp = await self._call(protocol.RawAction("get_group_list"), timeout=15.0)
        except Exception:  # noqa: BLE001
            return None
        for group in (resp.get("data") or []) if isinstance(resp, dict) else []:
            if not isinstance(group, dict) or group.get("group_id") is None:
                continue
            gid = group["group_id"]
            name = str(group.get("group_name") or gid)
            self._group_names[int(gid)] = name
            channels.append({
                "id": format_chat_id(True, gid),
                "name": name,
                "type": "group",
            })
        try:
            resp = await self._call(protocol.RawAction("get_friend_list"), timeout=15.0)
        except Exception:  # noqa: BLE001 — groups alone are still useful
            return channels
        for friend in (resp.get("data") or []) if isinstance(resp, dict) else []:
            if not isinstance(friend, dict) or friend.get("user_id") is None:
                continue
            uid = friend["user_id"]
            name = str(friend.get("remark") or friend.get("nickname") or uid)
            channels.append({"id": str(uid), "name": name, "type": "dm"})
        return channels


def _load_group_keywords(extra: Dict[str, Any]) -> Dict[str, List[str]]:
    """Read the per-group keyword map from config or env.

    A malformed map is logged and treated as empty rather than raised: an
    unparseable keyword list must not take the whole gateway down, and with
    the default policy the fallback (mention-only) is the safe direction.
    """
    raw = extra.get("group_keywords")
    if isinstance(raw, dict):
        try:
            return {str(k): [str(v) for v in (vals or [])] for k, vals in raw.items()}
        except Exception:  # noqa: BLE001
            logger.warning("OneBot: ignoring malformed group_keywords config")
            return {}
    text = raw if isinstance(raw, str) else os.getenv("ONEBOT_GROUP_KEYWORDS", "")
    try:
        return parse_group_keywords(text or "")
    except json.JSONDecodeError as exc:
        logger.warning("OneBot: ONEBOT_GROUP_KEYWORDS is not valid JSON (%s)", exc)
        return {}


# ---------------------------------------------------------------------------
# Plugin entry points
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Passive readiness probe — no side effects, no installs.

    Called by ``hermes setup`` / ``hermes status`` / dashboard readiness, so
    it must stay cheap and never install anything.
    """
    if not os.getenv("ONEBOT_WS_URL"):
        return False
    try:
        import websockets  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


def validate_config(config: Any) -> bool:
    """Whether this config could actually connect."""
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("ONEBOT_WS_URL") or extra.get("ws_url"))


def is_connected(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("ONEBOT_WS_URL") or extra.get("ws_url"))


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed ``PlatformConfig.extra`` from env during gateway config load.

    Runs BEFORE adapter construction so ``hermes status`` reflects an
    env-only configuration without opening a socket.  ``home_channel`` is
    handled by the core hook and becomes a real ``HomeChannel``.
    """
    ws_url = os.getenv("ONEBOT_WS_URL", "").strip()
    if not ws_url:
        return None
    seed: Dict[str, Any] = {"ws_url": ws_url}
    home = os.getenv("ONEBOT_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("ONEBOT_HOME_CHANNEL_NAME", "").strip() or home,
        }
    return seed


#: Generic keys the core config loader already merges with the correct
#: precedence.  Re-emitting them from ``apply_yaml_config_fn`` would clobber
#: that (the hook's return value is applied with ``dict.update``).
_GENERIC_MERGE_KEYS = {
    "reply_prefix",
    "reply_in_thread",
    "reply_to_mode",
    "unauthorized_dm_behavior",
    "notice_delivery",
    "require_mention",
    "channel_prompts",
    "gateway_restart_notification",
    "allow_from",
    "allow_admin_from",
    "dm_policy",
    "group_policy",
    "typing_indicator",
}

#: Adapter-private YAML keys under ``platforms.onebot``.
_PRIVATE_YAML_KEYS = (
    "ws_url",
    "access_token",
    "token_in_query",
    "self_ids",
    "instance_id",
    "bot_nickname",
    "direct_replies_enabled",
    "group_replies_enabled",
    "group_whitelist",
    "group_keywords",
    "group_reply_policy",
    "group_reply_cooldown_secs",
    "group_rate_limit_window_minutes",
    "group_rate_limit_max_messages",
    "rate_limit_group_per_min",
    "rate_limit_sender_per_min",
    "reply_with_mention",
    "max_bubbles_per_reply",
    "forward_threshold",
    "wait_for_send_ack",
    "suppress_system_messages",
    "max_concurrency",
    "health_probe_secs",
    "health_lost_secs",
    # Proactive speech (see .proactive).  Off unless proactive_enabled is set.
    "proactive_enabled",
    "proactive_groups",
    "proactive_min_gap_minutes",
    "proactive_max_gap_minutes",
    "proactive_daily_max",
    "proactive_active_start_hour",
    "proactive_active_end_hour",
    "proactive_timezone",
    "proactive_probability",
    "proactive_context_messages",
    "proactive_prompt",
    # Persistent group-message archive (see .group_history).  Off unless
    # group_history_enabled is set.  ``group_history_db`` names the file this
    # gateway WRITES; the monitors' own QQ_GROUP_HISTORY_DB names the file
    # they READ, and during coexistence those are two different files.
    "group_history_enabled",
    "group_history_groups",
    "group_history_db",
    "group_history_retention_days",
    "group_history_batch_rows",
    "group_history_flush_secs",
    "group_history_queue_max",
)


def _apply_yaml_config(
    yaml_cfg: Dict[str, Any], platform_cfg: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Merge ``platforms.onebot.*`` YAML into ``PlatformConfig.extra``."""
    extras: Dict[str, Any] = {}
    nested = (
        platform_cfg.get("extra") if isinstance(platform_cfg.get("extra"), dict) else {}
    )
    for key in _PRIVATE_YAML_KEYS:
        if key in platform_cfg:
            extras.setdefault(key, platform_cfg[key])
        elif key in nested:
            extras.setdefault(key, nested[key])
    for k, v in (nested or {}).items():
        if k not in _GENERIC_MERGE_KEYS:
            extras.setdefault(k, v)
    return extras or None


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process delivery (``hermes cron`` without a running gateway).

    Opens one short-lived connection, sends the (chunked) text, closes.
    Without this hook, ``deliver=onebot`` jobs fail with "No live adapter
    for platform".  Media is not attempted here: inlining a base64 payload
    over a throwaway connection is exactly the memory spike this deployment
    cannot afford.
    """
    try:
        import websockets  # noqa: PLC0415
    except ImportError:
        return {"error": "websockets not installed. Run: pip install websockets"}

    extra = getattr(pconfig, "extra", {}) or {}
    ws_url = (os.getenv("ONEBOT_WS_URL") or extra.get("ws_url") or "").strip()
    if not ws_url:
        return {"error": "OneBot standalone send: ONEBOT_WS_URL is required"}
    token = (
        os.getenv("ONEBOT_ACCESS_TOKEN") or extra.get("access_token") or ""
    ).strip()
    try:
        is_group, target = parse_chat_id(chat_id)
    except ValueError as exc:
        return {"error": f"OneBot standalone send: {exc}"}
    # The same mute, on the path a cron job takes when it runs OUTSIDE the
    # gateway process (``tools/send_message_tool.py`` falls back here when the
    # in-process adapter weakref is None).  Gating only the live adapter would
    # leave exactly the hole D44 was raised about, just one process over.
    # There is no adapter object here, so the flag is read straight off the
    # platform config with the same default as ``OneBotAdapter.__init__``.
    if is_group and not _as_bool(
        extra.get("group_replies_enabled", os.getenv("ONEBOT_GROUP_REPLIES_ENABLED")),
        False,
    ):
        logger.warning(
            "OneBot: group speech is muted (%s) — standalone delivery to group "
            "%s refused",
            MUTED_REASON,
            target,
        )
        return {
            "error": (
                "OneBot: group speech is muted "
                f"({MUTED_REASON}); nothing was sent to group {target}"
            ),
            MUTED_MARKER: True,
            "reason": MUTED_REASON,
            "group_id": target,
        }

    uri = ws_url
    headers: List[Tuple[str, str]] = []
    if token:
        headers.append(("Authorization", f"Bearer {token}"))
        from urllib.parse import quote

        sep = "&" if "?" in uri else "?"
        uri = f"{uri}{sep}access_token={quote(token)}"

    try:
        async with websockets.connect(
            uri, additional_headers=headers or None, open_timeout=10, close_timeout=5
        ) as ws:
            for chunk in chunk_text(message or "", MAX_MESSAGE_LENGTH):
                action: protocol.Action = (
                    protocol.SendGroupMsg(
                        group_id=target, message=[protocol.TextSegment(text=chunk)]
                    )
                    if is_group
                    else protocol.SendPrivateMsg(
                        user_id=target, message=[protocol.TextSegment(text=chunk)]
                    )
                )
                await ws.send(json.dumps(protocol.action_to_wire(action)))
            # Give the backend a moment to consume the frames before the
            # close handshake tears the socket down.
            await asyncio.sleep(0.5)
        return {"success": True, "platform": PLATFORM_NAME, "chat_id": chat_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"OneBot send failed: {exc}"}


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup gateway`` → OneBot."""
    print()
    print("QQ via OneBot v11 (NapCat / Lagrange) setup")
    print("-------------------------------------------")
    print("Requirements:")
    print("  1. A OneBot v11 backend exposing a FORWARD WebSocket server")
    print("     (NapCat: Network → websocketServers), e.g. ws://127.0.0.1:3001")
    print("  2. Its access token — NOT the WebUI token; they are different secrets.")
    print("  3. This is unrelated to the built-in 'qqbot' platform, which speaks")
    print("     Tencent's official Bot API instead.")
    print()
    try:
        from hermes_cli.config import get_env_value, save_env_value
    except ImportError:
        print("hermes_cli.config unavailable; set ONEBOT_* in ~/.hermes/.env manually")
        return

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        existing = get_env_value(var) if callable(get_env_value) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                from hermes_cli.secret_prompt import masked_secret_prompt

                value = masked_secret_prompt(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            save_env_value(var, value)

    _prompt("ONEBOT_WS_URL", "OneBot forward WebSocket URL (e.g. ws://127.0.0.1:3001)")
    _prompt("ONEBOT_ACCESS_TOKEN", "OneBot access token (blank if none)", secret=True)
    _prompt("ONEBOT_ALLOWED_USERS", "Allowed QQ user ids (comma-separated; blank=skip)")
    _prompt("ONEBOT_HOME_CHANNEL", "Home channel for cron delivery (g<group> or <qq>)")
    print()
    print("Group replies are OFF until you set ONEBOT_GROUP_REPLIES_ENABLED=true.")
    print("See plugins/platforms/onebot/README.md for the full key list.")


PLATFORM_HINT = (
    "You are talking through QQ via a OneBot v11 bridge. QQ renders plain "
    "text only — no markdown, no code fences, no link previews; write for a "
    "phone screen. Keep replies short and conversational: several short "
    "messages read better than one long one, and you can force a bubble "
    "break with the literal marker [MSG_BREAK]. Very long replies are folded "
    "into a tappable 'chat record' card automatically. In a group you are "
    "one participant among many — answer when addressed, don't narrate. "
    "Chat ids are 'g<group_id>' for groups and the bare QQ number for direct "
    "messages."
)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label=PLATFORM_LABEL,
        adapter_factory=lambda cfg: OneBotAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ONEBOT_WS_URL"],
        install_hint=(
            "Point ONEBOT_WS_URL at a running NapCat/Lagrange OneBot v11 "
            "forward WebSocket (e.g. ws://127.0.0.1:3001)."
        ),
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="ONEBOT_ALLOWED_USERS",
        allow_all_env="ONEBOT_ALLOW_ALL_USERS",
        cron_deliver_env_var="ONEBOT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🐧",
        allow_update_command=True,
        platform_hint=PLATFORM_HINT,
    )
