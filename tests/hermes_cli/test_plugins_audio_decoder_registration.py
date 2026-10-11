"""Tests for PluginContext.register_audio_decoder().

Exercises the plugin context hook end-to-end: drops a fake plugin into
``$HERMES_HOME/plugins/``, runs ``PluginManager().discover_and_load()``, and
asserts the registration result.

Mirrors the shape of ``test_plugins_transcription_registration.py`` and
``test_plugins_tts_registration.py`` — the two sibling hooks on the same
pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

_DECODER_SOURCE = """from agent.audio_decoder import AudioDecoder, DecodedAudio
    class D(AudioDecoder):
        @property
        def name(self): return {name!r}
        @property
        def container_ids(self): return frozenset({containers!r})
        @property
        def extensions(self): return frozenset({extensions!r}) or super().extensions
        def decode(self, file_path):
            return DecodedAudio(path=file_path)
    ctx.register_audio_decoder(D())"""


def _write_plugin(
    root: Path,
    name: str,
    *,
    manifest_extra: Dict[str, Any] | None = None,
    register_body: str = "pass",
) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "0.1.0",
        "description": f"Test plugin {name}",
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    (plugin_dir / "plugin.yaml").write_text(yaml.dump(manifest))
    (plugin_dir / "__init__.py").write_text(
        f"def register(ctx):\n    {register_body}\n"
    )
    return plugin_dir


def _enable(hermes_home: Path, name: str) -> None:
    cfg_path = hermes_home / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            cfg = {}
    plugins_cfg = cfg.setdefault("plugins", {})
    enabled = plugins_cfg.setdefault("enabled", [])
    if isinstance(enabled, list) and name not in enabled:
        enabled.append(name)
    cfg_path.write_text(yaml.safe_dump(cfg))


def _decoder_body(name: str, containers: set, extensions: set = frozenset()) -> str:
    return _DECODER_SOURCE.format(
        name=name, containers=set(containers), extensions=set(extensions)
    )


class TestRegisterAudioDecoder:
    def test_accepts_a_valid_decoder(self):
        from hermes_cli.plugins import PluginManager

        from agent import audio_decoder_registry
        audio_decoder_registry._reset_for_tests()

        hermes_home = Path(os.environ["HERMES_HOME"])
        _write_plugin(
            hermes_home / "plugins",
            "my-codec-plugin",
            register_body=_decoder_body("fake-silk", {"silk"}),
        )
        _enable(hermes_home, "my-codec-plugin")

        mgr = PluginManager()
        mgr.discover_and_load()

        assert mgr._plugins["my-codec-plugin"].enabled is True, (
            f"Plugin failed to load: {mgr._plugins['my-codec-plugin'].error}"
        )
        assert audio_decoder_registry.get_decoder("fake-silk") is not None
        # Dispatch is by container, not by the registry name.
        assert audio_decoder_registry.find_decoder(
            container_id="silk", suffix=".amr"
        ) is not None

        audio_decoder_registry._reset_for_tests()

    def test_widens_the_transcription_pipeline_supported_formats(self):
        """The point of the hook: a claimed format stops being 'unsupported'."""
        from hermes_cli.plugins import PluginManager

        from agent import audio_decoder_registry
        audio_decoder_registry._reset_for_tests()

        hermes_home = Path(os.environ["HERMES_HOME"])
        _write_plugin(
            hermes_home / "plugins",
            "widen-codec-plugin",
            # A container the shared sniffer has no extension for, so the
            # widening is unambiguously this plugin's and not the bundled one's.
            register_body=_decoder_body(
                "fake-widener", {"mystery"}, {".myvoice"}
            ),
        )
        _enable(hermes_home, "widen-codec-plugin")

        mgr = PluginManager()
        mgr.discover_and_load()

        from tools.transcription_tools import SUPPORTED_FORMATS, supported_formats

        assert ".myvoice" in supported_formats()
        assert ".myvoice" not in SUPPORTED_FORMATS

        audio_decoder_registry._reset_for_tests()

    def test_rejects_a_non_decoder(self, caplog):
        from hermes_cli.plugins import PluginManager

        from agent import audio_decoder_registry
        audio_decoder_registry._reset_for_tests()

        hermes_home = Path(os.environ["HERMES_HOME"])
        _write_plugin(
            hermes_home / "plugins",
            "bad-codec-plugin",
            register_body="ctx.register_audio_decoder('not a decoder')",
        )
        _enable(hermes_home, "bad-codec-plugin")

        with caplog.at_level("WARNING"):
            mgr = PluginManager()
            mgr.discover_and_load()

        # The bundled voice_codecs decoders are in the registry too — assert
        # about this plugin's contribution, not about the registry's size.
        assert mgr._plugins["bad-codec-plugin"].enabled is True
        assert audio_decoder_registry.get_decoder("not a decoder") is None
        assert not [
            r
            for r in mgr._ownership_ledger.get("bad-codec-plugin", [])
            if r.kind == "audio_decoder"
        ]
        assert "does not inherit from AudioDecoder" in caplog.text

        audio_decoder_registry._reset_for_tests()

    def test_rejects_a_builtin_format_shadow(self, caplog):
        """A decoder must not be able to intercept every mp3 on the install."""
        from hermes_cli.plugins import PluginManager

        from agent import audio_decoder_registry
        audio_decoder_registry._reset_for_tests()

        hermes_home = Path(os.environ["HERMES_HOME"])
        _write_plugin(
            hermes_home / "plugins",
            "shadow-codec-plugin",
            register_body=_decoder_body("greedy", {"mp3"}),
        )
        _enable(hermes_home, "shadow-codec-plugin")

        with caplog.at_level("WARNING"):
            mgr = PluginManager()
            mgr.discover_and_load()

        # Plugin still loads normally — shadowing is a warning, not a crash.
        assert mgr._plugins["shadow-codec-plugin"].enabled is True
        assert audio_decoder_registry.get_decoder("greedy") is None
        assert "already accepts" in caplog.text

        audio_decoder_registry._reset_for_tests()

    def test_registration_is_released_with_the_plugin(self):
        """Unloading a plugin takes its decoder out of the registry with it."""
        from hermes_cli.plugins import PluginManager

        from agent import audio_decoder_registry
        audio_decoder_registry._reset_for_tests()

        hermes_home = Path(os.environ["HERMES_HOME"])
        _write_plugin(
            hermes_home / "plugins",
            "releasable-codec-plugin",
            register_body=_decoder_body("fake-silk", {"silk"}),
        )
        _enable(hermes_home, "releasable-codec-plugin")

        mgr = PluginManager()
        mgr.discover_and_load()
        scope = mgr.scope_key
        assert audio_decoder_registry.get_decoder("fake-silk", scope=scope) is not None

        for registration in list(
            mgr._ownership_ledger.get("releasable-codec-plugin", [])
        ):
            registration.release()

        assert audio_decoder_registry.get_decoder("fake-silk", scope=scope) is None

        audio_decoder_registry._reset_for_tests()
