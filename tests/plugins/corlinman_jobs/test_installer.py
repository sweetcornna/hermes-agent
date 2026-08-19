"""The installer: dry runs that touch nothing, installs that land paused.

The properties under test are the ones an operator's safety depends on:

* ``plan()`` writes no file, creates no directory and creates no job;
* an install refuses to start when preflight fails;
* every job it does create is paused before the function returns;
* re-running it is a no-op, and it will not clobber a hand-edited script.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import replace

import pytest

from plugins.corlinman_jobs import installer, preflight
from plugins.corlinman_jobs.installer import DIARY_CHANNELS, NO_TOOLS_SENTINEL
from plugins.corlinman_jobs.specs import ALL_SPECS, JOB_SPECS, TIMEZONE, spec_by_name

#: Jobs that need no QQ session at all — the subset installable on a bare
#: profile with none of onebot/qzone_state/qq_group_history configured.
#: Scoped to the original nine scheduler jobs on purpose: qunjlu (a
#: monitor) needs no onebot connectivity (deliver=local) but does need
#: qq_group_history, so it does not belong in "needs nothing QQ-shaped".
NON_QQ = tuple(s.name for s in JOB_SPECS if not installer.needs_qq(s))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "HERMES_TIMEZONE",
        "HERMES_CRON_MAX_PARALLEL",
        "ONEBOT_WS_URL",
        "ONEBOT_HTTP_URL",
        "TELEGRAM_BOT_TOKEN",
        "QZONE_PERSONA_ID",
        "QZONE_STATE_DIR",
        "QQ_GROUP_HISTORY_DB",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config", lambda *a, **k: {}, raising=False
    )


def _make_qq_history_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE group_messages (id INTEGER PRIMARY KEY, instance_id TEXT, "
        "group_id TEXT, sender_user_id TEXT, sender_name TEXT, message_id TEXT, "
        "event_time_ms INTEGER, received_at_ms INTEGER, text TEXT)"
    )
    conn.execute(
        "INSERT INTO group_messages (instance_id, group_id, sender_user_id, "
        "sender_name, event_time_ms, received_at_ms, text) VALUES "
        "('default', '980927602', '1', 'a', 0, 0, 'hi')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def ready(monkeypatch, tmp_path):
    """A profile where every preflight check passes, QQ included — all
    twelve jobs (nine scheduler jobs + three monitors) installable."""
    root = tmp_path / "qzone-state"
    (root / "qzone_post_log").mkdir(parents=True)
    (root / "qzone_post_log" / "grantley.json").write_text(
        json.dumps({"version": 1, "posts": [{"ts": "2026-08-01", "text": "hi"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("QZONE_STATE_DIR", str(root))
    monkeypatch.setenv("QZONE_PERSONA_ID", "grantley")
    monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
    monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
    monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "2")
    history_db = tmp_path / "qq_group_history.sqlite"
    _make_qq_history_db(history_db)
    monkeypatch.setenv("QQ_GROUP_HISTORY_DB", str(history_db))
    return root


def _jobs():
    from cron.jobs import load_jobs

    return {str(j.get("name")): j for j in load_jobs()}


# ---------------------------------------------------------------------------
# Generated scripts
# ---------------------------------------------------------------------------


class TestGeneratedScripts:
    def test_one_script_per_scripted_job_plus_the_library(self):
        planned = installer.planned_files()
        expected = {s.script for s in ALL_SPECS if s.script}
        assert set(planned) == expected | {installer.LIB_FILENAME}

    def test_the_library_is_copied_verbatim(self):
        planned = installer.planned_files()
        assert planned[installer.LIB_FILENAME] == installer.source_lib_path().read_text(
            encoding="utf-8"
        )

    def test_rendering_is_deterministic(self):
        """Drift detection compares bytes; an unstable render breaks it."""
        assert installer.planned_files() == installer.planned_files()

    def test_every_generated_script_compiles(self):
        for name, text in installer.planned_files().items():
            compile(text, name, "exec")

    def test_every_generated_script_says_where_it_came_from(self):
        for name, text in installer.planned_files().items():
            if name == installer.LIB_FILENAME:
                continue
            assert installer.GENERATED_MARKER in text
            assert "Do not edit" in text

    def test_entry_scripts_import_the_shared_library(self):
        for spec in ALL_SPECS:
            if not spec.script:
                continue
            text = installer.render_entry_script(spec)
            function, _, _ = installer.script_call(spec)
            assert f"from corlinman_jobs_lib import {function}" in text

    def test_parameters_are_baked_in(self):
        agenda = installer.render_entry_script(spec_by_name("hermes.daily_agenda"))
        assert "scheduler_data/class_schedule.yaml" in agenda
        assert TIMEZONE in agenda

    def test_youtube_script_carries_channels_and_watermark_file(self):
        spec = spec_by_name("hermes.youtube_daily")
        text = installer.render_entry_script(spec)
        for channel in spec.params["youtube_channels"]:
            assert channel in text
        assert spec.params["state_file"] in text
        assert spec.name in text  # job_name, used to find its own run history

    def test_diary_channels_come_from_the_source_not_the_metadata(self):
        """corlinman hardcoded them in _diary_summary_action, not in metadata."""
        spec = spec_by_name("hermes.diary_summary")
        assert "channels" not in spec.params
        _, kwargs, _ = installer.script_call(spec)
        assert kwargs["channels"] == list(DIARY_CHANNELS)

    def test_analysis_channels_do_come_from_the_metadata(self):
        spec = spec_by_name("hermes.analysis_digest")
        _, kwargs, _ = installer.script_call(spec)
        assert kwargs["channels"] == list(spec.params["channels"])

    def test_qzone_corpus_script_gets_an_explicit_repo_root(self):
        """The cron subprocess env strips hermes-owned PYTHONPATH entries."""
        _, kwargs, _ = installer.script_call(spec_by_name("hermes.qzone_daily"))
        assert (installer.repo_root() / "plugins" / "qzone" / "state.py").is_file()
        assert kwargs["repo_root"] == str(installer.repo_root())

    def test_decay_runs_grantleys_own_script_and_stays_silent(self):
        """An hourly maintenance tick has no product; stdout must stay empty."""
        function, kwargs, silent = installer.script_call(spec_by_name("persona.decay"))
        assert function == "main_grantley_decay"
        assert kwargs["script_path"].endswith("grantley_job.py")
        assert silent is True
        text = installer.render_entry_script(spec_by_name("persona.decay"))
        assert "redirect_stdout(sys.stderr)" in text

    def test_no_other_script_silences_its_output(self):
        for spec in ALL_SPECS:
            if spec.script and spec.name != "persona.decay":
                assert installer.script_call(spec)[2] is False

    def test_decay_prefers_a_deployed_grantley_over_the_repo_copy(self, tmp_path):
        deployed = (
            installer.hermes_home() / "plugins" / "grantley" / "scripts"
        )
        deployed.mkdir(parents=True)
        (deployed / "grantley_job.py").write_text("# deployed\n", encoding="utf-8")
        assert installer.grantley_decay_script() == deployed / "grantley_job.py"

    def test_decay_falls_back_to_the_repo_copy(self):
        resolved = installer.grantley_decay_script()
        assert resolved == (
            installer.repo_root() / "plugins" / "grantley" / "scripts" / "grantley_job.py"
        )
        assert resolved.is_file()

    def test_a_scripted_job_the_table_does_not_know_is_a_hard_error(self):
        rogue = replace(spec_by_name("persona.decay"), name="hermes.invented")
        with pytest.raises(KeyError):
            installer.script_call(rogue)

    def test_unbakeable_values_are_refused(self):
        with pytest.raises(TypeError):
            installer._py_literal({"a": 1})


class TestToolsetTranslation:
    def test_named_toolsets_pass_through(self):
        fields = installer._spec_job_fields(spec_by_name("hermes.competition_daily"))
        assert fields["enabled_toolsets"] == ["web"]

    def test_an_empty_toolset_becomes_the_no_tools_sentinel(self):
        """An empty list is falsy and would silently mean "all cron tools"."""
        fields = installer._spec_job_fields(spec_by_name("hermes.diary_summary"))
        assert fields["enabled_toolsets"] == [NO_TOOLS_SENTINEL]

    def test_the_sentinel_really_resolves_to_no_tools(self):
        from cron.scheduler import _resolve_cron_enabled_toolsets

        resolved = _resolve_cron_enabled_toolsets(
            {"enabled_toolsets": [NO_TOOLS_SENTINEL]}, {}
        )
        assert resolved == []

    def test_no_agent_jobs_carry_no_toolsets(self):
        for spec in ALL_SPECS:
            if spec.no_agent:
                assert installer._spec_job_fields(spec)["enabled_toolsets"] is None


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_plan_creates_no_files_or_directories(self):
        installer.plan()
        assert not installer.scripts_dir().exists()
        assert not installer.state_dir().exists()
        assert not installer.manifest_path().exists()

    def test_plan_creates_no_jobs(self):
        installer.plan()
        assert _jobs() == {}

    def test_plan_reports_every_job_and_file_it_would_touch(self):
        plan = installer.plan()
        assert {a.name for a in plan.jobs} == {s.name for s in ALL_SPECS}
        assert {a.action for a in plan.jobs} == {"create"}
        assert {a.action for a in plan.files} == {"create"}

    def test_plan_is_blocked_on_a_bare_profile(self):
        plan = installer.plan()
        assert plan.blocked
        assert "timezone" in {c.key for c in plan.blocking_checks}

    def test_plan_is_ready_on_a_configured_profile(self, ready):
        plan = installer.plan()
        assert not plan.blocked
        assert len(plan.would_create) == len(ALL_SPECS)

    def test_plan_skips_the_qq_checks_when_no_qq_job_is_selected(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        plan = installer.plan(only=list(NON_QQ))
        assert "onebot" not in {c.key for c in plan.checks}
        assert not plan.blocked

    def test_the_qq_checks_return_when_a_qq_job_is_selected(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        plan = installer.plan(only=["hermes.qzone_daily"])
        assert "onebot" in {c.key for c in plan.checks}
        assert plan.blocked

    def test_formatted_plan_names_the_public_feed_writers(self, ready):
        text = installer.format_plan(installer.plan())
        assert text.count("WRITES A REAL PUBLIC QQ FEED") == 3
        assert "hermes.qzone_daily" in text

    def test_formatted_plan_of_a_blocked_profile_says_nothing_would_happen(self):
        text = installer.format_plan(installer.plan())
        assert "BLOCKED — nothing would be installed" in text

    def test_unknown_job_names_are_rejected_with_the_known_list(self):
        with pytest.raises(KeyError) as excinfo:
            installer.select_specs(["hermes.nope"])
        assert "persona.decay" in str(excinfo.value)

    def test_selection_preserves_schedule_order(self):
        chosen = installer.select_specs(["persona.decay", "hermes.daily_agenda"])
        assert [s.name for s in chosen] == ["hermes.daily_agenda", "persona.decay"]


# ---------------------------------------------------------------------------
# The real install
# ---------------------------------------------------------------------------


class TestInstallRefusals:
    def test_a_failing_preflight_stops_the_install(self):
        result = installer.install()
        assert not result.ok
        assert "preflight failed" in result.error

    def test_a_refused_install_writes_nothing(self):
        installer.install()
        assert not installer.scripts_dir().exists()
        assert _jobs() == {}

    def test_preflight_failure_is_not_waivable_by_force(self):
        result = installer.install(force=True)
        assert not result.ok
        assert "preflight failed" in result.error

    def test_a_spec_that_asks_to_be_enabled_is_refused(self, ready, monkeypatch):
        poisoned = tuple(
            replace(s, install_enabled=True) if s.name == "persona.decay" else s
            for s in JOB_SPECS
        )
        monkeypatch.setattr(
            installer, "select_specs", lambda only=None: poisoned
        )
        result = installer.install()
        assert not result.ok
        assert "install_enabled=True" in result.error
        assert _jobs() == {}


class TestInstallProducts:
    @pytest.fixture(autouse=True)
    def _installed(self, ready):
        self.result = installer.install()
        assert self.result.ok, self.result.error

    def test_every_script_lands_in_the_hermes_scripts_dir(self):
        directory = installer.scripts_dir()
        for name, text in installer.planned_files().items():
            assert (directory / name).read_text(encoding="utf-8") == text

    def test_scripts_satisfy_the_schedulers_containment_contract(self):
        """``cron/scheduler.py::_run_job_script`` resolves each script against
        ``$HERMES_HOME/scripts`` and refuses anything that lands outside it,
        then picks an interpreter by suffix. Reproduced here rather than
        imported: the check is inline in that function, and calling it would
        execute the script.
        """
        root = installer.scripts_dir().resolve()
        for spec in ALL_SPECS:
            if not spec.script:
                continue
            resolved = (installer.scripts_dir() / spec.script).resolve()
            resolved.relative_to(root)  # raises if it escapes
            assert resolved.is_file()
            assert resolved.suffix == ".py"  # runs under sys.executable, not bash

    def test_every_job_exists(self):
        assert set(_jobs()) == {s.name for s in ALL_SPECS}

    def test_every_job_is_paused(self):
        for name, job in _jobs().items():
            assert job["enabled"] is False, f"{name} was installed ENABLED"
            assert job["state"] == "paused"
            assert job["paused_reason"]

    def test_no_job_is_runnable(self):
        from cron.jobs import is_job_runnable

        for job in _jobs().values():
            assert not is_job_runnable(job)

    def test_no_job_is_due(self):
        from cron.jobs import get_due_jobs

        assert get_due_jobs() == []

    def test_the_pause_reason_records_which_kind_of_disabled(self):
        jobs = _jobs()
        assert "before the migration" in jobs["hermes.daily_agenda"]["paused_reason"]
        assert "at cutover" in jobs["hermes.competition_daily"]["paused_reason"]

    def test_schedules_are_the_staggered_ones(self):
        jobs = _jobs()
        for spec in ALL_SPECS:
            assert jobs[spec.name]["schedule_display"] == spec.schedule
            assert jobs[spec.name]["schedule"]["expr"] == spec.schedule

    def test_delivery_targets_survive(self):
        jobs = _jobs()
        for spec in ALL_SPECS:
            assert jobs[spec.name]["deliver"] == spec.deliver

    def test_script_and_no_agent_flags_survive(self):
        jobs = _jobs()
        for spec in ALL_SPECS:
            assert jobs[spec.name]["script"] == spec.script
            assert jobs[spec.name]["no_agent"] is spec.no_agent

    def test_prompts_survive(self):
        jobs = _jobs()
        for spec in ALL_SPECS:
            assert jobs[spec.name]["prompt"] == (spec.prompt or "")

    def test_toolsets_survive(self):
        jobs = _jobs()
        assert jobs["hermes.competition_daily"]["enabled_toolsets"] == ["web"]
        assert jobs["hermes.diary_summary"]["enabled_toolsets"] == [NO_TOOLS_SENTINEL]
        assert jobs["persona.decay"]["enabled_toolsets"] is None

    def test_the_result_lists_what_it_did(self):
        assert len(self.result.created) == len(ALL_SPECS)
        assert set(self.result.written) == set(installer.planned_files())
        assert self.result.skipped == ()
        assert self.result.unpaused == ()

    def test_the_manifest_records_the_files_and_the_job_ids(self):
        manifest = installer.read_manifest()
        assert set(manifest["files"]) == set(installer.planned_files())
        assert set(manifest["jobs"]) == {s.name for s in ALL_SPECS}
        assert manifest["repo_root"] == str(installer.repo_root())

    def test_drift_is_clean_immediately_after(self):
        assert set(installer.script_drift().values()) == {"ok"}


class TestIdempotency:
    def test_a_second_install_creates_no_duplicate_jobs(self, ready):
        assert installer.install().ok
        first = _jobs()
        second = installer.install()
        assert second.ok
        assert second.created == ()
        assert set(second.skipped) == {s.name for s in ALL_SPECS}
        assert {n: j["id"] for n, j in _jobs().items()} == {
            n: j["id"] for n, j in first.items()
        }

    def test_a_second_install_rewrites_nothing(self, ready):
        assert installer.install().ok
        assert installer.install().written == ()

    def test_an_existing_job_is_reported_rather_than_rebuilt(self, ready):
        assert installer.install().ok
        actions = {a.name: a for a in installer.job_actions()}
        assert actions["persona.decay"].action == "exists"
        assert "Left untouched" in actions["persona.decay"].reason

    def test_an_operator_edited_job_is_reported_as_differing(self, ready):
        from cron.jobs import update_job

        assert installer.install().ok
        job = _jobs()["hermes.competition_daily"]
        update_job(job["id"], {"deliver": "local"})
        action = {a.name: a for a in installer.job_actions()}["hermes.competition_daily"]
        assert "differs from the spec in deliver" in action.reason

    def test_reinstalling_never_re_enables_an_operator_enabled_job(self, ready):
        """Cutover flips a job on; a later install must not fight the operator."""
        from cron.jobs import resume_job

        assert installer.install().ok
        job = _jobs()["hermes.daily_agenda"]
        resume_job(job["id"])
        assert _jobs()["hermes.daily_agenda"]["enabled"] is True
        assert installer.install().ok
        assert _jobs()["hermes.daily_agenda"]["enabled"] is True
        action = {a.name: a for a in installer.job_actions()}["hermes.daily_agenda"]
        assert "ENABLED" in action.reason


class TestLocalEdits:
    def _install_then_edit(self, text="# hand edited\n"):
        assert installer.install().ok
        target = installer.scripts_dir() / "corlinman_daily_agenda.py"
        target.write_text(text, encoding="utf-8")
        return target

    def test_a_hand_edited_script_is_a_conflict(self, ready):
        self._install_then_edit()
        actions = {a.filename: a for a in installer.file_actions()}
        assert actions["corlinman_daily_agenda.py"].action == "conflict"
        assert actions["corlinman_daily_agenda.py"].blocking

    def test_an_install_refuses_to_clobber_it(self, ready):
        target = self._install_then_edit()
        result = installer.install()
        assert not result.ok
        assert "corlinman_daily_agenda.py" in result.error
        assert target.read_text(encoding="utf-8") == "# hand edited\n"

    def test_force_overwrites_and_says_so(self, ready):
        target = self._install_then_edit()
        result = installer.install(force=True)
        assert result.ok
        assert "corlinman_daily_agenda.py" in result.written
        assert installer.GENERATED_MARKER in target.read_text(encoding="utf-8")

    def test_our_own_stale_file_is_refreshed_without_force(self, ready):
        """A file whose hash matches the manifest is ours to update."""
        assert installer.install().ok
        target = installer.scripts_dir() / "corlinman_daily_agenda.py"
        stale = "# an older generated version\n"
        target.write_text(stale, encoding="utf-8")
        manifest = installer.read_manifest()
        manifest["files"]["corlinman_daily_agenda.py"] = installer._sha256(stale)
        installer.manifest_path().write_text(json.dumps(manifest), encoding="utf-8")

        actions = {a.filename: a for a in installer.file_actions()}
        assert actions["corlinman_daily_agenda.py"].action == "update"
        result = installer.install()
        assert result.ok
        assert "corlinman_daily_agenda.py" in result.written
        assert installer.GENERATED_MARKER in target.read_text(encoding="utf-8")

    def test_a_deleted_script_is_reinstalled(self, ready):
        assert installer.install().ok
        target = installer.scripts_dir() / "corlinman_youtube_state.py"
        target.unlink()
        assert installer.script_drift()["corlinman_youtube_state.py"] == "missing"
        assert installer.install().ok
        assert target.is_file()

    def test_an_unreadable_manifest_does_not_crash_the_planner(self, ready):
        assert installer.install().ok
        installer.manifest_path().write_text("{not json", encoding="utf-8")
        assert installer.read_manifest() == {}
        installer.plan()


class TestPartialInstall:
    def test_only_installs_the_named_jobs(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        result = installer.install(only=["hermes.daily_agenda"])
        assert result.ok
        assert set(_jobs()) == {"hermes.daily_agenda"}

    def test_only_writes_the_scripts_those_jobs_need(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        installer.install(only=["hermes.daily_agenda"])
        present = {p.name for p in installer.scripts_dir().iterdir()}
        assert present == {installer.LIB_FILENAME, "corlinman_daily_agenda.py"}

    def test_a_job_that_needs_no_script_writes_no_library(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", TIMEZONE)
        result = installer.install(only=["hermes.competition_daily"])
        assert result.ok
        assert result.written == ()
        assert not installer.scripts_dir().exists() or not list(
            installer.scripts_dir().iterdir()
        )

    def test_the_rest_can_be_added_later(self, ready):
        assert installer.install(only=["hermes.daily_agenda"]).ok
        second = installer.install()
        assert second.ok
        assert set(_jobs()) == {s.name for s in ALL_SPECS}
        assert "hermes.daily_agenda" in second.skipped


class TestCli:
    def _run(self, action, **kwargs):
        namespace = argparse.Namespace(
            corlinman_jobs_action=action, only=None, force=False, **kwargs
        )
        return installer.corlinman_jobs_command(namespace)

    def test_plan_exits_nonzero_when_blocked(self, capsys):
        assert self._run("plan") == 1
        assert "BLOCKED" in capsys.readouterr().out

    def test_plan_exits_zero_when_ready(self, ready, capsys):
        assert self._run("plan") == 0
        assert "ready:" in capsys.readouterr().out

    def test_plan_changes_nothing(self, ready):
        self._run("plan")
        assert _jobs() == {}
        assert not installer.scripts_dir().exists()

    def test_install_reports_every_created_job_as_paused(self, ready, capsys):
        assert self._run("install") == 0
        out = capsys.readouterr().out
        assert out.count("created (paused)") == len(ALL_SPECS)
        assert "Every job is PAUSED" in out

    def test_install_exits_nonzero_when_blocked(self, capsys):
        assert self._run("install") == 1
        assert "install failed" in capsys.readouterr().err

    def test_status_reports_not_installed_first(self, capsys):
        assert self._run("status") == 0
        assert "NOT INSTALLED" in capsys.readouterr().out

    def test_status_reports_paused_after_an_install(self, ready, capsys):
        self._run("install")
        capsys.readouterr()
        assert self._run("status") == 0
        out = capsys.readouterr().out
        assert "ENABLED" not in out
        paused = [line for line in out.splitlines() if ": paused " in line]
        assert len(paused) == len(ALL_SPECS)

    def test_no_action_prints_usage(self, capsys):
        assert installer.corlinman_jobs_command(argparse.Namespace()) == 2
        assert "Usage:" in capsys.readouterr().out

    def test_an_unknown_job_name_is_a_usage_error(self, capsys):
        namespace = argparse.Namespace(
            corlinman_jobs_action="plan", only=["nope"], force=False
        )
        assert installer.corlinman_jobs_command(namespace) == 2
        assert "unknown job" in capsys.readouterr().err

    def test_the_parser_accepts_the_documented_flags(self):
        parser = argparse.ArgumentParser()
        installer.register_cli(parser)
        args = parser.parse_args(["install", "--only", "persona.decay", "--force"])
        assert args.corlinman_jobs_action == "install"
        assert args.only == ["persona.decay"]
        assert args.force is True
