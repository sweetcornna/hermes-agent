"""Speaking out loud on QQ: the one outbound path for a native voice bubble.

What this is
------------
Text in, a QQ 语音消息 out — synthesized with whatever TTS provider the
operator configured, delivered as a ``record`` segment.  Not a file
attachment sitting in the group's file area, not an audio player card: the
green voice bubble a person taps to listen.

Why it is a module and not three call sites
-------------------------------------------
``OneBotAdapter.send_voice`` reaches the wire through ``_send_attachment``,
which does **not** go through ``OneBotAdapter.send``.  That shortcut skips
three guarantees the text path has, and every one of them is a promise made
to somebody:

* **The emergency mute.**  ``send`` checks :func:`~.adapter.group_speech_muted`
  before it computes anything.  ``_send_attachment`` does check it too — that
  one is honoured — but it is the *only* one of the three that survived the
  shortcut, and a path that gets two of three right is the most expensive kind
  of nearly-correct.
* **The Tencent content policy.**  ``send`` runs ``moderate_text`` and fails
  closed; the attachment path does not run it at all (the ``TODO(media)`` in
  ``adapter.send`` is an acknowledgement of exactly this).  Audio is not
  exempt from risk control: the sentence is still spoken by this account, and
  the account is still what gets frozen.
* **Self-recording.**  ``send`` appends every group post to the shared
  recent-chatter buffer with ``is_self=True``.  Without that,
  ``proactive.last_message_is_self`` reads the room as "a human spoke last"
  and the bot answers itself out loud — the worst possible place for that
  failure, because a voice message is the one form a reader cannot skim past.

So the fix is not "patch ``send_voice``".  Three consumers are queued up
behind this (a tools plugin, a voice-call session layer, a proactive-voice
lane) and each of them would otherwise grow its own half-correct copy of the
gate list.  :func:`speak` is the whole outbound contract in one place, in the
order the guarantees have to hold, and the adapter is wired over to it rather
than the other way round.

Wire form, and what we do not control
-------------------------------------
``RecordSegment(file="base64://…")``.  The base64 form is not an optimisation:
the backend may run in its own container and cannot read this
process's filesystem, so a host path handed across that boundary resolves to
nothing.  The backend then transcodes the audio to QQ's SILK codec itself —
which is why nothing here encodes SILK, and why the honest limitation is that
**the audio quality of the delivered bubble is not ours to control**.  We
choose the words and the provider; QQ chooses the bitrate.

Everything in here is offline-testable: the pure parts (envelope parsing,
file vetting) take their inputs as arguments, and the one side-effecting
collaborator — the TTS tool — is imported lazily at call time so a test can
replace it without importing a synthesizer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from gateway.platforms.base import SendResult, validate_media_delivery_path
from tools.tts_text_normalize import prepare_spoken_text

from . import protocol
from .adapter import (
    _AUDIO_EXTS,
    _response_error,
    _response_ok,
    BASE64_MAX_BYTES,
    OneBotAdapter,
    classify_onebot_error,
    encode_base64_file,
    group_speech_muted,
    guess_media_kind,
    muted_send_result,
    parse_chat_id,
    guard_outbound_text,
    record_group_message,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_AUDIO_BYTES",
    "SpeakOutcome",
    "SynthResult",
    "parse_tts_envelope",
    "speak",
    "speak_ai_character",
    "spoken_script",
    "synthesize",
    "vet_audio_file",
]

#: Ceiling on one synthesized voice file.
#:
#: The reference implementation this was ported from uses 30 MiB, chosen as
#: "big enough that nothing real hits it".  That number is wrong *here* for a
#: structural reason rather than a taste one: above
#: :data:`~.adapter.BASE64_MAX_BYTES` the adapter's ``encode_base64_file``
#: refuses to inline and its callers fall back to handing the backend a
#: literal path.  For an image that fallback is a long shot that sometimes
#: works; for a voice bubble on a containerized backend it is a guaranteed
#: silent non-delivery.  So the cap is the inlining ceiling, and crossing it
#: is a reported failure instead of a fallback nobody hears.  For scale: a
#: minute of speech is well under 1 MiB, so 8 MiB is roughly ten minutes of
#: talking — far past anything that belongs in a chat window anyway.
MAX_AUDIO_BYTES = BASE64_MAX_BYTES


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthResult:
    """What one call to the TTS backend produced.

    ``paths`` is plural because long text is chunked and then repacked
    against platform upload limits, so a single sentence-and-a-half request
    can legitimately come back as two files.  The caller owns every one of
    them: the TTS tool deletes its own intermediates and nothing else.
    """

    ok: bool
    paths: Tuple[str, ...] = ()
    provider: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class SpeakOutcome:
    """What :func:`speak` did, and why it did not.

    ``failure`` carries the canonical :class:`SendResult` for the refusal —
    the one built by :func:`~.adapter.muted_send_result` or
    :func:`~.adapter.policy_blocked_send_result`, markers and all.  It exists
    so the adapter's ``send_voice`` can be wired straight through to here
    without re-deriving a result and losing ``MUTED_MARKER`` /
    ``POLICY_MARKER`` on the way — those markers are how a caller tells "a
    local policy refused this" from "the send failed", and re-inventing the
    SendResult would quietly turn a mute into a retryable transient.
    """

    spoke: bool
    message_ids: Tuple[str, ...] = ()
    audio_bytes: int = 0
    reason: Optional[str] = None
    failure: Optional[SendResult] = None

    @property
    def message_id(self) -> Optional[str]:
        """The last delivered message id, matching ``SendResult.message_id``."""
        return self.message_ids[-1] if self.message_ids else None


def _not_spoken(reason: str, failure: Optional[SendResult] = None) -> SpeakOutcome:
    """A refusal, with the machine-readable result when one exists."""
    return SpeakOutcome(spoke=False, reason=reason, failure=failure)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def spoken_script(text: str) -> str:
    """The text a TTS provider should actually be handed.

    Markdown, ``<think>`` blocks and compact symbols are written for eyes; a
    provider reads them out literally ("asterisk asterisk"), which is how a
    voice reply ends up unlistenable.  Deterministic, so calling it twice is
    the same as calling it once — :func:`speak` relies on that to audit
    exactly the string :func:`synthesize` will later produce internally.

    The 4000-character default cap stays: it is the provider-safety limit the
    rest of the gateway uses, and a voice message that long is not a chat
    message anyway.
    """
    if not text:
        return ""
    try:
        return prepare_spoken_text(text)
    except Exception:  # noqa: BLE001 — normalisation must never lose the reply
        # Same posture as ``BasePlatformAdapter.prepare_tts_text``: a broken
        # normalizer degrades to speaking the raw words, which is ugly, rather
        # than to speaking nothing at all.
        logger.warning("OneBot voice: TTS text normalisation failed", exc_info=True)
        return str(text).strip()


def parse_tts_envelope(raw: Any) -> SynthResult:
    """Turn ``text_to_speech_tool``'s JSON string into a :class:`SynthResult`.

    Pure, and deliberately paranoid about the envelope.  Three things go
    wrong in production and all three have to come back as a *result* rather
    than an exception, because the caller has cleanup to run either way:

    * the tool returned something that is not JSON at all (a traceback, an
      empty string, ``None`` from a stubbed provider);
    * ``success`` is false — the provider's own message is surfaced verbatim,
      because a rewritten error is an error the operator cannot grep for;
    * ``success`` is true but the files are gone or were never written, which
      a downstream ``open()`` would otherwise report as a confusing
      ``FileNotFoundError`` several layers away from the cause.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        preview = str(raw)[:200] if raw is not None else "None"
        return SynthResult(
            ok=False, error=f"text_to_speech returned an unparseable result: {preview}"
        )
    if not isinstance(payload, dict):
        return SynthResult(
            ok=False,
            error=f"text_to_speech returned {type(payload).__name__}, expected an object",
        )
    provider = payload.get("provider")
    provider = str(provider) if provider else None
    if not payload.get("success"):
        return SynthResult(
            ok=False,
            provider=provider,
            error=str(payload.get("error") or "speech synthesis failed"),
        )
    # ``file_paths`` is authoritative and ``file_path`` is only its first
    # entry; older/simpler provider branches set just the singular key.
    raw_paths = payload.get("file_paths") or [payload.get("file_path")]
    paths = tuple(str(p) for p in raw_paths if p and os.path.isfile(str(p)))
    if not paths:
        return SynthResult(
            ok=False,
            provider=provider,
            error="text_to_speech reported success but produced no readable file",
        )
    return SynthResult(ok=True, paths=paths, provider=provider)


async def synthesize(
    text: str,
    *,
    provider: Optional[str] = None,
    speed: Optional[float] = None,
) -> SynthResult:
    """Speak *text* into one or more audio files.

    ``text_to_speech_tool`` is synchronous and blocking — it makes network
    calls and shells out to ffmpeg — so it runs on a worker thread, the same
    way every other async caller in the tree invokes it.  It is imported
    lazily for the reason the reference implementation records: the TTS module
    pulls in heavy optional dependencies, and importing it at module scope
    would make merely *loading* the QQ plugin pay for a feature most
    deployments never use.

    No ``output_path`` is passed.  The gateway's auto-TTS path picks one to
    force an Ogg/Opus container for platforms whose voice bubbles require it;
    QQ is not one of those — the OneBot backend transcodes to SILK itself and
    accepts whatever container the provider produced — so choosing a path here
    would only override the tool's own platform-aware default with a worse
    guess.

    Never raises for an ordinary failure: the caller has files to clean up
    and a chat to answer, and an exception at this seam turns both into a
    lost turn.
    """
    script = spoken_script(text)
    if not script:
        return SynthResult(ok=False, error="nothing to say (empty text after cleanup)")
    from tools.tts_tool import text_to_speech_tool  # noqa: PLC0415 — see docstring

    try:
        raw = await asyncio.to_thread(
            text_to_speech_tool, text=script, speed=speed, provider=provider
        )
    except Exception as exc:  # noqa: BLE001 — one clear message, never a crash
        logger.warning("OneBot voice: speech synthesis raised: %s", exc)
        return SynthResult(ok=False, provider=provider, error=f"{exc}")
    return parse_tts_envelope(raw)


# ---------------------------------------------------------------------------
# Vetting and cleanup
# ---------------------------------------------------------------------------


def vet_audio_file(
    path: str, *, max_bytes: Optional[int] = None
) -> Tuple[Optional[str], int, Optional[str]]:
    """``(safe_path, size, error)`` for one candidate voice file.

    Pure enough to unit-test against a temp dir, and it runs over the *whole*
    batch before anything is sent — a two-file result that fails vetting on
    the second file must not first post the first one, because a reader
    cannot un-hear half a sentence.

    The media-delivery policy is applied even though these paths came from
    our own TTS tool: this function is also the seam a later tools plugin
    will hand model-chosen paths to, and a check that only exists on the
    untrusted path is a check somebody eventually routes around.

    ``max_bytes`` is resolved at call time rather than bound as a default,
    for the reason ``adapter.encode_base64_file`` records about its own
    ceiling: it stays one knob instead of a value frozen at import.
    """
    if max_bytes is None:
        max_bytes = MAX_AUDIO_BYTES
    safe_path = validate_media_delivery_path(path)
    if not safe_path:
        return None, 0, "audio path rejected by the media-delivery policy"
    if guess_media_kind(safe_path) != "audio":
        wrong_kind = (
            f"{os.path.basename(safe_path)} is not an audio file "
            f"(expected one of {sorted(_AUDIO_EXTS)})"
        )
        return None, 0, wrong_kind
    try:
        size = os.path.getsize(safe_path)
    except OSError as exc:
        return None, 0, f"cannot read the audio file: {exc}"
    if size <= 0:
        return None, 0, "the audio file is empty"
    if size > max_bytes:
        too_big = (
            f"audio is {size} bytes, over the {max_bytes}-byte inlining ceiling; "
            "a larger file cannot be delivered as a voice bubble"
        )
        return None, size, too_big
    return safe_path, size, None


def _discard(paths: Iterable[str]) -> None:
    """Delete synthesized files.  Best effort, and never the reason a turn fails.

    The TTS tool cleans up its own intermediates but not what it returns, so
    without this every voice message leaks a file into the audio cache — and
    the cache is on the same small disk the gateway runs from.
    """
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


async def _policy_refusal(
    adapter: OneBotAdapter,
    target: int,
    is_group: bool,
    texts: Sequence[str],
) -> Optional[SendResult]:
    """The refusal for the first text either gate blocks, else ``None``.

    Delegates to ``adapter.guard_outbound_text`` so the voice lane runs the
    *same* two stages, in the same order, as every other outbound path.  It
    previously ran the keyword table alone: the agent gate was added to
    ``send()`` and this lane silently kept the older, weaker check, which is
    the exact drift a shared choke point exists to prevent.

    Every string that will leave the process is audited — the spoken script
    *and* the caption — because they go out as two separate messages, and a
    gate that only reads one of them is a gate with a hole the shape of the
    other.  Unlike the mute, this is not group-only: the freeze risk is
    account-level and Tencent does not care which window the words appeared in.
    """
    return await guard_outbound_text(
        *texts,
        target=target,
        is_group=is_group,
        agent_config=getattr(adapter, "config", None),
    )


# ---------------------------------------------------------------------------
# The outbound path
# ---------------------------------------------------------------------------


async def speak(
    adapter: OneBotAdapter,
    chat_id: str,
    text: str,
    *,
    caption: Optional[str] = None,
    reply_to: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    provider: Optional[str] = None,
    record_self: bool = True,
) -> SpeakOutcome:
    """Say *text* out loud into *chat_id* as a QQ voice message.

    The order below is the contract, not an implementation detail — each step
    is placed where it is because of what the step after it costs:

    1. resolve the target.  A mistyped chat id is a structured refusal, never
       an exception thrown at a caller mid-conversation.
    2. the emergency mute, groups only.  Before synthesis, so pressing the
       operator's panic switch stops the bot from *paying* to be silent, and
       before anything reaches the client at all.
    3. the account-auth guard.  Before normalization and TTS, so a known
       logged-out account performs no provider work and writes no wire action.
    4. the Tencent content policy, on the spoken script and the caption
       alike, groups and DMs alike, failing closed.  Also before synthesis:
       refused text must not be spoken into a temp file either.
    5. the connection check.  A transient, so it is checked after the
       decisions (a muted or refused message must not be retried into
       existence) but before the expensive part.
    6. synthesize, then vet the whole batch before a byte goes out.
    7. the caption first, as its own text message.  QQ renders a voice
       message as a bubble of its own and drops anything sitting beside it,
       so a caption can only be a separate, earlier message.
    8. the ``record`` segment(s), inline as ``base64://``.
    9. record the post into the shared recent-chatter buffer with
       ``is_self=True``, so the proactive persona knows the bot just spoke.
    10. delete every synthesized file, on every path out of here.

    Only the *first* outgoing message carries the quote and the @mention —
    the same rule ``adapter.send`` follows, and a deliberate difference from
    ``_send_attachment``, which leads both its caption and its media message
    and so pings the recipient twice for one reply.
    """
    try:
        is_group, target = parse_chat_id(chat_id)
    except ValueError as exc:
        return _not_spoken(
            str(exc),
            SendResult(success=False, error=str(exc), error_kind="not_found"),
        )

    if is_group and group_speech_muted(adapter):
        failure = muted_send_result(target)
        return _not_spoken(failure.error or "group speech is muted", failure)

    account_failure = adapter._account_send_guard(  # noqa: SLF001
        target, is_group
    )
    if account_failure is not None:
        return _not_spoken(
            account_failure.error or "QQ account is auth-required",
            account_failure,
        )

    script = spoken_script(text)
    if not script:
        return _not_spoken("nothing to say (empty text after cleanup)")

    refusal = await _policy_refusal(adapter, target, is_group, (script, caption or ""))
    if refusal is not None:
        return _not_spoken(refusal.error or "refused by the content policy", refusal)

    if adapter._client is None:  # noqa: SLF001 — same package, one plugin
        failure = SendResult(
            success=False,
            error="OneBot adapter is not connected",
            error_kind="transient",
            retryable=True,
        )
        return _not_spoken(failure.error or "not connected", failure)

    synth = await synthesize(text, provider=provider)
    try:
        if not synth.ok:
            # The provider's own words, unedited.  A rewritten message is one
            # the operator cannot match against the provider's own logs.
            return _not_spoken(synth.error or "speech synthesis failed")

        prepared: List[Tuple[str, int]] = []
        for raw_path in synth.paths:
            safe_path, size, error = vet_audio_file(raw_path)
            if safe_path is None:
                return _not_spoken(error or "unusable synthesized audio")
            prepared.append((safe_path, size))

        payloads: List[Tuple[str, int]] = []
        for safe_path, size in prepared:
            payload = encode_base64_file(safe_path, MAX_AUDIO_BYTES)
            if payload is None:
                # Vetting already cleared the size, so this is a read failure.
                # No literal-path fallback: the backend usually cannot see our
                # disk, and a segment it cannot resolve is silence the sender
                # believes was delivered.
                return _not_spoken(f"could not inline the audio at {safe_path}")
            payloads.append((payload, size))

        at_user_id = (metadata or {}).get("onebot_at_user_id") if is_group else None
        message_ids: List[str] = []
        first = True

        if caption:
            cap_segments = adapter._lead_segments(  # noqa: SLF001
                is_group, at_user_id, reply_to
            )
            cap_segments.append(
                protocol.TextSegment(
                    text=(" " if cap_segments and is_group else "") + caption
                )
            )
            ok, mid, failure = await adapter._send_segments(  # noqa: SLF001
                is_group, target, cap_segments
            )
            if not ok and failure is not None:
                # Stop rather than speak anyway.  The caption is the context
                # the bubble is answering; a voice message arriving without it
                # reads as the bot talking to itself, and the caller retrying
                # gets a coherent pair instead of an orphan.
                return _not_spoken(failure.error or "caption send failed", failure)
            if mid:
                message_ids.append(mid)
            first = False

        audio_bytes = 0
        for payload, size in payloads:
            segments = adapter._lead_segments(  # noqa: SLF001
                is_group,
                at_user_id if first else None,
                reply_to if first else None,
            )
            segments.append(protocol.RecordSegment(file=payload))
            ok, mid, failure = await adapter._send_segments(  # noqa: SLF001
                is_group, target, segments
            )
            if not ok and failure is not None:
                return SpeakOutcome(
                    spoke=bool(message_ids),
                    message_ids=tuple(message_ids),
                    audio_bytes=audio_bytes,
                    reason=failure.error or "voice send failed",
                    failure=failure,
                )
            if mid:
                message_ids.append(mid)
            audio_bytes += size
            first = False

        if is_group and record_self:
            # The spoken script, not "[语音]".  This buffer is prompt input:
            # the proactive persona reads it back as the transcript of the
            # room, and a placeholder there teaches it that it said nothing.
            record_group_message(
                adapter.instance_id,
                target,
                adapter._bot_nickname or "bot",  # noqa: SLF001
                script,
                True,
            )

        return SpeakOutcome(
            spoke=True,
            message_ids=tuple(message_ids),
            audio_bytes=audio_bytes,
        )
    finally:
        _discard(synth.paths)


async def speak_ai_character(
    adapter: OneBotAdapter,
    chat_id: str,
    text: str,
    character: str,
    *,
    record_self: bool = True,
) -> SpeakOutcome:
    """Speak *text* in one of QQ's own AI voice characters (群 AI 声聊).

    Group-only and a OneBot v11 extension, both of which are the platform's
    constraints rather than ours: QQ exposes no private-chat equivalent, and
    NapCat and SnowLuma implement the action while a Lagrange/go-cqhttp
    backend may reject it.

    Same gate order as :func:`speak` minus the parts that only exist because
    of local synthesis — there is no file, so nothing to vet, cap, or clean
    up, and ``audio_bytes`` is always ``0`` because the audio is produced on
    Tencent's side and never passes through this process.  The content policy
    still runs, and matters *more* here: the text is handed to Tencent as
    plaintext, so a refused sentence would be shipped straight to the party
    the policy exists to keep quiet.
    """
    try:
        is_group, target = parse_chat_id(chat_id)
    except ValueError as exc:
        return _not_spoken(
            str(exc),
            SendResult(success=False, error=str(exc), error_kind="not_found"),
        )
    if not is_group:
        return _not_spoken(
            "QQ has no AI voice characters in private chats; use speak() instead"
        )
    if group_speech_muted(adapter):
        failure = muted_send_result(target)
        return _not_spoken(failure.error or "group speech is muted", failure)

    account_failure = adapter._account_send_guard(target, True)  # noqa: SLF001
    if account_failure is not None:
        return _not_spoken(
            account_failure.error or "QQ account is auth-required",
            account_failure,
        )

    script = spoken_script(text)
    if not script:
        return _not_spoken("nothing to say (empty text after cleanup)")
    refusal = await _policy_refusal(adapter, target, True, (script,))
    if refusal is not None:
        return _not_spoken(refusal.error or "refused by the content policy", refusal)

    action = protocol.SendGroupAiRecord(
        group_id=target, character=str(character), text=script
    )
    try:
        resp = await adapter._call(action)  # noqa: SLF001
    except (asyncio.TimeoutError, TimeoutError):
        # Same optimistic rule ``_send_segments`` documents: some backends
        # never echo, and reporting a delivered voice message as failed makes
        # the caller post it a second time.
        logger.warning(
            "OneBot voice: no acknowledgement for the AI voice message — "
            "assuming it was delivered"
        )
        resp = {"status": "ok", "retcode": 0}
    except Exception as exc:  # noqa: BLE001 — transport errors are results here
        failure = SendResult(
            success=False,
            error=str(exc),
            error_kind=classify_onebot_error(None, str(exc), exc),
            retryable=True,
        )
        return _not_spoken(str(exc), failure)

    if not _response_ok(resp):
        detail = _response_error(resp)
        failure = SendResult(
            success=False,
            error=f"OneBot rejected the AI voice message: {detail}",
            error_kind=classify_onebot_error(resp.get("retcode"), detail),
            raw_response=resp,
        )
        return _not_spoken(failure.error or detail, failure)

    data = resp.get("data")
    message_ids: Tuple[str, ...] = ()
    if isinstance(data, dict) and data.get("message_id") is not None:
        message_ids = (str(data["message_id"]),)
    if record_self:
        record_group_message(
            adapter.instance_id,
            target,
            adapter._bot_nickname or "bot",  # noqa: SLF001
            script,
            True,
        )
    return SpeakOutcome(spoke=True, message_ids=message_ids)
