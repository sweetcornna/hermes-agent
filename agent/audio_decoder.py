"""
Audio Decoder ABC
=================

Defines the pluggable-backend interface for *container decoding* on the
transcription path: raw codec streams that no STT provider accepts, which
have to become a WAV before :func:`tools.transcription_tools.transcribe_audio`
can dispatch them. Decoders register instances via
:meth:`PluginContext.register_audio_decoder`; the transcription pipeline
consults the registry for every source whose container is not already one
the built-in providers take unconverted.

Why this is a plugin surface and not core
-----------------------------------------
The two decoders that exist today — SILK (WeChat/QQ voice notes) and AMR
(3GPP telephony) — need an optional Python dependency (``pilk``) and an
external binary (``ffmpeg``) respectively, and matter only to installs that
receive voice notes from Chinese messaging platforms. ``AGENTS.md``'s
"widen the generic plugin surface, don't special-case it in core" rule puts
them behind an extension point; ``plugins/voice_codecs/`` is the consumer
that ships with them.

Three coexisting surfaces on the transcription path — do not confuse them:

1. **Container detection** (:mod:`tools.audio_container`) — a magic-byte
   lookup table, deliberately **core** and deliberately **not** pluggable.
   A decoder *reads* :func:`~tools.audio_container.sniff_container` output
   to decide whether it claims a file; it never extends it.
2. **Decoding** (this ABC) — turning a claimed container into a file an STT
   provider accepts.
3. **Transcription** (:mod:`agent.transcription_provider`) — the STT backend
   itself. A decoder runs *before* one, for every provider, built-in or not.

Built-ins-always-win
--------------------
A decoder may not claim a container the built-in pipeline already hands to
providers unconverted (mp3/mp4/m4a/wav/webm/ogg/aac/flac). Registration is
rejected with a warning
(:func:`agent.audio_decoder_registry.register_decoder`), so no installed
plugin can insert a conversion hop into every mp3.

Content over filename
---------------------
Voice notes routinely arrive with a guessed extension — QQ hands a SILK
payload to the OneBot adapter named ``voice.amr``, and the reverse happens
too. The default :meth:`AudioDecoder.claims` therefore keys off the sniffed
container id and consults the suffix **only** when the header is
unrecognizable (header-stripped SILK is real and still decodable). Override
it at your peril.

Temp-file ownership
-------------------
Exactly one owner per outcome, and it never changes hands mid-flight:

- **Success** — :meth:`decode` returns :class:`DecodedAudio`; the *caller*
  (the transcription pipeline) removes ``cleanup_dir`` in a ``finally``
  once the STT provider is done with the file. The decoder must not delete
  it, and must not return a path outside it.
- **Failure** — :meth:`decode` raises :class:`AudioDecodeError` after
  removing everything it created. The caller gets no handle and has nothing
  to clean up.

That is the rule the in-core SILK/AMR converters already followed; it is
written down here because it is now a contract with third-party code.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import FrozenSet, Optional


class AudioDecodeError(Exception):
    """A decoder could not produce a transcribable file.

    ``str(exc)`` is shown to the user verbatim, so the message must be
    actionable — name the missing dependency and how to get it, not just
    "decode failed". Raise this for expected failures (absent optional
    dependency, missing external binary, corrupt payload). Unexpected
    exceptions are caught by the dispatcher too, but land in the logs as
    bugs rather than as guidance.
    """


@dataclass(frozen=True)
class DecodedAudio:
    """The result of a successful :meth:`AudioDecoder.decode`.

    Attributes:
        path: Absolute path to a file an STT provider accepts (in practice
            a WAV). Must exist and be non-empty when returned.
        cleanup_dir: Temporary directory holding ``path``, handed to the
            caller to remove — see "Temp-file ownership" in the module
            docstring. ``None`` means the decoder created nothing and
            ``path`` is a file it does not own.
    """

    path: str
    cleanup_dir: Optional[str] = None


class AudioDecoder(abc.ABC):
    """Abstract base class for a container/codec decoder.

    Subclasses must implement :attr:`name`, :attr:`container_ids` and
    :meth:`decode`. :attr:`extensions` and :meth:`claims` have defaults that
    encode the content-over-filename rule — override only if your codec
    needs something the default cannot express.

    Deliberately **no** ``is_available()``, unlike the STT/TTS provider
    ABCs. Those providers are selected by name in ``config.yaml``, so
    "configured but unavailable" is a state worth reporting separately. A
    decoder is selected by the *bytes of the file the user just sent*: if
    its dependency is missing, silently declining would degrade to a
    generic "Unsupported format" dead end. Report it from :meth:`decode`
    as an :class:`AudioDecodeError` naming the missing piece instead.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier, e.g. ``silk``, ``amr``.

        Lowercase, no spaces. Used as the registry key (so a reload
        replaces rather than duplicates) and in diagnostics. Dispatch keys
        off :attr:`container_ids`, not off this name.
        """

    @property
    @abc.abstractmethod
    def container_ids(self) -> FrozenSet[str]:
        """Container ids from :func:`tools.audio_container.sniff_container`.

        Non-empty, lowercase. Ids the built-in pipeline already accepts
        unconverted are refused at registration.
        """

    @property
    def extensions(self) -> FrozenSet[str]:
        """File suffixes this decoder claims when the header is unreadable.

        Also the set that widens the transcription pipeline's advertised
        supported formats, so a source this decoder can handle is not
        rejected as unsupported before it ever gets asked.

        Defaults to the canonical extension of each of :attr:`container_ids`.
        Lowercase, leading dot.
        """
        from tools.audio_container import CONTAINER_TO_EXT

        return frozenset(
            CONTAINER_TO_EXT[cid] for cid in self.container_ids if cid in CONTAINER_TO_EXT
        )

    def claims(self, *, container_id: Optional[str], suffix: str) -> bool:
        """Return whether this decoder wants to decode the described source.

        The default implements the content-over-filename rule: a recognized
        header decides on its own, and a source whose bytes match no known
        container falls back to its suffix. Must not raise — the dispatcher
        logs and skips a decoder that does.

        Args:
            container_id: :func:`~tools.audio_container.sniff_container`
                result for the file's first bytes, or ``None`` when the
                header matches nothing known.
            suffix: Lowercased file extension including the leading dot.
        """
        if container_id is not None:
            return container_id in self.container_ids
        return suffix in self.extensions

    @abc.abstractmethod
    def decode(self, file_path: str) -> DecodedAudio:
        """Decode ``file_path`` into a file an STT provider accepts.

        Args:
            file_path: Absolute path to the source. The pipeline has
                already checked it is a regular, non-symlinked file within
                the decoder size cap.

        Returns:
            :class:`DecodedAudio` whose ``path`` exists and is non-empty.

        Raises:
            AudioDecodeError: with a user-facing, actionable message. Every
                temporary file this call created must already be gone.
        """
