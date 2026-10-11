"""SILK and AMR voice-note decoders for the transcription pipeline.

Layout::

    plugins/voice_codecs/
    ├── plugin.yaml   kind: backend (bundled, auto-loaded)
    ├── __init__.py   register(ctx) — two ctx.register_audio_decoder calls
    ├── common.py     shared temp-workspace + output checks
    ├── silk.py       SilkDecoder — optional ``pilk`` dependency
    └── amr.py        AmrDecoder  — external ``ffmpeg`` binary

Why a plugin and not ``tools/transcription_tools.py``: these two codecs
matter to installs that receive Chinese-messaging voice notes and to nobody
else, and each one needs something Hermes does not ship (an optional Python
package, an external binary). ``AGENTS.md``'s rubric puts that behind a
generic extension point rather than a branch in a shared core file — so
:mod:`agent.audio_decoder` is the surface and this is its consumer.

Why not scoped to QQ: SILK is WeChat's voice codec as much as QQ's, and AMR
is the generic 3GPP telephony codec that turns up on SMS/MMS bridges and
call recordings. A ``qq_voice``-scoped decoder would have to be re-shipped
for the next bridge that carries the same bytes.

This plugin registers **no tools**. It adds nothing to the model's schema
and costs no tokens on any API call; its whole contribution is two entries
in the audio-decoder registry. Neither ``pilk`` nor ``ffmpeg`` is touched at
registration time — that happens inside ``decode()``, when a voice note in
that codec actually arrives.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def register(ctx) -> None:
    """Register the SILK and AMR decoders. Called once by the plugin loader."""
    from .amr import AmrDecoder
    from .silk import SilkDecoder

    for decoder in (SilkDecoder(), AmrDecoder()):
        ctx.register_audio_decoder(decoder)
