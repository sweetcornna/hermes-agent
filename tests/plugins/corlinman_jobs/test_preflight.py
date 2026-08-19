"""Both branches of every preflight check.

A check that only ever passes is decoration. Each of these guards a specific
way the migration can be silently wrong, so each one is exercised in the
state where it must fail as well as the state where it must pass.
"""

from __future__ import annotations

import builtins
import json
from dataclasses import replace

import pytest

from plugins.corlinman_jobs import preflight
from plugins.corlinman_jobs.preflight import FAIL, OK, WARN
from plugins.corlinman_jobs.specs import JOB_SPECS, TIMEZONE


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from a profile that declares nothing."""
    for var in (
        "HERMES_TIMEZONE",
        "HERMES_CRON_MAX_PARALLEL",
        "ONEBOT_WS_URL",
        "ONEBOT_HTTP_URL",
        "TELEGRAM_BOT_TOKEN",
        "QZONE_PERSONA_ID",
        "QZONE_STATE_DIR",
        "QZONE_QQ_INSTANCE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    # read_raw_config() would otherwise reach for the real config.yaml.
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config", lambda *a, **k: {}, raising=False
    )


@pytest.fixture
def qzone_ledgers(tmp_path, monkeypatch):
    """A migrated-looking qzone state directory keyed to the right persona."""
    root = tmp_path / "qzone-state"
    (root / "qzone_post_log").mkdir(parents=True)
    (root / "qzone_post_log" / "grantley.json").write_text(
        json.dumps({"version": 1, "posts": [{"ts": "2026-08-01", "text": "hi"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("QZONE_STATE_DIR", str(root))
    monkeypatch.setenv("QZONE_PERSONA_ID", "grantley")
    return root


class TestTimezone:
    def test_ok_when_the_env_matches_the_declared_zone(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        check = preflight.check_timezone()
        assert check.level == OK
        assert not check.blocking
        assert "HERMES_TIMEZONE" in check.message

    def test_ok_when_config_yaml_supplies_it(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda *a, **k: {"timezone": TIMEZONE},
            raising=False,
        )
        check = preflight.check_timezone()
        assert check.level == OK
        assert "config.yaml" in check.message

    def test_fails_when_nothing_is_configured(self):
        """Unset means "the host's local zone" — Asia/Tokyo in production."""
        check = preflight.check_timezone()
        assert check.level == FAIL
        assert check.blocking
        assert TIMEZONE in check.detail

    def test_fails_on_the_production_hosts_own_zone(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")
        check = preflight.check_timezone()
        assert check.level == FAIL
        assert "Asia/Tokyo" in check.message

    def test_fails_when_specs_disagree_among_themselves(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        mixed = (JOB_SPECS[0], replace(JOB_SPECS[1], timezone="Europe/Berlin"))
        check = preflight.check_timezone(mixed)
        assert check.level == FAIL
        assert "more than one timezone" in check.message

    def test_env_wins_over_config(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda *a, **k: {"timezone": TIMEZONE},
            raising=False,
        )
        assert preflight.check_timezone().level == FAIL

    def test_unreadable_config_is_not_a_crash(self, monkeypatch):
        def explode(*a, **k):
            raise RuntimeError("no config here")

        monkeypatch.setattr("hermes_cli.config.read_raw_config", explode, raising=False)
        assert preflight.check_timezone().level == FAIL


class TestCroniter:
    def test_ok_when_importable(self):
        assert preflight.check_croniter().level == OK

    def test_fails_when_missing(self, monkeypatch):
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "croniter":
                raise ImportError("simulated missing dependency")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        check = preflight.check_croniter()
        assert check.level == FAIL
        assert check.blocking


class TestParallelism:
    def test_warns_when_unset(self):
        check = preflight.check_parallelism()
        assert check.level == WARN
        assert not check.blocking

    def test_ok_at_the_designed_ceiling(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "2")
        assert preflight.check_parallelism().level == OK

    def test_ok_below_the_ceiling(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "1")
        assert preflight.check_parallelism().level == OK

    def test_warns_above_the_ceiling(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "8")
        check = preflight.check_parallelism()
        assert check.level == WARN
        assert "8" in check.message

    def test_warns_on_a_non_integer(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "lots")
        assert preflight.check_parallelism().level == WARN

    def test_reads_config_yaml_when_the_env_is_unset(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda *a, **k: {"cron": {"max_parallel_jobs": 2}},
            raising=False,
        )
        check = preflight.check_parallelism()
        assert check.level == OK
        assert "config.yaml" in check.message


class TestOneBot:
    def test_fails_with_no_backend(self):
        check = preflight.check_onebot()
        assert check.level == FAIL
        assert check.blocking

    def test_ok_with_a_websocket_endpoint(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert preflight.check_onebot().level == OK

    def test_ok_with_an_http_endpoint(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        assert preflight.check_onebot().level == OK

    def test_blank_value_does_not_count(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "   ")
        assert preflight.check_onebot().level == FAIL


class TestQzoneState:
    def test_fails_when_the_persona_resolves_wrong(self, tmp_path, monkeypatch):
        """The dedup ledgers are keyed by persona; the wrong key reads empty."""
        monkeypatch.setenv("QZONE_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("QZONE_PERSONA_ID", "default")
        check = preflight.check_qzone_state()
        assert check.level == FAIL
        assert "grantley" in check.detail

    def test_fails_when_every_ledger_is_empty(self, tmp_path, monkeypatch):
        """An empty ledger means the first run re-replies to old comments."""
        monkeypatch.setenv("QZONE_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("QZONE_PERSONA_ID", "grantley")
        check = preflight.check_qzone_state()
        assert check.level == FAIL
        assert "execution-state" in check.detail

    def test_ok_once_the_ledgers_are_migrated(self, qzone_ledgers):
        check = preflight.check_qzone_state()
        assert check.level == OK
        assert "post_log=1" in check.message

    def test_unimportable_module_fails_rather_than_raising(self, monkeypatch):
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith("plugins.qzone"):
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        monkeypatch.delitem(
            __import__("sys").modules, "plugins.qzone.state", raising=False
        )
        monkeypatch.delitem(__import__("sys").modules, "plugins.qzone", raising=False)
        check = preflight.check_qzone_state()
        assert check.level == FAIL


class TestTelegram:
    def test_warns_when_no_token_is_configured(self):
        check = preflight.check_telegram()
        assert check.level == WARN
        assert not check.blocking
        assert "TELEGRAM_BOT_TOKEN" in check.message

    def test_warns_about_which_bot_owns_the_token(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        check = preflight.check_telegram()
        assert check.level == WARN
        assert "sweetcornna2_bot" in check.message

    def test_never_blocks_an_install(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        assert not preflight.check_telegram().blocking


class TestScriptsInstalled:
    def test_fails_before_anything_is_installed(self):
        check = preflight.check_scripts_installed()
        assert check.level == FAIL
        assert "not installed" in check.message

    def test_ok_after_a_real_install(self, monkeypatch, qzone_ledgers):
        from plugins.corlinman_jobs import installer

        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        result = installer.install()
        assert result.ok, result.error
        check = preflight.check_scripts_installed()
        assert check.level == OK

    def test_warns_when_an_installed_script_drifts(self, monkeypatch, qzone_ledgers):
        from plugins.corlinman_jobs import installer

        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert installer.install().ok
        target = installer.scripts_dir() / "corlinman_daily_agenda.py"
        target.write_text("# someone edited this\n", encoding="utf-8")
        check = preflight.check_scripts_installed()
        assert check.level == WARN
        assert "corlinman_daily_agenda.py" in check.message


class TestRunChecks:
    def test_qq_checks_can_be_left_out(self):
        keys = {c.key for c in preflight.run_checks(include_qq=False, include_scripts=False)}
        assert "onebot" not in keys and "qzone_state" not in keys

    def test_script_check_can_be_left_out(self):
        """An install cannot require the files it is about to write."""
        keys = {c.key for c in preflight.run_checks(include_scripts=False)}
        assert "scripts" not in keys

    def test_all_checks_are_included_by_default(self):
        keys = {c.key for c in preflight.run_checks()}
        assert keys == {
            "timezone",
            "croniter",
            "max_parallel_jobs",
            "scripts",
            "onebot",
            "qzone_state",
            "telegram",
        }

    def test_blocking_selects_only_failures(self):
        checks = preflight.run_checks(include_qq=False, include_scripts=False)
        blocking = preflight.blocking(checks)
        assert all(c.level == FAIL for c in blocking)
        assert {c.key for c in blocking} == {"timezone"}

    def test_a_fully_configured_profile_has_no_blockers(
        self, monkeypatch, qzone_ledgers
    ):
        from plugins.corlinman_jobs import installer

        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "2")
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        assert installer.install().ok
        assert preflight.blocking(preflight.run_checks()) == []
