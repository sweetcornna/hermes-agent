"""
Audio Decoder Registry
======================

Central map of registered audio decoders. Populated by plugins at
import-time via :meth:`PluginContext.register_audio_decoder`; consumed by
:mod:`tools.transcription_tools` to decode a source no STT provider accepts
(SILK, AMR, …) into a WAV before dispatch, and to widen the pipeline's
advertised supported formats accordingly.

Dispatch is by container, not by name
-------------------------------------
The sibling provider registries (TTS, STT, image-gen, …) resolve a name the
user wrote in ``config.yaml``. Nobody configures a decoder: the *bytes of
the inbound file* select one. :func:`find_decoder` is therefore the real
lookup, and ``name`` is only the registry key — it keeps re-registration,
snapshot/restore and ``hermes plugins list`` attribution working exactly
like every other registry in the family.

Built-ins-always-win
--------------------
A decoder that claimed ``mp3`` could insert a conversion hop into every
voice note on the install. So a decoder whose containers or extensions
overlap what the built-in pipeline already accepts unconverted is rejected
at registration with a warning. This is the same invariant the sibling
registries enforce on provider *names*, applied to the key that actually
selects a decoder. :mod:`tools.transcription_tools` re-checks it at
dispatch time by never consulting this registry for a built-in format.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, FrozenSet, List, Optional

from agent.audio_decoder import AudioDecoder
from hermes_constants import hermes_home_key
from tools.audio_container import CONTAINER_TO_EXT

logger = logging.getLogger(__name__)


# Extensions the built-in transcription pipeline hands to STT providers
# unconverted. **Kept in sync with ``SUPPORTED_FORMATS`` in
# :mod:`tools.transcription_tools`** — a regression test in
# ``tests/agent/test_audio_decoder_registry.py::TestBuiltinSync`` fails if
# the two drift. Importing from ``tools.transcription_tools`` directly would
# create a circular dependency (it imports this module for dispatch).
_BUILTIN_EXTENSIONS = frozenset({
    ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg",
    ".oga", ".opus", ".aac", ".flac", ".caf",
})

# The container ids those extensions correspond to, derived rather than
# duplicated so a container added to the shared sniffer is guarded the
# moment its extension is one the providers already accept.
_BUILTIN_CONTAINERS = frozenset(
    cid for cid, ext in CONTAINER_TO_EXT.items() if ext in _BUILTIN_EXTENSIONS
)


_decoders: Dict[str, AudioDecoder] = {}
_scoped_decoders: Dict[str, Dict[str, AudioDecoder]] = {}
_lock = threading.Lock()


def _validated_claim_sets(decoder: AudioDecoder) -> tuple[FrozenSet[str], FrozenSet[str]]:
    """Return ``(container_ids, extensions)`` after shape validation.

    Reading both properties once here means a decoder whose properties are
    expensive or unstable cannot make dispatch flaky — everything after
    registration works off these frozen copies.
    """
    containers = decoder.container_ids
    if not isinstance(containers, (set, frozenset)) or not containers:
        raise ValueError("Audio decoder .container_ids must be a non-empty set")
    if any(not isinstance(cid, str) or not cid.strip() for cid in containers):
        raise ValueError("Audio decoder .container_ids must contain non-empty strings")

    extensions = decoder.extensions
    if not isinstance(extensions, (set, frozenset)):
        raise ValueError("Audio decoder .extensions must be a set")
    if any(not isinstance(ext, str) or not ext.startswith(".") for ext in extensions):
        raise ValueError("Audio decoder .extensions must be dotted suffixes, e.g. '.silk'")

    return (
        frozenset(cid.strip().lower() for cid in containers),
        frozenset(ext.strip().lower() for ext in extensions),
    )


def register_decoder(decoder: AudioDecoder, *, scope: Optional[str] = None) -> None:
    """Register an audio decoder.

    Rejects:

    - Non-:class:`AudioDecoder` instances (raises :class:`TypeError`).
    - Empty/whitespace ``.name`` (raises :class:`ValueError`).
    - Malformed ``.container_ids`` / ``.extensions`` (raises
      :class:`ValueError`).
    - Containers or extensions the built-in pipeline already accepts
      unconverted (logs a warning, silently ignores — built-ins-always-win
      invariant).

    Re-registration (same ``name``) overwrites the previous entry and logs a
    debug message — makes hot-reload scenarios (tests, dev loops) behave
    predictably.
    """
    if not isinstance(decoder, AudioDecoder):
        raise TypeError(
            f"register_decoder() expects an AudioDecoder instance, "
            f"got {type(decoder).__name__}"
        )
    name = decoder.name
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Audio decoder .name must be a non-empty string")
    key = name.strip().lower()

    containers, extensions = _validated_claim_sets(decoder)
    shadowed = (containers & _BUILTIN_CONTAINERS) | (extensions & _BUILTIN_EXTENSIONS)
    if shadowed:
        logger.warning(
            "Audio decoder '%s' claims format(s) the built-in transcription "
            "pipeline already accepts (%s); registration ignored. Built-in "
            "formats always win — a decoder may only claim a container no "
            "STT provider takes unconverted.",
            key, ", ".join(sorted(shadowed)),
        )
        return

    with _lock:
        target = _decoders if scope is None else _scoped_decoders.setdefault(scope, {})
        existing = target.get(key)
        target[key] = decoder
    if existing is not None:
        logger.debug(
            "Audio decoder '%s' re-registered (was %r)",
            key, type(existing).__name__,
        )
    else:
        logger.debug(
            "Registered audio decoder '%s' (%s) for container(s) %s",
            key, type(decoder).__name__, ", ".join(sorted(containers)),
        )


def list_decoders(*, scope: Optional[str] = None) -> List[AudioDecoder]:
    """Return all registered decoders, sorted by name."""
    with _lock:
        merged = dict(_decoders)
        merged.update(_scoped_decoders.get(scope or hermes_home_key(), {}))
        items = list(merged.values())
    return sorted(items, key=lambda d: d.name)


def get_decoder(name: str, *, scope: Optional[str] = None) -> Optional[AudioDecoder]:
    """Return the decoder registered under *name*, or None.

    Name matching is case-insensitive and whitespace-tolerant, mirroring the
    sibling registries. This is introspection: dispatch goes through
    :func:`find_decoder`.
    """
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    with _lock:
        return _scoped_decoders.get(scope or hermes_home_key(), {}).get(key) or _decoders.get(key)


def find_decoder(
    *,
    container_id: Optional[str],
    suffix: str,
    scope: Optional[str] = None,
) -> Optional[AudioDecoder]:
    """Return the decoder claiming this source, or None when none does.

    Ties (two decoders claiming the same container) resolve to the
    lowest-sorting name — deterministic, so an install does not decode the
    same voice note two different ways on two different days.

    A decoder whose :meth:`~agent.audio_decoder.AudioDecoder.claims` raises
    is logged and skipped rather than allowed to break transcription for
    every other format.
    """
    normalized_container = container_id.strip().lower() if container_id else None
    normalized_suffix = (suffix or "").strip().lower()
    for decoder in list_decoders(scope=scope):
        try:
            claimed = decoder.claims(
                container_id=normalized_container, suffix=normalized_suffix
            )
        except Exception as exc:  # noqa: BLE001 — one bad decoder must not break the rest
            logger.warning(
                "Audio decoder '%s' claims() raised: %s — skipping it",
                getattr(decoder, "name", type(decoder).__name__), exc, exc_info=True,
            )
            continue
        if claimed:
            return decoder
    return None


def decodable_extensions(*, scope: Optional[str] = None) -> FrozenSet[str]:
    """Return every file extension the registered decoders claim.

    :mod:`tools.transcription_tools` unions this with its built-in formats
    so a source a decoder can handle is not rejected as unsupported before
    the decoder is ever asked.
    """
    extensions: set = set()
    for decoder in list_decoders(scope=scope):
        try:
            extensions.update(ext.strip().lower() for ext in decoder.extensions)
        except Exception as exc:  # noqa: BLE001 — same isolation as find_decoder
            logger.warning(
                "Audio decoder '%s' extensions raised: %s — skipping it",
                getattr(decoder, "name", type(decoder).__name__), exc, exc_info=True,
            )
    return frozenset(extensions)


def snapshot_registration(
    name: str, *, scope: Optional[str] = None
) -> Optional[AudioDecoder]:
    key = name.strip().lower()
    with _lock:
        target = _decoders if scope is None else _scoped_decoders.get(scope, {})
        return target.get(key)


def restore_registration(
    name: str,
    current: AudioDecoder,
    previous: Optional[AudioDecoder],
    *,
    scope: Optional[str] = None,
) -> bool:
    """Restore a plugin registration only when *current* is still installed."""
    key = name.strip().lower()
    with _lock:
        target = _decoders if scope is None else _scoped_decoders.setdefault(scope, {})
        if target.get(key) is not current:
            return False
        if previous is None:
            target.pop(key, None)
        else:
            target[key] = previous
        if scope is not None and not target:
            _scoped_decoders.pop(scope, None)
    return True


def _reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    with _lock:
        _decoders.clear()
        _scoped_decoders.clear()
