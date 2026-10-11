"""QQ platform adapter speaking OneBot v11 (SnowLuma / NapCat / Lagrange / go-cqhttp).

**This is not the same thing as ``gateway/platforms/qqbot/``.**  That adapter
speaks Tencent's *official* QQ Bot API v2 (``api.sgroup.qq.com``, appid +
secret).  This one speaks the community OneBot v11 protocol against a local
backend driving a normal QQ account.  Two different protocols, two different
platform names — ``qqbot`` vs ``onebot`` — on purpose.

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
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SEND_ERROR_KINDS,
    cache_audio_from_bytes,
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
from plugins.platforms.onebot.agent_gate import (
    AGENT_GATE_VERSION,
    judge_outbound,
    resolve_agent_gate_config,
)

from . import (
    main_window,
    media_gate,
    persona_binding,
    protocol,
    reply_batch,
    reply_context,
    sticker,
)
from .inbound_gate import (
    NoticeCooldown,
    log_blocked as log_inbound_blocked,
    resolve_inbound_gate_config,
    screen_inbound,
)
from .client import (
    OneBotClient,
    OneBotConfig,
    OneBotConfigError,
    OneBotTransportError,
    set_live_adapter,
    set_live_client,
)
from .rate_limit import SlidingWindowCounter, TokenBucket
from .native_backend import NativeQQSettings, native_features
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

#: The backend's practical per-message ceiling is ~4500-5000 characters; keep
#: a safety margin so we never discover the real limit in production.
MAX_MESSAGE_LENGTH = 3800


def _normalise_backend(app_name: Any) -> str:
    """Map a backend's reported ``app_name`` onto the small known vocabulary.

    OneBot v11 does not standardise the spelling of the implementation name,
    and some backends include a build or protocol suffix.  Substring matching
    keeps the result stable without making the identity probe part of message
    routing.  Unknown implementations remain explicitly unknown rather than
    being treated as a known backend by accident.
    """
    name = str(app_name or "").casefold()
    if "snowluma native text bridge" in name:
        return "qq-native"
    if "snowluma" in name:
        return "snowluma"
    if "napcat" in name:
        return "napcat"
    if "lagrange" in name:
        return "lagrange"
    if "go-cqhttp" in name or "gocqhttp" in name:
        return "go-cqhttp"
    return "unknown"

#: Above this, a reply stops being chat and becomes a considered answer — the
#: WHOLE reply folds into one merged-forward ("聊天记录") card the reader taps
#: to expand, instead of being split into bubbles and capped.  Measured on the
#: entire finished body, not per bubble: it is the reply's character that
#: decides its shape, and that is not a property of any one fragment.
FORWARD_TEXT_THRESHOLD = 1000

#: A forward card cannot carry an @mention, so in a group we post this line
#: (with the @) first — otherwise the person who asked never gets a ping.
FORWARD_LEAD_TEXT = "完整回复在下面。"


@dataclass(frozen=True)
class _ForwardDeliveryResult:
    """Outcome of a card attempt, including any separately delivered lead."""

    success: bool
    failure: Optional[SendResult]
    lead_sent: bool
    message_id: Optional[str] = None


#: Persona-style bubble separator: one logical reply, several chat bubbles.
BUBBLE_SEPARATOR = "[MSG_BREAK]"
LONG_REPLY_MARKER = "[LONG_REPLY]"
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
#: Account probes are a recovery/fallback signal, not a replacement for the
#: heartbeat.  They stay slower than the link probe so an offline QQ account
#: does not turn a healthy WebSocket into an action storm.
ACCOUNT_PROBE_SECS = 60.0

#: Stable marker for the local "QQ needs a login" send refusal.  This is kept
#: distinct from transport errors: retrying an auth-required message cannot
#: make the account log in, and classifying it as ``forbidden`` would make the
#: shared dead-target tracker quarantine an otherwise valid chat.
ACCOUNT_AUTH_REQUIRED_MARKER = "onebot_account_auth_required"
ACCOUNT_AUTH_REQUIRED_REASON = "qq_account_auth_required"
ACCOUNT_AUTH_REQUIRED_ERROR_KIND = "unknown"

ACCOUNT_STATE_UNKNOWN = "unknown"
ACCOUNT_STATE_ONLINE = "online"
ACCOUNT_STATE_AUTH_REQUIRED = "auth_required"

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
# The voice-call seam
# ---------------------------------------------------------------------------
#
# ``.voice_call`` (the session state machine) and ``.voice`` (the outbound
# voice bubble) both import THIS module, so neither can be imported at module
# scope here.  They are also both optional: ``.voice`` reaches a TTS provider
# and ``.voice_call`` sits on top of it, and a deployment with neither
# configured must keep its QQ channel even if one of them fails to import.
#
# Hence one memoised accessor rather than a lazy import at each of the four
# call sites.  ``None`` means "unavailable", and every caller answers that by
# behaving exactly as it did before this feature existed — which is also what
# makes the no-session inbound path byte-for-byte unchanged.

#: How often the resident sweeper looks for a call deadline that has passed.
#:
#: This bounds how LATE a timeout can fire, so it is picked against the
#: *smallest deadline the config can express*: ``voice_call.MIN_TIMEOUT_SECS``
#: is 1.0s, and any tick longer than that would let a one-second ring timeout
#: run for several seconds — the deadline would stop meaning what it says.
#: 1 Hz is affordable because an idle beat is a scan of an empty dict plus one
#: config resolve, and the overwhelmingly common case is no sessions at all.
#: Deadlines are also swept right after each turn (see
#: :meth:`OneBotAdapter._sweep_calls`), so this timer only has to catch the
#: deadlines that pass while nobody is talking.
CALL_SWEEP_SECS = 1.0

#: Private ``send()`` metadata flag meaning "this text IS the fallback for a
#: voice line that could not be synthesized".  Without it the fallback would
#: re-enter the voice path and pay for a second failing TTS job before
#: degrading to the same text.
VOICE_FALLBACK_META = "onebot_voice_fallback"

_VOICE_CALL_MODULE: Optional[Any] = None
_VOICE_CALL_IMPORTED = False


def _call_module() -> Optional[Any]:
    """The call state machine, or ``None`` when it will not import.

    Memoised on the *attempt*, not on success: a broken optional module must
    be reported once, not once per inbound message.
    """
    global _VOICE_CALL_MODULE, _VOICE_CALL_IMPORTED
    if not _VOICE_CALL_IMPORTED:
        _VOICE_CALL_IMPORTED = True
        try:
            from . import voice_call as _voice_call

            _VOICE_CALL_MODULE = _voice_call
        except Exception:  # noqa: BLE001 — an optional feature, never the channel
            logger.exception(
                "OneBot: voice calls unavailable — continuing without them"
            )
            _VOICE_CALL_MODULE = None
    return _VOICE_CALL_MODULE


def voice_script(content: str) -> str:
    """The body to speak, with this plugin's own machine markers lifted out.

    ``voice.spoken_script`` knows about markdown; it does not know about the
    two markers this plugin invents.  Left in, a provider reads
    "[MSG_BREAK]" and "[STICKER:thumbs-up]" out loud — the single most
    obvious way a voice reply gives itself away.  So the same three steps
    ``send()`` runs, in the same order (strip markdown → lift the sticker →
    split bubbles), and then one utterance whose line breaks carry the
    persona's bubble rhythm as pauses.  The sticker goes nowhere: a voice
    bubble cannot carry an image, and posting one beside it would be a second
    message the caller did not ask for.
    """
    body, _slugs = sticker.extract_markers(strip_markdown(content or ""))
    return "\n".join(split_bubbles(body))


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


def account_auth_required_send_result(target: Any, is_group: bool) -> SendResult:
    """Return the honest result for a send while QQ needs a manual login.

    The WebSocket may still be perfectly healthy when NapCat reports
    ``bot_offline``.  This is therefore neither a transport retry nor a dead
    destination: it is a local account-auth decision.  ``unknown`` is the
    shared error kind that avoids poisoning dead-target tracking, while the
    explicit marker lets callers surface the remediation without parsing
    prose.
    """
    where = f"group {target}" if is_group else f"user {target}"
    logger.warning(
        "OneBot: QQ account is auth-required — refusing outbound message to %s; "
        "manual QR/password login is required on the backend",
        where,
    )
    return SendResult(
        success=False,
        error=(
            "OneBot: QQ account is offline/auth-required; nothing was sent to "
            f"{where}. Log in again on the OneBot backend (usually by manual QR)."
        ),
        error_kind=ACCOUNT_AUTH_REQUIRED_ERROR_KIND,
        retryable=False,
        raw_response={
            ACCOUNT_AUTH_REQUIRED_MARKER: True,
            "reason": ACCOUNT_AUTH_REQUIRED_REASON,
            ("group_id" if is_group else "user_id"): target,
        },
    )


def is_account_auth_required_send_result(result: Any) -> bool:
    """Whether *result* is the local QQ auth-required refusal."""
    if isinstance(result, dict):
        return bool(result.get(ACCOUNT_AUTH_REQUIRED_MARKER))
    raw = getattr(result, "raw_response", None)
    return bool(isinstance(raw, dict) and raw.get(ACCOUNT_AUTH_REQUIRED_MARKER))


#: Marker for a refusal by the *agent* gate (stage 1) rather than the
#: deterministic policy table (stage 2).  Kept separate on purpose: the two
#: gates have opposite failure postures and very different trust levels, so an
#: operator reading a refusal must be able to tell which one fired without
#: parsing prose.  See ``plugins/platforms/onebot/agent_gate.py``.
AGENT_GATE_MARKER = "onebot_agent_gate_blocked"
AGENT_GATE_REASON = "agent_self_review"


def agent_gate_blocked_send_result(
    target: Any, is_group: bool, verdict: Any
) -> SendResult:
    """Refusal shaped exactly like :func:`policy_blocked_send_result`.

    Same contract: no source text in the audit fields (the model's ``reason``
    is its own words about the draft, not the draft), and a stable marker so a
    caller can distinguish a local refusal from a send failure.
    """
    where = f"group {target}" if is_group else f"user {target}"
    logger.warning(
        "OneBot: the agent gate refused an outbound message to %s "
        "(category=%s gate=%s)",
        where,
        getattr(verdict, "category", "") or "unspecified",
        AGENT_GATE_VERSION,
    )
    return SendResult(
        success=False,
        error=(
            "OneBot: the agent judged this message unsafe to send "
            f"({AGENT_GATE_REASON}); nothing was sent to {where}"
        ),
        error_kind="unknown",
        retryable=False,
        raw_response={
            AGENT_GATE_MARKER: True,
            "reason": AGENT_GATE_REASON,
            ("group_id" if is_group else "user_id"): target,
            **getattr(verdict, "audit_fields", dict)(),
        },
    )


def is_agent_gate_blocked_send_result(result: Any) -> bool:
    """Whether *result* is a stage-1 agent-gate refusal."""
    if isinstance(result, dict):
        return bool(result.get(AGENT_GATE_MARKER))
    raw = getattr(result, "raw_response", None)
    return bool(isinstance(raw, dict) and raw.get(AGENT_GATE_MARKER))


# ---------------------------------------------------------------------------
# The one place every outbound string is audited
# ---------------------------------------------------------------------------
#
# There are five ways a string can reach QQ from this plugin: ``send()``, the
# inline branch of ``_send_attachment``, ``send_image``'s remote-URL branch,
# ``_standalone_send``, and the voice lane in ``.voice``.  Each one used to
# carry its own copy of the gate, and the copies had drifted — only ``send()``
# ever grew the stage-1 agent gate, so four of the five were running on the
# keyword table alone.  That is the failure mode a duplicated gate always has:
# it is not that someone removes it, it is that someone improves *one* of them.
#
# So the audit lives here once and every path calls it.  Adding a sixth send
# path now means either calling this function or writing a visibly gate-shaped
# block of code next to five call sites that do not have one.


async def guard_outbound_text(
    *texts: Optional[str],
    target: Any,
    is_group: bool,
    agent_config: Any = None,
) -> Optional[SendResult]:
    """Run both gates over every outbound string.  ``None`` means allowed.

    Order is stage 1 then stage 2, per the operator's instruction ("先 agent
    闸后机器闸"), and the two keep their opposite failure postures:

    * **Stage 1 — the agent gate.** A model reads the draft.  Sees implication,
      roleplay framing, and context a keyword table cannot.  **Fails open**: a
      timeout or an unparseable answer falls through to stage 2, because stage
      2 is local and deterministic and there is therefore always a real gate in
      the path.  If both stages failed closed, one provider hiccup would mute
      the bot completely and an operator could not tell that from the policy
      working.
    * **Stage 2 — the deterministic table.** **Fails closed**: an exploding
      classifier refuses, because it is the last thing between a bad sentence
      and a frozen account.

    Every string is audited, not just the first, and each is audited **whole**:
    bubble-splitting and length-chunking cut text at boundaries that have
    nothing to do with meaning, so a phrase the table watches for can straddle
    a cut and pass as two innocent halves.

    No ``is_group`` condition on either stage.  The mute is about speaking in a
    room full of people; the freeze risk these gates contain is account-level,
    and Tencent's risk control does not care which window the words appeared in.
    """
    candidates = [t for t in texts if t and str(t).strip()]
    if not candidates:
        return None

    gate_cfg = resolve_agent_gate_config(
        agent_config.get("agent_gate") if isinstance(agent_config, dict) else None
    )
    for candidate in candidates:
        verdict = await judge_outbound(candidate, config=gate_cfg, is_group=is_group)
        if verdict.consulted and not verdict.safe:
            return agent_gate_blocked_send_result(target, is_group, verdict)

    policy_cfg = content_policy.resolve_config(None)
    for candidate in candidates:
        try:
            decision = content_policy.moderate_text_all(candidate, policy_cfg).decision
        except Exception:  # noqa: BLE001 — fail closed, never fail open
            decision = content_policy.classifier_failure_decision(candidate)
        if not decision.allowed:
            return policy_blocked_send_result(target, is_group, decision)
    return None


#: Marker for an attachment dropped by the media gate.  Distinct from the two
#: text markers because the *outcome* differs: a blocked attachment degrades to
#: a text-only send rather than refusing the message, so a caller inspecting
#: this must not treat it as "nothing was delivered".
MEDIA_GATE_MARKER = "onebot_media_gate_blocked"
MEDIA_GATE_REASON = "media_self_review"


async def guard_outbound_media(
    path: Optional[str],
    *,
    target: Any,
    is_group: bool,
    media_config: Any = None,
) -> Optional[SendResult]:
    """Screen one outgoing file.  ``None`` means allowed.

    Unlike the text gate this **fails closed** — see the fail-posture section
    of :mod:`.media_gate`.  There is no deterministic fallback behind it: this
    gate *is* the classifier, and the ported ``policy.moderate_media`` denies
    anything unclassified.

    The returned refusal is a signal to the caller, which is expected to drop
    the attachment and send the text alone rather than fail the whole message.
    That is the same degradation the source system performs when an attachment
    is refused but the body is not.
    """
    if not path:
        return None
    cfg = media_gate.resolve_media_gate_config(
        media_config.get("media_gate") if isinstance(media_config, dict) else None
    )
    verdict = await media_gate.judge_media_file(path, config=cfg)

    # Route the model's answer through the ported policy rather than acting on
    # it directly: ``moderate_media`` owns the deny-by-default rule and the
    # master switch, and this keeps the classifier a *source of evidence* for
    # that function instead of a second, parallel decision-maker.
    decision = content_policy.moderate_media(
        config=content_policy.resolve_config(None),
        classified_safe=True if verdict.safe else None,
    )
    if decision.allowed:
        return None

    where = f"group {target}" if is_group else f"user {target}"
    logger.warning(
        "OneBot: the media gate refused an attachment to %s "
        "(category=%s cause=%s gate=%s) — sending text only",
        where,
        verdict.category or "unspecified",
        verdict.fallthrough_cause or "classified_unsafe",
        media_gate.MEDIA_GATE_VERSION,
    )
    return SendResult(
        success=False,
        error=(
            "OneBot: the media gate refused this attachment "
            f"({MEDIA_GATE_REASON}); it was not sent to {where}"
        ),
        error_kind="unknown",
        retryable=False,
        raw_response={
            MEDIA_GATE_MARKER: True,
            "reason": MEDIA_GATE_REASON,
            ("group_id" if is_group else "user_id"): target,
            **verdict.audit_fields(),
            **content_policy.policy_error_payload(decision),
        },
    )


def is_media_gate_blocked_send_result(result: Any) -> bool:
    """Whether *result* is a media-gate refusal."""
    if isinstance(result, dict):
        return bool(result.get(MEDIA_GATE_MARKER))
    raw = getattr(result, "raw_response", None)
    return bool(isinstance(raw, dict) and raw.get(MEDIA_GATE_MARKER))


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
#: Empty by operator decision: on this deployment QQ is a persona channel and
#: **nothing** of hermes' own is allowed to surface there ("全部静默"). The two
#: carve-outs that used to live here — the ``hermes update`` prompt and the
#: session-database alarm — are given up knowingly; both still reach the
#: journal at WARNING, which is where an operator looks anyway, and the
#: operator also has Telegram. Restoring either is a one-line edit here.
_ALWAYS_DELIVER_PATTERNS: tuple = ()


#: Operational replies that arrive as ORDINARY REPLY TEXT, not as flagged sends.
#:
#: This is the hole ``is_suppressible_system_message`` cannot see. Messages like
#: "at the active session limit", "Another turn is still running", and the
#: provider-error family are *returned* by the gateway as the turn's answer, so
#: they never pass through ``_non_conversational_metadata`` and never carry the
#: flag. From the adapter they are indistinguishable from a persona reply
#: except by what they say.
#:
#: Matching on text is normally the wrong tool — a persona that writes emoji
#: eventually collides with an emoji-shaped filter. Two conditions together
#: make it safe here, and BOTH are required:
#:
#: 1. the message opens with an operational glyph (⚠ ⏳ ⚕ 🔒 ❌), and
#: 2. it contains no CJK at all.
#:
#: This persona speaks Chinese. Every real reply it writes — including ones
#: that open with an emoji, including ones quoting an error — carries CJK. A
#: message that leads with a warning glyph and contains not one Chinese
#: character is hermes talking about itself, in English, to an operator.
_OPERATIONAL_GLYPH_RE = re.compile(r"^\s*[\u26a0\u23f3\u2695\U0001f512\u274c]\ufe0f?")
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")

#: Operational replies with no glyph to key on. Exact substrings, taken from
#: the emitting source so a wording change breaks the match loudly rather than
#: silently un-suppressing.
_OPERATIONAL_LITERALS = (
    "Hermes is at the active session limit",
    "active session limit",
)


#: The one thing "全部静默" must not swallow: a question that BLOCKS a live turn.
#:
#: An approval request is not a status announcement. ``_approval_notify_sync``
#: asks whether to run a dangerous command and the agent then waits for the
#: answer, so hiding it does not make the channel quieter — it deadlocks the
#: turn on a question nobody was shown, with no timeout and no alert. Silence
#: is the goal; a stuck bot is not silence.
#:
#: This is the only exception, and it is narrow on purpose. The ``hermes
#: update`` prompt blocks the same way and is NOT excepted, because it only
#: appears when an operator deliberately runs an update and is therefore
#: already watching; an approval can be triggered by ordinary agent work at
#: any time.
_APPROVAL_REQUEST_RE = re.compile(
    r"\b(requires? approval|approval required|Smart DENY|owner override)\b",
    re.IGNORECASE,
)


def is_operational_reply(content: Any) -> bool:
    """Whether *content* is hermes talking about itself rather than a reply.

    See :data:`_OPERATIONAL_GLYPH_RE` for why glyph-plus-no-CJK is a safe test
    on this deployment and a text filter alone would not be, and
    :data:`_APPROVAL_REQUEST_RE` for the single category this deliberately
    lets through.
    """
    text = str(content or "")
    if not text.strip():
        return False
    if _APPROVAL_REQUEST_RE.search(text):
        return False
    if _OPERATIONAL_GLYPH_RE.match(text) and not _CJK_RE.search(text):
        return True
    return any(literal in text for literal in _OPERATIONAL_LITERALS)


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
    from . import voice_call as _voice_call  # local: same import cycle

    _proactive.reset_state()
    # Call sessions are process-wide by design (a reconnect must not drop a
    # live call), which is exactly why they leak between test files unless
    # they are cleared here alongside the proactive budget.
    _voice_call.reset_state()
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


#: Inbound-event metadata key naming the message an answer is answering.
#: Set on the event this adapter builds and read back in :meth:`send`, which
#: is what confines the quote to the reply lane: the proactive lane, a cron
#: delivery and a tool's ``send_message`` all reach ``send`` without it, and a
#: message nobody asked for must not point at some older message as if it had.
REPLY_ANCHOR_META = "onebot_reply_to_message_id"


def reply_anchor_mode(extra: Optional[Mapping[str, Any]]) -> str:
    """``"first"`` (quote the message being answered) or ``"off"``.

    Same knob name the Telegram and Discord adapters read, so an operator who
    knows one knows this one.  ``"all"`` is accepted for parity but resolves
    to ``"first"``: QQ renders a quote as a full inset block, and repeating it
    on every bubble of a three-bubble reply triples the answer's height while
    adding nothing — which is also why :meth:`_lead_segments` has always put
    the quote on the first chunk only.

    Read live rather than at construction so an operator can switch quoting
    off through a config reconcile, without a restart, the moment it turns out
    to read badly in some group.
    """
    raw = extra.get("reply_to_mode") if isinstance(extra, Mapping) else None
    if raw is None:
        raw = os.getenv("ONEBOT_REPLY_TO_MODE")
    return "off" if str(raw or "").strip().lower() == "off" else "first"


#: Per-channel answer to "an over-fragmented reply becomes what?".
#: ``"card"`` (the default, and what every channel did before this existed)
#: keeps D88's behaviour; ``"merge"`` sends the capped bubbles instead.
BUBBLE_OVERFLOW_MERGE = "merge"


def bubble_overflow_is_merge(extra: Optional[Mapping[str, Any]], chat_id: Any) -> bool:
    """Does *chat_id* want fragmentation overflow merged rather than carded?

    D88 chose a forward card for an over-fragmented reply because merging the
    overflow bloats the final bubble and loses the persona's intentional
    ``[MSG_BREAK]`` rhythm.  That trade-off is a property of the *persona*,
    not of the protocol: a character whose bubbles run about six characters
    merges into something still shorter than one ordinary sentence, while a
    forward card reads as a document and destroys the illusion outright.  So
    the answer has to be per-channel, because one OneBot account now serves
    channels bound to different personas.

    Only fragmentation is affected.  A reply over ``forward_threshold`` is
    still a card for every channel: length is a real reading problem, and no
    persona is served by a wall of text in a group.

    The lookup is duplicated from :func:`sticker._per_channel` rather than
    shared, because ``sticker`` is imported *by* this module and moving the
    helper the other way would close an import cycle over eight lines.
    """
    extra = extra or {}
    table = extra.get("bubble_overflow")
    if not isinstance(table, Mapping) or chat_id is None:
        return False
    raw = str(chat_id).strip()
    if not raw:
        return False
    for candidate in (raw, raw[1:] if raw[:1] in ("g", "G") else None, "g" + raw):
        if candidate and candidate in table:
            return str(table[candidate]).strip().lower() == BUBBLE_OVERFLOW_MERGE
    return False


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


#: OneBot / backend retcodes whose meaning ``classify_send_error`` cannot know
#: from the message text alone.  Everything else falls through to the shared
#: classifier so this adapter speaks the same failure vocabulary as the rest
#: of the gateway (it drives dead-target detection).
_RETCODE_HINTS: Dict[int, str] = {
    1401: "forbidden",  # unauthorized — bad or missing access token
    1403: "forbidden",  # forbidden
    1404: "not_found",  # unknown action / unknown target
    10003: "bad_format",  # backend: invalid parameter
    10004: "forbidden",  # backend: insufficient permission
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
    # QQ backends speak Chinese for the cases that matter most: being kicked
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


#: What the recent-chatter buffer records for an OUTBOUND file that carries no
#: caption, keyed by :func:`guess_media_kind`'s vocabulary.  Deliberately the
#: labels a QQ client itself shows in place of the media, so the persona reads
#: back the same thing a person scrolling the group sees.
_SELF_MEDIA_MARKERS = {
    "image": "[图片]",
    "audio": "[语音]",
    "video": "[视频]",
    "document": "[文件]",
}


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


#: Extension assumed for an inbound QQ voice note when nothing at all names
#: one.  QQ ships SILK and AMR interchangeably and the segment rarely says
#: which, so this is a placeholder for the cache to overwrite: the sniffer in
#: ``tools/audio_container.py`` renames the file to match its magic bytes.
_VOICE_FALLBACK_EXT = ".amr"

#: What ``get_record`` is asked to transcode into.  MP3 because every STT
#: backend takes it and every OneBot implementation that has the action at
#: all can produce it.
_VOICE_OUT_FORMAT = "mp3"


def _voice_mime(cached_path: str, guessed_ext: str = "") -> str:
    """MIME for a cached voice note, honest about how much we actually know.

    ``cache_audio_from_bytes`` renames the file after sniffing its magic
    bytes, so the cached suffix is normally the sniffer's verdict and can be
    asserted.  ``guessed_ext`` is the extension *we* invented because nothing
    named one; if that is all that came back, the sniffer had no idea either,
    and we say ``audio/*`` rather than claim a container the very same
    sniffer will contradict downstream.

    ``audio/``-prefixed either way, deliberately: the gateway's STT gate
    (``gateway/run.py`` ``_event_media_is_stt_input``) routes an attachment
    into transcription on that prefix, and a QQ voice note must never miss it.
    """
    ext = Path(cached_path).suffix
    if not ext or (guessed_ext and ext == guessed_ext):
        return "audio/*"
    return f"audio/{ext.lstrip('.')}"


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


def _probe_response_ok(resp: Any) -> bool:
    """Whether *resp* is an unambiguously successful account-probe envelope.

    Probe evidence can lift an ``auth_required`` account back online, so its
    success test is intentionally stricter than the general send-response
    helper above.  A contradictory ``status=failed, retcode=0`` envelope is a
    failure, boolean retcodes are not integers for this purpose, and async
    acceptance is not a completed read.
    """
    if not isinstance(resp, dict):
        return False
    has_status = "status" in resp
    has_retcode = "retcode" in resp
    if has_status and str(resp.get("status") or "").strip().casefold() != "ok":
        return False
    if has_retcode:
        retcode = resp.get("retcode")
        if isinstance(retcode, bool) or not isinstance(retcode, int) or retcode != 0:
            return False
    return has_status or has_retcode


def _response_error(resp: Dict[str, Any]) -> str:
    """Human-readable detail from a failed OneBot response envelope."""
    msg = resp.get("message") or resp.get("wording") or resp.get("msg") or ""
    return f"{msg} (retcode={resp.get('retcode')})".strip()


def _response_online_evidence(resp: Any) -> Tuple[Optional[bool], bool]:
    """Return ``(online, conflicted)`` for every explicit flag in *resp*."""
    if not isinstance(resp, dict):
        return None, False
    candidates: List[Any] = []
    for container in (resp, resp.get("data")):
        if not isinstance(container, dict):
            continue
        if "online" in container:
            candidates.append(container.get("online"))
        status = container.get("status")
        if isinstance(status, dict):
            if "online" in status:
                candidates.append(status.get("online"))
            app = status.get("app")
            if isinstance(app, dict) and "online" in app:
                candidates.append(app.get("online"))
            if "good" in status:
                candidates.append(status.get("good"))
    values = {
        value
        for candidate in candidates
        if (value := OneBotClient._coerce_online(candidate)) is not None
    }
    if len(values) > 1:
        return None, True
    return (next(iter(values)), False) if values else (None, False)


def _response_online(resp: Any) -> Optional[bool]:
    """Extract an explicit account-online flag from a probe response.

    OneBot implementations use both ``data.online`` and a nested
    ``data.status.online`` shape.  Returning ``None`` means the response did
    not carry a usable flag; it is intentionally not treated as offline.
    """
    online, conflicted = _response_online_evidence(resp)
    return None if conflicted else online


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
        self._reply_batcher = reply_batch.ReplyBatcher(self)
        self._native_qq = NativeQQSettings.from_extra(extra)
        self._native_verified = False
        self._native_features: frozenset[str] = frozenset()

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
        # 主窗口 (see .main_window): groups do not operate the gateway.  Read
        # here so ``hermes status`` and the router agree with what
        # ``_build_message_event`` stamps on each event; a config reconcile
        # re-runs this constructor, so the two cannot drift.
        _mw_settings = main_window.settings_view(
            extra, getattr(config, "home_channel", None)
        )
        self.group_gateway_control_enabled = main_window.group_control_enabled(extra)
        self.main_window_chat_id = main_window.resolve_main_window(_mw_settings)
        self.router = ChannelRouter(
            group_keywords=self.group_keywords,
            group_replies_enabled=self.group_replies_enabled,
            direct_replies_enabled=self.direct_replies_enabled,
            group_commands_enabled=self.group_gateway_control_enabled,
            group_whitelist=self.group_whitelist,
            group_reply_policy=self.group_reply_policy,
            group_reply_cooldown_secs=self.group_reply_cooldown_secs,
            self_ids=list(self.self_ids),
            main_window_settings=_mw_settings,
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
        configured_account_probe_secs = max(
            0.0, _as_float(extra.get("account_probe_secs"), ACCOUNT_PROBE_SECS)
        )
        # Zero explicitly disables fallback account actions.  Positive values
        # have a modest floor so a typo such as 0.01 cannot turn a healthy
        # socket into a high-frequency get_status/get_login_info loop.
        self.account_probe_secs = (
            0.0
            if configured_account_probe_secs == 0.0
            else max(5.0, configured_account_probe_secs)
        )

        # ---- runtime state ----------------------------------------------
        self._client: Optional[OneBotClient] = None
        self._dispatch_task: Optional[asyncio.Task] = None
        self._backend_identity_task: Optional[asyncio.Task] = None
        self._backend = "unknown"
        self._backend_app_name: Optional[str] = None
        self._backend_app_version: Optional[str] = None
        #: Rate limiter for the inbound gate's DM notice, so someone spamming
        #: blocked messages cannot turn the bot into a refusal metronome.
        self._inbound_notices = NoticeCooldown()
        self._health_task: Optional[asyncio.Task] = None
        self._proactive_task: Optional[asyncio.Task] = None
        self._proactive_cancel: Optional[asyncio.Event] = None
        #: Resident proactive CALL lane — the twin of the pair above, for the
        #: lane that rings somebody instead of posting text.  Its own handles
        #: rather than a shared pair, because stopping one lane must not be
        #: able to stop the other by accident.
        self._proactive_call_task: Optional[asyncio.Task] = None
        self._proactive_call_cancel: Optional[asyncio.Event] = None
        #: Resident voice-call deadline sweeper.  ``voice_call`` is pure and
        #: fires nothing on its own, so without this loop a ring timeout, an
        #: idle timeout and a duration cap simply never happen.
        self._call_sweeper_task: Optional[asyncio.Task] = None
        self._call_sweeper_cancel: Optional[asyncio.Event] = None
        #: Persistent group-message archive (D3).  ``None`` until ``connect()``
        #: resolves a config that switches it on; see :mod:`.group_history`.
        self._history_writer: Optional[Any] = None
        self._turn_tasks: set = set()
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._dedup = MessageDeduplicator()
        self._group_names: Dict[int, str] = {}
        self._bot_nickname = str(extra.get("bot_nickname") or "").strip()
        self._account_online: Optional[bool] = None
        self._account_state = ACCOUNT_STATE_UNKNOWN
        self._account_status_reason: Optional[str] = None
        self._account_status_at_ms: Optional[int] = None
        self._last_account_probe_at: Optional[float] = None
        self._link_online = False

        logger.info(
            "OneBot adapter initialised: url=%s groups=%s whitelist=%s "
            "keywords=%d policy=%s cap=%d/%.0fs main_window=%s "
            "group_control=%s",
            self.ws_url or "<unset>",
            "enabled" if self.group_replies_enabled else "MUTED",
            "off" if self.group_whitelist is None else len(self.group_whitelist),
            len(self.group_keywords),
            self.group_reply_policy,
            self.group_window_max,
            self.group_window_secs,
            self.main_window_chat_id or "<unset>",
            "ON" if self.group_gateway_control_enabled else "locked",
        )
        # A group in the home-channel slot is where "reminders all go to the
        # group chat" comes from: bare ``deliver=onebot`` and every gateway
        # notification resolve through it.  The adapter does not silently
        # rewrite an operator's config — but it says so at WARNING, once per
        # start, because the symptom otherwise shows up as unexplained
        # restart notices in a room full of people.
        _home_id = main_window.home_channel_chat_id(_mw_settings)
        if main_window.is_group_chat_id(_home_id):
            logger.warning(
                "OneBot home_channel is GROUP %s — bare 'deliver=onebot' "
                "targets and gateway notifications will land in that group. "
                "Point home_channel at the private chat that owns this "
                "gateway (see main_window.py); a group id is refused as the "
                "main window, so commands stay locked there regardless.",
                _home_id,
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

    def _set_account_status(
        self,
        online: Optional[bool],
        *,
        at_ms: Optional[int] = None,
        reason: str = "probe",
    ) -> None:
        """Apply an explicit account signal and log only real transitions.

        ``None`` is deliberately ignored: a connected OneBot endpoint that
        has not emitted an account signal yet is *unknown*, not offline.  Once
        a heartbeat/notice/probe has established ``False``, an unknown sample
        cannot erase that evidence; only an explicit online signal can recover
        it.
        """
        if online is None:
            return
        online = bool(online)
        previous = self._account_online
        self._account_online = online
        self._account_state = (
            ACCOUNT_STATE_ONLINE if online else ACCOUNT_STATE_AUTH_REQUIRED
        )
        self._account_status_reason = str(reason or "probe")
        if at_ms is not None:
            self._account_status_at_ms = int(at_ms)
        # Probe results originate at the adapter, but the transport exposes
        # the same observation to callers of ``OneBotClient``.  Suppress the
        # callback here because this method is itself the callback target.
        client = self._client
        observe = getattr(client, "observe_account_status", None) if client else None
        if callable(observe):
            try:
                observe(
                    online,
                    now_ms=self._account_status_at_ms,
                    reason=self._account_status_reason,
                    notify=False,
                )
            except TypeError:
                # Compatibility with an older client double that does not yet
                # expose the optional ``notify`` keyword.
                try:
                    observe(
                        online,
                        now_ms=self._account_status_at_ms,
                        reason=self._account_status_reason,
                    )
                except Exception:  # noqa: BLE001 — observability only
                    pass
            except Exception:  # noqa: BLE001 — observability only
                pass
        if previous == online:
            return

        previous_state = (
            ACCOUNT_STATE_UNKNOWN
            if previous is None
            else ACCOUNT_STATE_ONLINE
            if previous is True
            else "auth-required"
        )

        if online:
            logger.info(
                "OneBot: QQ account state %s -> online (source=%s)",
                previous_state,
                self._account_status_reason,
            )
        else:
            logger.warning(
                "OneBot: QQ account state %s -> auth-required (source=%s); "
                "the WebSocket remains a transport link and will not be restarted",
                previous_state,
                self._account_status_reason,
            )
        self._publish_account_runtime_status()

    def _on_client_account_status(
        self, online: Optional[bool], at_ms: int, reason: str
    ) -> None:
        """Synchronous callback from :class:`OneBotClient`'s frame pump."""
        self._set_account_status(online, at_ms=at_ms, reason=reason)

    def _sync_account_status_from_client(self) -> Optional[bool]:
        """Mirror a transport double or client status into adapter state."""
        client = self._client
        if client is None:
            return self._account_online
        online = getattr(client, "last_status_online", None)
        client_status_at = getattr(client, "last_status_online_at_ms", None)
        # Do not let a stale compatibility-double value (or an old heartbeat
        # cached before a bot_offline notice) erase a known auth-required
        # state.  The real client invokes ``_on_client_account_status`` for a
        # fresh true heartbeat, while doubles without timestamps have no way
        # to prove that their true value is newer.
        stale_online = (
            online is True
            and self._account_online is False
            and (
                client_status_at is None
                or int(client_status_at) <= 0
                or (
                    self._account_status_at_ms is not None
                    and int(client_status_at) <= self._account_status_at_ms
                )
            )
        )
        if online is not None and online != self._account_online and not stale_online:
            self._set_account_status(
                online,
                at_ms=client_status_at,
                reason=getattr(client, "last_status_reason", None) or "heartbeat",
            )
        return self._account_online

    def _account_send_guard(self, target: Any, is_group: bool) -> Optional[SendResult]:
        """Refuse sends only after account offline is explicitly known."""
        if self._sync_account_status_from_client() is False:
            return account_auth_required_send_result(target, is_group)
        return None

    def _heartbeat_age_seconds(self, now_ms: Optional[int] = None) -> Optional[float]:
        """Age of the most recent heartbeat, or ``None`` before the first."""
        client = self._client
        at_ms = getattr(client, "last_heartbeat_at_ms", None) if client else None
        if at_ms is None:
            # Compatibility for old client doubles: an explicit status sample
            # was historically the only timestamp they exposed.
            at_ms = getattr(client, "last_status_online_at_ms", None) if client else None
        if at_ms is None:
            return None
        current = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return max(0.0, (current - int(at_ms)) / 1000.0)

    def _publish_account_runtime_status(self, *, link_online: Optional[bool] = None) -> None:
        """Project OneBot account health onto the existing runtime contract.

        ``gateway.status`` intentionally has a small cross-platform schema
        (state/error code/message).  Keep that contract intact while making
        the account-vs-transport distinction and heartbeat timing visible in
        the operational error text.  The structured details remain available
        from :meth:`health_snapshot` for consumers that need numeric fields.
        """
        if link_online is None:
            link_online = bool(self._client is not None and self._client.connected)
        if self._account_online is False:
            platform_state = "auth_required"
            error_code = ACCOUNT_AUTH_REQUIRED_REASON
            prefix = "QQ account is offline/auth-required; manual login is required"
        elif not link_online:
            platform_state = "disconnected"
            error_code = "onebot_link_offline"
            prefix = "OneBot WebSocket link is offline; reconnect is in progress"
        elif self._account_online is True:
            platform_state = "connected"
            error_code = None
            prefix = "QQ account is online"
        else:
            # Unknown during startup is not a degraded platform state.  The
            # base adapter's connected marker remains authoritative until a
            # real account signal arrives.
            platform_state = "connected"
            error_code = None
            prefix = "QQ account status is unknown (awaiting heartbeat/probe)"
        heartbeat_age = self._heartbeat_age_seconds()
        heartbeat_age_text = (
            "unknown" if heartbeat_age is None else f"{heartbeat_age:.1f}s"
        )
        status_at = (
            "unknown"
            if self._account_status_at_ms is None
            else str(self._account_status_at_ms)
        )
        message = (
            f"{prefix}; source={self._account_status_reason or 'none'}; "
            f"last_status_at_ms={status_at}; heartbeat_age={heartbeat_age_text}; "
            f"link_online={link_online}"
        )
        self._write_runtime_status_safe(
            "account_status",
            platform_state=platform_state,
            error_code=error_code,
            error_message=message,
        )

    def health_snapshot(self) -> Dict[str, Any]:
        """Two different questions, two different answers.

        ``link_online`` is "is the WebSocket up"; ``account_online`` is "is
        the QQ account actually logged in".  They diverge exactly when the
        account gets kicked offline — the socket keeps heartbeating while the
        bot silently stops being able to say anything.
        """
        client = self._client
        last_status_at_ms = (
            self._account_status_at_ms
            if self._account_status_at_ms is not None
            else getattr(client, "last_status_online_at_ms", None)
        )
        last_heartbeat_at_ms = (
            getattr(client, "last_heartbeat_at_ms", None) if client else None
        )
        if last_heartbeat_at_ms is None:
            last_heartbeat_at_ms = last_status_at_ms
        heartbeat_age = self._heartbeat_age_seconds()
        # Keep the public snapshot truthful even for a compatibility client
        # double (or an embedding that sets the boolean directly) that has not
        # populated the private derived-state field yet.
        account_state = (
            ACCOUNT_STATE_AUTH_REQUIRED
            if self._account_online is False
            else ACCOUNT_STATE_ONLINE
            if self._account_online is True
            else ACCOUNT_STATE_UNKNOWN
        )
        return {
            "platform": PLATFORM_NAME,
            "url": self.ws_url,
            "link_online": bool(client is not None and client.connected),
            "account_online": self._account_online,
            "account_state": account_state,
            "account_status": account_state,
            "account_auth_required": self._account_online is False,
            "auth_required": self._account_online is False,
            "account_status_reason": self._account_status_reason,
            "last_status_online_at_ms": last_status_at_ms,
            "last_status_at_ms": last_status_at_ms,
            "last_heartbeat_at_ms": last_heartbeat_at_ms,
            "heartbeat_age_seconds": heartbeat_age,
            "heartbeat_age_s": heartbeat_age,
            "last_event_at_ms": client.last_event_at_ms if client else None,
            "inbound_dropped": client.inbound_dropped_count if client else 0,
            "outbound_queue_depth": client.outbound_queue_depth if client else 0,
            "self_id": self.self_id,
            "backend": self._backend,
            "native_qq_enabled": self._native_qq.enabled,
            "native_qq_verified": self._native_verified,
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
            operator has to restart the gateway by hand after a backend restart.
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
                    native_delivery=self._native_qq.enabled,
                    call_timeout=self._native_qq.send_timeout_seconds if self._native_qq.enabled else 15.0,
                ),
                on_self_id=self._on_self_id,
            )
        except OneBotConfigError as exc:
            logger.error("OneBot: %s", exc)
            return False

        # Install this after construction rather than adding a required
        # constructor argument at the call site: a few integrations provide a
        # tiny OneBotClient-compatible double that only accepts ``on_self_id``.
        # The real client invokes the callback for heartbeat and
        # ``bot_offline`` evidence as soon as a frame arrives.
        set_status_callback = getattr(client, "set_account_status_callback", None)
        if callable(set_status_callback):
            set_status_callback(self._on_client_account_status)

        auth_failed = await self._preflight()
        if auth_failed:
            # A rejected handshake is not something the ladder can fix, and
            # the WebUI credential and OneBot token are easy to mix up on both
            # NapCat and SnowLuma — say so loudly instead of reconnecting
            # forever.
            self._set_fatal_error(
                "onebot_auth_rejected",
                (
                    f"OneBot rejected the handshake at {self.ws_url}. Check "
                    "ONEBOT_ACCESS_TOKEN — the OneBot access token is NOT the "
                    "same secret as the NapCat WebUI token or SnowLuma WebUI "
                    "password."
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
        self._start_proactive_call_loop()
        self._start_call_sweeper()
        self._start_group_history()
        # Publish for the synchronous tool layer (tools/onebot_client.py) so
        # QQ-borrowing tools reuse this connection instead of opening a
        # second one against the same backend.
        set_live_client(client, asyncio.get_running_loop())
        # ...and the adapter one layer up, for the tools that need more than
        # the socket.  Without this the model-facing voice tools fall back to
        # the raw transport, which has neither the emergency mute nor the
        # Tencent content policy on it — the two guarantees ``voice.speak``
        # exists to apply.  That fallback is correct under CLI/cron, where no
        # gateway runs; here it would be a silent downgrade.
        set_live_adapter(self, asyncio.get_running_loop())
        self._mark_connected()
        self._backend = "unknown"
        self._backend_app_name = None
        self._backend_app_version = None
        self._backend_identity_task = asyncio.create_task(
            self._identify_backend(client), name="onebot-backend-identity"
        )
        logger.info("OneBot: adapter connected (%s)", self.ws_url)
        return True

    async def _identify_backend(self, client: OneBotClient) -> None:
        """Best-effort probe of the OneBot implementation behind the socket.

        The client itself owns the reconnect ladder, so this is deliberately a
        background action rather than part of :meth:`connect`.  A backend that
        is slow, offline, or does not implement ``get_version_info`` must not
        delay inbound message handling or make an otherwise usable channel
        fail to start.
        """
        self._native_verified = False
        self._native_features = frozenset()
        self._backend = "unknown"
        self._backend_app_name = None
        self._backend_app_version = None
        try:
            response = await client.call_action(
                protocol.RawAction("get_version_info"), timeout=10.0
            )
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, dict):
                raise RuntimeError("get_version_info returned no data object")
            self._backend_app_name = str(data.get("app_name") or "").strip()
            self._backend_app_version = str(data.get("app_version") or "").strip()
            self._backend = _normalise_backend(self._backend_app_name)
            if self._native_qq.enabled:
                features = native_features(data)
                if features is None:
                    raise RuntimeError("Native QQ backend capabilities were not verified")
                if self._native_qq.guard:
                    policy_result = await client.call_action(protocol.RawAction(
                        "set_native_policy", {"policy": self._native_qq.guard}
                    ), timeout=10.0)
                    if not _response_ok(policy_result):
                        raise RuntimeError("Native QQ send policy was not accepted")
                self._native_features = features
                self._native_verified = True
            logger.info(
                "OneBot: backend identified as %s (app_name=%s app_version=%s)",
                self._backend,
                self._backend_app_name or "<unknown>",
                self._backend_app_version or "<unknown>",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — identity is diagnostic only
            logger.debug("OneBot: backend identity probe failed: %s", exc)

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

    def _start_proactive_call_loop(self) -> None:
        """Spawn the resident proactive CALL lane.

        ``proactive.proactive_call_loop`` is the structural twin of
        ``proactive_loop`` for the lane that rings a person instead of posting
        text.  Nothing started it, which made a whole resident loop dead code;
        it is started here, on the same terms and for the same reasons its
        sibling above is:

        * always started, even when ``proactive_call_enabled`` is false — the
          loop re-resolves the ``proactive_call_*`` block every beat and idles
          on the cheap 60-second re-check, which is what lets an operator turn
          calling on (or off) without restarting the channel;
        * ``cfg`` resolved once *here* rather than left to the loop, because
          the loop's own docstring says the argument is only the INITIAL
          resolve — it is what the lane logs itself as having started as, and
          every beat re-reads the live settings regardless.

        Imported lazily and wrapped, exactly like :meth:`_start_proactive_loop`:
        :mod:`.proactive` imports this module, so a module-scope import would
        be circular, and a broken optional lane must never cost the channel its
        QQ connection.
        """
        if (
            self._proactive_call_task is not None
            and not self._proactive_call_task.done()
        ):
            # ``connect()`` also runs on reconnect.  A second caller is worse
            # than the second speaker the guard above prevents: both would draw
            # their own gaps and place their own invites, so a real person's
            # phone rings twice for one decision to call them.
            return
        self._proactive_call_cancel = asyncio.Event()
        try:
            from . import proactive as _proactive
        except Exception:  # noqa: BLE001 — a broken optional loop must not kill the channel
            logger.exception(
                "OneBot: proactive calling unavailable — continuing without it"
            )
            return
        self._proactive_call_task = asyncio.create_task(
            _proactive.proactive_call_loop(
                self,
                self._proactive_call_cancel,
                _proactive.live_call_config(self),
            ),
            name="onebot-proactive-call",
        )

    def _start_call_sweeper(self) -> None:
        """Spawn the resident voice-call deadline sweeper.

        Always started, even when ``voice_call_enabled`` is false, for the two
        reasons :meth:`_start_proactive_loop` gives and one more that is
        specific to calls: a session that was live when the operator switched
        the feature OFF has to be collected (``EndReason.DISABLED``), and a
        loop that returned early on a disabled config would leave that session
        as a zombie nothing ever ends.

        Imported lazily, because :mod:`.voice_call` imports this module.
        """
        if self._call_sweeper_task is not None and not self._call_sweeper_task.done():
            # ``connect()`` also runs on reconnect.  Two sweepers would each
            # resolve the config and each perform the same expiry, so a single
            # timeout would speak its sign-off twice.
            return
        if _call_module() is None:
            return
        self._call_sweeper_cancel = asyncio.Event()
        self._call_sweeper_task = asyncio.create_task(
            self._call_sweeper_loop(self._call_sweeper_cancel),
            name="onebot-voice-call",
        )

    async def _call_sweeper_loop(self, cancel: asyncio.Event) -> None:
        """Fire crossed call deadlines on a timer, forever.

        ``CALL_SWEEP_SECS`` is re-read every beat rather than captured once,
        so the tick is a live value like every other setting in this adapter.
        """
        while not cancel.is_set():
            try:
                await asyncio.wait_for(cancel.wait(), timeout=CALL_SWEEP_SECS)
                return
            except (asyncio.TimeoutError, TimeoutError):
                pass
            except asyncio.CancelledError:
                return
            try:
                await self._sweep_calls()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad beat must not end the loop
                logger.exception("OneBot: voice call sweep beat failed")

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
        self,
        event: protocol.MessageEvent,
        sender_name: str,
        *,
        text: Optional[str] = None,
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

        *text* overrides the body, and is how the inbound gate keeps a blocked
        message out of the archive.  It matters more here than in the in-memory
        buffer: this table is the 24h window three cron monitors read back and
        **summarise**, so an unredacted row is content the bot may later restate
        in a message it sends itself.  ``None`` keeps the original behaviour for
        the direct callers in the tests.
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
                text=(
                    text
                    if text is not None
                    else protocol.segments_to_text(event.message) or event.raw_message
                ),
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
        set_live_adapter(None)
        if self._proactive_cancel is not None:
            # Ask first, cancel second: a beat that is mid-send should finish
            # writing rather than leave half a message in the group.
            self._proactive_cancel.set()
        if self._proactive_call_cancel is not None:
            # Same order, same reason, higher stakes: a beat that is already
            # placing a call should finish placing it rather than be cut off
            # mid-invite, leaving a session half-open and a phone ringing at
            # somebody nothing will ever answer.
            self._proactive_call_cancel.set()
        if self._call_sweeper_cancel is not None:
            # Same order, same reason: a beat that is mid-sign-off should get
            # the words out rather than hang up on somebody in silence.
            self._call_sweeper_cancel.set()
        for task in (
            self._dispatch_task,
            self._backend_identity_task,
            self._health_task,
            self._proactive_task,
            self._proactive_call_task,
            self._call_sweeper_task,
        ):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._dispatch_task = None
        self._backend_identity_task = None
        self._health_task = None
        self._proactive_task = None
        self._proactive_cancel = None
        self._proactive_call_task = None
        self._proactive_call_cancel = None
        self._call_sweeper_task = None
        self._call_sweeper_cancel = None
        await self._reply_batcher.close()
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

    def _apply_probe_response(
        self, action_name: str, response: Any, *, at_ms: Optional[int] = None
    ) -> bool:
        """Apply one successful account probe; return whether it was useful.

        ``get_status`` is preferred because it can explicitly report
        ``online=false``.  A successful ``get_login_info`` with a data object
        is the fallback many backends support when ``get_status`` is absent.
        A malformed/failed response does not turn an unknown account into
        offline: startup must retain the historical send behaviour until real
        evidence exists.
        """
        online, conflicted = self._probe_response_evidence(action_name, response)
        if online is None or conflicted:
            return False
        if at_ms is None:
            at_ms = int(time.time() * 1000)
        self._set_account_status(online, at_ms=at_ms, reason=action_name)
        return True

    @staticmethod
    def _probe_response_evidence(
        action_name: str, response: Any
    ) -> Tuple[Optional[bool], bool]:
        """Return ``(online, conflicted)`` from one completed probe action.

        Invalid/failed envelopes carry no evidence.  A conflict is stronger:
        it poisons the whole probe cycle so another action's first-looking
        signal cannot restore the account online over contradictory data.
        """
        if not _probe_response_ok(response):
            return None, False
        data = response.get("data")
        if not isinstance(data, dict):
            return None, False
        online, conflicted = _response_online_evidence(response)
        if conflicted:
            return None, True
        if action_name != "get_login_info":
            # ``get_status`` without an explicit boolean is transport
            # evidence only, not account evidence.
            return online, False

        # A completed login-info response proves an account only when it
        # identifies a real positive UIN.  ``bool`` must be rejected before
        # the integer branch because ``True == 1`` in Python.
        user_id = data.get("user_id")
        if isinstance(user_id, bool):
            valid_user_id = False
        elif isinstance(user_id, int):
            valid_user_id = user_id > 0
        elif isinstance(user_id, str):
            normalized_user_id = user_id.strip()
            valid_user_id = (
                normalized_user_id.isdecimal() and int(normalized_user_id) > 0
            )
        else:
            valid_user_id = False
        if online is False:
            if valid_user_id:
                # A positive identity and explicit offline flag in the same
                # response disagree; neither wins.
                return None, True
            # Offline is explicit negative evidence and does not require a
            # logged-in identity.  Keeping it means a get_status=true /
            # get_login_info=false pair is still detected as a cross-action
            # conflict even when the offline response has no user id.
            return False, False
        if not valid_user_id:
            return None, False
        return True, False

    async def _probe_account(self, client: Any) -> None:
        """Refresh account state without reconnecting a healthy WebSocket."""
        if client is None or not getattr(client, "connected", False):
            return
        observations: List[Tuple[str, bool]] = []
        conflicted = False
        # Always collect both supported account views before applying either.
        # Apart they look authoritative; together they can expose a backend in
        # transition (one says online while the other says offline).
        for action_name in ("get_status", "get_login_info"):
            try:
                response = await client.call_action(
                    protocol.RawAction(action_name), timeout=10.0
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — failure is not auth proof
                logger.debug("OneBot: %s account probe failed: %s", action_name, exc)
                continue
            online, response_conflicted = self._probe_response_evidence(
                action_name, response
            )
            conflicted = conflicted or response_conflicted
            if online is not None:
                observations.append((action_name, online))

        values = {online for _action_name, online in observations}
        if conflicted or len(values) != 1:
            if conflicted or len(values) > 1:
                logger.warning(
                    "OneBot: conflicting account probe evidence; preserving %s state",
                    self._account_state,
                )
            return
        if not observations:
            return
        online = observations[0][1]
        # Prefer get_status as the audit source when both actions agree; it is
        # the direct account-state action rather than an identity inference.
        reason = next(
            (
                action_name
                for action_name, value in observations
                if action_name == "get_status" and value is online
            ),
            observations[0][0],
        )
        self._set_account_status(
            online, at_ms=int(time.time() * 1000), reason=reason
        )

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
            # A callback from the reader handles normal heartbeat/notice
            # frames immediately.  This sync also covers old client doubles and
            # status values observed just before the health tick.
            self._sync_account_status_from_client()
            now_mono = time.monotonic()
            if self.account_probe_secs > 0.0 and (
                self._last_account_probe_at is None
                or now_mono - self._last_account_probe_at >= self.account_probe_secs
            ):
                self._last_account_probe_at = now_mono
                await self._probe_account(client)
                self._sync_account_status_from_client()
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
            # Keep the operational record's heartbeat age fresh without
            # logging a duplicate state transition every health tick.
            self._publish_account_runtime_status(link_online=link_online)

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

        # ── The inbound gate ──────────────────────────────────────────────
        # Screened here, before the message is written anywhere, because the
        # three things that consume it downstream all persist it: the room
        # buffer replays it into later prompts, the archive feeds the cron
        # summarisers, and the turn hands it to the model.  Redacting at the
        # point of *use* would still leave the raw text sitting in two stores.
        #
        # Deterministic only — no model call.  See ``.inbound_gate`` for why
        # (briefly: the outbound agent gate already reads everything the bot
        # says, and a second LLM call here would roughly double reply latency
        # to re-check text that the outbound gate will see the consequences of
        # anyway).
        inbound_cfg = resolve_inbound_gate_config(
            (_gh_live_extra(self) or {}).get("inbound_gate")
        )
        screened = screen_inbound(
            event.raw_message or protocol.segments_to_text(event.message), inbound_cfg
        )
        if not screened.allowed:
            log_inbound_blocked(
                screened,
                is_group=is_group,
                where=(event.group_id if is_group else event.user_id),
            )

        # Snapshot the room BEFORE this message joins it (see .reply_context).
        # Taken here rather than at event-build time for two reasons: this is
        # the only point at which "everything except the trigger" is exactly
        # the buffer's contents, and messages that arrive while the turn is
        # being assembled must not leak into a window that is supposed to end
        # at the message being answered.
        room_before: List[Tuple[float, str, str, bool]] = []
        if is_group and event.group_id is not None:
            room_before = recent_group_messages(self.instance_id, event.group_id)
            # Feed the context buffer BEFORE the gate: a persona should see
            # the whole room, not only the messages it chose to answer.
            record_group_message(
                self.instance_id,
                event.group_id,
                sender_name,
                # The screened form, never the raw one.  This buffer is replayed
                # into every later prompt for this group, so storing the raw text
                # of a blocked message would reintroduce it on the next turn —
                # exactly the poisoning the gate exists to stop.
                screened.text,
                False,
            )
            # ...and the PERSISTENT archive, for the same reason and at the
            # same point.  The two are complements, not duplicates: the buffer
            # above is 30 rows of 200 chars in RAM, sized to be a proactive
            # turn's prompt context and lost on restart; this one survives
            # restarts and holds the 24h window three cron monitors summarise.
            self._record_group_history(event, sender_name, text=screened.text)

        # A live call answers before the router does.  Placed here — after the
        # buffers, before the gate — because a call IS the addressing: someone
        # whose phone we rang answers "好啊", and demanding an @mention on top
        # of that would leave the call ringing forever.  With no session this
        # returns immediately and nothing below changes.
        if await self._route_call_inbound(event, is_group):
            return

        # Blocked inbound stops here.  The buffers above already hold the
        # redacted form, so the room stays coherent; what does not happen is a
        # turn.  Groups get silence, never a refusal: answering "这个话题不适合
        # 在 QQ 上讨论" in a room where someone just posted bait is the bot
        # publicly attaching itself to that message, visible to every member.
        # A DM gets one notice, rate-limited, because there a silent bot just
        # looks broken to the one person watching.
        if not screened.allowed:
            if not is_group and inbound_cfg.dm_notice:
                if self._inbound_notices.should_notify(
                    str(event.user_id),
                    time.time(),
                    inbound_cfg.notice_cooldown_secs,
                ):
                    await self.send(
                        format_chat_id(False, event.user_id),
                        content_policy.QQ_SAFE_REFUSAL_TEXT,
                    )
            return

        continuation = self._reply_batcher.contains(event)
        routed = self.router.dispatch(event, continuation=continuation)
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

        seconds = reply_batch.quiet_seconds(_gh_live_extra(self))
        control = routed.is_command and main_window.gateway_control_allowed(
            self._main_window_settings(),
            chat_id=event.group_id if is_group else event.user_id,
            is_group=is_group,
        )
        if control:
            command = routed.content.strip().split()[0].lower()
            if command in {"/stop", "/new", "/reset", "/clear", "/restart"}:
                self._reply_batcher.discard_conversation(event)
        elif seconds > 0 or continuation:
            self._reply_batcher.enqueue(event, routed.content, room_before, seconds)
            return

        hermes_event = await self._build_message_event(
            event, routed.content, room_before=room_before
        )
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

    def _main_window_settings(self) -> Mapping[str, Any]:
        """Live settings as :mod:`.main_window` needs to see them.

        ``_gh_live_extra`` alone would miss a YAML ``home_channel`` (the core
        loader parses that into ``PlatformConfig.home_channel``, not into
        ``extra``), so the fallback is folded back in here — once, for every
        caller.
        """
        return main_window.settings_view(
            _gh_live_extra(self), getattr(self.config, "home_channel", None)
        )

    async def _build_message_event(
        self,
        event: protocol.MessageEvent,
        routed_text: str,
        *,
        room_before: Optional[Sequence[Tuple[float, str, str, bool]]] = None,
    ) -> MessageEvent:
        """Translate an OneBot message into the Hermes event shape.

        *room_before* is the per-group chatter buffer as it stood just before
        this message was recorded into it — see :mod:`.reply_context` for why
        the snapshot is taken by the caller rather than read here.  ``None``
        means "no room context", which is what every non-group event and every
        direct test caller gets.
        """
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
            # What the room said between our last post and this one, in the
            # same field Discord/Slack/relay fill (see .reply_context).  A DM
            # never gets one: its whole history is already the conversation.
            channel_context=(
                reply_context.render_context(
                    room_before or (),
                    reply_context.limit_from_extra(_gh_live_extra(self)),
                    sender_id=str(event.user_id),
                )
                if is_group
                else None
            ),
            metadata={
                "onebot_self_id": self_id,
                "onebot_group_id": event.group_id,
                # The uin to @ when replying in a group.
                "onebot_at_user_id": str(event.user_id),
                # …and the message to quote.  Carried as metadata rather than
                # threaded through the gateway's send signature because that
                # is already how the @mention target travels, and because it
                # keeps the anchor attached to the turn it belongs to: a send
                # that did not come from this event simply has no key here.
                REPLY_ANCHOR_META: str(event.message_id),
                "onebot_message_type": event.message_type.value,
            },
            # 主窗口 (see .main_window).  ``False`` makes
            # ``MessageEvent.is_command()`` false, so this text can never
            # reach a slash-command path — the group lock is the absence of
            # the capability, not a denial message.  Every group event gets
            # ``False`` unless ``group_gateway_control_enabled``; a DM gets
            # ``True`` only when it IS the main window (or when no main
            # window is configured at all, which leaves DMs as they were).
            allow_gateway_control=main_window.gateway_control_allowed(
                self._main_window_settings(),
                chat_id=event.group_id if is_group else event.user_id,
                is_group=is_group,
            ),
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
                    cached = await self._cache_voice(ref)
                    if cached is None:
                        continue
                    path, mime = cached
                    media_types.append(mime)
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

    async def _cache_voice(self, ref: protocol.MediaRef) -> Optional[Tuple[str, str]]:
        """Cache one inbound voice note; return ``(local_path, mime)``.

        Two shapes arrive.  gocq hands us a URL and this is an ordinary
        download.  The backend may hand us a *name-only* ``record`` — no URL,
        just the ``file`` id — which ``protocol.segments_to_media`` now lets
        through with an empty ``url`` precisely so it reaches this branch;
        before that the voice note was dropped with no attachment and no log
        line, so the user's message simply arrived empty.

        ``None`` means "the bytes are not obtainable": the caller skips the
        attachment and the turn still runs, so an unfetchable voice note
        degrades to a visible gap rather than a crash or a silent drop.
        """
        hint = Path(ref.file_name or "").suffix
        if ref.url:
            fallback = hint or _VOICE_FALLBACK_EXT
            path = await cache_audio_from_url(ref.url, ext=fallback)
            # A suffix the segment itself named is evidence; our own
            # ``.amr`` placeholder is not.
            return path, _voice_mime(path, "" if hint else fallback)
        file_id = (ref.file_name or "").strip()
        if not file_id:
            return None
        path = await self._resolve_record(file_id)
        if path is None:
            return None
        # Everything reaching here was either sniffed from the bytes or named
        # by the backend, so the suffix is worth asserting.
        return path, _voice_mime(path)

    async def _resolve_record(self, file_id: str) -> Optional[str]:
        """Resolve a url-less voice note through the ``get_record`` action.

        What lands in ``data`` is backend-dependent, so take the shapes in
        the order they are actually usable to us:

        1. ``url`` — an ordinary download, SSRF-checked like any other media.
        2. ``base64`` — the bytes inline; decode straight into the cache.
        3. ``file`` — a path on the **backend's** filesystem.  The backend
           may run in another container or on another host (the same reason
           the outbound path hands it ``base64://`` instead of a path, see
           ``protocol.UploadPrivateFile``), so this is usable only when the
           path happens to resolve here too.  Never trust it blind: a path
           that does not exist would enter ``media_urls`` as a phantom
           attachment and blow up much later, far from the cause.

        ``None`` when none of the three yields bytes — including a go-cqhttp
        build that rejects ``get_record`` outright, which is why the call is
        wrapped the way ``_refresh_group_name`` wraps its own optional
        action.  One warning, no exception.
        """
        try:
            resp = await self._call(
                protocol.GetRecord(file=file_id, out_format=_VOICE_OUT_FORMAT),
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001 — gocq may not have the action
            logger.warning("OneBot: get_record failed for voice %s (%s)", file_id, exc)
            return None
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict) or not _response_ok(resp):
            logger.warning(
                "OneBot: get_record did not resolve voice %s (%s)",
                file_id,
                _response_error(resp)
                if isinstance(resp, dict)
                else type(resp).__name__,
            )
            return None

        url = str(data.get("url") or "").strip()
        if url:
            return await cache_audio_from_url(url, ext=f".{_VOICE_OUT_FORMAT}")

        payload = data.get("base64")
        if isinstance(payload, str) and payload.strip():
            # Some backends prefix a data URI; keep only the payload.
            raw = base64.b64decode(payload.strip().split("base64,", 1)[-1])
            return cache_audio_from_bytes(raw, ext=f".{_VOICE_OUT_FORMAT}")

        local = str(data.get("file") or "").strip()
        if local:
            local_path = Path(local[7:] if local.startswith("file://") else local)
            if local_path.is_file():
                return cache_audio_from_bytes(
                    local_path.read_bytes(),
                    ext=local_path.suffix or f".{_VOICE_OUT_FORMAT}",
                )
            logger.warning(
                "OneBot: get_record answered voice %s with %s, a path on the "
                "backend's host that does not exist here — dropping the "
                "attachment rather than pointing the agent at an unreadable file",
                file_id,
                local,
            )
            return None

        logger.warning(
            "OneBot: get_record returned no url/base64/file for voice %s (keys=%s)",
            file_id,
            sorted(data),
        )
        return None

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
    # Voice calls
    # ------------------------------------------------------------------
    #
    # :mod:`.voice_call` decides and :mod:`.voice` speaks; everything here is
    # the wire between them.  Four seams, and each one is load-bearing:
    #
    # * ``_route_call_inbound`` — the state decides what a word means, so
    #   "好啊" to a ringing phone is an ANSWER and never also a chat turn;
    # * ``_speak_call_reply`` — while the call is up, this chat hears us
    #   instead of reading us.  Hooked into ``send()``, the one outbound choke
    #   point both the reply lane and the proactive lane pass through;
    # * ``_sweep_calls`` — deadlines, on the resident timer AND right after a
    #   turn, because a ``max_turns`` cap is crossed BY the turn;
    # * ``start_voice_call`` / ``end_voice_call`` — the only way a session is
    #   ever born.  There is deliberately no natural-language "they want a
    #   call" detector: a false positive rings a real person unprompted, and
    #   that is not a mistake an apology undoes.  ``voice_call.offer()`` — the
    #   "they asked for it" direction — is consequently unused for now.

    async def _route_call_inbound(
        self, event: protocol.MessageEvent, is_group: bool
    ) -> bool:
        """Hand one inbound message to the call, if this chat has one.

        ``True`` means the call consumed it and no agent turn should run.

        Three answers, and the difference between them is the whole point of
        routing through the state machine rather than matching words here:

        * **no session** — ``False`` immediately, before any text is even
          extracted.  This is today's path, byte for byte;
        * **an answer** (accept / decline / hangup) — ``True``.  Running a
          turn as well would answer "好啊" twice, once as a call and once as
          conversation;
        * **an ordinary word during an ACTIVE call** — ``False``.  It really
          is a chat turn; we only note it so the idle deadline and the turn
          counter move.
        """
        vc = _call_module()
        if vc is None:
            return False
        chat_id = format_chat_id(
            is_group, event.group_id if is_group else event.user_id
        )
        key = speech_key(self.instance_id, chat_id)
        if vc.snapshot(key) is None:
            return False

        # The mention-stripped text, the same string the model would see: in a
        # group the answer usually arrives as "@bot 好啊", and the raw CQ code
        # in front of it matches no intent pattern at all.
        self_id = event.self_id or (self.self_id or 0)
        text = protocol.strip_self_mention(event.message, self_id).strip()
        if not text:
            text = (event.raw_message or "").strip()

        decision = vc.on_inbound(key, text, time.monotonic())
        if not decision.ok:
            # NO_SESSION / UNCLEAR / WRONG_STATE all mean "not mine".
            return False
        await self._perform_call_decision(decision, chat_id, str(event.user_id or ""))
        # Their turn may have been the one that crossed a cap.
        await self._sweep_calls()
        return not (
            decision.state is vc.CallState.ACTIVE
            and decision.action is vc.CallAction.NOTHING
        )

    async def _speak_call_reply(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> Optional[SendResult]:
        """Answer with our voice while a call is ACTIVE.

        ``None`` means "type it instead", and it covers both the ordinary case
        (no call here) and the degraded one (TTS is down).  Degrading to text
        is deliberate: a call that fell back to typing still answered, while a
        dropped reply leaves a person listening to silence.  A *decision* —
        the emergency mute or the content policy — comes back as its canonical
        :class:`SendResult` instead, markers intact, because those must never
        be retried as text.
        """
        vc = _call_module()
        if vc is None:
            return None
        key = speech_key(self.instance_id, chat_id)
        session = vc.snapshot(key)
        if session is None or session.state is not vc.CallState.ACTIVE:
            return None
        script = voice_script(content)
        if not script:
            return None
        try:
            from . import voice as _voice  # noqa: PLC0415 — .voice imports this module
        except Exception:  # noqa: BLE001
            logger.exception("OneBot: voice output unavailable — answering in text")
            return None

        outcome = await _voice.speak(
            self, chat_id, script, reply_to=reply_to, metadata=metadata
        )
        if outcome.spoke:
            vc.note_local_turn(key, script, time.monotonic())
            # Our own bubble counts against ``voice_call_max_turns`` too, so
            # sweep now rather than let the cap wait out a tick.
            await self._sweep_calls()
            return SendResult(
                success=True,
                message_id=outcome.message_id,
                continuation_message_ids=tuple(outcome.message_ids[:-1]),
                raw_response={"message_ids": list(outcome.message_ids)},
            )
        if outcome.failure is not None:
            return outcome.failure
        logger.warning(
            "OneBot: voice call in %s fell back to text (%s)",
            chat_id,
            outcome.reason or "speech synthesis failed",
        )
        return None

    async def _call_speak(self, chat_id: str, line: str) -> None:
        """The voice injected into :func:`voice_call.perform`.

        Straight to ``voice.speak`` rather than through :meth:`send`, because
        most of these lines are spoken when the session is NOT active — the
        ring, and every sign-off, which ``voice_call`` emits after it has
        already removed the session.  The same honest degradation applies: a
        ring nobody can hear is a call that never happened.
        """
        try:
            from . import voice as _voice  # noqa: PLC0415 — .voice imports this module

            outcome = await _voice.speak(self, chat_id, line)
        except Exception:  # noqa: BLE001 — degrade, do not lose the line
            logger.exception("OneBot: voice call line could not be spoken")
            outcome = None
        if outcome is not None and (outcome.spoke or outcome.failure is not None):
            # Spoken, or refused by the mute / the content policy — and a
            # refusal must not be re-sent as text through the back door.
            return
        if outcome is not None:
            logger.warning(
                "OneBot: voice call line fell back to text in %s (%s)",
                chat_id,
                outcome.reason or "speech synthesis failed",
            )
        await self.send(chat_id, line, metadata={VOICE_FALLBACK_META: True})

    async def _call_poke(self, chat_id: str, peer: str) -> None:
        """The 戳一戳 nudge that stands in for a ringing phone."""
        is_group, target = parse_chat_id(chat_id)
        raw_peer = str(peer or "").strip()
        if raw_peer.isdigit():
            user_id = int(raw_peer)
        elif is_group:
            # A group call has no single person to nudge.  Skip it; ``perform``
            # treats a missing poke as decoration and speaks anyway.
            return
        else:
            user_id = target
        await self._call(
            protocol.SendPoke(user_id=user_id, group_id=target if is_group else None),
            timeout=10.0,
        )

    async def _perform_call_decision(
        self, decision: Any, chat_id: str, peer: str
    ) -> bool:
        """Put one :class:`voice_call.CallDecision` on the wire."""
        vc = _call_module()
        if vc is None:
            return False

        async def _speak(line: str) -> None:
            await self._call_speak(chat_id, line)

        async def _poke() -> None:
            await self._call_poke(chat_id, peer)

        try:
            return await vc.perform(decision, speak=_speak, poke=_poke)
        except Exception:  # noqa: BLE001 — a lost line is not a lost channel
            logger.exception("OneBot: a voice call line did not go out")
            return False

    async def _sweep_calls(self) -> None:
        """Collect every deadline THIS instance has crossed, and sign off.

        ``instance_id`` is a correctness guard, not decoration: two configured
        OneBot instances each run a sweeper with its own resolved config, and
        an unfiltered sweep would judge the other instance's calls by the
        wrong timeouts.
        """
        vc = _call_module()
        if vc is None:
            return
        try:
            expired = vc.sweep(
                time.monotonic(),
                vc.live_config(self),
                instance_id=self.instance_id,
            )
        except Exception:  # noqa: BLE001 — never let a sweep end the channel
            logger.exception("OneBot: voice call sweep failed")
            return
        for item in expired:
            session = item.decision.session
            if session is None or not session.chat_id:  # pragma: no cover
                continue
            await self._perform_call_decision(
                item.decision, session.chat_id, session.peer
            )

    async def start_voice_call(self, chat_id: str, *, peer: str = "") -> Optional[Any]:
        """Place a call into *chat_id*: ring, then wait for them to answer.

        The programmatic entry point, for the proactive-call lane and for a
        model-facing tool.  *peer* is the QQ uin the ``voice_call_allowed_users``
        filter is evaluated against; in a DM it defaults to the other party,
        and in a group it must be named (a non-empty allowlist plus an unknown
        peer is a refusal, by design).

        Returns the :class:`~.voice_call.CallDecision` — refusals included, so
        a caller can tell "disabled" from "already live" — or ``None`` when
        the call layer is unavailable or the chat id will not parse.
        """
        vc = _call_module()
        if vc is None:
            return None
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError as exc:
            logger.warning("OneBot: cannot place a voice call to %r (%s)", chat_id, exc)
            return None
        peer_id = str(peer or "").strip() or ("" if is_group else str(target))
        decision = vc.invite(
            speech_key(self.instance_id, chat_id),
            chat_id=chat_id,
            is_group=is_group,
            peer=peer_id,
            now=time.monotonic(),
            cfg=vc.live_config(self),
        )
        if decision.ok:
            await self._perform_call_decision(decision, chat_id, peer_id)
        return decision

    async def end_voice_call(
        self, chat_id: str, *, reason: Optional[Any] = None
    ) -> Optional[Any]:
        """Hang up on *chat_id*, speak the sign-off, and drop the session.

        Defaults to ``EndReason.HANGUP_LOCAL`` — *we* are the ones hanging up,
        which is the only direction a programmatic caller can be.
        """
        vc = _call_module()
        if vc is None:
            return None
        decision = vc.hangup(
            speech_key(self.instance_id, chat_id),
            reason or vc.EndReason.HANGUP_LOCAL,
            time.monotonic(),
        )
        if decision.ok:
            peer = decision.session.peer if decision.session is not None else ""
            await self._perform_call_decision(decision, chat_id, peer)
        return decision

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def _call(
        self, action: protocol.Action, *, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        if self._client is None:
            raise OneBotTransportError("OneBot adapter is not connected")
        return await self._client.call_action(action, timeout=timeout)

    async def _ensure_native_backend(self) -> bool:
        if not self._native_qq.enabled or self._native_verified:
            return True
        task = self._backend_identity_task
        if task is None or task.done():
            if self._client is None:
                return False
            task = self._backend_identity_task = asyncio.create_task(self._identify_backend(self._client))
        await asyncio.shield(task)
        return self._native_verified

    def _native_supports(self, feature: str) -> bool:
        return not self._native_qq.enabled or feature in self._native_features

    @staticmethod
    def _native_send_failure(reason: str, response: Optional[Dict[str, Any]] = None) -> SendResult:
        return SendResult(success=False, error=reason, error_kind="unknown", retryable=False,
            raw_response={"onebot_native_delivery_unconfirmed": True, **(response or {})})

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
        account_failure = self._account_send_guard(target, is_group)
        if account_failure is not None:
            return False, None, account_failure
        if self._native_qq.enabled:
            if not await self._ensure_native_backend():
                return False, None, self._native_send_failure("Native QQ backend is not verified; nothing was sent")
            if any(not isinstance(segment, (protocol.TextSegment, protocol.AtSegment, protocol.ReplySegment)) for segment in segments):
                return False, None, self._native_send_failure("This native QQ backend does not support the requested message segment")
        action: protocol.Action = (
            protocol.SendGroupMsg(group_id=target, message=segments)
            if is_group
            else protocol.SendPrivateMsg(user_id=target, message=segments)
        )
        if not self.wait_for_send_ack and not self._native_qq.enabled:
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
            resp = await self._call(action, timeout=self._native_qq.send_timeout_seconds if self._native_qq.enabled else None)
        except (asyncio.TimeoutError, TimeoutError):
            if self._native_qq.enabled:
                return False, None, self._native_send_failure("Native QQ delivery outcome is unknown; no automatic retry")
            logger.warning(
                "OneBot: no send acknowledgement within the timeout — assuming "
                "delivery (the backend may not echo responses)"
            )
            return True, None, None
        except OneBotTransportError as exc:
            if self._native_qq.enabled:
                return False, None, self._native_send_failure("Native QQ transport interrupted; delivery was not assumed and no automatic retry")
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
            if self._native_qq.enabled:
                return False, None, self._native_send_failure(f"Native QQ did not confirm delivery: {detail}", resp)
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
        if self._native_qq.enabled and (resp.get("retcode") != 0 or resp.get("status") != "ok" or not mid or not mid.isdigit() or int(mid) <= 0):
            return False, None, self._native_send_failure("Native QQ returned no valid delivery confirmation", resp)
        return True, mid, None

    def _reply_anchor(
        self, reply_to: Optional[str], metadata: Mapping[str, Any]
    ) -> Optional[str]:
        """Which message this send quotes, if any.

        An explicit *reply_to* from the caller wins — some lanes (a threaded
        DM, a call reply) have already decided what they are answering.  With
        none given, the turn's own inbound message is the anchor, which is the
        whole point: until this existed a QQ group saw the bot's answers as
        free-floating messages, with nothing tying "对，那个包得先装" to the
        question three messages up that it was answering.

        ``reply_to_mode: off`` suppresses the quote even when a caller passed
        one, matching what the same value does on Telegram and Discord.
        """
        if reply_anchor_mode(_gh_live_extra(self)) == "off":
            return None
        return reply_to or (metadata.get(REPLY_ANCHOR_META) or None)

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

    def _sticker_segment(
        self, slugs: Sequence[str], chat_id: Optional[str] = None
    ) -> Optional[protocol.ImageSegment]:
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
        if not self._native_supports("image"):
            return None
        live_extra = _gh_live_extra(self)
        if sticker.probability_from_extra(live_extra, chat_id) <= 0:
            return None
        for slug in slugs:
            path = sticker.sticker_path(slug, sticker.dir_from_extra(live_extra, chat_id))
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
        force_card = (content or "").lstrip().startswith(LONG_REPLY_MARKER)
        if force_card:
            content = content.lstrip()[len(LONG_REPLY_MARKER):].lstrip()
        reply_to = self._reply_anchor(reply_to, metadata)
        # Hermes' own status messages, hidden before anything else happens.
        # First because a message that is not going out should not be parsed,
        # moderated, split, or measured against the mute — and because the
        # mute logs a warning per refusal, which would turn every hidden
        # heartbeat into an alarm about a group nobody tried to post to.
        if system_messages_suppressed(self) and (
            is_suppressible_system_message(metadata, content)
            # ...and the same treatment for operational text that arrives with
            # no flag at all, because the gateway RETURNS those as the turn's
            # reply rather than sending them through the flagged path. Without
            # this second test the switch reads as "system messages are hidden"
            # while "Hermes is at the active session limit" still lands in the
            # chat, which is precisely what was happening.
            or is_operational_reply(content)
        ):
            return suppressed_send_result(chat_id, content)
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError as exc:
            return SendResult(success=False, error=str(exc), error_kind="not_found")
        # Resolve every account-level refusal before the optional voice-call
        # route.  That route performs TTS, so checking only after it returns
        # lets an auth-required account spend provider work before the common
        # wire choke point finally refuses the bubble.
        if is_group and group_speech_muted(self):
            return muted_send_result(target)
        account_failure = self._account_send_guard(target, is_group)
        if account_failure is not None:
            return account_failure
        # ── While a call is up, this chat HEARS us instead of reading us ────
        # Hooked here because this method is the single outbound choke point:
        # every lane that can answer a person mid-call (the reply lane, the
        # proactive lane, a tool calling ``send_message``) already funnels
        # through it, so one hook covers all of them and none can grow its own
        # half of the behaviour later.  After the suppression check, because a
        # hidden heartbeat read out loud is worse than one merely printed; and
        # before the text shaping below, because ``voice.speak`` runs its own
        # equivalent chain (mute → policy → connection) in the order a voice
        # bubble needs.  ``None`` back means "type it after all".
        if not force_card and not metadata.get(VOICE_FALLBACK_META):
            spoken = await self._speak_call_reply(chat_id, content, reply_to, metadata)
            if spoken is not None:
                return spoken

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
        # A complete answer must preserve code operators/underscores and
        # source URLs, which generic Markdown stripping can change or remove.
        # Only fence delimiter lines are removed; code and URLs survive.
        complete_answer = force_card or (
            self.forward_threshold > 0 and len(content or "") > self.forward_threshold
        )
        content = (
            re.sub(r"(?m)^ {0,3}```[\w+-]*[ \t]*(?:\n|$)", "", content or "").strip()
            if complete_answer else strip_markdown(content or "")
        )

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
        # Media is gated too, on the paths that carry it: ``_send_attachment``
        # and ``send_image`` screen their attachment through
        # ``guard_outbound_media`` before it is encoded.  This method only ever
        # sees text, so the media check does not belong here — but it is no
        # longer missing, which it was when this comment read TODO(media).
        refusal = await guard_outbound_text(
            content,
            target=target,
            is_group=is_group,
            agent_config=getattr(self, "config", None),
        )
        if refusal is not None:
            return refusal

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
        cards_enabled = self.forward_threshold > 0 and self._native_supports("forward")
        too_long = len(content) > self.forward_threshold
        too_many_bubbles = (
            self.max_bubbles_per_reply > 0 and len(bubbles) > self.max_bubbles_per_reply
        )
        # ``bubble_overflow: merge`` flips the fragmentation branch only —
        # see :func:`bubble_overflow_is_merge`.  Length still wins a card.
        merge_overflow = bubble_overflow_is_merge(_gh_live_extra(self), chat_id)
        needs_card = cards_enabled and (
            force_card or too_long or (too_many_bubbles and not merge_overflow)
        )
        needs_capped_fallback = not cards_enabled or (
            too_many_bubbles and merge_overflow and not too_long
        )
        lead_already_sent = False
        if needs_card:
            forward_result = await self._deliver_forward(
                is_group, target, bubbles or [content], at_user_id, reply_to
            )
            lead_already_sent = forward_result.lead_sent
            if forward_result.success:
                delivered_as_card = True
                if forward_result.message_id:
                    sent_ids.append(forward_result.message_id)
            elif (
                forward_result.failure is not None
                and (
                    forward_result.failure.error_kind in ("forbidden", "not_found")
                    or (
                        self._native_qq.enabled
                        and (forward_result.failure.raw_response or {}).get("native_qq", {}).get("not_sent") is not True
                    )
                )
            ):
                # Missing target or uncertain native delivery: chunking can
                # either fail again or duplicate a card already delivered.
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
            sticker_segment = self._sticker_segment(sticker_slugs, chat_id)
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
            resp = await self._call(action, timeout=120.0 if self._native_qq.enabled else None)
        except (asyncio.TimeoutError, TimeoutError):
            if self._native_qq.enabled:
                return _ForwardDeliveryResult(False, self._native_send_failure(
                    "Native QQ card delivery outcome is unknown; no automatic retry or text fallback"), lead_sent)
            return _ForwardDeliveryResult(False, None, lead_sent)
        except OneBotTransportError as exc:
            if self._native_qq.enabled:
                return _ForwardDeliveryResult(False, self._native_send_failure(
                    "Native QQ card transport interrupted; no automatic retry or text fallback"), lead_sent)
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
            if self._native_qq.enabled:
                mid = str((resp.get("data") or {}).get("message_id") or "")
                if not mid.isdigit() or int(mid) <= 0:
                    return _ForwardDeliveryResult(False, self._native_send_failure(
                        "Native QQ returned no valid card delivery confirmation", resp), lead_sent)
            return _ForwardDeliveryResult(True, None, lead_sent,
                str((resp.get("data") or {}).get("message_id") or "") or None)
        detail = _response_error(resp)
        return _ForwardDeliveryResult(
            False,
            SendResult(
                success=False,
                error=f"OneBot rejected the forward card: {detail}",
                error_kind=classify_onebot_error(resp.get("retcode"), detail),
                raw_response=resp,
                retryable=False if self._native_qq.enabled else None,
            ),
            lead_sent,
        )

    # ------------------------------------------------------------------
    # Outbound media
    # ------------------------------------------------------------------

    def _record_self_media(
        self, target: int, media_kind: str, caption: Optional[str]
    ) -> None:
        """Tell the recent-chatter buffer that the bot just posted a file.

        ``send()`` records every outbound group bubble; this path did not, so
        after the bot posted a voice note the buffer still read as if it had
        said nothing.  That buffer is load-bearing twice over — it is the
        transcript the proactive persona is shown, and
        ``proactive.last_message_is_self`` reads it to decide whether the room
        is waiting on somebody else — so an unrecorded bubble means the
        proactive lane can talk straight over a voice message it just sent.

        What gets recorded, and why it is not nothing: ``voice.speak`` records
        the *spoken script* rather than ``[语音]``, on the stated grounds that
        this buffer is prompt input and a placeholder there teaches the persona
        it said nothing.  A file has no script — there is no true text to
        prefer — so the choice here is between a short honest marker and
        silence, and silence is the option that leaves the bot invisible to
        itself.  The marker says *that* it spoke without claiming to know
        *what* was in the recording, and it flips the anti-self-spam check,
        which is the behaviour that was actually missing.  A caption is real
        words the bot typed, so it wins over the marker whenever there is one.
        """
        record_group_message(
            self.instance_id,
            target,
            self._bot_nickname or "bot",
            (caption or "").strip() or _SELF_MEDIA_MARKERS.get(media_kind, "[文件]"),
            True,
        )

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
        # Same anchor rule as ``send()``: this path does not go through it, so
        # without this line a picture answer would be the one reply in the
        # room that quotes nothing — and ``reply_to_mode: off`` would be a
        # switch that only half worked.
        reply_to = self._reply_anchor(reply_to, metadata or {})
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError as exc:
            return SendResult(success=False, error=str(exc), error_kind="not_found")
        # A picture posted into a muted group is still the bot talking in that
        # group.  Gated here rather than only in ``send()`` because the file
        # and inline-media paths do not go through it.
        if is_group and group_speech_muted(self):
            return muted_send_result(target)
        account_failure = self._account_send_guard(target, is_group)
        if account_failure is not None:
            return account_failure
        safe_path = validate_media_delivery_path(file_path)
        if not safe_path:
            return SendResult(
                success=False,
                error="attachment path rejected by media-delivery policy",
                error_kind="forbidden",
            )
        media_kind = kind or guess_media_kind(safe_path)

        # This branch writes its caption straight to the wire, so ``send()``'s
        # gates never see it — and a caption is text the bot is saying, which
        # is precisely what they exist to audit.  Both stages run here, via the
        # shared choke point, so this path gets the agent gate too; it used to
        # run the keyword table alone.  Refusal is all-or-nothing: the media is
        # the caption's context, and posting the picture with a refused
        # sentence quietly deleted says something nobody wrote.
        #
        # Before ``encode_base64_file`` so a refusal does not first read a file
        # it is never going to send, and only for the inline kinds — the upload
        # branch's caption goes out through ``send()``, which gates it there.
        if caption and media_kind in ("image", "audio"):
            refusal = await guard_outbound_text(
                caption,
                target=target,
                is_group=is_group,
                agent_config=getattr(self, "config", None),
            )
            if refusal is not None:
                return refusal

        # The BYTES, which nothing looked at until now.  A refusal here does
        # not fail the message: the attachment is dropped and the caption goes
        # out alone, matching how the source system degrades a refused
        # attachment to a text-only publish.  With no caption there is nothing
        # left to send, so the refusal is returned as-is.
        if media_kind == "image":
            blocked = await guard_outbound_media(
                safe_path,
                target=target,
                is_group=is_group,
                media_config=getattr(self, "config", None),
            )
            if blocked is not None:
                if not caption:
                    return blocked
                return await self.send(
                    chat_id, caption, reply_to=reply_to, metadata=metadata
                )

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
                ok, _cap_mid, failure = await self._send_segments(
                    is_group, target, cap_segments
                )
                if not ok and failure is not None:
                    # Stop rather than post the media anyway — the same call
                    # ``voice.speak`` makes at this exact split, for the same
                    # reason: the caption is the context the bubble answers, a
                    # media bubble arriving without it reads as the bot talking
                    # to itself, and a caller retry then delivers a coherent
                    # pair instead of an orphan.  Discarding this result also
                    # let ``_record_self_media`` below teach the chatter buffer
                    # a sentence that never arrived, which the persona would
                    # later read back as its own words.
                    return failure
            ok, mid, failure = await self._send_segments(is_group, target, segments)
            if not ok and failure is not None:
                return failure
            if is_group:
                # After the failure return, never before it: a message that was
                # refused, muted or never acknowledged must not be taught to
                # the buffer as something the bot said.
                self._record_self_media(target, media_kind, caption)
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
            # Through ``send()`` on purpose: it is the moderated path, and it
            # already records the caption in the chatter buffer, so recording
            # here as well would put the same sentence in the transcript twice.
            await self.send(chat_id, caption, reply_to=reply_to, metadata=metadata)
        elif is_group:
            self._record_self_media(target, media_kind, None)
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
        account_failure = self._account_send_guard(target, is_group)
        if account_failure is not None:
            return account_failure
        # This method writes its caption to the wire itself, so ``send()``'s
        # gates never see it — the same hole the inline branch of
        # ``_send_attachment`` had, on the sibling call path.  The local-file
        # branch above delegates to ``send_image_file`` and is gated there;
        # only this remote-URL branch was left open.  Same choke point, so this
        # path now runs the agent gate as well as the keyword table.
        #
        # The image BYTES are NOT screened on this branch, and cannot be: the
        # backend fetches the URL itself, so the bytes never pass through this
        # process.  Downloading them here only to check them would be checking
        # a different fetch than the one the backend performs — the URL is free
        # to serve something else the second time.  What protects this path is
        # that the URL is one the bot produced or was handed in-process, and
        # the caption around it is audited; a remote image whose provenance is
        # not already trusted should be routed through ``send_image_file``,
        # which screens real bytes.
        if caption:
            refusal = await guard_outbound_text(
                caption,
                target=target,
                is_group=is_group,
                agent_config=getattr(self, "config", None),
            )
            if refusal is not None:
                return refusal
        segments = self._lead_segments(
            is_group,
            (metadata or {}).get("onebot_at_user_id"),
            self._reply_anchor(reply_to, metadata or {}),
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

        Any failure is swallowed: a backend that does not implement the
        extension answers "unsupported action", and a missing typing bubble
        must never block the reply.
        """
        if not self.typing_indicator or self._client is None or not self._native_supports("typing"):
            return
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError:
            return
        if is_group:
            return
        if self._account_send_guard(target, False) is not None:
            return
        try:
            await self._client.send_action(
                protocol.SetInputStatus(user_id=target, event_type=1)
            )
        except Exception:  # noqa: BLE001
            pass

    async def stop_typing(self, chat_id: str) -> None:
        if not self.typing_indicator or self._client is None or not self._native_supports("typing"):
            return
        try:
            is_group, target = parse_chat_id(chat_id)
        except ValueError:
            return
        if is_group:
            return
        if self._account_send_guard(target, False) is not None:
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
    # 主窗口: carried into ``extra`` so ``main_window.resolve_main_window``
    # sees an env-only deployment the same way it sees a YAML one.  A group
    # id here is refused by ``normalize_peer_id`` downstream, not silently
    # promoted into a console.
    peer = main_window.normalize_peer_id(
        os.getenv(main_window.MAIN_WINDOW_ENV, "").strip()
    )
    if peer:
        seed["main_window_chat_id"] = peer
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
    # 主窗口 (see .main_window): the one private chat that receives bare
    # ``deliver=onebot`` traffic and is the only scope whose slash commands
    # are honoured, plus the off-by-default escape hatch that would hand
    # group members that same control.
    "main_window_chat_id",
    "group_gateway_control_enabled",
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
    "reply_debounce_seconds",
    # Recent-chatter block prepended to an addressed group turn (see
    # .reply_context).  On by default; ``0`` turns it off.
    "group_context_messages",
    "wait_for_send_ack",
    "suppress_system_messages",
    "max_concurrency",
    "health_probe_secs",
    "health_lost_secs",
    "account_probe_secs",
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
    # Call-shaped voice sessions (see .voice_call).  Off unless
    # voice_call_enabled is set.  YAML-only, with no ONEBOT_VOICE_CALL_*
    # env form, for the reason README.md and decision B4-5 give for
    # ``proactive_*``: the switch deciding whether the bot talks to real
    # people lives in exactly one place, and an env var is read once at
    # process start so it could never hot-apply.  Anything missing from this
    # tuple is dropped by _apply_yaml_config in silence — which for a switch
    # like this means a feature that can never be turned on.
    "voice_call_enabled",
    "voice_call_ring_timeout_secs",
    "voice_call_idle_timeout_secs",
    "voice_call_max_duration_secs",
    "voice_call_max_turns",
    "voice_call_allow_groups",
    "voice_call_allowed_users",
    # Proactive VOICE (see .proactive): the proactive lane occasionally
    # speaking its post instead of typing it.
    "proactive_voice_enabled",
    "proactive_voice_probability",
    "proactive_voice_max_chars",
    "proactive_voice_provider",
    # Proactive CALLS (see .proactive): the bot placing a call nobody asked
    # for.  Same YAML-only reasoning, and it applies here hardest of all.
    "proactive_call_enabled",
    "proactive_call_targets",
    "proactive_call_daily_max",
    "proactive_call_min_gap_minutes",
    "proactive_call_active_start_hour",
    "proactive_call_active_end_hour",
    "proactive_call_probability",
    "proactive_call_prompt",
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
    native = NativeQQSettings.from_extra(extra)
    if native.enabled:
        if media_files:
            return {"error": "Native QQ standalone media delivery is not supported", "retryable": False}
        # Reuse the same adapter decisions and confirmed-send path used by
        # live conversations. Cron must not retain a fire-and-forget escape.
        temporary = OneBotAdapter(pconfig)
        client = OneBotClient(OneBotConfig(url=ws_url, access_token=token or None,
            token_in_query=temporary.token_in_query, native_delivery=True, call_timeout=native.send_timeout_seconds))
        temporary._client = client
        try:
            await client.connect()
            await temporary._identify_backend(client)
            result = await temporary.send(chat_id, message)
            if not result.success:
                return {"error": result.error, "retryable": False, **(result.raw_response or {})}
            return {"success": True, "platform": PLATFORM_NAME, "chat_id": chat_id, "message_id": result.message_id}
        finally:
            await client.close()
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

    # The content policy, on the same terms and for the same reason the mute
    # above is here: this lane puts caller-supplied text on the wire without
    # ever touching ``send()``, so gating only the in-process adapter would
    # leave the account-freeze hole open one process over.  Whole message,
    # before the chunking below, exactly as ``send()`` audits it — a phrase the
    # rule table catches can straddle a length boundary.  Before the socket is
    # opened: a refusal is a decision, and a connection error is a transient
    # the caller would retry.  ``SendResult`` has no place in this lane's dict
    # protocol, so the refusal is flattened into the same key shape the mute
    # uses and ``is_policy_blocked_send_result`` already recognises.
    #
    # Both stages run here, through the shared choke point — this lane used to
    # run the keyword table alone, which meant a cron job could put on the wire
    # a sentence the in-process adapter would have refused.  The agent gate is
    # configured from the platform extras rather than a live adapter, because
    # by definition there is no adapter in this process.
    refusal = await guard_outbound_text(
        message or "",
        target=target,
        is_group=is_group,
        agent_config=extra,
    )
    if refusal is not None:
        return {"error": refusal.error, **(refusal.raw_response or {})}

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
    print("QQ via OneBot v11 (SnowLuma / NapCat / Lagrange / go-cqhttp) setup")
    print("-------------------------------------------------------------------")
    print("Requirements:")
    print("  1. A OneBot v11 backend exposing a FORWARD WebSocket server")
    print("     NapCat default: ws://127.0.0.1:3001/ (WebSocket server)")
    print("       WebUI: http://localhost:5099 (user admin) → OneBot; token is auto-generated")
    print("       or config/onebot.json → networks.wsServers")
    print("     NapCat: Network → websocketServers; Lagrange/go-cqhttp: their OneBot config")
    print("  2. Its OneBot access token — NOT the WebUI credential; they are different secrets.")
    print("     NapCat's WebUI token and the OneBot access token are independent.")
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
    "text only — Markdown styling and link previews are not rendered; write for a "
    "phone screen. For everyday chat, usually send one concise message; "
    "choose two or three only when the thought naturally needs separate bubbles. "
    "Use [MSG_BREAK] only BETWEEN bubbles, never to meet a quota. "
    "For complex questions and tasks, answer fully with the necessary code, "
    "steps and sources. Start such an answer with [LONG_REPLY] to send the "
    "complete answer in a tappable chat-record card, even below the automatic "
    "length threshold. Never shorten a task answer to fit casual chat. In a group you are "
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
            "Point ONEBOT_WS_URL at a running SnowLuma/NapCat/Lagrange/"
            "go-cqhttp OneBot v11 forward WebSocket (e.g. "
            "ws://127.0.0.1:3001)."
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
