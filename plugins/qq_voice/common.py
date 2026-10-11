"""Shared plumbing for both QQ voice tools.

Three jobs live here, all of them offline-testable on purpose:

1. **Argument shaping** — coercing a QQ id the model may have sent as an int
   or as a string, and enforcing "exactly one target".
2. **Audio vetting** — everything checked before a byte reaches the network,
   so a bad path fails as a readable tool error rather than as a backend
   retcode nobody can diagnose.
3. **The transport choice** — the one decision this plugin exists to get
   right.  See :func:`live_adapter`.

Nothing in here retries.  ``tools/onebot_client.py`` spells out why: a
silently retried send double-posts, and a voice bubble is the one message
form a reader cannot skim past twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Coroutine, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIO_EXTS",
    "DELIVERY_DIRECT",
    "DELIVERY_GATEWAY",
    "DELIVERY_NOTES",
    "LIVE_CALL_TIMEOUT",
    "MAX_AUDIO_BYTES",
    "chat_id_for",
    "discard_files",
    "live_adapter",
    "read_audio_file",
    "resolve_single_target",
    "run_on_adapter_loop",
    "spoken_script",
    "synthesize_speech",
    "vet_audio_path",
]

T = TypeVar("T")

#: Containers QQ voice messages accept.  Kept in step with
#: ``plugins/platforms/onebot/adapter.py::_AUDIO_EXTS`` — the gateway path
#: vets against that set, and a plugin that accepted a superset would let a
#: file through here only for the adapter to reject it one layer down.
AUDIO_EXTS = frozenset({
    ".mp3",
    ".wav",
    ".ogg",
    ".amr",
    ".silk",
    ".m4a",
    ".flac",
    ".aac",
    ".opus",
})

#: Ceiling on one voice payload, in bytes.
#:
#: 8 MiB, not the reference implementation's 30 MiB, and the reason is
#: structural rather than taste: both delivery paths inline the audio as
#: ``base64://`` because the backend may run in its own container and cannot
#: read this process's disk.  Above ``adapter.BASE64_MAX_BYTES`` (8 MiB) the
#: gateway path refuses to inline, so a larger file is a silent non-delivery
#: there.  Matching the number keeps the two paths from disagreeing about
#: what is sendable.  For scale: a minute of speech is well under 1 MiB.
MAX_AUDIO_BYTES = 8 * 1024 * 1024

#: How long a synchronous tool handler waits on work handed to the gateway's
#: event loop.  Generous because synthesis is in there — a provider round trip
#: plus an ffmpeg transcode routinely takes tens of seconds — and because the
#: penalty for giving up early is not a retry (we never retry) but an
#: "unknown outcome" the model has to reason about.
LIVE_CALL_TIMEOUT = 180.0

#: ``delivery_path`` values reported in every result.  The model and the
#: operator both need to tell a guaranteed send from a best-effort one.
DELIVERY_GATEWAY = "gateway"
DELIVERY_DIRECT = "direct"

DELIVERY_NOTES = {
    DELIVERY_GATEWAY: (
        "Sent through the running gateway adapter: the emergency group mute, "
        "the Tencent content policy and the self-recording into the "
        "recent-chatter buffer were all applied."
    ),
    DELIVERY_DIRECT: (
        "Sent over the plain OneBot transport because no gateway is running "
        "in this process (CLI / cron). None of the gateway's mute, content "
        "policy or recent-chatter bookkeeping exists on this path — there is "
        "no gateway state here to bypass, but the send is best-effort."
    ),
}


# ---------------------------------------------------------------------------
# Targets (pure)
# ---------------------------------------------------------------------------


def _coerce_qq_id(value: Any, field: str) -> int:
    """Coerce a QQ user/group id to a positive int.

    Tolerates ids passed as either an int or a numeric string, because the
    model sends both and a JSON schema cannot make it consistent.  Raises
    ``ValueError`` with a human-readable reason — a mistyped id must fail
    loudly here rather than deliver a voice message to the wrong QQ number.
    """
    try:
        qid = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"'{field}' must be a numeric QQ id, got {value!r}") from None
    if qid <= 0:
        raise ValueError(f"'{field}' must be a positive QQ id, got {qid}")
    return qid


def resolve_single_target(args: dict, tool: str) -> Tuple[Optional[int], Optional[int]]:
    """``(user_id, group_id)`` with exactly one of them set.

    Raises ``ValueError`` when neither or both were given.  "Both" is not
    resolved by preferring one: the model asked for two different recipients
    and guessing which it meant posts the message to a chat nobody chose.
    """
    raw_user = args.get("user_id")
    raw_group = args.get("group_id")
    has_user = raw_user not in (None, "")
    has_group = raw_group not in (None, "")
    if has_user == has_group:
        raise ValueError(
            f"{tool} requires exactly one target: 'user_id' (private chat) or "
            "'group_id' (group chat)."
        )
    if has_group:
        return None, _coerce_qq_id(raw_group, "group_id")
    return _coerce_qq_id(raw_user, "user_id"), None


def chat_id_for(user_id: Optional[int], group_id: Optional[int]) -> str:
    """The adapter's own chat-id spelling — ``g<gid>`` or a bare uid.

    Mirrors ``plugins/platforms/onebot/adapter.py::format_chat_id`` rather
    than importing it: importing the adapter here would pull the whole
    gateway into a CLI-only tool call.
    """
    return f"g{group_id}" if group_id is not None else str(user_id)


# ---------------------------------------------------------------------------
# Audio (pure enough to test against a temp dir)
# ---------------------------------------------------------------------------


def vet_audio_path(path: str, *, max_bytes: Optional[int] = None) -> Tuple[str, int]:
    """``(resolved_path, size)`` for one candidate voice file.

    Raises ``ValueError`` with a human-readable reason for every problem, so
    a handler fails fast before touching the network.  Vetting happens up
    front for the same reason the gateway path vets a whole batch before
    sending any of it: a reader cannot un-hear a half-delivered message.

    Returns the path rather than the bytes because the gateway path hands the
    adapter a path and lets it do its own reading; :func:`read_audio_file` is
    the bytes-returning wrapper the direct path needs.
    """
    if max_bytes is None:
        max_bytes = MAX_AUDIO_BYTES
    resolved = os.path.expanduser(str(path))
    if not os.path.isfile(resolved):
        raise ValueError("file not found")
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in AUDIO_EXTS:
        raise ValueError(
            f"unsupported audio type '{ext}' (allowed: {sorted(AUDIO_EXTS)})"
        )
    size = os.path.getsize(resolved)
    if size == 0:
        raise ValueError("file is empty")
    if size > max_bytes:
        raise ValueError(
            f"audio is {size} bytes, over the {max_bytes}-byte inlining ceiling; "
            "a larger file cannot be delivered as a voice bubble"
        )
    return resolved, size


def read_audio_file(path: str, *, max_bytes: Optional[int] = None) -> bytes:
    """Vet a local audio file and return its bytes, for inlining as base64."""
    resolved, _size = vet_audio_path(path, max_bytes=max_bytes)
    with open(resolved, "rb") as fh:
        return fh.read()


def discard_files(paths: List[str]) -> None:
    """Delete synthesized files.  Best effort, never the reason a call fails.

    ``text_to_speech_tool`` cleans up its own intermediates but not what it
    returns, so without this every voice message leaks a file into the audio
    cache — which sits on the same small disk the agent runs from.
    """
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Synthesis (direct path only — the gateway path synthesizes inside speak())
# ---------------------------------------------------------------------------


def spoken_script(text: str) -> str:
    """The text a TTS provider should actually be handed.

    Markdown, ``<think>`` blocks and compact symbols are written for eyes; a
    provider reads them out literally ("asterisk asterisk"), which is how a
    voice reply ends up unlistenable.  ``voice.speak()`` runs the identical
    normalizer, so applying it here is what keeps the two delivery paths from
    producing audibly different messages for the same input.
    """
    if not text:
        return ""
    try:
        from tools.tts_text_normalize import prepare_spoken_text

        return prepare_spoken_text(text)
    except Exception:  # noqa: BLE001 — a broken normalizer must not lose the message
        logger.warning("qq_voice: TTS text normalisation failed", exc_info=True)
        return str(text).strip()


def synthesize_speech(text: str) -> List[str]:
    """Synthesize *text* through the configured TTS backend; return file paths.

    Reuses the in-tree ``text_to_speech`` tool — whichever provider and voice
    the operator configured under ``tts:`` in ``~/.hermes/config.yaml`` — so
    no second TTS integration is introduced here.  Imported lazily because
    the TTS module pulls in heavy optional dependencies and importing it at
    module scope would make merely *loading* this plugin pay for a feature
    most deployments never use.

    The result is a list because long text is chunked: a single request can
    legitimately come back as several files, and dropping all but the first
    would silently truncate the message.  Raises ``RuntimeError`` on failure,
    with the provider's own wording — a rewritten error is one the operator
    cannot grep for in the provider's logs.
    """
    from tools.tts_tool import text_to_speech_tool  # noqa: PLC0415 — see docstring

    script = spoken_script(text)
    if not script:
        raise RuntimeError("nothing to say (empty text after cleanup)")
    raw = text_to_speech_tool(text=script)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        preview = str(raw)[:200] if raw is not None else "None"
        raise RuntimeError(
            f"text_to_speech returned an unparseable result: {preview}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"text_to_speech returned {type(payload).__name__}, expected an object"
        )
    if not payload.get("success"):
        raise RuntimeError(str(payload.get("error") or "speech synthesis failed"))
    # ``file_paths`` is authoritative; ``file_path`` is only its first entry
    # and is all the simpler provider branches set.
    raw_paths = payload.get("file_paths") or [payload.get("file_path")]
    paths = [str(p) for p in raw_paths if p and os.path.isfile(str(p))]
    if not paths:
        raise RuntimeError(
            "text_to_speech reported success but produced no readable file"
        )
    return paths


# ---------------------------------------------------------------------------
# The transport choice
# ---------------------------------------------------------------------------


def live_adapter() -> Tuple[Optional[Any], Optional[asyncio.AbstractEventLoop]]:
    """``(adapter, loop)`` when a gateway is live in this process, else ``(None, None)``.

    This is the decision the whole plugin turns on.  A live adapter means the
    send must go through ``plugins/platforms/onebot/voice.py``: the raw
    transport would happily speak into a group the operator has
    emergency-muted, would never run the Tencent content policy, and would
    never record the post into the recent-chatter buffer — so the proactive
    persona reads the room as if the bot had said nothing and talks over
    itself.  ``(None, None)`` means CLI or cron, where none of that state
    exists to bypass; that is the normal answer there, not an error.

    Three ways to end up on the fallback path, all deliberate:

    * the plugin is not importable (``plugins.platforms.onebot`` disabled);
    * nothing has published an adapter, or its socket has since dropped —
      ``get_live_adapter`` checks that for us;
    * we are already running *on* the adapter's own loop, where blocking on
      it would deadlock.  Same guard ``tools/onebot_client.py::
      _try_live_client`` keeps, for the same reason.
    """
    try:
        from plugins.platforms.onebot.client import get_live_adapter
    except Exception:  # noqa: BLE001 — plugin absent / not importable here
        return None, None

    adapter, loop = get_live_adapter()
    if adapter is None or loop is None or loop.is_closed():
        return None, None

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None and running is loop:
        return None, None
    return adapter, loop


def run_on_adapter_loop(
    coro: Coroutine[Any, Any, T],
    loop: asyncio.AbstractEventLoop,
    *,
    timeout: float = LIVE_CALL_TIMEOUT,
) -> T:
    """Run *coro* on the gateway's loop from this synchronous handler.

    ``asyncio.run_coroutine_threadsafe`` is the same hand-off
    ``tools/onebot_client.py::_try_live_client`` uses, and for the same
    reason: a tool handler runs on a worker thread while the adapter owns its
    own loop on another.

    A timeout here is *not* retried and not swallowed — the coroutine may
    still be mid-send, and the honest report is "outcome unknown", which the
    caller turns into a visible tool error.
    """
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)
