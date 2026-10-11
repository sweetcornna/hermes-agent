"""``qq_ai_voice`` — a voice message spoken by one of QQ's built-in AI characters.

群 AI 声聊: Tencent synthesizes the audio on its own side from a
``character_id`` and the text, and posts it into the group.  Nothing is
synthesized locally and no audio ever passes through this process.

Two platform constraints, both Tencent's rather than ours:

* **Group-only.**  QQ exposes no private-chat equivalent — there is no
  ``send_private_ai_record`` — so a ``user_id`` target is refused with a
  pointer at ``qq_send_voice``, which does work in a DM.
* **OneBot v11 extension.**  ``send_group_ai_record`` and
  ``get_ai_characters`` are implemented by NapCat and SnowLuma; a Lagrange or
  go-cqhttp backend may reject them.  That rejection arrives as a bare retcode
  the model cannot diagnose, so every backend failure here is annotated with
  the likely cause.

Discovery is folded into the action rather than split into a second tool: the
model cannot know a ``character_id`` a priori, and inventing one produces
exactly the undiagnosable rejection above.  Called without ``character``,
this lists what the group offers and sends nothing.

Same live-vs-direct rule as ``qq_send_voice`` — see ``common.live_adapter``.
The gate list matters *more* here, not less: the text is handed to Tencent as
plaintext, so a sentence the content policy would refuse gets shipped
straight to the party the policy exists to keep quiet.

Caps: one of the two in ``quota.py`` applies here and one does not, because
they exist for different reasons.

* ``max_text_chars`` **applies.**  It is a promise to the listener,
  not to the accountant — a five-minute voice bubble is exactly as
  unlistenable in one of Tencent's AI voices as in ours, and QQ gives the
  reader no way to skim it either way.  Exempting this tool would also leave
  the model a trivial way around the rule: say the wall of text as an AI
  character instead.
* ``daily_max`` **does not.**  That budget is a cost control on a
  paid per-synthesis TTS provider, and nothing here touches one: Tencent
  synthesizes on its own side, for free as far as this install is concerned.
  Charging a free call against a paid budget would make the model ration the
  wrong resource, and would let this tool starve ``qq_send_voice`` of sends
  that actually cost something.  Group chattiness — the other thing a daily
  count could bound — is already the adapter's job, not this plugin's: on the
  gateway path ``speak_ai_character`` goes through the same emergency mute
  and content policy as every other spoken reply.  Accordingly no
  ``voice_quota`` block appears in this tool's results; there is no budget
  here to report.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
from typing import Any, Dict, List, Optional

from tools.registry import tool_error

from .common import (
    DELIVERY_DIRECT,
    DELIVERY_GATEWAY,
    DELIVERY_NOTES,
    _coerce_qq_id,
    live_adapter,
    run_on_adapter_loop,
)
from .quota import resolve_limits, spoken_length, text_cap_refusal

logger = logging.getLogger(__name__)

QQ_AI_VOICE_TOOL = "qq_ai_voice"

_TIMEOUTS = (TimeoutError, concurrent.futures.TimeoutError)

#: Appended to every backend failure. A raw ``retcode=1200`` tells the model
#: nothing; an extension may be unavailable on the detected backend, and that
#: is actionable ("check the backend or use qq_send_voice").
NAPCAT_ONLY_HINT = (
    "QQ AI voice (send_group_ai_record / get_ai_characters) is a OneBot v11 "
    "extension; NapCat and SnowLuma implement it, while Lagrange and "
    "go-cqhttp may not. If this keeps failing, check the detected backend or "
    "use qq_send_voice for a normally-synthesized voice message instead."
)


def _backend_hint(adapter: Any | None = None) -> str:
    """Add the detected backend to the extension hint when the gateway knows it."""
    backend = str(getattr(adapter, "_backend", "") or "").strip()  # noqa: SLF001
    if not backend or backend == "unknown":
        return NAPCAT_ONLY_HINT
    display_name = {
        "snowluma": "SnowLuma",
        "napcat": "NapCat",
        "lagrange": "Lagrange",
        "go-cqhttp": "go-cqhttp",
    }.get(backend, backend)
    return f"Detected OneBot backend: {display_name}. {NAPCAT_ONLY_HINT}"

__all__ = [
    "NAPCAT_ONLY_HINT",
    "QQ_AI_VOICE_SCHEMA",
    "QQ_AI_VOICE_TOOL",
    "handle_qq_ai_voice",
]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _flatten_characters(data: Any) -> List[Dict[str, Any]]:
    """Flatten ``get_ai_characters``'s style-grouped payload into a flat list.

    The wire shape is ``[{"type": "推荐"|"古风"|"现代", "characters": [...]}]``.
    The style is folded into each entry so one flat list keeps it, and
    ``preview_url`` is dropped: the model cannot listen to a sample, so it is
    pure token cost in every reply.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(data, list):
        return out
    for bucket in data:
        if not isinstance(bucket, dict):
            continue
        style = bucket.get("type")
        for entry in bucket.get("characters") or []:
            if not isinstance(entry, dict):
                continue
            character_id = entry.get("character_id")
            if character_id is None:
                continue
            out.append({
                "character_id": str(character_id),
                "character_name": entry.get("character_name"),
                "style": style,
            })
    return out


def _list_characters(group_id: int, text: str) -> str:
    """List the group's AI characters instead of guessing one.

    ``get_ai_characters`` is a read — no mute, no content policy, nothing to
    record — so it goes through ``onebot_call``, which already reuses the
    gateway's live socket when there is one.  The live-*adapter* seam is only
    needed where the gates apply.
    """
    from tools.onebot_client import onebot_call  # noqa: PLC0415 — backend call

    try:
        data = onebot_call("get_ai_characters", {"group_id": group_id})
    except Exception as exc:  # noqa: BLE001 — verbatim, plus the likely cause
        logger.error("qq_ai_voice: get_ai_characters failed: %s", exc)
        return tool_error(
            f"Could not list QQ AI voice characters for group {group_id}: {exc}. "
            f"{NAPCAT_ONLY_HINT}",
            code="onebot_failed",
        )

    characters = _flatten_characters(data)
    if not characters:
        return tool_error(
            f"Group {group_id} reported no QQ AI voice characters. {NAPCAT_ONLY_HINT}",
            code="no_characters",
        )
    return json.dumps(
        {
            "success": True,
            "sent": False,
            "group_id": group_id,
            "characters": characters,
            "count": len(characters),
            "pending_text": text or None,
            "message": (
                "Nothing was sent: no 'character' was given. Pick a "
                "'character_id' from this list and call qq_ai_voice again "
                "with it plus the text to speak."
            ),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _send_via_gateway(
    adapter: Any, loop: Any, group_id: int, text: str, character: str
) -> str:
    """Hand the send to ``voice.speak_ai_character`` on the gateway's loop."""
    from plugins.platforms.onebot import voice  # noqa: PLC0415 — gateway-only import

    hint = _backend_hint(adapter)
    try:
        outcome = run_on_adapter_loop(
            voice.speak_ai_character(adapter, f"g{group_id}", text, character), loop
        )
    except _TIMEOUTS:
        # Never retried: the coroutine may be mid-send, and Tencent posting
        # the same sentence twice in two AI voices is worse than a visible
        # failure.
        return tool_error(
            "Timed out waiting for the gateway to post the AI voice message. It "
            "may still have been delivered — check the group before retrying.",
            code="timeout",
            delivery_path=DELIVERY_GATEWAY,
        )
    except Exception as exc:  # noqa: BLE001 — surface one clear message
        logger.error("qq_ai_voice: gateway speak_ai_character failed: %s", exc)
        return tool_error(
            f"Could not post the AI voice message through the gateway: {exc}. "
            f"{hint}",
            code="gateway_failed",
            delivery_path=DELIVERY_GATEWAY,
        )

    if not outcome.spoke:
        detail = outcome.reason or "the gateway refused the AI voice message"
        failure = outcome.failure
        kind = getattr(failure, "error_kind", None) if failure else None
        # ``raw_response`` is set only when the *backend* answered with a
        # rejection, which is exactly when the extension may be unavailable on
        # this backend. A mute or a policy refusal carries no raw response and
        # must not be blamed on the backend.
        if failure is not None and getattr(failure, "raw_response", None) is not None:
            detail = f"{detail}. {hint}"
        return tool_error(
            f"AI voice message not sent: {detail}",
            code=kind or "not_spoken",
            delivery_path=DELIVERY_GATEWAY,
        )

    return json.dumps(
        {
            "success": True,
            "sent": True,
            "delivery_path": DELIVERY_GATEWAY,
            "delivery_note": DELIVERY_NOTES[DELIVERY_GATEWAY],
            "message_id": outcome.message_id,
            "message_ids": list(outcome.message_ids),
            "target": {"type": "group", "id": group_id},
            "character": character,
            "message": "AI voice message posted to the QQ group.",
        },
        ensure_ascii=False,
    )


def _send_direct(group_id: int, text: str, character: str) -> str:
    """Post over the plain OneBot transport (no gateway in this process)."""
    from tools.onebot_client import onebot_call  # noqa: PLC0415 — direct path only

    params = {
        "group_id": group_id,
        "character": character,
        # Both spellings, same value. NapCat's own docs disagree with
        # themselves about this field ("text" in the live API doc, "test" in
        # the commit that introduced it). SnowLuma v1.14.14's generated action
        # catalog (packages/mcp/src/generated/catalog.json) names "text" as
        # canonical and sets additionalProperties to true, so the redundant
        # "test" key is harmless there too. ``protocol.action_to_wire`` sends
        # both deliberately for compatibility with older NapCat builds.
        # Do not "clean up" the duplicate.
        "text": text,
        "test": text,
    }
    try:
        data = onebot_call("send_group_ai_record", params)
    except Exception as exc:  # noqa: BLE001 — verbatim, never a silent retry
        logger.error("qq_ai_voice: send_group_ai_record failed: %s", exc)
        return tool_error(
            f"Could not post the AI voice message: {exc}. {NAPCAT_ONLY_HINT}",
            code="onebot_failed",
            delivery_path=DELIVERY_DIRECT,
        )
    message_id = data.get("message_id") if isinstance(data, dict) else None
    return json.dumps(
        {
            "success": True,
            "sent": True,
            "delivery_path": DELIVERY_DIRECT,
            "delivery_note": DELIVERY_NOTES[DELIVERY_DIRECT],
            "message_id": message_id,
            "target": {"type": "group", "id": group_id},
            "character": character,
            "message": "AI voice message posted to the QQ group.",
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handle_qq_ai_voice(args: dict, **_kw: Any) -> str:
    """Handler for ``qq_ai_voice``."""
    # ``user_id`` is not in the schema, but the model reaches for it by
    # analogy with ``qq_send_voice``. Refusing it by name — with the working
    # alternative attached — beats letting it be silently ignored and a voice
    # message landing in a group the user never mentioned.
    if args.get("user_id") not in (None, ""):
        return tool_error(
            "qq_ai_voice is group-only: QQ exposes no AI voice characters for "
            "private chats. Use qq_send_voice with 'user_id' to send a "
            "normally-synthesized voice message to a person.",
            code="invalid_args",
        )

    raw_group: Optional[Any] = args.get("group_id")
    if raw_group in (None, ""):
        return tool_error(
            "qq_ai_voice requires 'group_id' — QQ AI voice characters exist "
            "only in groups.",
            code="invalid_args",
        )
    try:
        group_id = _coerce_qq_id(raw_group, "group_id")
    except ValueError as exc:
        return tool_error(f"qq_ai_voice: {exc}", code="invalid_args")

    character = str(args.get("character") or "").strip()
    text = str(args.get("text") or "").strip()

    if not character:
        # Discovery, not a failure: listing beats guessing a character_id the
        # backend would reject with a code the model cannot interpret.
        return _list_characters(group_id, text)

    if not text:
        return tool_error(
            "qq_ai_voice requires 'text' — the words the AI character should speak.",
            code="invalid_args",
        )

    # The listener-side cap, and only that one — see the module docstring for
    # why the daily budget stops at ``qq_send_voice``. Checked before the
    # backend call so an over-long script never reaches Tencent, and re-read
    # per call so a config edit applies immediately.
    limits = resolve_limits()
    length = spoken_length(text)
    if limits.text_over_cap(length):
        return tool_error(
            text_cap_refusal(length, limits.max_text_chars),
            code="text_too_long",
            text_chars=length,
            max_text_chars=limits.max_text_chars,
        )

    adapter, loop = live_adapter()
    if adapter is not None and loop is not None:
        return _send_via_gateway(adapter, loop, group_id, text, character)
    return _send_direct(group_id, text, character)


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

QQ_AI_VOICE_SCHEMA = {
    "name": QQ_AI_VOICE_TOOL,
    "description": (
        "Post a voice message into a QQ group spoken by one of QQ's own "
        "built-in AI characters (群 AI 声聊) — Tencent synthesizes the audio, "
        "not the local TTS provider. Group-only: QQ has no private-chat "
        "equivalent, so use qq_send_voice for a DM. Call it WITHOUT "
        "'character' to get the list of characters the group offers (nothing "
        "is sent), then call it again with a 'character_id' from that list. "
        "This OneBot v11 extension is implemented by NapCat and SnowLuma; "
        "Lagrange and go-cqhttp may reject these actions. The result's "
        "'delivery_path' says how it went out — "
        "'gateway' means the running bot applied the emergency mute, the "
        "Tencent content policy and its own chat bookkeeping; 'direct' means "
        "no gateway was running (CLI / cron) and the send was best-effort. "
        "A posted voice message cannot be unsent from here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "Target QQ group number. Required.",
            },
            "text": {
                "type": "string",
                "description": (
                    "The words the AI character should speak. Required when "
                    "'character' is given; optional when listing characters. "
                    "Capped at max_text_chars characters (200 by "
                    "default) — a voice bubble longer than that is a chore to "
                    "listen to whoever synthesized it, so send that as text."
                ),
            },
            "character": {
                "type": "string",
                "description": (
                    "A 'character_id' from a previous listing call. Omit it "
                    "to list the group's available characters instead of "
                    "sending — do not guess an id, the backend rejects an "
                    "unknown one with an uninformative error."
                ),
            },
        },
        "required": ["group_id"],
    },
}
