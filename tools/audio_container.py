"""Shared magic-byte audio/AV container detection.

ONE sniffer owns container detection for the whole codebase:

- **Outbound** (``tools/tts_tool.py``): TTS backends silently ignore the
  requested opus format (Edge emits MP3, Piper writes WAV, ...), so the
  synthesized file is sniffed and repaired when the bytes don't match the
  ``.ogg`` extension (PR #73072).
- **Inbound** (``gateway/platforms/base.py`` ``cache_audio_from_bytes`` /
  ``cache_audio_from_url``): platform adapters frequently pass a wrong or
  guessed extension for voice notes (Telegram ``.oga``, iOS Signal M4A-branded
  MP4, RIFF/WAVE attachments). The cache sniffs the real container so STT and
  downstream players get an honest extension — the inbound mirror of the
  outbound repair.
- ``gateway/platforms/signal.py`` ``_guess_extension`` delegates its audio/AV
  branches here instead of duplicating the byte patterns.

Detection notes:

- RIFF needs the form-type at bytes 8-11 to split ``WAVE`` (wav) from ``WEBP``
  (image — deliberately NOT handled here; this module only claims audio/AV
  containers, callers check images first).
- ``ftyp`` needs the brand at bytes 8-11 to split audio brands (``M4A ``,
  ``M4B ``) from video brands (isom/mp42/avc1/qt).
- The ``0xFF 0xFx`` sync word is shared by MP3 and ADTS AAC; bits 3-1 of
  byte 1 disambiguate (ADTS: ``ID=0``, ``layer=00``).
- AMR and SILK are raw codec streams, not real containers, but QQ/WeChat voice
  notes arrive as one of the two under a guessed ``.amr`` name — without them
  here the guessed extension survives onto disk and STT rejects the file.
  Tencent prefixes SILK with one byte, so the SILK magic is accepted at offset
  0 and offset 1.
"""

from __future__ import annotations

from typing import Optional

# Container id -> canonical file extension.
CONTAINER_TO_EXT = {
    "m4a": ".m4a",
    "mp4": ".mp4",
    "ogg": ".ogg",
    "flac": ".flac",
    "wav": ".wav",
    "mp3": ".mp3",
    "aac": ".aac",
    "webm": ".webm",
    "amr": ".amr",
    "silk": ".silk",
}

# MP4 ftyp brands that mean "this is audio" (iOS voice notes use M4A ).
_MP4_AUDIO_BRANDS = (b"m4a ", b"m4b ")

# Covers narrowband (``#!AMR\n``), wideband (``#!AMR-WB\n``) and the
# multi-channel ``_MC1.0`` variants of both.
_AMR_MAGIC = b"#!AMR"
_SILK_MAGIC = b"#!SILK"
# Tencent's one-byte SILK prefix. Both spellings are in the wild — the full
# magic pushed to offset 1, and the truncated ``\x02!`` header that
# ``gateway/platforms/qqbot/adapter.py`` already keys off.
_SILK_TENCENT_HEADER = b"\x02!"


def sniff_container(data: bytes) -> Optional[str]:
    """Return a container id from magic bytes, or ``None`` when unknown.

    Possible ids: ``m4a``, ``mp4``, ``ogg``, ``flac``, ``wav``, ``mp3``,
    ``aac``, ``webm``, ``amr``, ``silk``. Only audio/AV containers are
    claimed — images (including RIFF/WEBP) return ``None`` so callers can
    layer their own image detection first.
    """
    if len(data) >= 8 and data[4:8] == b"ftyp":
        # Brand at bytes 8-11: audio brands ("M4A ", "M4B ") are voice
        # notes / audiobooks; everything else (isom/mp42/avc1/qt) is video.
        if len(data) >= 12 and data[8:12].lower() in _MP4_AUDIO_BRANDS:
            return "m4a"
        return "mp4"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3"):
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # ``0xFF 0xFx`` is shared by MP3 and ADTS AAC. Bits 3-1 of byte 1
        # disambiguate: ADTS has ``ID=0`` and ``layer=00`` (mask 0xF6,
        # target 0xF0); MP3 has ``ID=1`` and ``layer`` in {01,10,11}.
        if (data[1] & 0xF6) == 0xF0:
            return "aac"
        return "mp3"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if data.startswith(_SILK_MAGIC) or data[1:].startswith(_SILK_MAGIC):
        return "silk"
    if data.startswith(_SILK_TENCENT_HEADER):
        return "silk"
    if data.startswith(_AMR_MAGIC):
        return "amr"
    return None


def sniff_audio_ext(data: bytes, fallback_ext: str = ".ogg") -> str:
    """Return a container-matching extension, or ``fallback_ext`` when unknown.

    Used on inbound audio paths where the caller *claims* the bytes are audio:
    generic MP4 containers are mapped to ``.m4a`` (audio-in-MP4) because in an
    audio context the payload is AAC audio regardless of brand — STT accepts
    ``.m4a``/``.mp4`` but voice-bubble routing keys off audio extensions.
    """
    fallback = fallback_ext if fallback_ext.startswith(".") else f".{fallback_ext}"
    container = sniff_container(data)
    if container is None:
        return fallback
    if container == "mp4":
        return ".m4a"
    return CONTAINER_TO_EXT[container]
