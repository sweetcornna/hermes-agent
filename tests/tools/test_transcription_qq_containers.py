"""Decoder dispatch on the transcription path — the core half.

QQ hands voice notes to the OneBot adapter with a guessed filename, so a SILK
payload routinely arrives named ``voice.amr`` (and the reverse happens too).
Deciding the decoder from the suffix picks the wrong codec and every QQ voice
message fails to transcribe. That bug is why this dispatch exists.

Core no longer knows what SILK or AMR *are*: the codecs ship as
``plugins/voice_codecs/`` and register through
``agent.audio_decoder_registry``. So this file pins the half core still owns —
that a registered decoder is consulted, that content beats filename, that an
absent decoder degrades with a message naming the gap, that built-in formats
never reach the registry at all, and that the temp directory has exactly one
owner. The SILK/AMR specifics (pilk, ffmpeg, magic bytes, error strings) are
pinned in ``tests/plugins/voice_codecs/test_voice_codecs_plugin.py``.

No real codecs, binaries, or audio: the decoders here are fakes.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import shutil
import textwrap
from pathlib import Path
from typing import FrozenSet, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from agent.audio_decoder import AudioDecodeError, AudioDecoder, DecodedAudio

pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")

# Real magic bytes: the sniffer is core and shared, so the dispatch has to be
# exercised through the ids it actually emits. Which decoder claims them is
# what these tests fake.
SILK_BYTES = b"\x02#!SILK_V3" + b"\x00" * 32
AMR_BYTES = b"#!AMR\n" + b"\x00" * 32
OGG_BYTES = b"OggS\x00\x02" + b"\x00" * 32
WAV_HEADER = b"RIFF\x24\x08\x00\x00WAVEfmt "


class FakeDecoder(AudioDecoder):
    """A decoder that records what it was asked and writes a stub WAV."""

    def __init__(
        self,
        name: str,
        containers: FrozenSet[str],
        *,
        raises: Optional[BaseException] = None,
        emit_output: bool = True,
    ) -> None:
        self._name = name
        self._containers = frozenset(containers)
        self._raises = raises
        self._emit_output = emit_output
        self.claim_calls: List[Tuple[Optional[str], str]] = []
        self.decoded: List[str] = []
        self.temp_dirs: List[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def container_ids(self) -> FrozenSet[str]:
        return self._containers

    def claims(self, *, container_id: Optional[str], suffix: str) -> bool:
        self.claim_calls.append((container_id, suffix))
        return super().claims(container_id=container_id, suffix=suffix)

    def decode(self, file_path: str) -> DecodedAudio:
        import tempfile

        self.decoded.append(file_path)
        if self._raises is not None:
            raise self._raises
        temp_dir = tempfile.mkdtemp(prefix=f"hermes-{self._name}-")
        self.temp_dirs.append(temp_dir)
        out = os.path.join(temp_dir, f"{Path(file_path).stem}.wav")
        if self._emit_output:
            Path(out).write_bytes(WAV_HEADER)
        return DecodedAudio(path=out, cleanup_dir=temp_dir)


@pytest.fixture
def registry(monkeypatch):
    """An empty decoder registry with plugin discovery frozen.

    The bundled ``voice_codecs`` plugin registers the real SILK/AMR decoders
    on discovery; without freezing it these tests would be asserting about
    pilk and ffmpeg instead of about the dispatch.
    """
    from agent import audio_decoder_registry as module

    module._reset_for_tests()
    monkeypatch.setattr(
        "hermes_cli.plugins._ensure_plugins_discovered",
        lambda force=False: None,
    )
    yield module
    module._reset_for_tests()


@pytest.fixture
def voice_file(tmp_path):
    """Return a factory writing *data* under *name* in a temp dir."""

    def _make(name: str, data: bytes) -> str:
        path = tmp_path / name
        path.write_bytes(data)
        return str(path)

    return _make


def _register(registry, decoder: FakeDecoder) -> FakeDecoder:
    registry.register_decoder(decoder)
    return decoder


# ---------------------------------------------------------------------------
# Container sniffing decides the decoder, not the filename
# ---------------------------------------------------------------------------


class TestContentBeatsExtension:
    def test_bytes_of_one_codec_under_another_name_pick_the_right_decoder(
        self, registry, voice_file
    ):
        """The exact QQ failure, with the codecs abstracted away."""
        from tools.transcription_tools import _prepare_audio_for_transcription

        silk = _register(registry, FakeDecoder("silk", frozenset({"silk"})))
        amr = _register(registry, FakeDecoder("amr", frozenset({"amr"})))

        prepared, cleanup_dir, error = _prepare_audio_for_transcription(
            voice_file("voice.amr", SILK_BYTES)
        )
        shutil.rmtree(cleanup_dir or "", ignore_errors=True)

        assert error is None
        assert prepared is not None and prepared.endswith(".wav")
        assert silk.decoded, "the silk decoder was not consulted for SILK bytes"
        assert not amr.decoded, "the amr decoder must not see SILK bytes"

    def test_the_reverse_mismatch_picks_the_other_decoder(self, registry, voice_file):
        from tools.transcription_tools import _prepare_audio_for_transcription

        silk = _register(registry, FakeDecoder("silk", frozenset({"silk"})))
        amr = _register(registry, FakeDecoder("amr", frozenset({"amr"})))

        prepared, cleanup_dir, error = _prepare_audio_for_transcription(
            voice_file("voice.silk", AMR_BYTES)
        )
        shutil.rmtree(cleanup_dir or "", ignore_errors=True)

        assert error is None and prepared is not None
        assert amr.decoded and not silk.decoded

    def test_headerless_source_falls_back_to_the_suffix(self, registry, voice_file):
        """A header-stripped stream is still decodable — the suffix is the last resort."""
        from tools.transcription_tools import _decoder_for_source

        _register(registry, FakeDecoder("silk", frozenset({"silk"})))
        _register(registry, FakeDecoder("amr", frozenset({"amr"})))

        assert _decoder_for_source(voice_file("v.silk", b"\x00" * 32)).name == "silk"
        assert _decoder_for_source(voice_file("v.amr", b"\x00" * 32)).name == "amr"

    def test_recognized_builtin_container_is_never_pre_converted(
        self, registry, voice_file
    ):
        """No input that already transcribes today gains a conversion hop."""
        from tools.transcription_tools import _decoder_for_source

        _register(registry, FakeDecoder("silk", frozenset({"silk"})))
        _register(registry, FakeDecoder("amr", frozenset({"amr"})))

        assert _decoder_for_source(voice_file("voice.ogg", OGG_BYTES)) is None
        # Even under a bogus .amr name, real Ogg bytes go straight to STT.
        assert _decoder_for_source(voice_file("voice.amr", OGG_BYTES)) is None

    def test_builtin_formats_never_reach_the_registry(self, registry, voice_file):
        """A plugin decoder is not even asked about a format providers accept.

        Registration already refuses a built-in container; short-circuiting
        also keeps every ordinary voice note off the plugin-discovery path.
        """
        from tools.transcription_tools import _decoder_for_source

        greedy = _register(registry, FakeDecoder("silk", frozenset({"silk"})))

        assert _decoder_for_source(voice_file("voice.ogg", OGG_BYTES)) is None
        assert _decoder_for_source(voice_file("voice.wav", b"\x00" * 32)) is None
        assert greedy.claim_calls == []


# ---------------------------------------------------------------------------
# Validation follows the registry
# ---------------------------------------------------------------------------


class TestValidationFollowsTheRegistry:
    @pytest.mark.parametrize(
        "name,data", [("voice.amr", AMR_BYTES), ("voice.silk", SILK_BYTES)]
    )
    def test_a_registered_decoders_formats_are_accepted(
        self, registry, voice_file, name, data
    ):
        from tools.transcription_tools import _validate_audio_file

        _register(registry, FakeDecoder("silk", frozenset({"silk"})))
        _register(registry, FakeDecoder("amr", frozenset({"amr"})))

        assert _validate_audio_file(voice_file(name, data)) is None

    @pytest.mark.parametrize("ext", [".amr", ".silk"])
    def test_decoder_extensions_are_advertised_as_supported(self, registry, ext):
        from tools.transcription_tools import SUPPORTED_FORMATS, supported_formats

        _register(registry, FakeDecoder("silk", frozenset({"silk"})))
        _register(registry, FakeDecoder("amr", frozenset({"amr"})))

        assert ext in supported_formats()
        # ...but the constant stays the built-in half only. A format nothing
        # can decode must not be advertised as supported.
        assert ext not in SUPPORTED_FORMATS

    def test_absent_decoder_names_the_gap_rather_than_saying_unsupported(
        self, registry, voice_file
    ):
        """With no decoder installed the message must say what is missing."""
        from tools.transcription_tools import _validate_audio_file

        result = _validate_audio_file(voice_file("voice.amr", AMR_BYTES))

        assert result is not None and result["success"] is False
        assert "No audio decoder plugin claims" in result["error"]
        assert "amr" in result["error"]

    def test_a_genuinely_unknown_format_keeps_the_plain_message(
        self, registry, voice_file
    ):
        """The gap clause is for identified containers, not for junk."""
        from tools.transcription_tools import _validate_audio_file

        result = _validate_audio_file(voice_file("notes.xyz", b"\x00" * 32))

        assert result is not None
        assert "Unsupported format: .xyz" in result["error"]
        assert "No audio decoder plugin claims" not in result["error"]


# ---------------------------------------------------------------------------
# A failing decoder degrades; it never ends the turn
# ---------------------------------------------------------------------------


class TestDecoderFailuresDegradeCleanly:
    def test_decode_error_message_reaches_the_caller_verbatim(
        self, registry, voice_file
    ):
        from tools.transcription_tools import transcribe_audio

        _register(
            registry,
            FakeDecoder(
                "amr",
                frozenset({"amr"}),
                raises=AudioDecodeError(
                    "Unsupported format: .amr. Install ffmpeg to enable AMR "
                    "voice transcription."
                ),
            ),
        )

        result = transcribe_audio(voice_file("voice.amr", AMR_BYTES))

        assert result["success"] is False
        assert "Install ffmpeg" in result["error"]

    def test_an_unexpected_decoder_crash_becomes_an_envelope(
        self, registry, voice_file
    ):
        from tools.transcription_tools import transcribe_audio

        _register(
            registry,
            FakeDecoder("amr", frozenset({"amr"}), raises=RuntimeError("boom")),
        )

        result = transcribe_audio(voice_file("voice.amr", AMR_BYTES))

        assert result["success"] is False
        assert "boom" in result["error"]

    def test_a_decoder_that_produces_nothing_is_reported(self, registry, voice_file):
        from tools.transcription_tools import transcribe_audio

        broken = _register(
            registry,
            FakeDecoder("amr", frozenset({"amr"}), emit_output=False),
        )

        result = transcribe_audio(voice_file("voice.amr", AMR_BYTES))

        assert result["success"] is False
        assert "did not produce a readable file" in result["error"]
        # ...and the directory it opened is not left behind.
        assert not os.path.exists(broken.temp_dirs[0])

    def test_a_decoder_whose_claims_raises_is_skipped_not_fatal(
        self, registry, voice_file
    ):
        from tools.transcription_tools import _decoder_for_source

        class Exploding(FakeDecoder):
            def claims(self, *, container_id, suffix):
                raise RuntimeError("claims exploded")

        _register(registry, Exploding("bad", frozenset({"silk"})))
        good = _register(registry, FakeDecoder("good", frozenset({"amr"})))

        assert _decoder_for_source(voice_file("v.amr", AMR_BYTES)) is good
        assert _decoder_for_source(voice_file("v.silk", SILK_BYTES)) is None


# ---------------------------------------------------------------------------
# Temp-file lifecycle — the host owns cleanup on success
# ---------------------------------------------------------------------------


class TestTempFileCleanup:
    def _run_with_provider(self, path, provider_result):
        from tools.transcription_tools import transcribe_audio

        seen = {}

        def _capture(file_path, *args, **kwargs):
            seen["path"] = file_path
            return provider_result

        with patch(
            "tools.transcription_tools._transcribe_prepared_audio", side_effect=_capture
        ):
            return seen, transcribe_audio(path)

    def test_converted_temp_dir_is_removed_after_success(self, registry, voice_file):
        _register(registry, FakeDecoder("amr", frozenset({"amr"})))
        path = voice_file("voice.amr", AMR_BYTES)

        seen, result = self._run_with_provider(
            path, {"success": True, "transcript": "hi", "provider": "local"}
        )

        assert result["success"] is True
        assert seen["path"] != path, "the raw source reached the provider"
        assert not os.path.exists(seen["path"])
        assert not os.path.exists(os.path.dirname(seen["path"]))

    def test_converted_temp_dir_is_removed_after_failure(self, registry, voice_file):
        _register(registry, FakeDecoder("amr", frozenset({"amr"})))
        path = voice_file("voice.amr", AMR_BYTES)

        seen, result = self._run_with_provider(
            path, {"success": False, "transcript": "", "error": "provider exploded"}
        )

        assert result["success"] is False
        assert not os.path.exists(os.path.dirname(seen["path"]))

    def test_a_decoder_that_raises_leaves_nothing_for_the_host_to_clean(
        self, registry, voice_file
    ):
        """Failure cleanup belongs to the decoder — the host gets no handle."""
        from tools.transcription_tools import _prepare_audio_for_transcription

        _register(
            registry,
            FakeDecoder("amr", frozenset({"amr"}), raises=AudioDecodeError("nope")),
        )

        prepared, cleanup_dir, error = _prepare_audio_for_transcription(
            voice_file("v.amr", AMR_BYTES)
        )

        assert prepared is None and cleanup_dir is None
        assert error is not None and error["error"] == "nope"


# ---------------------------------------------------------------------------
# The size cap runs before a decoder does
# ---------------------------------------------------------------------------


class TestDecoderSourcesAreCapped:
    def test_oversized_decoder_source_is_rejected_before_decoding(
        self, registry, voice_file, monkeypatch
    ):
        from tools.transcription_tools import transcribe_audio

        decoder = _register(registry, FakeDecoder("amr", frozenset({"amr"})))
        monkeypatch.setattr("tools.transcription_tools.MAX_FILE_SIZE", 8)

        result = transcribe_audio(voice_file("voice.amr", AMR_BYTES))

        assert result["success"] is False
        assert "File too large" in result["error"]
        assert not decoder.decoded, "the decoder ran on an over-cap source"


# ---------------------------------------------------------------------------
# The local recovery path shares the same dispatch
# ---------------------------------------------------------------------------


class TestLocalFallbackSharesTheDispatch:
    def test_local_fallback_decodes_before_whisper(self, registry, voice_file):
        from tools.transcription_tools import transcribe_audio_local_fallback

        _register(registry, FakeDecoder("amr", frozenset({"amr"})))
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


# ---------------------------------------------------------------------------
# The point of the exercise: core carries no codec knowledge
# ---------------------------------------------------------------------------


class TestCoreCarriesNoCodecKnowledge:
    _SOURCE = Path(
        inspect.getsourcefile(__import__("tools.transcription_tools", fromlist=["x"]))
    ).read_text(encoding="utf-8")

    @pytest.mark.parametrize("token", ["silk", "amr", "pilk"])
    def test_no_codec_name_survives_in_core(self, token):
        """Extend, don't special-case: the codec names belong to the plugin."""
        hits = re.findall(rf"\b{token}\b", self._SOURCE, flags=re.IGNORECASE)
        assert not hits, f"tools/transcription_tools.py still names '{token}'"

    def test_no_codec_magic_bytes_survive_in_core(self):
        for magic in ("#!AMR", "#!SILK", r"\x02!"):
            assert magic not in self._SOURCE, (
                f"tools/transcription_tools.py grew a magic byte pattern: {magic}"
            )

    def test_the_dispatch_helpers_carry_no_byte_literals_or_codec_tools(self):
        """Guards the whole refactor — a second copy of the rules starts here.

        Mirrors ``tests/gateway/test_qqbot.py::
        test_detection_helpers_carry_no_magic_bytes``.
        """
        from tools import transcription_tools

        for fn in (
            transcription_tools._sniff_source_container,
            transcription_tools._decoder_for_source,
            transcription_tools._prepare_audio_for_transcription,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            body = tree.body[0].body
            if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # drop the docstring; prose may explain a magic
            code = "\n".join(ast.unparse(node) for node in body)
            literals = [
                node.value
                for stmt in body
                for node in ast.walk(stmt)
                if isinstance(node, ast.Constant) and isinstance(node.value, bytes)
            ]
            assert not literals, (
                f"{fn.__name__} grew its own magic bytes: {literals}"
            )
            for forbidden in ("_find_ffmpeg_binary", "subprocess", "tempfile"):
                assert forbidden not in code, (
                    f"{fn.__name__} reaches for {forbidden}; decoding is the "
                    f"plugin's job"
                )
