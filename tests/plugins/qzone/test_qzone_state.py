"""Tests for the three on-disk sidecars.

The formats here are not this port's inventions — they are the files
production is writing right now, so the fixtures below are the *real*
shapes captured in ``docs/migration-corlinman/A2-grantley-system-inventory.md``
(a ``version: 1`` post log with 19 entries, a ``version: 2`` seen-comments
map, a ``version: 1`` friend-comments list). If these tests fail, a cutover
loses dedup state and duplicates posts on a live feed.
"""

from __future__ import annotations

import json
import time

import pytest

from plugins.qzone import state


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point the sidecars at a scratch directory and fix the persona."""
    monkeypatch.setenv("QZONE_STATE_DIR", str(tmp_path / "execution-state"))
    monkeypatch.setenv("QZONE_PERSONA_ID", "grantley")
    monkeypatch.delenv("QZONE_QQ_INSTANCE_ID", raising=False)
    return tmp_path / "execution-state"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class TestPaths:
    def test_default_instance_is_unqualified(self, _isolated_state):
        paths = state.store_paths()
        assert paths["post_log"] == _isolated_state / "qzone_post_log" / "grantley.json"
        assert paths["seen_comments"] == (
            _isolated_state / "qzone_seen_comments" / "grantley.json"
        )
        assert paths["friend_comments"] == (
            _isolated_state / "qzone_friend_comments" / "grantley.json"
        )

    def test_named_instance_adds_a_segment(self, _isolated_state, monkeypatch):
        monkeypatch.setenv("QZONE_QQ_INSTANCE_ID", "bot-a")
        assert state.store_paths()["post_log"] == (
            _isolated_state / "qzone_post_log" / "bot-a" / "grantley.json"
        )

    def test_persona_argument_beats_the_environment(self):
        assert state.resolve_persona_id("other") == "other"
        assert state.resolve_persona_id(None) == "grantley"

    @pytest.mark.parametrize("bad", ["..", "a/b", "a\\b", "", ".", "a b", "персона"])
    def test_traversal_slugs_are_refused(self, bad):
        assert state.valid_slug(bad) is False

    @pytest.mark.parametrize("good", ["grantley", "bot-a", "bot_a", "a1"])
    def test_ordinary_slugs_are_accepted(self, good):
        assert state.valid_slug(good) is True

    def test_unsafe_persona_yields_no_path_and_writes_nothing(self, _isolated_state):
        state.record_publish(
            persona_id="../escape",
            text="nope",
            tid="t",
            qzone_url="u",
            outcome=state.OUTCOME_SENT,
        )
        assert not (_isolated_state / "qzone_post_log").exists()


# ---------------------------------------------------------------------------
# Post log
# ---------------------------------------------------------------------------


class TestPostLog:
    def test_written_shape_matches_production(self, _isolated_state):
        state.record_publish(
            persona_id=None,
            text="本来只想去图书馆吹会儿凉风",
            tid="1cbe3d3c72aa6c6a01750700",
            qzone_url="https://user.qzone.qq.com/1010679324/mood/1cbe3d3c72aa6c6a01750700",
            outcome=state.OUTCOME_SENT,
            job="hermes.qzone_daily",
        )
        payload = _read(_isolated_state / "qzone_post_log" / "grantley.json")
        assert payload["version"] == 1
        entry = payload["posts"][0]
        assert set(entry) == {"ts", "job", "tid", "qzone_url", "text", "outcome"}
        assert entry["job"] == "hermes.qzone_daily"
        assert entry["tid"] == "1cbe3d3c72aa6c6a01750700"

    def test_reads_a_real_production_file_without_outcome(self, _isolated_state):
        """The 19 live entries predate ``outcome``; they must still load."""
        path = _isolated_state / "qzone_post_log" / "grantley.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "posts": [
                        {
                            "ts": "2026-07-28T23:00:04+09:00",
                            "job": "hermes.qzone_daily",
                            "tid": "1cbe3d3c6ec2816a29a50a00",
                            "qzone_url": "https://user.qzone.qq.com/1010679324/mood/x",
                            "text": "本来只想去图书馆吹会儿凉风",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        entries = state.post_log_entries()
        assert len(entries) == 1
        assert entries[0]["tid"] == "1cbe3d3c6ec2816a29a50a00"
        assert "outcome" not in entries[0]

    def test_appending_preserves_existing_entries(self, _isolated_state):
        for i in range(3):
            state.record_publish(
                persona_id=None, text=f"post {i}", tid=str(i),
                qzone_url=None, outcome=state.OUTCOME_SENT,
            )
        assert [e["text"] for e in state.post_log_entries()] == [
            "post 0", "post 1", "post 2"
        ]

    def test_keeps_only_the_last_thirty(self, _isolated_state):
        for i in range(35):
            state.record_publish(
                persona_id=None, text=f"post {i}", tid=str(i),
                qzone_url=None, outcome=state.OUTCOME_SENT,
            )
        entries = state.post_log_entries()
        assert len(entries) == 30
        assert entries[0]["text"] == "post 5"
        assert entries[-1]["text"] == "post 34"

    def test_body_is_capped_at_five_hundred_chars(self, _isolated_state):
        state.record_publish(
            persona_id=None, text="x" * 900, tid="t",
            qzone_url=None, outcome=state.OUTCOME_SENT,
        )
        assert len(state.post_log_entries()[0]["text"]) == 500

    def test_corrupt_file_reads_as_empty(self, _isolated_state):
        path = _isolated_state / "qzone_post_log" / "grantley.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert state.post_log_entries() == []

    def test_limit_returns_the_newest(self, _isolated_state):
        for i in range(5):
            state.record_publish(
                persona_id=None, text=f"p{i}", tid=str(i),
                qzone_url=None, outcome=state.OUTCOME_SENT,
            )
        assert [e["text"] for e in state.post_log_entries(limit=2)] == ["p3", "p4"]


# ---------------------------------------------------------------------------
# S17 — the unknown-outcome guard
# ---------------------------------------------------------------------------


class TestUnknownPublishGuard:
    def test_no_guard_when_nothing_is_logged(self):
        assert state.unknown_publish_guard("anything") is None

    def test_sent_outcome_does_not_block_a_repeat(self):
        state.record_publish(
            persona_id=None, text="hello", tid="t",
            qzone_url=None, outcome=state.OUTCOME_SENT,
        )
        assert state.unknown_publish_guard("hello") is None

    def test_unknown_outcome_blocks_the_same_text(self):
        state.record_publish(
            persona_id=None, text="hello", tid=None,
            qzone_url=None, outcome=state.OUTCOME_UNKNOWN,
        )
        blocked = state.unknown_publish_guard("hello")
        assert blocked is not None
        assert blocked["outcome"] == "unknown"

    def test_unknown_outcome_does_not_block_different_text(self):
        state.record_publish(
            persona_id=None, text="hello", tid=None,
            qzone_url=None, outcome=state.OUTCOME_UNKNOWN,
        )
        assert state.unknown_publish_guard("something else") is None

    def test_guard_expires(self):
        state.record_publish(
            persona_id=None, text="hello", tid=None,
            qzone_url=None, outcome=state.OUTCOME_UNKNOWN,
        )
        future = time.time() + state.UNKNOWN_PUBLISH_GUARD_SECS + 60
        assert state.unknown_publish_guard("hello", now=future) is None

    def test_guard_compares_against_the_stored_capped_text(self):
        state.record_publish(
            persona_id=None, text="y" * 900, tid=None,
            qzone_url=None, outcome=state.OUTCOME_UNKNOWN,
        )
        assert state.unknown_publish_guard("y" * 900) is not None

    def test_unparseable_timestamp_keeps_the_guard_engaged(self, _isolated_state):
        """A broken clock must fail safe, not silently unblock a re-post."""
        path = _isolated_state / "qzone_post_log" / "grantley.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {"version": 1, "posts": [
                    {"ts": "not-a-date", "job": "", "tid": None,
                     "qzone_url": None, "text": "hello", "outcome": "unknown"}
                ]}
            ),
            encoding="utf-8",
        )
        assert state.unknown_publish_guard("hello") is not None

    def test_empty_text_is_never_guarded(self):
        state.record_publish(
            persona_id=None, text="", tid=None,
            qzone_url=None, outcome=state.OUTCOME_UNKNOWN,
        )
        assert state.unknown_publish_guard("") is None


# ---------------------------------------------------------------------------
# Comment identity
# ---------------------------------------------------------------------------


class TestCommentIdentity:
    def test_stable_id_wins(self):
        assert state.comment_identity(
            reply_to_comment_id="7", reply_to_comment_content="hi"
        ) == "id:7"

    def test_content_falls_back_to_a_digest(self):
        identity = state.comment_identity(reply_to_comment_content="hi")
        assert identity.startswith("sha256:")
        assert len(identity) == len("sha256:") + 64

    def test_nothing_yields_empty(self):
        assert state.comment_identity() == ""


# ---------------------------------------------------------------------------
# Reply ledger (own posts) — qzone_seen_comments
# ---------------------------------------------------------------------------


class TestSeenComments:
    def test_written_shape_matches_production(self, _isolated_state):
        state.mark_comment(
            owner_uin="10001", tid="1cbe3d3c72aa6c6a01750700",
            identity="id:1", actor_uin="10001",
        )
        payload = _read(_isolated_state / "qzone_seen_comments" / "grantley.json")
        assert payload["version"] == 2
        entries = payload["seen"]["1cbe3d3c72aa6c6a01750700"]
        assert len(entries) == 1
        assert entries[0].startswith("id:1:")

    def test_reads_the_real_production_file(self, _isolated_state):
        path = _isolated_state / "qzone_seen_comments" / "grantley.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "seen": {
                        "1cbe3d3c72aa6c6a01750700": ["id:1:1785546023"],
                        "1cbe3d3c6ec2816a29a50a00": [
                            "id:2:1786928431", "id:3:1786928431"
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        assert state.is_recorded_comment(
            owner_uin="10001", tid="1cbe3d3c72aa6c6a01750700",
            identity="id:1", actor_uin="10001",
        )
        assert state.is_recorded_comment(
            owner_uin="10001", tid="1cbe3d3c6ec2816a29a50a00",
            identity="id:3", actor_uin="10001",
        )
        assert not state.is_recorded_comment(
            owner_uin="10001", tid="1cbe3d3c6ec2816a29a50a00",
            identity="id:9", actor_uin="10001",
        )

    def test_bare_uin_records_are_understood(self, _isolated_state):
        """The oldest records store a bare uin, not an ``id:``/``sha256:``."""
        path = _isolated_state / "qzone_seen_comments" / "grantley.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": 2, "seen": {"t1": ["20002:1785546023"]}}),
            encoding="utf-8",
        )
        assert state.is_recorded_comment(
            owner_uin="10001", tid="t1", identity="uin:20002", actor_uin="10001",
        )

    def test_marking_twice_does_not_duplicate(self, _isolated_state):
        for _ in range(3):
            state.mark_comment(
                owner_uin="10001", tid="t1", identity="id:1", actor_uin="10001"
            )
        payload = _read(_isolated_state / "qzone_seen_comments" / "grantley.json")
        assert len(payload["seen"]["t1"]) == 1

    def test_tid_cap_rolls_off_least_recently_updated(self, _isolated_state):
        for i in range(105):
            state.mark_comment(
                owner_uin="10001", tid=f"t{i}", identity="id:1", actor_uin="10001"
            )
        payload = _read(_isolated_state / "qzone_seen_comments" / "grantley.json")
        assert len(payload["seen"]) == 100
        assert "t0" not in payload["seen"]
        assert "t104" in payload["seen"]

    def test_touching_a_tid_refreshes_its_recency(self, _isolated_state):
        state.mark_comment(owner_uin="10001", tid="old", identity="id:1",
                           actor_uin="10001")
        for i in range(100):
            state.mark_comment(owner_uin="10001", tid=f"t{i}", identity="id:1",
                               actor_uin="10001")
            if i == 40:
                # Re-touch the oldest tid so it survives the cap.
                state.mark_comment(owner_uin="10001", tid="old", identity="id:2",
                                   actor_uin="10001")
        payload = _read(_isolated_state / "qzone_seen_comments" / "grantley.json")
        assert "old" in payload["seen"]

    def test_identity_cap_per_tid(self, _isolated_state):
        for i in range(205):
            state.mark_comment(
                owner_uin="10001", tid="t1", identity=f"id:{i}", actor_uin="10001"
            )
        payload = _read(_isolated_state / "qzone_seen_comments" / "grantley.json")
        assert len(payload["seen"]["t1"]) == 200

    def test_empty_identity_records_nothing(self, _isolated_state):
        state.mark_comment(owner_uin="10001", tid="t1", identity="",
                           actor_uin="10001")
        assert not (_isolated_state / "qzone_seen_comments").exists()


# ---------------------------------------------------------------------------
# Friend ledger — qzone_friend_comments
# ---------------------------------------------------------------------------


class TestFriendComments:
    def test_written_shape_matches_production(self, _isolated_state):
        state.mark_comment(
            owner_uin="2104743984", tid="deadbeef", identity="", actor_uin="1010679324"
        )
        payload = _read(_isolated_state / "qzone_friend_comments" / "grantley.json")
        assert payload == {"version": 1, "seen": ["2104743984:deadbeef"]}

    def test_reads_the_real_production_shape(self, _isolated_state):
        path = _isolated_state / "qzone_friend_comments" / "grantley.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": 1, "seen": ["2104743984:abc", "1617513419:def"]}),
            encoding="utf-8",
        )
        assert state.is_recorded_comment(
            owner_uin="2104743984", tid="abc", identity="", actor_uin="1010679324"
        )
        assert not state.is_recorded_comment(
            owner_uin="2104743984", tid="zzz", identity="", actor_uin="1010679324"
        )

    def test_friend_comments_dedupe_by_post_not_by_comment(self, _isolated_state):
        """One comment per friend post, regardless of identity."""
        state.mark_comment(
            owner_uin="20002", tid="t1", identity="id:1", actor_uin="10001"
        )
        assert state.is_recorded_comment(
            owner_uin="20002", tid="t1", identity="id:99", actor_uin="10001"
        )

    def test_repeat_marks_do_not_duplicate(self, _isolated_state):
        for _ in range(4):
            state.mark_comment(
                owner_uin="20002", tid="t1", identity="", actor_uin="10001"
            )
        assert state.friend_comment_seen() == ["20002:t1"]

    def test_own_and_friend_ledgers_are_separate_files(self, _isolated_state):
        state.mark_comment(owner_uin="10001", tid="t1", identity="id:1",
                           actor_uin="10001")
        state.mark_comment(owner_uin="20002", tid="t2", identity="", actor_uin="10001")
        assert (_isolated_state / "qzone_seen_comments" / "grantley.json").is_file()
        assert (_isolated_state / "qzone_friend_comments" / "grantley.json").is_file()

    def test_cap_rolls_off_oldest(self, _isolated_state):
        for i in range(state._FRIEND_MAX + 10):
            state.mark_comment(
                owner_uin="20002", tid=f"t{i}", identity="", actor_uin="10001"
            )
        seen = state.friend_comment_seen()
        assert len(seen) == state._FRIEND_MAX
        assert seen[-1] == f"20002:t{state._FRIEND_MAX + 9}"


# ---------------------------------------------------------------------------
# Default state root
# ---------------------------------------------------------------------------


def test_state_root_defaults_to_plugin_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("QZONE_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert state.state_root() == tmp_path / "home" / "plugin-data" / "qzone"
