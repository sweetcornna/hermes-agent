"""The registration chain — the part whose absence makes 2800 lines dead code.

A plugin that is never discovered, or discovered but registers nothing, fails
in the quietest way available: no error, no log line, just a command that does
not exist. These tests walk the real discovery path (``PluginManager``'s own
manifest scanner) rather than asserting on the YAML text, and then assert that
what ``register()`` actually does matches what the manifest promises.
"""

from __future__ import annotations

import argparse

import pytest

import plugins.corlinman_jobs as plugin
from plugins.corlinman_jobs import installer, preflight


@pytest.fixture(scope="module")
def manifest():
    """The manifest as hermes's own scanner reads it."""
    from hermes_cli.plugins import PluginManager, get_bundled_plugins_dir

    manager = PluginManager()
    found = {
        m.key: m
        for m in manager._scan_directory(get_bundled_plugins_dir(), "bundled")
    }
    assert "corlinman_jobs" in found, (
        "the plugin is not discoverable — plugin.yaml missing, unparseable, or "
        "in the wrong directory"
    )
    return found["corlinman_jobs"]


class _RecordingCtx:
    """The slice of ``PluginContext`` this plugin is allowed to touch."""

    def __init__(self):
        self.cli_commands = []
        self.tools = []
        self.hooks = []
        self.commands = []
        self.providers = []
        self.prompt_sections = []

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs)

    def register_tool(self, **kwargs):  # pragma: no cover - must never run
        self.tools.append(kwargs)

    def register_hook(self, *args, **kwargs):  # pragma: no cover
        self.hooks.append(args)

    def register_command(self, *args, **kwargs):  # pragma: no cover
        self.commands.append(args)

    def register_memory_provider(self, provider):  # pragma: no cover
        self.providers.append(provider)

    def register_system_prompt_section(self, *args, **kwargs):  # pragma: no cover
        self.prompt_sections.append(args)


@pytest.fixture
def registered():
    ctx = _RecordingCtx()
    plugin.register(ctx)
    return ctx


class TestDiscovery:
    def test_the_plugin_is_found_by_the_real_scanner(self, manifest):
        assert manifest.name == "corlinman_jobs"
        assert manifest.source == "bundled"

    def test_it_is_opt_in_not_auto_loaded(self, manifest):
        """``standalone`` bundled plugins are gated by ``plugins.enabled``.
        Installing production cron jobs must never be a side effect of
        starting hermes."""
        assert manifest.kind == "standalone"

    def test_the_manifest_describes_the_safety_posture(self, manifest):
        text = manifest.description
        assert "PAUSED" in text
        assert "QQ" in text

    def test_it_declares_the_timezone_contract(self, manifest):
        names = {
            entry["name"] if isinstance(entry, dict) else entry
            for entry in manifest.requires_env
        }
        assert names == {"HERMES_TIMEZONE"}

    def test_the_package_has_an_entry_point(self):
        assert callable(plugin.register)


class TestRegistration:
    def test_registers_exactly_one_cli_command(self, registered):
        assert len(registered.cli_commands) == 1

    def test_the_command_name_matches_the_module_constant(self, registered):
        assert registered.cli_commands[0]["name"] == plugin.CLI_COMMAND == "corlinman-jobs"

    def test_it_wires_the_installers_parser_and_handler(self, registered):
        entry = registered.cli_commands[0]
        assert entry["setup_fn"] is installer.register_cli
        assert entry["handler_fn"] is installer.corlinman_jobs_command

    def test_the_help_text_says_jobs_land_paused(self, registered):
        entry = registered.cli_commands[0]
        assert "paused" in entry["help"].lower()
        assert "PAUSED" in entry["description"]

    def test_it_registers_no_model_tools(self, registered, manifest):
        """No agent has any business creating these jobs, and a plugin with no
        tools costs zero tokens in every session."""
        assert registered.tools == []
        assert manifest.provides_tools == []

    def test_it_registers_no_hooks_commands_or_providers(self, registered, manifest):
        assert registered.hooks == []
        assert registered.commands == []
        assert registered.providers == []
        assert registered.prompt_sections == []
        assert manifest.provides_hooks == []


class TestRegistrationIsInert:
    def test_registering_creates_no_jobs(self, registered):
        from cron.jobs import load_jobs

        assert load_jobs() == []

    def test_registering_writes_no_scripts(self, registered):
        assert not installer.scripts_dir().exists()
        assert not installer.manifest_path().exists()

    def test_the_registered_parser_defaults_to_doing_nothing(self, registered):
        parser = argparse.ArgumentParser()
        registered.cli_commands[0]["setup_fn"](parser)
        args = parser.parse_args([])
        assert getattr(args, "corlinman_jobs_action", None) is None
        assert registered.cli_commands[0]["handler_fn"](args) == 2

    def test_no_subcommand_enables_anything(self, registered):
        parser = argparse.ArgumentParser()
        registered.cli_commands[0]["setup_fn"](parser)
        actions = {
            name
            for action in parser._subparsers._group_actions
            for name in action.choices
        }
        assert actions == {"plan", "install", "status"}
        assert "enable" not in actions and "resume" not in actions


class TestManifestMatchesReality:
    """The manifest is documentation that ships; it has to stay true."""

    def test_every_optional_env_var_is_one_the_code_actually_reads(self):
        """``optional_env`` is manifest documentation hermes does not model,
        so it is read straight from the YAML — and checked against the code,
        because a manifest that names a variable nothing reads is a lie an
        operator will act on."""
        import inspect

        import yaml

        from plugins.qzone import state as qzone_state

        data = yaml.safe_load(
            (installer.repo_root() / "plugins" / "corlinman_jobs" / "plugin.yaml")
            .read_text(encoding="utf-8")
        )
        sources = (
            inspect.getsource(preflight)
            + inspect.getsource(installer)
            + inspect.getsource(qzone_state)
        )
        names = [entry["name"] for entry in data["optional_env"]]
        assert names, "optional_env vanished from the manifest"
        for name in names:
            assert name in sources, f"{name} is documented but never read"

    def test_the_declared_required_var_is_the_one_preflight_blocks_on(
        self, manifest, monkeypatch
    ):
        monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config", lambda *a, **k: {}, raising=False
        )
        assert preflight.check_timezone().blocking

    def test_the_manifest_declares_no_python_dependencies(self, manifest):
        """The job scripts are standard library plus PyYAML, which hermes
        already depends on."""
        assert manifest.python_dependencies == []

    def test_the_yaml_parses_with_no_unknown_field_warnings(self, caplog):
        import logging

        from hermes_cli.plugins import PluginManager, get_bundled_plugins_dir

        with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
            PluginManager()._scan_directory(get_bundled_plugins_dir(), "bundled")
        offenders = [
            record.getMessage()
            for record in caplog.records
            if "corlinman_jobs" in record.getMessage()
        ]
        assert offenders == []
