"""Registration: five plugin tools, gated, and never core surface.

The important properties here are footprint properties. These tools must
(a) come in through ``ctx.register_tool``, not ``tools/*.py`` + a core
toolset edit, (b) vanish from the model's schema when no OneBot backend is
configured, and (c) reach an agent through the OneBot platform's
auto-generated toolset without anyone editing ``toolsets.py``.
"""

from __future__ import annotations

import json

import pytest

import plugins.qzone as qzone_plugin

_EXPECTED = [
    "qzone_publish",
    "qzone_list_feed",
    "qzone_get_post",
    "qzone_post_comment",
    "qzone_list_friends",
]


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
    qzone_plugin.register(ctx)
    return ctx


class TestRegistration:
    def test_registers_exactly_the_five_wire_names(self, registered):
        assert [t["name"] for t in registered.tools] == _EXPECTED

    def test_all_land_in_the_onebot_toolset(self, registered):
        assert {t["toolset"] for t in registered.tools} == {"onebot"}

    def test_every_tool_is_gated(self, registered):
        for tool in registered.tools:
            assert tool["check_fn"] is qzone_plugin._check_qzone_available
            assert tool["requires_env"] == ["ONEBOT_WS_URL"]

    def test_every_tool_has_an_emoji(self, registered):
        assert all(t["emoji"] for t in registered.tools)

    def test_schema_name_matches_the_registered_name(self, registered):
        for tool in registered.tools:
            assert tool["schema"]["name"] == tool["name"]

    def test_handlers_return_json_strings(self, registered, monkeypatch):
        """AGENTS.md: every handler returns a JSON string, never a dict."""
        # Unconfigure the backend so the two handlers that would otherwise
        # reach for a session fail locally instead of opening a socket.
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        for tool in registered.tools:
            out = tool["handler"]({})  # invalid args, but must still be JSON
            assert isinstance(out, str)
            payload = json.loads(out)
            assert "error" in payload and payload.get("code")

    def test_no_tool_overrides_a_builtin(self, registered):
        assert all(not t.get("override") for t in registered.tools)


class TestGating:
    def test_unavailable_without_an_endpoint(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        assert qzone_plugin._check_qzone_available() is False

    def test_available_with_a_ws_url(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert qzone_plugin._check_qzone_available() is True

    def test_available_with_an_http_url(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        assert qzone_plugin._check_qzone_available() is True

    def test_blank_url_is_not_configured(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "   ")
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        assert qzone_plugin._check_qzone_available() is False


class TestFootprint:
    def test_not_core_tools(self):
        """A core tool is sent on every API call; none of these qualify."""
        from toolsets import _HERMES_CORE_TOOLS

        assert not (set(_EXPECTED) & set(_HERMES_CORE_TOOLS))

    def test_no_static_toolset_entry_was_added(self):
        """The plugin route must not need an edit to ``toolsets.py``."""
        from toolsets import TOOLSETS

        assert "qzone" not in TOOLSETS

    def test_reachable_through_the_platform_toolset(self, monkeypatch):
        """``hermes-onebot`` folds in tools registered under ``onebot``.

        This is the mechanism that exposes them without touching core: the
        auto-generated platform toolset unions ``_HERMES_CORE_TOOLS`` with
        every registry entry whose toolset equals the platform name.
        """
        from gateway.platform_registry import platform_registry
        from tools.registry import registry
        from toolsets import resolve_toolset

        monkeypatch.setattr(platform_registry, "is_registered", lambda name: name == "onebot")

        ctx = _RecordingCtx()
        qzone_plugin.register(ctx)
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
                snapshots.append(
                    (tool["name"], registry.snapshot_registration(tool["name"]), previous)
                )
            resolved = set(resolve_toolset("hermes-onebot", include_registry=True))
            assert set(_EXPECTED) <= resolved
        finally:
            for name, current, previous in reversed(snapshots):
                registry.restore_registration(name, current, previous)


class TestManifest:
    def test_manifest_declares_the_five_tools(self):
        from pathlib import Path

        text = Path(qzone_plugin.__file__).with_name("plugin.yaml").read_text(
            encoding="utf-8"
        )
        for name in _EXPECTED:
            assert f"- {name}" in text

    def test_manifest_is_a_backend_plugin(self):
        from pathlib import Path

        text = Path(qzone_plugin.__file__).with_name("plugin.yaml").read_text(
            encoding="utf-8"
        )
        assert "kind: backend" in text
