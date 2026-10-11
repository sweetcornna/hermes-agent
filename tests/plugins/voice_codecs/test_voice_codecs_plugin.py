"""SILK and AMR decoding — the plugin half of the transcription dispatch.

QQ hands voice notes to the OneBot adapter with a guessed filename, so a SILK
payload routinely arrives named ``voice.amr`` (and the reverse happens too).
Deciding the codec from the suffix picks the wrong one and every QQ voice
message fails to transcribe. The two tests named for that mismatch are the
actual bug this whole line of work exists to fix.

What core still owns — that a registered decoder is consulted at all, that
built-in formats never reach the registry, that an absent decoder degrades
with a message naming the gap — is pinned in
``tests/tools/test_transcription_qq_containers.py``.

pilk and ffmpeg are always patched: no real codecs, binaries, or audio.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.audio_decoder import AudioDecodeError, AudioDecoder

import plugins.voice_codecs as voice_codecs
from plugins.voice_codecs.amr import AmrDecoder
from plugins.voice_codecs.silk import SilkDecoder

SILK_BYTES = b"\x02#!SILK_V3" + b"\x00" * 32
AMR_BYTES = b"#!AMR\n" + b"\x00" * 32
AMR_WB_BYTES = b"#!AMR-WB\n" + b"\x00" * 32
OGG_BYTES = b"OggS\x00\x02" + b"\x00" * 32
WAV_HEADER = b"RIFF\x24\x08\x00\x00WAVEfmt "


@pytest.fixture
def voice_file(tmp_path):
    """Return a factory writing *data* under *name* in a temp dir."""

    def _make(name: str, data: bytes) -> str:
        path = tmp_path / name
        path.write_bytes(data)
        return str(path)

    return _make


@pytest.fixture
def fake_pilk():
    """Install a stub ``pilk`` module whose ``silk_to_wav`` writes a WAV."""
    calls = []

    def _silk_to_wav(src, dst, **kwargs):
        calls.append((src, dst))
        with open(dst, "wb") as handle:
            handle.write(WAV_HEADER)

    module = SimpleNamespace(silk_to_wav=_silk_to_wav, calls=calls)
    with (
        patch.dict("sys.modules", {"pilk": module}),
        patch("plugins.voice_codecs.silk._HAS_PILK", True),
    ):
        yield module


@pytest.fixture
def fake_ffmpeg():
    """Pretend ffmpeg exists and have ``subprocess.run`` emit a WAV."""
    calls = []

    def _run(command, **kwargs):
        calls.append(command)
        with open(command[-2], "wb") as handle:
            handle.write(WAV_HEADER)
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        patch(
            "tools.transcription_tools._find_ffmpeg_binary",
            return_value="/usr/bin/ffmpeg",
        ),
        patch("plugins.voice_codecs.amr.subprocess.run", side_effect=_run) as mock_run,
    ):
        mock_run.commands = calls
        yield mock_run


@pytest.fixture
def registered(monkeypatch):
    """Register the real decoders into an empty registry, discovery frozen."""
    from agent import audio_decoder_registry as module

    module._reset_for_tests()
    monkeypatch.setattr(
        "hermes_cli.plugins._ensure_plugins_discovered",
        lambda force=False: None,
    )

    class _Ctx:
        def register_audio_decoder(self, decoder):
            module.register_decoder(decoder)
            return None

    voice_codecs.register(_Ctx())
    yield module
    module._reset_for_tests()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registers_exactly_two_decoders_and_no_tools(self):
        """No tools means no schema entries and no per-call token cost."""
        seen = []

        class _Ctx:
            def register_audio_decoder(self, decoder):
                seen.append(decoder)
                return None

            def register_tool(self, **kwargs):  # pragma: no cover - must not run
                raise AssertionError("voice_codecs must not register model tools")

        voice_codecs.register(_Ctx())

        assert [d.name for d in seen] == ["silk", "amr"]
        assert all(isinstance(d, AudioDecoder) for d in seen)

    def test_each_decoder_claims_only_its_own_container(self):
        assert SilkDecoder().container_ids == frozenset({"silk"})
        assert AmrDecoder().container_ids == frozenset({"amr"})

    def test_extensions_are_derived_from_the_shared_sniffer(self):
        """Extensions come from ``CONTAINER_TO_EXT``, not a second table."""
        assert SilkDecoder().extensions == frozenset({".silk"})
        assert AmrDecoder().extensions == frozenset({".amr"})

    def test_the_bundled_manifest_auto_loads_the_plugin(self):
        """``kind: backend`` is what keeps SILK/AMR working out of the box."""
        import yaml

        from hermes_cli.plugins import get_bundled_plugins_dir

        manifest = yaml.safe_load(
            (get_bundled_plugins_dir() / "voice_codecs" / "plugin.yaml").read_text()
        )
        assert manifest["name"] == "voice_codecs"
        assert manifest["kind"] == "backend"
        assert "provides_tools" not in manifest


# ---------------------------------------------------------------------------
# Container sniffing decides the decoder, not the filename
# ---------------------------------------------------------------------------


class TestContentBeatsExtension:
    def test_silk_bytes_named_amr_take_the_silk_path(
        self, registered, voice_file, fake_pilk, fake_ffmpeg
    ):
        """The exact QQ failure: SILK payload under a guessed ``.amr`` name."""
        from tools.transcription_tools import _prepare_audio_for_transcription

        path = voice_file("voice.amr", SILK_BYTES)
        prepared, cleanup_dir, error = _prepare_audio_for_transcription(path)
        shutil.rmtree(cleanup_dir or "", ignore_errors=True)

        assert error is None
        assert prepared is not None and prepared.endswith(".wav")
        assert fake_pilk.calls, "pilk was not used for SILK bytes named .amr"
        assert not fake_ffmpeg.commands, "ffmpeg must not decode SILK bytes"

    def test_amr_bytes_named_silk_take_the_amr_path(
        self, registered, voice_file, fake_pilk, fake_ffmpeg
    ):
        from tools.transcription_tools import _prepare_audio_for_transcription

        path = voice_file("voice.silk", AMR_BYTES)
        prepared, cleanup_dir, error = _prepare_audio_for_transcription(path)
        shutil.rmtree(cleanup_dir or "", ignore_errors=True)

        assert error is None
        assert prepared is not None and prepared.endswith(".wav")
        assert fake_ffmpeg.commands, "ffmpeg was not used for AMR bytes named .silk"
        assert not fake_pilk.calls, "pilk must not decode AMR bytes"

    def test_amr_wideband_is_recognized(self, registered, voice_file):
        from tools.transcription_tools import _decoder_for_source

        assert _decoder_for_source(voice_file("wb.bin", AMR_WB_BYTES)).name == "amr"

    def test_bare_silk_magic_is_recognized(self, registered, voice_file):
        from tools.transcription_tools import _decoder_for_source

        path = voice_file("v.bin", b"#!SILK_V3" + b"\x00" * 16)
        assert _decoder_for_source(path).name == "silk"

    def test_headerless_source_falls_back_to_the_suffix(self, registered, voice_file):
        """pilk accepts header-stripped SILK — the suffix stays the last resort."""
        from tools.transcription_tools import _decoder_for_source

        assert _decoder_for_source(voice_file("v.silk", b"\x00" * 32)).name == "silk"
        assert _decoder_for_source(voice_file("v.amr", b"\x00" * 32)).name == "amr"

    def test_recognized_formats_are_not_pre_converted(self, registered, voice_file):
        """No input that already transcribes today gains a conversion hop."""
        from tools.transcription_tools import _decoder_for_source

        assert _decoder_for_source(voice_file("voice.ogg", OGG_BYTES)) is None
        # Even under a bogus .amr name, real Ogg bytes go straight to STT.
        assert _decoder_for_source(voice_file("voice.amr", OGG_BYTES)) is None

    def test_ffmpeg_invocation_targets_16k_mono(
        self, registered, voice_file, fake_ffmpeg
    ):
        from tools.transcription_tools import _prepare_audio_for_transcription

        prepared, cleanup_dir, error = _prepare_audio_for_transcription(
            voice_file("voice.amr", AMR_BYTES)
        )
        shutil.rmtree(cleanup_dir or "", ignore_errors=True)

        assert error is None and prepared is not None
        command = fake_ffmpeg.commands[0]
        assert command[0] == "/usr/bin/ffmpeg"
        assert command[command.index("-ar") + 1] == "16000"
        assert command[command.index("-ac") + 1] == "1"


# ---------------------------------------------------------------------------
# Validation accepts what the decoders claim
# ---------------------------------------------------------------------------


class TestValidationAcceptsQQCodecs:
    @pytest.mark.parametrize(
        "name,data", [("voice.amr", AMR_BYTES), ("voice.silk", SILK_BYTES)]
    )
    def test_validate_audio_file_accepts(self, registered, voice_file, name, data):
        from tools.transcription_tools import _validate_audio_file

        assert _validate_audio_file(voice_file(name, data)) is None

    @pytest.mark.parametrize("ext", [".amr", ".silk"])
    def test_extensions_are_declared_supported(self, registered, ext):
        from tools.transcription_tools import supported_formats

        assert ext in supported_formats()


# ---------------------------------------------------------------------------
# Degradation when a codec backend is missing
# ---------------------------------------------------------------------------


class TestMissingBackendsDegradeCleanly:
    def test_missing_ffmpeg_returns_an_actionable_error(self, registered, voice_file):
        from tools.transcription_tools import transcribe_audio

        path = voice_file("voice.amr", AMR_BYTES)
        with patch("tools.transcription_tools._find_ffmpeg_binary", return_value=None):
            result = transcribe_audio(path)

        assert result["success"] is False
        assert "ffmpeg" in result["error"]
        assert ".amr" in result["error"]

    def test_missing_pilk_keeps_its_existing_error_shape(self, registered, voice_file):
        from tools.transcription_tools import transcribe_audio

        path = voice_file("voice.amr", SILK_BYTES)
        with (
            patch("plugins.voice_codecs.silk._HAS_PILK", False),
            patch("tools.lazy_deps.ensure", return_value=False),
            patch("plugins.voice_codecs.silk.module_available", return_value=False),
        ):
            result = transcribe_audio(path)

        assert result["success"] is False
        assert "pilk" in result["error"]

    def test_ffmpeg_failure_does_not_escape_transcribe_audio(
        self, registered, voice_file
    ):
        from tools.transcription_tools import transcribe_audio

        path = voice_file("voice.amr", AMR_BYTES)
        failure = subprocess.CalledProcessError(
            1, "ffmpeg", stderr="Invalid data found"
        )
        with (
            patch(
                "tools.transcription_tools._find_ffmpeg_binary",
                return_value="/usr/bin/ffmpeg",
            ),
            patch("plugins.voice_codecs.amr.subprocess.run", side_effect=failure),
        ):
            result = transcribe_audio(path)

        assert result["success"] is False
        assert "Invalid data found" in result["error"]

    def test_a_silent_codec_is_reported_rather_than_handed_on(
        self, registered, voice_file
    ):
        """ffmpeg exiting 0 with no output is a real failure mode."""
        with (
            patch(
                "tools.transcription_tools._find_ffmpeg_binary",
                return_value="/usr/bin/ffmpeg",
            ),
            patch(
                "plugins.voice_codecs.amr.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            pytest.raises(AudioDecodeError, match="did not produce a readable WAV"),
        ):
            AmrDecoder().decode(voice_file("v.amr", AMR_BYTES))


# ---------------------------------------------------------------------------
# Temp-file lifecycle — the decoder cleans up its own failures
# ---------------------------------------------------------------------------


class TestTempFileCleanup:
    def _spy_mkdtemp(self, created):
        real_mkdtemp = tempfile.mkdtemp

        def _spy(*args, **kwargs):
            made = real_mkdtemp(*args, **kwargs)
            created.append(made)
            return made

        return _spy

    def test_amr_conversion_failure_leaves_no_temp_dir(self, voice_file):
        created = []
        with (
            patch(
                "tools.transcription_tools._find_ffmpeg_binary",
                return_value="/usr/bin/ffmpeg",
            ),
            patch(
                "plugins.voice_codecs.common.tempfile.mkdtemp",
                side_effect=self._spy_mkdtemp(created),
            ),
            patch(
                "plugins.voice_codecs.amr.subprocess.run",
                side_effect=subprocess.TimeoutExpired("ffmpeg", 300),
            ),
            pytest.raises(AudioDecodeError),
        ):
            AmrDecoder().decode(voice_file("v.amr", AMR_BYTES))

        assert created and not os.path.exists(created[0])

    def test_silk_conversion_failure_leaves_no_temp_dir(self, voice_file, fake_pilk):
        created = []

        def _boom(src, dst, **kwargs):
            raise RuntimeError("silk stream is corrupt")

        fake_pilk.silk_to_wav = _boom
        with (
            patch(
                "plugins.voice_codecs.common.tempfile.mkdtemp",
                side_effect=self._spy_mkdtemp(created),
            ),
            pytest.raises(AudioDecodeError, match="silk stream is corrupt"),
        ):
            SilkDecoder().decode(voice_file("v.silk", SILK_BYTES))

        assert created and not os.path.exists(created[0])

    def test_a_successful_decode_hands_its_temp_dir_to_the_caller(
        self, voice_file, fake_ffmpeg
    ):
        """Ownership rule: on success the host removes ``cleanup_dir``."""
        decoded = AmrDecoder().decode(voice_file("v.amr", AMR_BYTES))
        try:
            assert decoded.cleanup_dir and os.path.isdir(decoded.cleanup_dir)
            assert os.path.dirname(decoded.path) == decoded.cleanup_dir
            assert os.path.getsize(decoded.path) > 0
        finally:
            shutil.rmtree(decoded.cleanup_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The local recovery path shares the same pre-conversion
# ---------------------------------------------------------------------------


class TestLocalFallbackSharesPreconversion:
    def test_local_fallback_decodes_before_whisper(
        self, registered, voice_file, fake_ffmpeg
    ):
        from unittest.mock import MagicMock

        from tools.transcription_tools import transcribe_audio_local_fallback

        local = MagicMock(
            return_value={"success": True, "transcript": "hi", "provider": "local"}
        )
        path = voice_file("voice.amr", AMR_BYTES)
        with (
            patch("tools.transcription_tools._HAS_FASTER_WHISPER", True),
            patch("tools.transcription_tools._transcribe_local", local),
            patch("tools.transcription_tools._load_stt_config", return_value={}),
        ):
            result = transcribe_audio_local_fallback(path)

        assert result["success"] is True
        handed_to_whisper = local.call_args[0][0]
        assert handed_to_whisper.endswith(".wav") and handed_to_whisper != path
        assert not os.path.exists(os.path.dirname(handed_to_whisper))
