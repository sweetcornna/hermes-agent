"""Shared plumbing for the two decoders: temp workspace and output checks.

Both decoders follow the same three steps — open a private temp directory,
run a codec, prove the codec actually wrote something — and both must obey
the temp-file ownership rule in :mod:`agent.audio_decoder`: the caller
removes the directory on success, the decoder removes it before raising.
Keeping the steps here means the two cannot drift apart on the half that is
about lifetimes rather than about codecs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Tuple


def open_workspace(prefix: str, source_path: str) -> Tuple[str, str]:
    """Create a private temp directory and return ``(temp_dir, wav_path)``.

    The WAV keeps the source's stem so a decoded file is still recognizable
    in logs and in an STT provider's upload metadata.
    """
    temp_dir = tempfile.mkdtemp(prefix=prefix)
    return temp_dir, os.path.join(temp_dir, f"{Path(source_path).stem}.wav")


def require_wav(wav_path: str, codec_label: str) -> None:
    """Raise unless *wav_path* is a readable, non-empty file.

    A codec that exits 0 having written nothing (or a zero-byte stub) is a
    real failure mode for both backends here. Catching it at the source
    turns "the STT provider rejected an empty upload" into a message naming
    the codec that actually failed.
    """
    path = Path(wav_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"{codec_label} did not produce a readable WAV file")


def module_available(module_name: str) -> bool:
    """Return whether *module_name* is importable, without importing it.

    Mirrors ``tools.transcription_tools._safe_find_spec``: a module already
    present in ``sys.modules`` without a ``__spec__`` (a test double, a
    frozen/embedded build) makes ``find_spec`` raise, and the honest answer
    there is "yes, it is available".
    """
    try:
        import importlib.util

        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        import sys

        return module_name in sys.modules
