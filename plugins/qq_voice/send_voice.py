"""``qq_send_voice`` — a native QQ voice bubble (语音消息).

Text in (synthesized with whatever TTS provider the operator configured) or
an existing local audio file in; a green voice bubble out.  Not a file
attachment in the group's file area, not an audio player card.

Two delivery paths, chosen automatically:

**gateway** — a OneBot adapter is live in this process, so the send goes
through ``plugins/platforms/onebot/voice.py::speak``, which applies the
emergency group mute, the Tencent content policy and the self-recording into
the shared recent-chatter buffer, in that order.  A raw ``send_msg`` would
skip all three: it speaks into muted groups, ships unmoderated text to
Tencent under this account, and leaves the proactive persona believing the
bot said nothing so it talks over itself out loud.

**direct** — no gateway is running (CLI, ``hermes cron``).  None of that
state exists in this process, so there is nothing to bypass; the audio goes
out over ``onebot_call("send_msg", …)`` as an inline ``base64://`` record
segment.  The result says so, because a caller has to be able to tell a
guaranteed send from a best-effort one.

A file send takes the gateway's ``send_voice`` rather than ``speak`` — there
is no text to moderate and no script to record, and ``speak`` synthesizes
rather than accepting a path.  That path still honours the emergency mute
(``_send_attachment`` checks it) and still reuses the gateway's healthy
socket, so it remains strictly better than the raw transport.

Both delivery paths are bounded by ``quota.py``: a text-length cap before any
synthesis runs, and a daily send budget charged only once a message is (or
may be) in the chat.  Which failures charge and which do not is decided here,
because only this module knows what the backend actually said — see
:data:`_Delivered`.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import tool_error

from .common import (
    DELIVERY_DIRECT,
    DELIVERY_GATEWAY,
    DELIVERY_NOTES,
    chat_id_for,
    discard_files,
    live_adapter,
    read_audio_file,
    resolve_single_target,
    run_on_adapter_loop,
    synthesize_speech,
    vet_audio_path,
)
from .quota import (
    QuotaSnapshot,
    charge_one_send,
    quota_refusal,
    quota_snapshot,
    resolve_limits,
    spoken_length,
    text_cap_refusal,
)

logger = logging.getLogger(__name__)

QQ_SEND_VOICE_TOOL = "qq_send_voice"

#: ``Future.result`` raises ``concurrent.futures.TimeoutError``, which is an
#: alias of the builtin on 3.11+ and its own class before that.  Both are
#: listed so the handler behaves identically on either interpreter.
_TIMEOUTS = (TimeoutError, concurrent.futures.TimeoutError)

#: Every send path returns ``(result json, delivered)``.  ``delivered`` is
#: "QQ has this message, or may have it" — the flag that decides whether the
#: day's budget is charged.  It is deliberately *not* the same thing as
#: ``success``: see :func:`_speak_via_gateway` for the one case where a
#: reported error still charges.
_Delivered = Tuple[str, bool]

__all__ = ["QQ_SEND_VOICE_SCHEMA", "QQ_SEND_VOICE_TOOL", "handle_qq_send_voice"]


# ---------------------------------------------------------------------------
# Wire shapes for the direct path
# ---------------------------------------------------------------------------


def _record_message(audio_b64: str) -> List[Dict[str, Any]]:
    """The OneBot v11 message array for a single voice (``record``) segment.

    ``base64://`` is not an optimisation: the backend may run in its own
    container and cannot read this process's filesystem, so a host
    path handed across that boundary resolves to nothing and the sender
    believes it delivered silence.  The backend transcodes to QQ's SILK codec
    itself, which is why nothing here encodes SILK.
    """
    return [{"type": "record", "data": {"file": f"base64://{audio_b64}"}}]


def _send_params(
    message: List[Dict[str, Any]], user_id: Optional[int], group_id: Optional[int]
) -> Dict[str, Any]:
    """``send_msg`` params for a private or group target."""
    if group_id is not None:
        return {"message_type": "group", "group_id": int(group_id), "message": message}
    return {"message_type": "private", "user_id": int(user_id or 0), "message": message}


def _target_of(user_id: Optional[int], group_id: Optional[int]) -> Dict[str, Any]:
    if group_id is not None:
        return {"type": "group", "id": group_id}
    return {"type": "private", "id": user_id}


# ---------------------------------------------------------------------------
# The gateway path
# ---------------------------------------------------------------------------


def _timed_out_mid_send() -> _Delivered:
    """The unknown-outcome result, and the one error that still costs budget.

    ``run_on_adapter_loop`` gave up waiting; the coroutine is still running on
    the gateway's loop and may well be mid-``send_msg``.  Nobody — here or on
    the model's side — can find out which without looking at the chat.

    It charges the day's budget, and that is a deliberate choice between two
    wrong answers.  Not charging is the worse one: the conditions that produce
    a timeout (a wedged backend, a saturated loop) are exactly the conditions
    that repeat, so a model that keeps calling past a timeout could post an
    unbounded run of real voice bubbles — each one a paid synthesis — while
    the counter never moves.  That is the cap failing open on precisely the
    path it exists to bound.  Charging instead costs, at worst, one of the
    day's sends for a message that never arrived; the loss is one unit, it is
    visible in the ``voice_quota`` block on this very error, and the operator
    can raise the plugin's ``daily_max`` setting.

    ``plugins/qzone/state.py::mark_comment`` makes the same call for the same
    reason: an ``unknown`` outcome is recorded as if it had landed, because a
    transport error means nobody knows and the safe assumption is that it did.
    """
    return (
        tool_error(
            "Timed out waiting for the gateway to send the voice message. It may "
            "still have been delivered — check the chat before sending again. "
            "It has been counted against today's voice budget, because a message "
            "that may be in the chat has to be assumed to be in the chat.",
            code="timeout",
            delivery_path=DELIVERY_GATEWAY,
        ),
        True,
    )


def _speak_via_gateway(
    adapter: Any,
    loop: Any,
    chat_id: str,
    text: str,
    target: Dict[str, Any],
) -> _Delivered:
    """Hand the whole send to ``voice.speak`` on the gateway's own loop."""
    from plugins.platforms.onebot import voice  # noqa: PLC0415 — gateway-only import

    try:
        outcome = run_on_adapter_loop(voice.speak(adapter, chat_id, text), loop)
    except _TIMEOUTS:
        # Deliberately not retried: the coroutine may be mid-send, and a
        # second attempt is how one message becomes two voice bubbles.
        return _timed_out_mid_send()
    except Exception as exc:  # noqa: BLE001 — surface one clear message
        logger.error("qq_send_voice: gateway speak() failed: %s", exc)
        # Not charged. ``voice.speak`` turns every outcome it can see into a
        # ``SpeakOutcome``, so an exception escaping it means the send never
        # got as far as the wire; the existing wording says so with no hedge,
        # unlike the timeout above.
        return (
            tool_error(
                f"Could not send the voice message through the gateway: {exc}",
                code="gateway_failed",
                delivery_path=DELIVERY_GATEWAY,
            ),
            False,
        )

    if not outcome.spoke:
        # A mute or a policy refusal is a *decision*, not a transport hiccup;
        # reporting it as success would teach the model the bot is audible in
        # a room it was deliberately silenced in.  Neither costs a synthesis
        # (``speak`` runs both gates before it pays for one), so neither costs
        # budget either.
        detail = outcome.reason or "the gateway refused the voice message"
        kind = getattr(outcome.failure, "error_kind", None) if outcome.failure else None
        return (
            tool_error(
                f"Voice message not sent: {detail}",
                code=kind or "not_spoken",
                delivery_path=DELIVERY_GATEWAY,
            ),
            False,
        )

    result: Dict[str, Any] = {
        "success": True,
        "delivery_path": DELIVERY_GATEWAY,
        "delivery_note": DELIVERY_NOTES[DELIVERY_GATEWAY],
        "message_id": outcome.message_id,
        "message_ids": list(outcome.message_ids),
        "target": target,
        "synthesized": True,
        "audio_bytes": outcome.audio_bytes,
        "message": "Voice message sent to QQ.",
    }
    if outcome.reason:
        # ``spoke`` with a reason means a multi-part message stopped partway.
        result["warning"] = outcome.reason
    return json.dumps(result, ensure_ascii=False), True


def _send_file_via_gateway(
    adapter: Any,
    loop: Any,
    chat_id: str,
    audio_path: str,
    size: int,
    target: Dict[str, Any],
) -> _Delivered:
    """Send an already-recorded file through the adapter's own voice path."""
    try:
        send_result = run_on_adapter_loop(adapter.send_voice(chat_id, audio_path), loop)
    except _TIMEOUTS:
        return _timed_out_mid_send()
    except Exception as exc:  # noqa: BLE001 — surface one clear message
        logger.error("qq_send_voice: gateway send_voice failed: %s", exc)
        return (
            tool_error(
                f"Could not send the voice message through the gateway: {exc}",
                code="gateway_failed",
                delivery_path=DELIVERY_GATEWAY,
            ),
            False,
        )

    if not getattr(send_result, "success", False):
        detail = getattr(send_result, "error", None) or "the gateway refused the send"
        return (
            tool_error(
                f"Voice message not sent: {detail}",
                code=getattr(send_result, "error_kind", None) or "not_spoken",
                delivery_path=DELIVERY_GATEWAY,
            ),
            False,
        )
    return (
        json.dumps(
            {
                "success": True,
                "delivery_path": DELIVERY_GATEWAY,
                "delivery_note": DELIVERY_NOTES[DELIVERY_GATEWAY],
                "message_id": getattr(send_result, "message_id", None),
                "target": target,
                "synthesized": False,
                "audio_bytes": size,
                "message": "Voice message sent to QQ.",
            },
            ensure_ascii=False,
        ),
        True,
    )


# ---------------------------------------------------------------------------
# The direct path
# ---------------------------------------------------------------------------


def _send_direct(
    payloads: List[Tuple[str, int]],
    user_id: Optional[int],
    group_id: Optional[int],
    target: Dict[str, Any],
    *,
    synthesized: bool,
) -> _Delivered:
    """Post each vetted payload over the plain OneBot transport, in order.

    Several payloads happen when long text was chunked by the TTS backend.
    A failure partway through is reported with the ids that already went out
    rather than retried — the delivered bubbles cannot be recalled, and a
    retry would repeat them.

    Budget: charged when *any* chunk was acknowledged, not only when all of
    them were.  A half-delivered message is still audible in the chat and
    still cost the syntheses that produced it.  A failure with nothing
    acknowledged is not charged: ``onebot_call`` "raises ``RuntimeError`` on a
    transport error, a ``failed`` status, or a missing ``data`` field — never
    returns a partial result", and the dominant case there is the backend
    saying no, which this plugin has always reported without the "may still
    have been delivered" hedge it puts on the gateway timeout.  A genuinely
    lost reply on this path is counted as a non-delivery; that is the one
    place the budget can under-count, and it is bounded by the fact that the
    same failure keeps the caller on a visible error rather than a silent one.
    """
    from tools.onebot_client import onebot_call  # noqa: PLC0415 — direct path only

    message_ids: List[Any] = []
    acknowledged = 0
    sent_bytes = 0
    for audio_b64, size in payloads:
        params = _send_params(_record_message(audio_b64), user_id, group_id)
        try:
            data = onebot_call("send_msg", params)
        except Exception as exc:  # noqa: BLE001 — verbatim, never a silent retry
            logger.error("qq_send_voice: OneBot send_msg failed: %s", exc)
            return (
                tool_error(
                    f"Could not send the voice message via OneBot: {exc}",
                    code="onebot_failed",
                    delivery_path=DELIVERY_DIRECT,
                    partial_message_ids=message_ids,
                ),
                acknowledged > 0,
            )
        # Counted per acknowledged call, not per returned id: a backend that
        # answers without a ``message_id`` still delivered the bubble.
        acknowledged += 1
        if isinstance(data, dict) and data.get("message_id") is not None:
            message_ids.append(data.get("message_id"))
        sent_bytes += size

    return (
        json.dumps(
            {
                "success": True,
                "delivery_path": DELIVERY_DIRECT,
                "delivery_note": DELIVERY_NOTES[DELIVERY_DIRECT],
                "message_id": message_ids[-1] if message_ids else None,
                "message_ids": message_ids,
                "target": target,
                "synthesized": synthesized,
                "audio_bytes": sent_bytes,
                "message": "Voice message sent to QQ.",
            },
            ensure_ascii=False,
        ),
        acknowledged > 0,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _with_quota(payload: str, snapshot: QuotaSnapshot) -> str:
    """Splice the ``voice_quota`` block into an already-built result.

    Attached to failures as well as successes so the model can see whether an
    error moved the budget (the timeout does; nothing else does) without
    having to call again to find out.

    Re-parsing JSON this module just produced is not free, but it is bounded
    by one small object per tool call and it keeps every result-building site
    above unaware of the budget — the alternative is threading a snapshot that
    does not exist yet through six return paths.  A payload that somehow is
    not a JSON object is returned untouched rather than replaced: a delivered
    voice message must never be reported as a failure over a bookkeeping
    field.
    """
    try:
        data = json.loads(payload)
    except ValueError:  # pragma: no cover - every producer above emits JSON
        return payload
    if not isinstance(data, dict):  # pragma: no cover - same
        return payload
    data["voice_quota"] = snapshot.as_dict()
    return json.dumps(data, ensure_ascii=False)


def handle_qq_send_voice(args: dict, **_kw: Any) -> str:
    """Handler for ``qq_send_voice``."""
    text = (args.get("text") or "").strip()
    audio_file = (args.get("audio_file") or "").strip()

    if not text and not audio_file:
        return tool_error(
            "qq_send_voice requires 'text' (to synthesize) or 'audio_file'.",
            code="invalid_args",
        )
    if text and audio_file:
        return tool_error(
            "qq_send_voice: provide either 'text' or 'audio_file', not both.",
            code="invalid_args",
        )

    try:
        user_id, group_id = resolve_single_target(args, QQ_SEND_VOICE_TOOL)
    except ValueError as exc:
        return tool_error(f"{exc}", code="invalid_args")

    # Re-read per call so an operator editing the plugin's settings block in
    # config.yaml sees it apply on the next tool call, not the next restart.
    limits = resolve_limits()

    # The text cap first, and before ``live_adapter()``: it is the one refusal
    # that must happen before any expensive work, since the whole point is not
    # to pay a per-synthesis provider for a message nobody wants to sit
    # through.  Only the text branch has a script to measure — an audio_file
    # send carries no text, and its length is already bounded by
    # ``MAX_AUDIO_BYTES``.
    if text:
        length = spoken_length(text)
        if limits.text_over_cap(length):
            return tool_error(
                text_cap_refusal(length, limits.max_text_chars),
                code="text_too_long",
                text_chars=length,
                max_text_chars=limits.max_text_chars,
            )

    # The budget covers *both* branches, not just the synthesized one. An
    # audio_file send is still a voice bubble in someone's chat, and exempting
    # it would leave the model an easy way round the cap: synthesize with
    # ``text_to_speech`` (which bills identically) and post the file.
    quota = quota_snapshot(limits)
    if quota.exhausted:
        return tool_error(
            quota_refusal(quota),
            code="quota_exhausted",
            voice_quota=quota.as_dict(),
        )

    target = _target_of(user_id, group_id)
    chat_id = chat_id_for(user_id, group_id)
    adapter, loop = live_adapter()

    payload, delivered = _dispatch_send(
        args_text=text,
        audio_file=audio_file,
        adapter=adapter,
        loop=loop,
        chat_id=chat_id,
        user_id=user_id,
        group_id=group_id,
        target=target,
    )
    if delivered:
        quota = charge_one_send(limits)
    return _with_quota(payload, quota)


def _dispatch_send(
    *,
    args_text: str,
    audio_file: str,
    adapter: Any,
    loop: Any,
    chat_id: str,
    user_id: Optional[int],
    group_id: Optional[int],
    target: Dict[str, Any],
) -> _Delivered:
    """Pick a branch and a transport, and report whether QQ got the message.

    Split out of the handler so the caps read as one block at the top and the
    four delivery combinations (file/text × gateway/direct) stay together.
    """
    if audio_file:
        try:
            resolved, size = vet_audio_path(audio_file)
        except ValueError as exc:
            return (
                tool_error(f"Audio file '{audio_file}': {exc}", code="invalid_args"),
                False,
            )
        if adapter is not None and loop is not None:
            return _send_file_via_gateway(
                adapter, loop, chat_id, resolved, size, target
            )
        audio_bytes = read_audio_file(resolved)
        payload = base64.b64encode(audio_bytes).decode("ascii")
        return _send_direct(
            [(payload, len(audio_bytes))], user_id, group_id, target, synthesized=False
        )

    # Text. On the gateway path, synthesis happens *inside* ``speak`` — after
    # the mute and the policy have had their say, so a refused message never
    # costs a TTS call in the first place.
    if adapter is not None and loop is not None:
        return _speak_via_gateway(adapter, loop, chat_id, args_text, target)

    try:
        paths = synthesize_speech(args_text)
    except Exception as exc:  # noqa: BLE001 — surface one clear message
        logger.error("qq_send_voice: speech synthesis failed: %s", exc)
        # Nothing reached QQ and, on this path, nothing was even produced, so
        # the day's budget is untouched.
        return tool_error(f"Speech synthesis failed: {exc}", code="tts_failed"), False
    try:
        payloads: List[Tuple[str, int]] = []
        for path in paths:
            try:
                audio_bytes = read_audio_file(path)
            except ValueError as exc:
                return (
                    tool_error(f"Synthesized audio unusable: {exc}", code="tts_failed"),
                    False,
                )
            payloads.append((
                base64.b64encode(audio_bytes).decode("ascii"),
                len(audio_bytes),
            ))
        return _send_direct(payloads, user_id, group_id, target, synthesized=True)
    finally:
        # The TTS tool cleans up its intermediates but not its output, and
        # this path (unlike ``speak``) owns those files.
        discard_files(paths)


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

QQ_SEND_VOICE_SCHEMA = {
    "name": QQ_SEND_VOICE_TOOL,
    "description": (
        "Send a voice message (语音消息) to a QQ user or group — a native QQ "
        "voice bubble, not a file attachment. Give 'text' to synthesize "
        "speech with the user's configured TTS provider and voice, or "
        "'audio_file' to send an existing local audio file. Exactly one of "
        "'user_id' (private chat) or 'group_id' (group chat) is required. "
        "The result's 'delivery_path' says how it went out: 'gateway' means "
        "the running bot applied the emergency mute, the Tencent content "
        "policy and its own chat bookkeeping; 'direct' means no gateway was "
        "running (CLI / cron) and the send was best-effort over the plain "
        "OneBot transport. Speech costs money per synthesis, so this tool is "
        "capped: 'text' longer than the configured max_text_chars (200 by "
        "default) is refused — send that as a text message instead — and "
        "there is a daily budget of daily_max sends (30 by default) that "
        "resets at local midnight. Every result carries 'voice_quota' "
        "(used_today / limit / remaining) so you can pace yourself. Note: "
        "this drives an unofficial automation path and can fail if the OneBot "
        "(SnowLuma / NapCat / Lagrange / go-cqhttp) login state is stale or Tencent risk control "
        "intervenes. A sent voice message cannot be unsent from here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Text to speak. Synthesized to audio with the user's "
                    "configured TTS provider and voice. Provide this OR "
                    "'audio_file', not both. Capped at max_text_chars "
                    "characters (200 by default) — anything longer belongs "
                    "in a text message."
                ),
            },
            "audio_file": {
                "type": "string",
                "description": (
                    "Path to an existing local audio file to send as the "
                    "voice message (mp3, wav, ogg, amr, silk, m4a, flac, "
                    "aac, opus; 8 MiB max). Provide this OR 'text', not both."
                ),
            },
            "user_id": {
                "type": "string",
                "description": (
                    "Target QQ number for a private-chat voice message. "
                    "Provide either 'user_id' or 'group_id', not both."
                ),
            },
            "group_id": {
                "type": "string",
                "description": (
                    "Target QQ group number for a group-chat voice message. "
                    "Provide either 'user_id' or 'group_id', not both."
                ),
            },
        },
        "required": [],
    },
}
