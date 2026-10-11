"""SILK decoder — WeChat / QQ voice notes, via the optional ``pilk`` codec."""

from __future__ import annotations

import logging
import shutil
from typing import FrozenSet

from agent.audio_decoder import AudioDecodeError, AudioDecoder, DecodedAudio

from .common import module_available, open_workspace, require_wav

logger = logging.getLogger(__name__)

# Probed once at import so the common path (no SILK voice notes on this
# install) costs nothing per call. Re-probed after the lazy install below,
# which is the only way this can become true mid-process.
_HAS_PILK = module_available("pilk")


class SilkDecoder(AudioDecoder):
    """Decode a SILK v3 stream to WAV.

    SILK is Skype's speech codec; WeChat and QQ both send voice messages as
    raw SILK, and Tencent prefixes the stream with one byte. No STT provider
    accepts it, so it has to become a WAV first.
    """

    @property
    def name(self) -> str:
        return "silk"

    @property
    def container_ids(self) -> FrozenSet[str]:
        return frozenset({"silk"})

    def decode(self, file_path: str) -> DecodedAudio:
        """Decode ``file_path`` with ``pilk``, lazy-installing it if needed."""
        if not _HAS_PILK:
            # pilk is a tiny silk-v3 codec binding — lazy-install it on first
            # .silk voice note instead of bloating the base install.
            try:
                from tools.lazy_deps import ensure as _lazy_ensure

                _lazy_ensure("stt.silk", prompt=False)
            except Exception:  # noqa: BLE001 — an unavailable installer is not fatal
                pass
            if not module_available("pilk"):
                raise AudioDecodeError(
                    "Unsupported format: .silk. Install the optional 'pilk' "
                    "dependency to enable WeChat voice transcription."
                )

        temp_dir, converted_path = open_workspace("hermes-silk-", file_path)
        try:
            import pilk

            pilk.silk_to_wav(file_path, converted_path)
            require_wav(converted_path, "pilk")
        except Exception as exc:  # noqa: BLE001 — every failure becomes one envelope
            # Ownership rule: nothing we created survives a raise.
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.error(
                "Failed to convert .silk audio %s: %s", file_path, exc, exc_info=True
            )
            raise AudioDecodeError(
                f"Failed to convert .silk audio for transcription: {exc}"
            ) from exc
        return DecodedAudio(path=converted_path, cleanup_dir=temp_dir)
