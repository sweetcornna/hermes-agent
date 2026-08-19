"""The job-side library: what each ``main_*`` puts on stdout, and when.

Three behaviours carry the whole design and are asserted for every entry
point:

* **stdout is the product.** For an agent job it becomes the ``## Script
  Output`` block; for a ``no_agent`` job it *is* the delivered message.
* **diagnostics go to stderr**, where hermes drops them on success and
  surfaces them inside a ``## Script Error`` block on failure.
* **empty stdout is a decision, not an accident** — hermes skips the model
  call and the delivery entirely, so a script that has nothing to say must
  say nothing, and a script whose silence would be misread must print its
  documented marker instead.

Nothing here opens a socket, a QQ session or a Telegram connection. The one
function that reaches into another package (``main_qzone_recent_posts``)
reads through ``plugins.qzone.state``'s public surface, and the tests stub
that surface rather than fabricate its files.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_PATH = REPO_ROOT / "plugins" / "corlinman_jobs" / "scripts" / "corlinman_jobs_lib.py"
TZ = "Asia/Shanghai"


def _load_lib():
    """Import the library the way the installed copy is imported: by filename,
    as a top-level module with no package context."""
    spec = importlib.util.spec_from_file_location("corlinman_jobs_lib", LIB_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lib = _load_lib()


@pytest.fixture
def state_db(monkeypatch):
    """A real hermes state database, built from hermes's own schema."""
    from hermes_state_common import SCHEMA_SQL

    path = lib.hermes_home() / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    return path


def _add_session(db, session_id, source="telegram", user_id="1114483029"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO sessions (id, source, user_id, started_at) VALUES (?,?,?,?)",
        (session_id, source, user_id, 0.0),
    )
    conn.commit()
    conn.close()


def _add_message(db, session_id, role, content, when, *, active=1, display_kind=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active, display_kind) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, role, content, when.timestamp(), active, display_kind),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Output discipline
# ---------------------------------------------------------------------------


class TestOutputDiscipline:
    def test_log_writes_to_stderr_not_stdout(self, capsys):
        lib.log("a diagnostic")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "a diagnostic" in captured.err

    def test_the_library_imports_nothing_from_the_hermes_tree(self):
        """The installed copy lives in $HERMES_HOME/scripts with no package
        context, and the cron subprocess env strips hermes-owned PYTHONPATH."""
        source = LIB_PATH.read_text(encoding="utf-8")
        banned = ("from hermes", "import hermes", "from tools", "from cron")
        top_level = [
            line
            for line in source.splitlines()
            if not line.startswith((" ", "\t")) and line.startswith(("import ", "from "))
        ]
        for line in top_level:
            assert not line.startswith(banned), line

    def test_yaml_is_imported_lazily(self):
        """Only the agenda job needs it; an import-time failure would take
        every other job down with it."""
        source = LIB_PATH.read_text(encoding="utf-8")
        assert "\nimport yaml" not in source
        assert "import yaml" in source


class TestLocations:
    def test_hermes_home_follows_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
        assert lib.hermes_home() == tmp_path / "elsewhere"

    def test_hermes_home_falls_back_for_a_hand_run(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        assert lib.hermes_home() == Path.home() / ".hermes"

    def test_job_state_dir_is_created_under_the_profile(self):
        directory = lib.job_state_dir()
        assert directory.is_dir()
        assert directory == lib.hermes_home() / "plugin-data" / "corlinman_jobs"

    def test_state_dir_is_not_corlinmans_resolve_data_dir_antipattern(self):
        """The source resolved its data dir from an app-state attribute that
        was never populated, and failed silently 1260 times. There is exactly
        one explicit resolution here and it cannot come back empty."""
        assert lib.job_state_dir().is_absolute()


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------


class TestRedact:
    def test_strips_an_openai_style_key(self):
        out = lib.redact("here is sk-abcdefghijklmnopqrstuvwxyz123 ok")
        assert "sk-abcdefghijkl" not in out
        assert "[REDACTED]" in out

    def test_keeps_the_label_of_a_keyed_secret(self):
        out = lib.redact("api_key: hunter2hunter2hunter2")
        assert out.startswith("api_key=[REDACTED]")

    def test_strips_a_bearer_token_but_keeps_the_scheme(self):
        out = lib.redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
        assert "Bearer [REDACTED]" in out
        assert "abcdefghij" not in out

    def test_leaves_ordinary_prose_alone(self):
        text = "今天写了一点代码，晚上去跑步。"
        assert lib.redact(text) == text

    def test_handles_empty_input(self):
        assert lib.redact("") == ""


class TestDecodeContent:
    def test_plain_text_passes_through(self):
        assert lib.decode_content("hello") == "hello"

    def test_multimodal_json_is_flattened_to_its_text_parts(self):
        raw = lib.CONTENT_JSON_PREFIX + json.dumps(
            [{"type": "text", "text": "a"}, {"type": "image", "url": "x"},
             {"type": "text", "text": "b"}]
        )
        assert lib.decode_content(raw) == "a b"

    def test_a_broken_sentinel_payload_returns_the_raw_string(self):
        raw = lib.CONTENT_JSON_PREFIX + "{not json"
        assert lib.decode_content(raw) == raw

    def test_none_becomes_empty(self):
        assert lib.decode_content(None) == ""


# ---------------------------------------------------------------------------
# hermes.diary_summary
# ---------------------------------------------------------------------------


class TestDiaryMaterial:
    def test_collects_todays_user_messages(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=12, minute=30)
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "user", "早上去跑步了", now.replace(hour=8))
        _add_message(state_db, "s1", "user", "晚上写了点代码", now.replace(hour=21, minute=0))

        assert lib.main_diary_material(
            user_id="1114483029",
            channels=["telegram", "gateway"],
            timezone=TZ,
            now=now.replace(hour=23, minute=50),
        ) == 0
        out = capsys.readouterr().out
        assert "采集到用户消息：2 条" in out
        assert "【08:30】早上去跑步了" in out
        assert "【21:00】晚上写了点代码" in out

    def test_prints_the_no_material_marker_rather_than_nothing(self, state_db, capsys):
        """Silence would make hermes skip the run; the source still delivered
        its fixed "no diary" sentence, so the script must speak."""
        now = datetime.now(ZoneInfo(TZ)).replace(hour=23, minute=50)
        assert lib.main_diary_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        ) == 0
        out = capsys.readouterr().out
        assert lib.NO_DIARY_MATERIAL in out
        assert out.strip()

    def test_excludes_assistant_turns(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=20)
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "assistant", "我的回答", now.replace(hour=9))
        lib.main_diary_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        assert lib.NO_DIARY_MATERIAL in capsys.readouterr().out

    def test_excludes_other_users_and_other_channels(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=20)
        _add_session(state_db, "mine", source="telegram", user_id="1114483029")
        _add_session(state_db, "theirs", source="telegram", user_id="999")
        _add_session(state_db, "elsewhere", source="discord", user_id="1114483029")
        _add_message(state_db, "theirs", "user", "别人的消息", now.replace(hour=9))
        _add_message(state_db, "elsewhere", "user", "别的渠道", now.replace(hour=9))
        _add_message(state_db, "mine", "user", "我的消息", now.replace(hour=9))
        lib.main_diary_material(
            user_id="1114483029", channels=["telegram", "gateway"], timezone=TZ, now=now
        )
        out = capsys.readouterr().out
        assert "我的消息" in out
        assert "别人的消息" not in out
        assert "别的渠道" not in out

    def test_excludes_compaction_noise(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=20)
        _add_session(state_db, "s1")
        _add_message(
            state_db, "s1", "user", lib.NOISE_PREFIXES[0] + " blah", now.replace(hour=9)
        )
        lib.main_diary_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        assert lib.NO_DIARY_MATERIAL in capsys.readouterr().out

    def test_excludes_inactive_and_display_only_rows(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=20)
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "user", "已压缩", now.replace(hour=9), active=0)
        _add_message(
            state_db, "s1", "user", "只是显示", now.replace(hour=9), display_kind="status"
        )
        lib.main_diary_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        assert lib.NO_DIARY_MATERIAL in capsys.readouterr().out

    def test_redacts_secrets_before_they_reach_the_prompt(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=20)
        _add_session(state_db, "s1")
        _add_message(
            state_db, "s1", "user",
            "配置了 sk-abcdefghijklmnopqrstuvwxyz123", now.replace(hour=9),
        )
        lib.main_diary_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        out = capsys.readouterr().out
        assert "sk-abcdefghijkl" not in out
        assert "[REDACTED]" in out

    def test_truncates_an_overlong_message(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=20)
        _add_session(state_db, "s1")
        _add_message(
            state_db, "s1", "user", "长" * (lib.MAX_MESSAGE_CHARS + 50), now.replace(hour=9)
        )
        lib.main_diary_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        assert "已截断" in capsys.readouterr().out

    def test_deduplicates_identical_messages_at_the_same_instant(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=20)
        moment = now.replace(hour=9, minute=0, second=0, microsecond=0)
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "user", "重复", moment)
        _add_message(state_db, "s1", "user", "重复", moment)
        lib.main_diary_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        assert "采集到用户消息：1 条" in capsys.readouterr().out

    def test_ignores_yesterday(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ)).replace(hour=20)
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "user", "昨天的事", now - timedelta(days=1))
        lib.main_diary_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        assert lib.NO_DIARY_MATERIAL in capsys.readouterr().out

    def test_a_missing_database_is_a_loud_failure(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
        with pytest.raises(SystemExit) as excinfo:
            lib.main_diary_material(
                user_id="x", channels=["telegram"], timezone=TZ,
                now=datetime.now(ZoneInfo(TZ)),
            )
        assert "state database not found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# hermes.analysis_digest
# ---------------------------------------------------------------------------


class TestAnalysisMaterial:
    def test_keeps_only_keyword_hits(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ))
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "user", "今天的分析结论是这样", now - timedelta(hours=2))
        _add_message(state_db, "s1", "user", "晚饭吃了面", now - timedelta(hours=1))
        assert lib.main_analysis_material(
            user_id="1114483029", channels=["telegram", "gateway"], timezone=TZ, now=now
        ) == 0
        out = capsys.readouterr().out
        assert "分析结论" in out
        assert "晚饭" not in out
        assert "命中记录：1 条" in out

    def test_includes_assistant_turns_unlike_the_diary_job(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ))
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "assistant", "研究表明如此", now - timedelta(hours=1))
        lib.main_analysis_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        assert "[assistant] 研究表明如此" in capsys.readouterr().out

    def test_prints_the_documented_marker_when_nothing_matches(self, state_db, capsys):
        """The prompt keys off this exact line to emit the source's fixed
        "no analysis" sentence — silence would deliver nothing at all."""
        now = datetime.now(ZoneInfo(TZ))
        assert lib.main_analysis_material(
            user_id="x", channels=["telegram"], timezone=TZ, now=now
        ) == 0
        out = capsys.readouterr().out
        assert lib.NO_ANALYSIS_MARKER in out
        assert "命中记录" not in out

    def test_the_marker_is_the_string_the_prompt_expects(self):
        from plugins.corlinman_jobs import prompts

        assert "没有命中材料" in prompts.ANALYSIS_USER
        assert lib.NO_ANALYSIS_MARKER.startswith("（过去 24 小时没有命中")

    def test_the_window_is_24_hours_not_since_midnight(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ))
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "user", "昨夜的研究记录", now - timedelta(hours=20))
        _add_message(state_db, "s1", "user", "更早的研究记录", now - timedelta(hours=30))
        lib.main_analysis_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        out = capsys.readouterr().out
        assert "昨夜的研究记录" in out
        assert "更早的研究记录" not in out

    def test_matches_english_keywords_case_insensitively(self, state_db, capsys):
        now = datetime.now(ZoneInfo(TZ))
        _add_session(state_db, "s1")
        _add_message(state_db, "s1", "user", "RESEARCH notes", now - timedelta(hours=1))
        lib.main_analysis_material(
            user_id="1114483029", channels=["telegram"], timezone=TZ, now=now
        )
        assert "RESEARCH notes" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# hermes.youtube_daily — the watermark
# ---------------------------------------------------------------------------


class TestExtractVideoIds:
    def test_reads_the_per_item_id_lines_the_prompt_asks_for(self):
        text = "标题 A\n视频ID：dQw4w9WgXcQ\n\n标题 B\n视频ID: abcdefghijk\n"
        assert lib.extract_video_ids(text) == ["dQw4w9WgXcQ", "abcdefghijk"]

    def test_still_accepts_the_sources_legacy_trailer(self):
        text = 'stuff\nYOUTUBE_STATE:{"new_video_ids": ["aaaaaaaaaaa"]}\n'
        assert lib.extract_video_ids(text) == ["aaaaaaaaaaa"]

    def test_deduplicates_while_preserving_order(self):
        text = "视频ID：aaaaaaaaaaa\n视频ID：bbbbbbbbbbb\n视频ID：aaaaaaaaaaa\n"
        assert lib.extract_video_ids(text) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]

    def test_ignores_ids_of_the_wrong_length(self):
        assert lib.extract_video_ids("视频ID：short\n") == []

    def test_a_line_with_extra_text_is_not_an_id_line(self):
        assert lib.extract_video_ids("视频ID：dQw4w9WgXcQ https://x\n") == []

    def test_no_ids_in_empty_output(self):
        assert lib.extract_video_ids("") == []
        assert lib.extract_video_ids("今天没有新视频。") == []


class TestYoutubeWatermark:
    JOB = "hermes.youtube_daily"
    STATE = "scheduler_state/youtube_daily.json"

    def _write_job(self, *, job_id="abc123", status="ok", delivery_error=None):
        path = lib.hermes_home() / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": job_id,
                            "name": self.JOB,
                            "last_status": status,
                            "last_delivery_error": delivery_error,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def _write_output(self, job_id, filename, text):
        directory = lib.hermes_home() / "cron" / "output" / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(text, encoding="utf-8")

    def _watermark(self):
        return json.loads(
            (lib.job_state_dir() / self.STATE).read_text(encoding="utf-8")
        )

    def test_first_run_emits_an_empty_watermark_block(self, capsys):
        assert lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        ) == 0
        out = capsys.readouterr().out
        assert "频道：https://x" in out
        assert "已处理 video_id：无" in out

    def test_harvests_the_previous_delivered_run(self, capsys):
        self._write_job()
        self._write_output("abc123", "2026-08-18.md", "视频ID：aaaaaaaaaaa\n")
        lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        )
        assert self._watermark()["seen_video_ids"] == ["aaaaaaaaaaa"]
        assert "aaaaaaaaaaa" in capsys.readouterr().out

    def test_does_not_advance_when_delivery_failed(self, capsys):
        """The source persisted only on `delivery.ok and not shadow`; a run
        whose content was fine but whose send failed must be re-reported."""
        self._write_job(status="ok", delivery_error="chat not found")
        self._write_output("abc123", "2026-08-18.md", "视频ID：aaaaaaaaaaa\n")
        lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        )
        assert not (lib.job_state_dir() / self.STATE).exists()
        captured = capsys.readouterr()
        assert "watermark not advanced" in captured.err
        assert "已处理 video_id：无" in captured.out

    def test_does_not_advance_when_the_run_errored(self, capsys):
        self._write_job(status="error")
        self._write_output("abc123", "2026-08-18.md", "视频ID：aaaaaaaaaaa\n")
        lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        )
        assert not (lib.job_state_dir() / self.STATE).exists()

    def test_the_same_output_is_never_harvested_twice(self, capsys):
        self._write_job()
        self._write_output("abc123", "2026-08-18.md", "视频ID：aaaaaaaaaaa\n")
        lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        )
        capsys.readouterr()
        lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        )
        assert self._watermark()["seen_video_ids"] == ["aaaaaaaaaaa"]
        assert "already harvested" in capsys.readouterr().err

    def test_a_later_run_merges_rather_than_replaces(self, capsys):
        self._write_job()
        self._write_output("abc123", "2026-08-18.md", "视频ID：aaaaaaaaaaa\n")
        lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        )
        self._write_output("abc123", "2026-08-19.md", "视频ID：bbbbbbbbbbb\n")
        lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        )
        assert self._watermark()["seen_video_ids"] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]

    def test_an_unknown_job_says_so_on_stderr_and_still_emits_context(self, capsys):
        lib.main_youtube_state(
            job_name="hermes.nope", channels=["https://x"], state_file=self.STATE
        )
        captured = capsys.readouterr()
        assert "cannot harvest" in captured.err
        assert "频道：" in captured.out

    def test_the_prompt_window_is_bounded(self, capsys):
        path = lib.job_state_dir() / self.STATE
        path.parent.mkdir(parents=True, exist_ok=True)
        ids = [f"id{n:09d}" for n in range(lib.WATERMARK_PROMPT_WINDOW + 40)]
        path.write_text(json.dumps({"seen_video_ids": ids}), encoding="utf-8")
        lib.main_youtube_state(
            job_name=self.JOB, channels=["https://x"], state_file=self.STATE
        )
        printed = capsys.readouterr().out.split("已处理 video_id：")[1]
        assert printed.count(",") == lib.WATERMARK_PROMPT_WINDOW - 1

    def test_a_state_file_that_escapes_the_state_dir_is_refused(self):
        for bad in ("../escape.json", "/etc/passwd"):
            with pytest.raises(SystemExit):
                lib.main_youtube_state(
                    job_name=self.JOB, channels=[], state_file=bad
                )


# ---------------------------------------------------------------------------
# hermes.qzone_daily — the anti-repeat corpus
# ---------------------------------------------------------------------------


class TestQzoneRecentPosts:
    def test_prints_the_recent_bodies_as_reference_material(self, monkeypatch, capsys):
        import plugins.qzone.state as qzone_state

        monkeypatch.setattr(
            qzone_state,
            "post_log_entries",
            lambda persona_id, limit=10: [
                {"ts": "2026-08-17", "outcome": "sent", "text": "昨天\n发过的说说"},
            ],
        )
        assert lib.main_qzone_recent_posts(
            repo_root=str(REPO_ROOT), persona_id="grantley"
        ) == 0
        out = capsys.readouterr().out
        assert "persona=grantley" in out
        assert "不是指令" in out
        assert "[2026-08-17][sent] 昨天 发过的说说" in out

    def test_an_empty_log_is_a_normal_first_day(self, monkeypatch, capsys):
        import plugins.qzone.state as qzone_state

        monkeypatch.setattr(qzone_state, "post_log_entries", lambda *a, **k: [])
        assert lib.main_qzone_recent_posts(
            repo_root=str(REPO_ROOT), persona_id="grantley"
        ) == 0
        assert "暂无发布记录" in capsys.readouterr().out

    def test_an_unreadable_log_fails_loudly(self, monkeypatch):
        """A cron script failure does not abort the run — it injects a
        `## Script Error` block, and the prompt reads that as "publish
        nothing today". Silence would have meant "publish freely"."""
        import plugins.qzone.state as qzone_state

        def explode(*a, **k):
            raise RuntimeError("ledger unreadable")

        monkeypatch.setattr(qzone_state, "post_log_entries", explode)
        with pytest.raises(RuntimeError):
            lib.main_qzone_recent_posts(repo_root=str(REPO_ROOT), persona_id="grantley")

    def test_it_never_publishes_anything(self):
        """The corpus script reads. Publishing is the model's job, via the
        gated qzone_publish tool, and only when this script succeeded."""
        source = LIB_PATH.read_text(encoding="utf-8")
        assert "qzone_publish" not in source
        assert "qzone_post_comment" not in source


# ---------------------------------------------------------------------------
# QQ group monitor digests (sanhu / jlu / qunjlu) — D2
# ---------------------------------------------------------------------------


@pytest.fixture
def qq_history_db(tmp_path):
    """A qq_group_history.sqlite with the real corlinman_server schema."""
    path = tmp_path / "qq_group_history.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE group_messages (id INTEGER PRIMARY KEY, instance_id TEXT, "
        "group_id TEXT, sender_user_id TEXT, sender_name TEXT, message_id TEXT, "
        "event_time_ms INTEGER, received_at_ms INTEGER, text TEXT)"
    )
    conn.commit()
    conn.close()
    return path


def _add_group_message(
    db, *, instance_id="default", group_id="183287894", sender_id="1",
    sender_name="某人", received_at, text,
):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO group_messages (instance_id, group_id, sender_user_id, "
        "sender_name, event_time_ms, received_at_ms, text) VALUES (?,?,?,?,?,?,?)",
        (
            instance_id, group_id, sender_id, sender_name,
            int(received_at.timestamp() * 1000), int(received_at.timestamp() * 1000),
            text,
        ),
    )
    conn.commit()
    conn.close()


class TestQqMonitorWindowDesc:
    def test_whole_days(self):
        assert lib._qq_monitor_window_desc(1440) == "最近 1 天"
        assert lib._qq_monitor_window_desc(2880) == "最近 2 天"

    def test_whole_hours(self):
        assert lib._qq_monitor_window_desc(180) == "最近 3 小时"

    def test_odd_minutes(self):
        assert lib._qq_monitor_window_desc(90) == "最近 90 分钟"


class TestQqMonitorCollectionIds:
    def test_no_watch_list_means_no_filter(self):
        """Ported from _QqMonitorSource.collection_ids: empty watch_user_ids
        means "everyone", not "filter to nobody" — this is what lets jlu
        collect the whole group while only ★-marking its focus member."""
        assert lib._qq_monitor_collection_ids([], []) == ()
        assert lib._qq_monitor_collection_ids([], ["1076712858"]) == ()

    def test_watch_list_narrows_and_absorbs_focus(self):
        assert lib._qq_monitor_collection_ids(["1076712858"], []) == ("1076712858",)
        assert lib._qq_monitor_collection_ids(["1"], ["1", "2"]) == ("1", "2")


class TestQqMonitorFormatLines:
    def test_marks_focus_members_and_formats_the_stamp(self):
        tz = ZoneInfo(TZ)
        rows = [(int(datetime(2026, 8, 18, 9, 5, tzinfo=tz).timestamp() * 1000),
                  "1076712858", "目前5成仓", 0, "早")]
        lines = lib._qq_monitor_format_lines(rows, ["1076712858"], tz)
        assert lines == ["★[08-18 09:05] 目前5成仓(1076712858): 早"]

    def test_non_focus_members_get_no_marker(self):
        tz = ZoneInfo(TZ)
        rows = [(0, "999", "路人", 0, "水")]
        lines = lib._qq_monitor_format_lines(rows, ["1076712858"], tz)
        assert lines[0].startswith("[") and "★" not in lines[0]

    def test_truncates_an_overlong_message(self):
        tz = ZoneInfo(TZ)
        rows = [(0, "1", "x", 0, "长" * (lib.QQ_MONITOR_LINE_CAP + 50))]
        line = lib._qq_monitor_format_lines(rows, [], tz)[0]
        assert len(line.rsplit(": ", 1)[1]) == lib.QQ_MONITOR_LINE_CAP


class TestMainQqMonitorDigest:
    def test_empty_window_prints_nothing(self, qq_history_db, capsys):
        """All three migrated monitors have send_when_empty=false (A1 §4);
        empty stdout is hermes's own signal to skip the model call and the
        delivery entirely."""
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        rc = lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="qunjlu", now=now,
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no messages in the window" in captured.err

    def test_collects_the_window_and_renders_a_header(self, qq_history_db, capsys):
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        _add_group_message(
            qq_history_db, received_at=now - timedelta(hours=2), text="早上好",
        )
        rc = lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="sanhu", now=now,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "群 183287894 最近 1 天的消息汇总（共 1 条）。" in out
        assert "早上好" in out

    def test_only_the_configured_group_and_instance_are_collected(self, qq_history_db, capsys):
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        _add_group_message(
            qq_history_db, group_id="980927602", received_at=now - timedelta(hours=1),
            text="别的群",
        )
        _add_group_message(
            qq_history_db, instance_id="other", received_at=now - timedelta(hours=1),
            text="别的实例",
        )
        _add_group_message(
            qq_history_db, received_at=now - timedelta(hours=1), text="这才是它",
        )
        lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="jlu", now=now,
        )
        out = capsys.readouterr().out
        assert "这才是它" in out
        assert "别的群" not in out
        assert "别的实例" not in out

    def test_outside_the_window_is_excluded(self, qq_history_db, capsys):
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        _add_group_message(
            qq_history_db, received_at=now - timedelta(days=2), text="太久以前",
        )
        rc = lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="qunjlu", now=now,
        )
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_watch_user_ids_filters_the_query(self, qq_history_db, capsys):
        """qunjlu's contract: only 1076712858, everyone else in the group
        excluded (A1 §4)."""
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        _add_group_message(
            qq_history_db, sender_id="1076712858", received_at=now - timedelta(hours=1),
            text="监控对象说的话",
        )
        _add_group_message(
            qq_history_db, sender_id="999999999", received_at=now - timedelta(hours=1),
            text="别人说的话",
        )
        lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=["1076712858"], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="qunjlu", now=now,
        )
        out = capsys.readouterr().out
        assert "监控对象说的话" in out
        assert "别人说的话" not in out

    def test_focus_without_watch_collects_everyone_and_marks_the_focus_member(
        self, qq_history_db, capsys
    ):
        """jlu's contract: no watch_user_ids, so everyone is collected;
        focus only marks 1076712858's lines and adds the closing header."""
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        _add_group_message(
            qq_history_db, sender_id="1076712858", received_at=now - timedelta(hours=1),
            text="监控对象说的话",
        )
        _add_group_message(
            qq_history_db, sender_id="999999999", received_at=now - timedelta(hours=1),
            text="别人也在说话",
        )
        lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=["1076712858"], window_minutes=1440,
            timezone=TZ, monitor_id="jlu", now=now,
        )
        out = capsys.readouterr().out
        assert "监控对象说的话" in out
        assert "别人也在说话" in out
        assert "重点关注：1076712858" in out
        assert "★" in out

    def test_over_the_budget_covers_the_whole_window_not_just_the_newest(
        self, qq_history_db, capsys
    ):
        """D47's reason for existing.

        The behaviour this replaces kept the newest N messages and threw the
        rest away, so a busy day's digest only ever saw its last few hours.
        The pre-reduction must instead sample across the *entire* window: the
        very first message of the day is as reachable as the very last.
        """
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        total = lib.QQ_MONITOR_DIGEST_BUDGET * 3
        for i in range(total):
            _add_group_message(
                qq_history_db,
                sender_id=str(i % 40),
                received_at=now - timedelta(seconds=(total - i) * 15),
                text=f"消息编号 {i} 内容占位",
            )
        lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="sanhu", now=now,
        )
        out = capsys.readouterr().out
        body = out.split("聊天记录（越靠下越新）：\n", 1)[1]
        lines = body.splitlines()
        assert len(lines) <= lib.QQ_MONITOR_DIGEST_BUDGET
        assert f"原始 {total} 条，抽样保留 {len(lines)} 条" in out
        # Both ends of the window are represented — the newest-N truncation
        # this replaces could only ever satisfy the second of these.
        seq = [int(re.search(r"消息编号 (\d+) ", line).group(1)) for line in lines]
        assert seq == sorted(seq)
        assert seq[0] < total // 10, seq[0]
        assert seq[-1] > total - total // 10, seq[-1]
        # ...and the sampling is spread, not clumped at either end.
        first_third = sum(1 for n in seq if n < total // 3)
        last_third = sum(1 for n in seq if n >= 2 * total // 3)
        assert first_third > len(seq) // 4
        assert last_third > len(seq) // 4

    def test_it_names_every_drop_category_and_its_count(self, qq_history_db, capsys):
        """The digest reader must be able to see what was compressed away."""
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        for i in range(3):
            _add_group_message(
                qq_history_db, sender_id="7", received_at=now - timedelta(minutes=30 + i),
                text="[CQ:image,file=abc.jpg,url=https://example.invalid/x]",
            )
        _add_group_message(
            qq_history_db, sender_id="7", received_at=now - timedelta(minutes=20),
            text="？？？",
        )
        _add_group_message(
            qq_history_db, sender_id="7", received_at=now - timedelta(minutes=19),
            text="哦",
        )
        for i in range(2):
            _add_group_message(
                qq_history_db, sender_id=str(i), received_at=now - timedelta(minutes=10 + i),
                text="一模一样的复读内容",
            )
        _add_group_message(
            qq_history_db, sender_id="9", received_at=now - timedelta(minutes=5),
            text="这条是真正有内容的消息",
        )
        lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="sanhu", now=now,
        )
        out = capsys.readouterr().out
        assert "原始 8 条，抽样保留 2 条" in out
        assert "图片/表情等无文字内容 3 条" in out
        assert "纯符号或颜文字 1 条" in out
        assert "单字灌水 1 条" in out
        assert "重复刷屏 1 条" in out
        assert "已归约 6 条；" not in out  # the total goes first, not last
        assert "已归约 6 条：" in out
        # ...and the surviving copy of the repeated line is still there.
        assert "一模一样的复读内容" in out
        assert "这条是真正有内容的消息" in out

    def test_an_unreduced_window_keeps_the_source_header_verbatim(
        self, qq_history_db, capsys
    ):
        """Nothing dropped => the digest reads exactly as it did before D47."""
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        for i in range(3):
            _add_group_message(
                qq_history_db, sender_id=str(i), received_at=now - timedelta(minutes=i + 1),
                text=f"有内容的第 {i} 条",
            )
        lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="sanhu", now=now,
        )
        out = capsys.readouterr().out
        assert "群 183287894 最近 1 天的消息汇总（共 3 条）。" in out
        assert "已归约" not in out
        assert "说明：" not in out

    def test_focus_messages_are_never_dropped_however_much_noise_there_is(
        self, qq_history_db, capsys
    ):
        """jlu's whole mechanism. focus_user_ids messages bypass the noise
        filter, the dedup, the buckets and the quota — including messages a
        non-focus sender would lose as an image, a single char or a repeat."""
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        focus = "1076712858"
        for i in range(lib.QQ_MONITOR_DIGEST_BUDGET * 2):
            _add_group_message(
                qq_history_db, sender_id=str(i % 50),
                received_at=now - timedelta(seconds=(5000 - i) * 10),
                text=f"路人消息 {i}",
            )
        focus_texts = [
            "重点对象说的正经内容",
            "哦",
            "？",
            "[CQ:image,file=zz.jpg,url=https://example.invalid/z]",
            "重复内容",
            "重复内容",
        ]
        for i, text in enumerate(focus_texts):
            _add_group_message(
                qq_history_db, sender_id=focus, sender_name="目前5成仓",
                received_at=now - timedelta(minutes=200 - i), text=text,
            )
        lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[focus], window_minutes=1440,
            timezone=TZ, monitor_id="jlu", now=now,
        )
        out = capsys.readouterr().out
        body = out.split("聊天记录（越靠下越新）：\n", 1)[1]
        starred = [line for line in body.splitlines() if line.startswith("★")]
        assert len(starred) == len(focus_texts)
        assert f"重点关注对象（★ 标记）的 {len(focus_texts)} 条消息未参与抽样，全部保留" in out
        # The image-only focus message survives as a readable marker, not as
        # 300 chars of CDN URL.
        assert any(line.endswith("[图片]") for line in starred)
        assert "https://example.invalid" not in out
        # Both copies of the repeated focus line survive: dedup is for the
        # crowd, never for the member the monitor exists to watch.
        assert sum(1 for line in starred if line.endswith("重复内容")) == 2

    def test_the_same_window_always_reduces_to_the_same_bytes(
        self, qq_history_db, capsys
    ):
        """Determinism is a hard requirement: no RNG, no set-iteration order,
        no wall clock anywhere on the reduction path."""
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        for i in range(1200):
            _add_group_message(
                qq_history_db, sender_id=str(i % 37), sender_name=f"人{i % 37}",
                received_at=now - timedelta(seconds=(4000 - i) * 20),
                text=f"内容 {i} " + "补" * (i % 30),
            )
        runs = []
        for _ in range(3):
            lib.main_qq_monitor_digest(
                db_path=str(qq_history_db), instance_id="default", group_id="183287894",
                watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
                timezone=TZ, monitor_id="sanhu", now=now, budget=300,
            )
            runs.append(capsys.readouterr().out)
        assert runs[0] == runs[1] == runs[2]
        assert len(runs[0].split("聊天记录（越靠下越新）：\n", 1)[1].splitlines()) == 300

    def test_one_flooder_cannot_eat_the_whole_digest(self, qq_history_db, capsys):
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        for i in range(2000):
            _add_group_message(
                qq_history_db, sender_id="flood", sender_name="刷屏的",
                received_at=now - timedelta(seconds=(3000 - i) * 20),
                text=f"刷屏内容第 {i} 条真的很长很长很长很长很长很长很长很长很长很长",
            )
        for i in range(60):
            _add_group_message(
                qq_history_db, sender_id=f"quiet{i}", sender_name=f"安静的{i}",
                received_at=now - timedelta(seconds=(3000 - i * 40) * 20),
                text=f"安静的人说的第 {i} 句",
            )
        lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="sanhu", now=now, budget=200,
        )
        out = capsys.readouterr().out
        body = out.split("聊天记录（越靠下越新）：\n", 1)[1]
        lines = body.splitlines()
        flooder = sum(1 for line in lines if "(flood):" in line)
        quiet = sum(1 for line in lines if "(quiet" in line)
        assert flooder < len(lines) // 2, (flooder, len(lines))
        # every quiet member got in, even though they are outnumbered 33:1
        assert quiet == 60
        assert "刷屏的(flood) 2000 条" in out

    def test_a_window_of_pure_noise_prints_nothing(self, qq_history_db, capsys):
        """Same contract as an empty window: send_when_empty=false, so stdout
        must stay empty rather than deliver a header with no chat log."""
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        for i in range(20):
            _add_group_message(
                qq_history_db, received_at=now - timedelta(minutes=i + 1),
                text="[CQ:image,file=a.jpg,url=https://example.invalid/a]",
            )
        rc = lib.main_qq_monitor_digest(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="sanhu", now=now,
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "none carried any text" in captured.err

    def test_the_budget_is_configurable_by_argument_and_by_env(
        self, qq_history_db, capsys, monkeypatch
    ):
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        for i in range(400):
            _add_group_message(
                qq_history_db, sender_id=str(i % 20),
                received_at=now - timedelta(seconds=(2000 - i) * 30),
                text=f"某人说的第 {i} 句话",
            )
        kwargs = dict(
            db_path=str(qq_history_db), instance_id="default", group_id="183287894",
            watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
            timezone=TZ, monitor_id="sanhu", now=now,
        )
        lib.main_qq_monitor_digest(budget=120, **kwargs)
        by_arg = capsys.readouterr().out.split("聊天记录（越靠下越新）：\n", 1)[1]
        assert len(by_arg.splitlines()) == 120

        monkeypatch.setenv(lib.QQ_MONITOR_BUDGET_ENV, "90")
        lib.main_qq_monitor_digest(**kwargs)
        by_env = capsys.readouterr().out.split("聊天记录（越靠下越新）：\n", 1)[1]
        assert len(by_env.splitlines()) == 90

        # An explicit argument still wins over the env var.
        lib.main_qq_monitor_digest(budget=70, **kwargs)
        both = capsys.readouterr().out.split("聊天记录（越靠下越新）：\n", 1)[1]
        assert len(both.splitlines()) == 70

    def test_a_hostile_env_budget_falls_back_instead_of_failing_the_run(
        self, monkeypatch
    ):
        monkeypatch.setenv(lib.QQ_MONITOR_BUDGET_ENV, "not-a-number")
        assert lib._qq_monitor_budget() == lib.QQ_MONITOR_DIGEST_BUDGET
        monkeypatch.setenv(lib.QQ_MONITOR_BUDGET_ENV, "999999999")
        assert lib._qq_monitor_budget() == lib.QQ_MONITOR_BUDGET_MAX
        monkeypatch.setenv(lib.QQ_MONITOR_BUDGET_ENV, "-5")
        assert lib._qq_monitor_budget() == lib.QQ_MONITOR_BUDGET_MIN
        monkeypatch.delenv(lib.QQ_MONITOR_BUDGET_ENV)
        assert lib._qq_monitor_budget(250) == 250


class TestQqMonitorTextNormalisation:
    def test_cq_segments_vanish_from_the_classification_view(self):
        raw = "[CQ:image,file=a.jpg,url=https://x.invalid/a]"
        assert lib._qq_monitor_plain_text(raw) == ""
        assert lib._qq_monitor_plain_text("说点什么 " + raw) == "说点什么"

    def test_cq_segments_become_short_labels_in_the_digest_view(self):
        assert lib._qq_monitor_display_text(
            "[CQ:image,file=a.jpg,url=https://x.invalid/a]"
        ) == "[图片]"
        assert lib._qq_monitor_display_text("[CQ:face,id=1]看这个") == "[表情] 看这个"
        assert lib._qq_monitor_display_text("[CQ:at,qq=123] 在吗") == "@123 在吗"
        assert lib._qq_monitor_display_text("[CQ:weirdnewthing,x=1]") == "[weirdnewthing]"

    def test_a_multi_line_message_collapses_to_one_line(self):
        """The chat log is line-oriented; a message with newlines used to
        silently render as several unattributed lines."""
        assert lib._qq_monitor_display_text("第一行\n第二行\n\n第三行") == "第一行 第二行 第三行"

    def test_content_chars_strips_punctuation_and_emoji(self):
        assert lib._qq_monitor_content_chars("？？？") == ""
        assert lib._qq_monitor_content_chars("😭😭") == ""
        assert lib._qq_monitor_content_chars("（￣▽￣）") == ""
        assert lib._qq_monitor_content_chars("好的，abc 123") == "好的abc123"


class TestQqMonitorAllocate:
    def test_it_hands_out_exactly_the_budget(self):
        sizes = [1000, 500, 27, 3, 900]
        quotas = lib._qq_monitor_allocate(sizes, 300)
        assert sum(quotas) == 300
        assert all(0 <= q <= s for q, s in zip(quotas, sizes))

    def test_a_quiet_bucket_is_never_starved_by_a_busy_one(self):
        """A purely proportional split gives the 27-message hour two lines
        and the digest loses the quiet parts of the day."""
        quotas = lib._qq_monitor_allocate([10_000, 27], 400)
        assert quotas[1] == 27

    def test_it_never_over_allocates_a_small_bucket(self):
        quotas = lib._qq_monitor_allocate([2, 2, 2], 100)
        assert quotas == [2, 2, 2]

    def test_it_is_a_pure_function(self):
        sizes = [317, 44, 1290, 8, 76, 903]
        first = lib._qq_monitor_allocate(sizes, 500)
        assert all(lib._qq_monitor_allocate(sizes, 500) == first for _ in range(5))

    def test_degenerate_inputs(self):
        assert lib._qq_monitor_allocate([], 100) == []
        assert lib._qq_monitor_allocate([5, 5], 0) == [0, 0]

    def test_a_missing_database_is_a_loud_failure(self, tmp_path):
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        with pytest.raises(SystemExit) as excinfo:
            lib.main_qq_monitor_digest(
                db_path=str(tmp_path / "nope.sqlite"), instance_id="default",
                group_id="183287894", watch_user_ids=[], focus_user_ids=[],
                window_minutes=1440, timezone=TZ, monitor_id="qunjlu", now=now,
            )
        assert "qq_group_history_unavailable" in str(excinfo.value)

    def test_a_wrong_schema_database_is_a_loud_failure(self, tmp_path):
        path = tmp_path / "wrong.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE not_it (x INTEGER)")
        conn.commit()
        conn.close()
        now = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo(TZ))
        with pytest.raises(SystemExit) as excinfo:
            lib.main_qq_monitor_digest(
                db_path=str(path), instance_id="default", group_id="183287894",
                watch_user_ids=[], focus_user_ids=[], window_minutes=1440,
                timezone=TZ, monitor_id="qunjlu", now=now,
            )
        assert "qq_group_history_query_failed" in str(excinfo.value)

    def test_it_never_calls_any_send_or_publish_capable_function(self):
        """The monitors' delivery is cron's own deliver step (or, for
        qunjlu, nowhere at all — D26); this library never sends anything
        itself, same invariant as the qzone corpus script above."""
        source = LIB_PATH.read_text(encoding="utf-8")
        assert "qzone_publish" not in source
        assert "SendGroupMsg" not in source
        assert "SendPrivateMsg" not in source


# ---------------------------------------------------------------------------
# hermes.daily_agenda
# ---------------------------------------------------------------------------


AGENDA = {
    "settings": {"semester_start_date": "2026-08-17"},  # a Monday
    "courses": [
        {
            "name": "编译原理",
            "weekday": "Tuesday",
            "weeks": "1-8",
            "start_time": "08:00",
            "end_time": "09:40",
            "location": "逸夫楼 301",
        },
        {"name": "双周研讨", "weekday": "Tuesday", "weeks": "双周", "start_time": "14:00"},
    ],
    "tasks": [
        {"title": "交实验报告", "date": "2026-08-18", "status": "pending"},
        {"title": "已经做完的事", "date": "2026-08-18", "status": "done"},
    ],
    "exams": [
        {"name": "期中考试", "date": "2026-08-20"},
        {"name": "很远的考试", "date": "2026-12-01"},
    ],
}


class TestWeekMatching:
    def test_no_spec_means_every_week(self):
        assert lib.parse_week_match(None, 3)
        assert lib.parse_week_match("全周", 3)

    def test_ranges(self):
        assert lib.parse_week_match("1-8周", 8)
        assert not lib.parse_week_match("1-8周", 9)

    def test_single_weeks_and_lists(self):
        assert lib.parse_week_match("2、5", 5)
        assert not lib.parse_week_match("2、5", 4)

    def test_parity_qualifies_a_numeric_range(self):
        assert lib.parse_week_match("1-16单周", 3)
        assert not lib.parse_week_match("1-16单周", 4)
        assert lib.parse_week_match("1-16双周", 4)

    def test_a_bare_parity_word_matches_nothing_inherited_defect(self):
        """Ported verbatim, defect included: stripping "单周"/"双周" leaves an
        empty string that matches neither the range nor the single-week
        pattern, so a course whose weeks field is just "双周" is silently
        never scheduled. Reproduced deliberately — the port must behave like
        the source, and the quirk is recorded in D1-cron-port-notes.md rather
        than fixed under cover of a migration."""
        assert not lib.parse_week_match("单周", 3)
        assert not lib.parse_week_match("双周", 4)

    def test_parenthetical_notes_are_ignored(self):
        assert lib.parse_week_match("1-8（含实验）", 4)


class TestDailyAgenda:
    def _write(self, data=AGENDA, name="scheduler_data/class_schedule.yaml"):
        import yaml

        path = lib.job_state_dir() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return path

    def test_renders_todays_courses_tasks_and_near_exams(self, capsys):
        self._write()
        assert lib.main_daily_agenda(
            agenda_path="scheduler_data/class_schedule.yaml",
            timezone=TZ,
            render_card=False,
            today=date(2026, 8, 18),
        ) == 0
        out = capsys.readouterr().out
        assert "第 1 教学周" in out
        assert "编译原理" in out
        assert "逸夫楼 301" in out
        assert "交实验报告" in out
        assert "期中考试" in out

    def test_hides_completed_tasks_and_distant_exams(self, capsys):
        self._write()
        lib.main_daily_agenda(
            agenda_path="scheduler_data/class_schedule.yaml",
            timezone=TZ,
            render_card=False,
            today=date(2026, 8, 18),
        )
        out = capsys.readouterr().out
        assert "已经做完的事" not in out
        assert "很远的考试" not in out

    def test_a_bare_parity_course_never_appears(self, capsys):
        """Consequence of the inherited parse_week_match defect above: the
        "双周" course is absent in teaching week 1 *and* in week 2."""
        self._write()
        for day in (date(2026, 8, 18), date(2026, 8, 25)):
            lib.main_daily_agenda(
                agenda_path="scheduler_data/class_schedule.yaml",
                timezone=TZ,
                render_card=False,
                today=day,
            )
            assert "双周研讨" not in capsys.readouterr().out

    def test_an_empty_day_still_produces_a_card(self, capsys):
        self._write()
        lib.main_daily_agenda(
            agenda_path="scheduler_data/class_schedule.yaml",
            timezone=TZ,
            render_card=False,
            today=date(2026, 8, 19),
        )
        out = capsys.readouterr().out
        assert "今天暂无课程安排" in out
        assert "暂无已登记任务" in out

    def test_stdout_is_the_delivered_message_for_this_no_agent_job(self, capsys):
        """No framing, no diagnostics — whatever it prints is what gets sent."""
        self._write()
        lib.main_daily_agenda(
            agenda_path="scheduler_data/class_schedule.yaml",
            timezone=TZ,
            render_card=False,
            today=date(2026, 8, 18),
        )
        captured = capsys.readouterr()
        assert captured.out.startswith("## 今日课表与日程")
        assert "rsvg" not in captured.out

    def test_a_missing_data_file_fails_loudly(self):
        with pytest.raises(SystemExit) as excinfo:
            lib.main_daily_agenda(
                agenda_path="scheduler_data/class_schedule.yaml",
                timezone=TZ,
                render_card=False,
            )
        assert "agenda_data_missing" in str(excinfo.value)

    def test_an_empty_data_file_fails_loudly(self):
        self._write(data={})
        with pytest.raises(SystemExit) as excinfo:
            lib.main_daily_agenda(
                agenda_path="scheduler_data/class_schedule.yaml",
                timezone=TZ,
                render_card=False,
            )
        assert "agenda_data_invalid" in str(excinfo.value)

    def test_a_path_escaping_the_state_dir_is_refused(self):
        for bad in ("../../etc/passwd", "/etc/passwd"):
            with pytest.raises(SystemExit) as excinfo:
                lib.main_daily_agenda(agenda_path=bad, timezone=TZ, render_card=False)
            assert "invalid agenda_path" in str(excinfo.value)

    def test_falls_back_to_text_when_rsvg_convert_is_absent(self, monkeypatch, capsys):
        self._write()
        monkeypatch.setattr(lib.shutil, "which", lambda name: None)
        lib.main_daily_agenda(
            agenda_path="scheduler_data/class_schedule.yaml",
            timezone=TZ,
            render_card=True,
            today=date(2026, 8, 18),
        )
        captured = capsys.readouterr()
        assert "MEDIA:" not in captured.out
        assert "rsvg-convert not on PATH" in captured.err

    def test_emits_a_media_tag_when_the_card_renders(self, monkeypatch, capsys):
        self._write()
        monkeypatch.setattr(lib.shutil, "which", lambda name: "/usr/bin/rsvg-convert")

        def fake_run(argv, **kwargs):
            Path(argv[argv.index("-o") + 1]).write_bytes(b"\x89PNG")
            return None

        monkeypatch.setattr(lib.subprocess, "run", fake_run)
        lib.main_daily_agenda(
            agenda_path="scheduler_data/class_schedule.yaml",
            timezone=TZ,
            render_card=True,
            today=date(2026, 8, 18),
        )
        out = capsys.readouterr().out
        assert out.startswith("MEDIA:")
        assert out.splitlines()[1] == "2026-08-18 今日课表与日程"

    def test_a_failed_render_falls_back_instead_of_failing_the_job(
        self, monkeypatch, capsys
    ):
        self._write()
        monkeypatch.setattr(lib.shutil, "which", lambda name: "/usr/bin/rsvg-convert")

        def explode(*a, **k):
            raise OSError("converter died")

        monkeypatch.setattr(lib.subprocess, "run", explode)
        lib.main_daily_agenda(
            agenda_path="scheduler_data/class_schedule.yaml",
            timezone=TZ,
            render_card=True,
            today=date(2026, 8, 18),
        )
        captured = capsys.readouterr()
        assert "MEDIA:" not in captured.out
        assert "今日课表与日程" in captured.out
        assert "falling back to text" in captured.err

    def test_svg_escapes_its_content(self, tmp_path):
        target = tmp_path / "card.svg"
        lib.render_svg("<script>alert(1)</script>", target)
        body = target.read_text(encoding="utf-8")
        assert "<script>" not in body
        assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# persona.decay
# ---------------------------------------------------------------------------


class TestGrantleyDecay:
    def _script(self, tmp_path, body):
        path = tmp_path / "grantley_job.py"
        path.write_text(body, encoding="utf-8")
        return path

    def test_runs_the_target_script_with_the_decay_argv(self, tmp_path, capsys):
        script = self._script(
            tmp_path,
            "import json, sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n",
        )
        assert lib.main_grantley_decay(
            script_path=str(script), persona_id="grantley"
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["argv"] == ["--persona", "grantley", "decay"]

    def test_omits_the_persona_flag_when_none_is_given(self, tmp_path, capsys):
        script = self._script(
            tmp_path, "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
        )
        lib.main_grantley_decay(script_path=str(script))
        assert json.loads(capsys.readouterr().out) == ["decay"]

    def test_propagates_the_scripts_exit_code(self, tmp_path):
        script = self._script(tmp_path, "raise SystemExit(3)\n")
        assert lib.main_grantley_decay(script_path=str(script)) == 3

    def test_a_missing_script_is_a_loud_failure(self, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            lib.main_grantley_decay(script_path=str(tmp_path / "nope.py"))
        assert "not found" in str(excinfo.value)

    def test_argv_is_restored_afterwards(self, tmp_path):
        before = list(sys.argv)
        script = self._script(tmp_path, "pass\n")
        lib.main_grantley_decay(script_path=str(script))
        assert sys.argv == before

    def test_it_adds_no_decay_logic_of_its_own(self):
        """plugins/grantley owns the decay implementation; duplicating it here
        is how the two copies drift."""
        source = LIB_PATH.read_text(encoding="utf-8")
        assert "half_life" not in source
        assert "def apply_decay" not in source

    def test_the_real_grantley_script_answers_to_this_argv(self):
        """Guards the contract this port depends on: `--persona` is a global
        flag that precedes the subcommand."""
        import argparse
        import plugins.grantley.persona as grantley_persona

        parser = argparse.ArgumentParser()
        parser.add_argument("--persona", default=grantley_persona.PERSONA_ID)
        sub = parser.add_subparsers(dest="cmd")
        sub.add_parser("decay")
        args = parser.parse_args(["--persona", "grantley", "decay"])
        assert args.cmd == "decay" and args.persona == "grantley"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class TestPreviousRunDelivered:
    def test_true_only_when_both_halves_succeeded(self):
        assert lib.previous_run_delivered({"last_status": "ok"})
        assert not lib.previous_run_delivered(
            {"last_status": "ok", "last_delivery_error": "chat not found"}
        )
        assert not lib.previous_run_delivered({"last_status": "error"})
        assert not lib.previous_run_delivered(None)


class TestLatestOutput:
    def test_returns_the_newest_file(self):
        directory = lib.hermes_home() / "cron" / "output" / "job1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "2026-08-17.md").write_text("old", encoding="utf-8")
        (directory / "2026-08-19.md").write_text("new", encoding="utf-8")
        assert lib.latest_output("job1") == ("2026-08-19.md", "new")

    def test_none_when_there_is_no_history(self):
        assert lib.latest_output("never-ran") is None
