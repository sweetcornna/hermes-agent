"""Registration: two plugin tools, gated, and never core surface.

The properties asserted here are footprint properties. The pair must
(a) arrive through ``ctx.register_tool`` rather than ``tools/*.py`` plus a
core toolset edit, (b) vanish from the model's schema when no OneBot backend
is configured — that is the whole reason they are allowed to exist at all —
and (c) reach an agent through the OneBot platform's auto-generated toolset
without anybody editing ``toolsets.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import plugins.qq_voice as qq_voice_plugin

_EXPECTED = ["qq_send_voice", "qq_ai_voice"]


class _RecordingCtx:
    """The slice of ``PluginContext`` this plugin actually uses."""

    def __init__(self):
        self.tools = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)
        return None


@pytest.fixture
def registered():
    ctx = _RecordingCtx()
    qq_voice_plugin.register(ctx)
    return ctx


class TestRegistration:
    def test_registers_exactly_the_two_wire_names(self, registered):
        assert [t["name"] for t in registered.tools] == _EXPECTED

    def test_both_land_in_the_onebot_toolset(self, registered):
        assert {t["toolset"] for t in registered.tools} == {"onebot"}

    def test_every_tool_is_gated(self, registered):
        for tool in registered.tools:
            assert tool["check_fn"] is qq_voice_plugin._check_qq_voice_available
            assert tool["requires_env"] == ["ONEBOT_WS_URL"]

    def test_every_tool_has_an_emoji(self, registered):
        assert all(t["emoji"] for t in registered.tools)

    def test_schema_name_matches_the_registered_name(self, registered):
        for tool in registered.tools:
            assert tool["schema"]["name"] == tool["name"]

    def test_handlers_return_json_strings(self, registered, monkeypatch):
        """AGENTS.md: every handler returns a JSON string, never a dict."""
        # No endpoint configured, and the arguments are invalid anyway, so
        # neither handler can reach a socket from here.
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        for tool in registered.tools:
            out = tool["handler"]({})
            assert isinstance(out, str)
            payload = json.loads(out)
            assert "error" in payload and payload.get("code")

    def test_no_tool_overrides_a_builtin(self, registered):
        assert all(not t.get("override") for t in registered.tools)


class TestGating:
    def test_unavailable_without_an_endpoint(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        assert qq_voice_plugin._check_qq_voice_available() is False

    def test_available_with_a_ws_url(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert qq_voice_plugin._check_qq_voice_available() is True

    def test_available_with_an_http_url(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        assert qq_voice_plugin._check_qq_voice_available() is True

    def test_blank_url_is_not_configured(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "   ")
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        assert qq_voice_plugin._check_qq_voice_available() is False


class TestSchemaExposure:
    """The gate has to hold where it is paid for: the model's schema."""

    @staticmethod
    def _definitions(registered):
        from tools.registry import invalidate_check_fn_cache, registry

        snapshots = []
        try:
            for tool in registered.tools:
                previous = registry.snapshot_registration(tool["name"])
                registry.register(
                    name=tool["name"],
                    toolset=tool["toolset"],
                    schema=tool["schema"],
                    handler=tool["handler"],
                    check_fn=tool["check_fn"],
                )
                snapshots.append((
                    tool["name"],
                    registry.snapshot_registration(tool["name"]),
                    previous,
                ))
            invalidate_check_fn_cache()
            defs = registry.get_definitions(set(_EXPECTED), quiet=True)
            return {d["function"]["name"] for d in defs}
        finally:
            from tools.registry import invalidate_check_fn_cache as drop

            for name, current, previous in reversed(snapshots):
                registry.restore_registration(name, current, previous)
            drop()

    def test_absent_from_the_schema_without_an_endpoint(self, registered, monkeypatch):
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        assert self._definitions(registered) == set()

    def test_present_in_the_schema_with_an_endpoint(self, registered, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert self._definitions(registered) == set(_EXPECTED)


class TestFootprint:
    def test_not_core_tools(self):
        """A core tool is sent on every API call; neither of these qualifies."""
        from toolsets import _HERMES_CORE_TOOLS

        assert not (set(_EXPECTED) & set(_HERMES_CORE_TOOLS))

    def test_no_static_toolset_entry_was_added(self):
        """The plugin route must not need an edit to ``toolsets.py``."""
        from toolsets import TOOLSETS

        assert "qq_voice" not in TOOLSETS

    def test_reachable_through_the_platform_toolset(self, monkeypatch):
        """``hermes-onebot`` folds in tools registered under ``onebot``.

        This is the mechanism that exposes them without touching core: the
        auto-generated platform toolset unions ``_HERMES_CORE_TOOLS`` with
        every registry entry whose toolset equals the platform name.
        """
        from gateway.platform_registry import platform_registry
        from tools.registry import registry
        from toolsets import resolve_toolset

        monkeypatch.setattr(
            platform_registry, "is_registered", lambda name: name == "onebot"
        )

        ctx = _RecordingCtx()
        qq_voice_plugin.register(ctx)
        snapshots = []
        try:
            for tool in ctx.tools:
                previous = registry.snapshot_registration(tool["name"])
                registry.register(
                    name=tool["name"],
                    toolset=tool["toolset"],
                    schema=tool["schema"],
                    handler=tool["handler"],
                    check_fn=None,  # gating is covered above; keep this pure
                )
                snapshots.append((
                    tool["name"],
                    registry.snapshot_registration(tool["name"]),
                    previous,
                ))
            resolved = set(resolve_toolset("hermes-onebot", include_registry=True))
            assert set(_EXPECTED) <= resolved
        finally:
            for name, current, previous in reversed(snapshots):
                registry.restore_registration(name, current, previous)


class TestManifest:
    @staticmethod
    def _manifest() -> dict:
        path = Path(qq_voice_plugin.__file__).with_name("plugin.yaml")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_manifest_parses(self):
        assert self._manifest()["name"] == "qq_voice"

    def test_manifest_declares_both_tools(self):
        assert self._manifest()["provides_tools"] == _EXPECTED

    def test_manifest_is_a_backend_plugin(self):
        assert self._manifest()["kind"] == "backend"

    def test_manifest_requires_only_the_shared_onebot_url(self):
        """No new env var: the OneBot endpoint is shared with the platform."""
        required = {e["name"] for e in self._manifest()["requires_env"]}
        optional = {e["name"] for e in self._manifest().get("optional_env", [])}
        assert required == {"ONEBOT_WS_URL"}
        assert optional <= {"ONEBOT_ACCESS_TOKEN", "ONEBOT_HTTP_URL"}


class TestSchemas:
    def test_send_voice_schema_shape(self):
        from plugins.qq_voice.send_voice import QQ_SEND_VOICE_SCHEMA as S

        props = S["parameters"]["properties"]
        assert set(props) == {"text", "audio_file", "user_id", "group_id"}
        # Nothing is structurally required: the XOR pairs are enforced in the
        # handler, where a violation can be explained rather than rejected.
        assert S["parameters"]["required"] == []
        assert "delivery_path" in S["description"]

    def test_ai_voice_schema_shape(self):
        from plugins.qq_voice.ai_voice import QQ_AI_VOICE_SCHEMA as S

        props = S["parameters"]["properties"]
        assert set(props) == {"group_id", "text", "character"}
        assert S["parameters"]["required"] == ["group_id"]
        # The two constraints the model cannot discover on its own.
        assert "NapCat" in S["description"]
        assert "Group-only" in S["description"] or "group-only" in S["description"]
