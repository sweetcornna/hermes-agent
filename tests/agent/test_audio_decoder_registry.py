"""Contract tests for the audio-decoder registry and its ABC.

The seventh registry in the provider family, and the one whose selection key
is not a name the user configured but the bytes of the file that just
arrived. So the interesting properties are: a decoder cannot shadow a format
the built-in pipeline already accepts, content outranks filename, and one
misbehaving decoder cannot take transcription down for the rest.
"""

from __future__ import annotations

from typing import FrozenSet, Optional

import pytest

from agent.audio_decoder import AudioDecodeError, AudioDecoder, DecodedAudio


class Fake(AudioDecoder):
    def __init__(self, name: str, containers, extensions=None) -> None:
        self._name = name
        self._containers = frozenset(containers)
        self._extensions = extensions

    @property
    def name(self) -> str:
        return self._name

    @property
    def container_ids(self) -> FrozenSet[str]:
        return self._containers

    @property
    def extensions(self) -> FrozenSet[str]:
        if self._extensions is None:
            return super().extensions
        return frozenset(self._extensions)

    def decode(self, file_path: str) -> DecodedAudio:
        return DecodedAudio(path=file_path)


@pytest.fixture
def registry():
    from agent import audio_decoder_registry as module

    module._reset_for_tests()
    yield module
    module._reset_for_tests()


class TestRegistration:
    def test_registers_and_looks_up_by_name(self, registry):
        decoder = Fake("silk", {"silk"})
        registry.register_decoder(decoder)

        assert registry.get_decoder("silk") is decoder
        assert registry.get_decoder("  SILK  ") is decoder
        assert registry.list_decoders() == [decoder]

    def test_rejects_a_non_decoder(self, registry):
        with pytest.raises(TypeError):
            registry.register_decoder("not a decoder")

    def test_rejects_an_empty_name(self, registry):
        with pytest.raises(ValueError, match="non-empty string"):
            registry.register_decoder(Fake("   ", {"silk"}))

    def test_rejects_empty_container_ids(self, registry):
        with pytest.raises(ValueError, match="non-empty set"):
            registry.register_decoder(Fake("silk", set()))

    def test_rejects_undotted_extensions(self, registry):
        with pytest.raises(ValueError, match="dotted suffixes"):
            registry.register_decoder(Fake("silk", {"silk"}, extensions={"silk"}))

    def test_reregistration_replaces_rather_than_duplicates(self, registry):
        first = Fake("silk", {"silk"})
        second = Fake("silk", {"silk"})
        registry.register_decoder(first)
        registry.register_decoder(second)

        assert registry.list_decoders() == [second]

    def test_lists_sorted_by_name(self, registry):
        registry.register_decoder(Fake("silk", {"silk"}))
        registry.register_decoder(Fake("amr", {"amr"}))

        assert [d.name for d in registry.list_decoders()] == ["amr", "silk"]

    def test_unknown_name_returns_none(self, registry):
        assert registry.get_decoder("nope") is None
        assert registry.get_decoder(None) is None


class TestBuiltinsAlwaysWin:
    @pytest.mark.parametrize("container", ["mp3", "wav", "ogg", "m4a", "webm"])
    def test_a_decoder_claiming_a_builtin_container_is_ignored(
        self, registry, container, caplog
    ):
        """Otherwise an installed plugin inserts a conversion hop into every mp3."""
        with caplog.at_level("WARNING"):
            registry.register_decoder(Fake("greedy", {container}))

        assert registry.list_decoders() == []
        assert "already accepts" in caplog.text

    def test_a_decoder_claiming_a_builtin_extension_is_ignored(self, registry):
        registry.register_decoder(
            Fake("greedy", {"silk"}, extensions={".silk", ".mp3"})
        )

        assert registry.list_decoders() == []

    def test_a_non_builtin_container_is_accepted(self, registry):
        registry.register_decoder(Fake("silk", {"silk"}))

        assert [d.name for d in registry.list_decoders()] == ["silk"]


class TestBuiltinSync:
    def test_builtin_extensions_match_the_transcription_pipeline(self):
        """The duplicated set must not drift from ``SUPPORTED_FORMATS``.

        ``agent.audio_decoder_registry`` cannot import
        ``tools.transcription_tools`` (that module imports it back), so the
        built-in extension set is copied — exactly like the TTS and STT
        registries copy their built-in provider names.
        """
        from agent.audio_decoder_registry import _BUILTIN_EXTENSIONS
        from tools.transcription_tools import SUPPORTED_FORMATS

        assert set(_BUILTIN_EXTENSIONS) == set(SUPPORTED_FORMATS)

    def test_builtin_containers_are_derived_not_duplicated(self):
        from agent.audio_decoder_registry import (
            _BUILTIN_CONTAINERS,
            _BUILTIN_EXTENSIONS,
        )
        from tools.audio_container import CONTAINER_TO_EXT

        assert _BUILTIN_CONTAINERS == frozenset(
            cid for cid, ext in CONTAINER_TO_EXT.items() if ext in _BUILTIN_EXTENSIONS
        )
        # The codecs a decoder exists for are deliberately outside the guard.
        assert "silk" not in _BUILTIN_CONTAINERS
        assert "amr" not in _BUILTIN_CONTAINERS


class TestFindDecoder:
    def test_a_recognized_container_decides_on_its_own(self, registry):
        silk = Fake("silk", {"silk"})
        amr = Fake("amr", {"amr"})
        registry.register_decoder(silk)
        registry.register_decoder(amr)

        # Content beats filename: SILK bytes under an .amr name.
        assert registry.find_decoder(container_id="silk", suffix=".amr") is silk
        assert registry.find_decoder(container_id="amr", suffix=".silk") is amr

    def test_an_unrecognized_header_falls_back_to_the_suffix(self, registry):
        silk = Fake("silk", {"silk"})
        registry.register_decoder(silk)

        assert registry.find_decoder(container_id=None, suffix=".silk") is silk
        assert registry.find_decoder(container_id=None, suffix=".bin") is None

    def test_a_recognized_container_nobody_claims_returns_none(self, registry):
        registry.register_decoder(Fake("silk", {"silk"}))

        assert registry.find_decoder(container_id="amr", suffix=".silk") is None

    def test_lookup_is_case_and_whitespace_tolerant(self, registry):
        silk = Fake("silk", {"silk"})
        registry.register_decoder(silk)

        assert registry.find_decoder(container_id=" SILK ", suffix=".AMR") is silk
        assert registry.find_decoder(container_id=None, suffix=" .SILK ") is silk

    def test_ties_resolve_deterministically_by_name(self, registry):
        """Two decoders for one container must not alternate between runs."""
        second = Fake("bbb", {"silk"})
        first = Fake("aaa", {"silk"})
        registry.register_decoder(second)
        registry.register_decoder(first)

        assert registry.find_decoder(container_id="silk", suffix=".silk") is first

    def test_a_decoder_whose_claims_raises_is_skipped(self, registry, caplog):
        class Exploding(Fake):
            def claims(self, *, container_id: Optional[str], suffix: str) -> bool:
                raise RuntimeError("claims exploded")

        registry.register_decoder(Exploding("aaa", {"amr"}))
        good = Fake("zzz", {"amr"})
        registry.register_decoder(good)

        with caplog.at_level("WARNING"):
            found = registry.find_decoder(container_id="amr", suffix=".amr")

        assert found is good
        assert "claims() raised" in caplog.text


class TestDecodableExtensions:
    def test_unions_every_registered_decoder(self, registry):
        registry.register_decoder(Fake("silk", {"silk"}))
        registry.register_decoder(Fake("amr", {"amr"}))

        assert registry.decodable_extensions() == frozenset({".silk", ".amr"})

    def test_empty_registry_advertises_nothing(self, registry):
        assert registry.decodable_extensions() == frozenset()

    def test_a_decoder_whose_extensions_raise_is_skipped(self, registry):
        class Exploding(Fake):
            @property
            def extensions(self) -> FrozenSet[str]:
                raise RuntimeError("extensions exploded")

        registry.register_decoder(Fake("amr", {"amr"}))
        # Registration validated the property once; make it fail afterwards
        # the way a decoder reading mutable state could.
        broken = Fake("silk", {"silk"})
        registry.register_decoder(broken)
        broken.__class__ = Exploding

        assert registry.decodable_extensions() == frozenset({".amr"})


class TestScopedRegistration:
    def test_a_scoped_decoder_is_visible_in_its_scope_only(self, registry):
        decoder = Fake("silk", {"silk"})
        registry.register_decoder(decoder, scope="scope-a")

        assert registry.get_decoder("silk", scope="scope-a") is decoder
        assert registry.get_decoder("silk", scope="scope-b") is None

    def test_snapshot_and_restore_round_trip(self, registry):
        first = Fake("silk", {"silk"})
        second = Fake("silk", {"silk"})
        registry.register_decoder(first, scope="scope-a")
        previous = registry.snapshot_registration("silk", scope="scope-a")
        registry.register_decoder(second, scope="scope-a")

        assert previous is first
        assert registry.restore_registration(
            "silk", second, previous, scope="scope-a"
        ) is True
        assert registry.get_decoder("silk", scope="scope-a") is first

    def test_restore_declines_when_someone_else_took_the_slot(self, registry):
        first = Fake("silk", {"silk"})
        second = Fake("silk", {"silk"})
        third = Fake("silk", {"silk"})
        registry.register_decoder(first, scope="scope-a")
        registry.register_decoder(third, scope="scope-a")

        assert registry.restore_registration(
            "silk", second, first, scope="scope-a"
        ) is False
        assert registry.get_decoder("silk", scope="scope-a") is third

    def test_restore_to_none_empties_the_scope(self, registry):
        decoder = Fake("silk", {"silk"})
        registry.register_decoder(decoder, scope="scope-a")

        assert registry.restore_registration(
            "silk", decoder, None, scope="scope-a"
        ) is True
        assert registry.get_decoder("silk", scope="scope-a") is None

    def test_reset_clears_global_and_scoped(self, registry):
        registry.register_decoder(Fake("silk", {"silk"}))
        registry.register_decoder(Fake("amr", {"amr"}), scope="scope-a")

        registry._reset_for_tests()

        assert registry.list_decoders() == []
        assert registry.get_decoder("amr", scope="scope-a") is None


class TestAbcDefaults:
    def test_extensions_default_to_the_shared_sniffer_table(self):
        assert Fake("silk", {"silk", "amr"}).extensions == frozenset(
            {".silk", ".amr"}
        )

    def test_an_unknown_container_contributes_no_extension(self):
        """A decoder for a container the sniffer cannot see must say so itself."""
        assert Fake("mystery", {"mystery"}).extensions == frozenset()

    def test_decode_error_is_an_exception_carrying_a_readable_message(self):
        exc = AudioDecodeError("Install ffmpeg to enable AMR voice transcription.")

        assert isinstance(exc, Exception)
        assert "Install ffmpeg" in str(exc)

    def test_decoded_audio_defaults_to_owning_nothing(self):
        assert DecodedAudio(path="/tmp/x.wav").cleanup_dir is None
