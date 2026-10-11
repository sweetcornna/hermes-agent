"""AMR decoder — 3GPP telephony voice notes, via an external ``ffmpeg``."""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import FrozenSet

from agent.audio_decoder import AudioDecodeError, AudioDecoder, DecodedAudio
from hermes_cli._subprocess_compat import windows_hide_flags

from .common import open_workspace, require_wav

logger = logging.getLogger(__name__)

# 16 kHz mono is what every STT backend Hermes ships wants, and AMR carries
# no more than that anyway (narrowband is 8 kHz, wideband 16 kHz mono).
_TARGET_SAMPLE_RATE = "16000"
_TARGET_CHANNELS = "1"
# ffmpeg on a corrupt stream can sit on a partial read; the source is already
# size-capped by the pipeline, so a wall-clock ceiling is the backstop.
_DECODE_TIMEOUT_SECONDS = 300


class AmrDecoder(AudioDecoder):
    """Decode an AMR (narrowband or wideband) stream to WAV.

    AMR is the 3GPP telephony codec: MMS voice clips, call recordings, and
    what QQ labels its voice notes as. ffmpeg is not lazy-installed — it is
    a system binary, not a Python package — so an install without it gets a
    message saying so rather than a silent failure.
    """

    @property
    def name(self) -> str:
        return "amr"

    @property
    def container_ids(self) -> FrozenSet[str]:
        return frozenset({"amr"})

    def decode(self, file_path: str) -> DecodedAudio:
        """Decode ``file_path`` to a 16 kHz mono WAV with ffmpeg."""
        # Reuse the pipeline's binary lookup rather than a bare
        # ``shutil.which``: it also checks the Homebrew / /usr/local prefixes
        # that are missing from a launchd or systemd PATH. Imported lazily so
        # loading this plugin does not drag in the STT module.
        from tools.transcription_tools import _find_ffmpeg_binary

        ffmpeg = _find_ffmpeg_binary()
        if not ffmpeg:
            raise AudioDecodeError(
                "Unsupported format: .amr. Install ffmpeg to enable AMR voice "
                "transcription."
            )

        temp_dir, converted_path = open_workspace("hermes-amr-", file_path)
        try:
            subprocess.run(
                [
                    ffmpeg, "-i", file_path,
                    "-ar", _TARGET_SAMPLE_RATE, "-ac", _TARGET_CHANNELS,
                    converted_path, "-y",
                ],
                check=True, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_DECODE_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL, creationflags=windows_hide_flags(),
            )
            require_wav(converted_path, "ffmpeg")
        except Exception as exc:  # noqa: BLE001 — every failure becomes one envelope
            # Ownership rule: nothing we created survives a raise.
            shutil.rmtree(temp_dir, ignore_errors=True)
            # ffmpeg says why on stderr; str(exc) alone is just "returned 1".
            details = getattr(exc, "stderr", None) or str(exc)
            logger.error(
                "Failed to convert .amr audio %s: %s", file_path, details, exc_info=True
            )
            raise AudioDecodeError(
                f"Failed to convert .amr audio for transcription: {str(details).strip()}"
            ) from exc
        return DecodedAudio(path=converted_path, cleanup_dir=temp_dir)
