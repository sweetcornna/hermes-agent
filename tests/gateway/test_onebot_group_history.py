"""Persistent QQ group-message archive for the OneBot adapter (D3).

This is the writer D2 found missing: three migrated cron monitors read a
``group_messages`` table that nothing in this port ever wrote (00-PLAN.md §19
/ §21).  What is tested here is therefore *not* "does it insert rows" — it is
the five properties that are silent in production when they break:

* **schema fidelity.**  The reader (``corlinman_jobs_lib._qq_monitor_query``)
  is positional and was written against corlinman's DDL.  So the DDL is pinned
  against a copy of the source text, against the real exported production
  snapshot when one is present, and — the test that actually matters — by
  running the unmodified reader against a file this module wrote.
* **the event loop is never blocked.**  ``sqlite3`` is blocking and the target
  journal mode is DELETE, where a commit is an fsync.  The connection must be
  created on the writer thread and ``record()`` must stay a queue push.
* **the queue is bounded.**  A stalled disk must cost archived rows, not the
  gateway's 512 MB memory cap.
* **fail-open.**  Every failure shape — unopenable file, raising commit,
  hostile input — must leave the adapter routing messages normally.
* **backfill idempotency.**  Cutover runs the import more than once; a second
  run must produce zero new rows, including for messages our own live writer
  already captured under a different ``received_at_ms``.

Everything is offline: temporary directories, real SQLite files, a fake OneBot
client.  No socket, no NapCat, no QQ session.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import group_history as GH
from plugins.platforms.onebot import group_history_backfill as BF
from plugins.platforms.onebot import protocol as P


REPO_ROOT = Path(__file__).resolve().parents[2]

#: The exported production snapshot.  Gitignored, so every test that uses it
#: skips cleanly on a fresh checkout — but when it *is* present the schema
#: assertions run against the real thing rather than against a transcription.
SNAPSHOT = REPO_ROOT / ".migration-export" / "sqlite" / "qq_group_history.sqlite"

#: ``corlinman_server/qq_group_history.py::_SCHEMA``, transcribed here so this
#: test fails if :data:`GH.SCHEMA_SQL` is ever "improved".  Kept as a separate
#: literal on purpose: importing the constant under test to compare against
#: itself would assert nothing.
CORLINMAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    sender_user_id  TEXT NOT NULL,
    sender_name     TEXT NOT NULL DEFAULT '',
    message_id      TEXT,
    event_time_ms   INTEGER NOT NULL,
    received_at_ms  INTEGER NOT NULL,
    text            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_group_messages_window
    ON group_messages(instance_id, group_id, received_at_ms);

CREATE TABLE IF NOT EXISTS monitor_state (
    key           TEXT PRIMARY KEY,
    last_fire_ms  INTEGER NOT NULL
);
"""

GROUP = "183287894"
OTHER_GROUP = "980927602"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_reader():
    """D2's job-side library, loaded by path (it is a deployed script, not a
    package module).  Used to prove the read path needs no change."""
    path = REPO_ROOT / "plugins" / "corlinman_jobs" / "scripts" / "corlinman_jobs_lib.py"
    spec = importlib.util.spec_from_file_location("_cjl_for_d3_tests", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def schema_rows(path: Path) -> Dict[str, str]:
    """``{object name: normalised DDL}`` for one SQLite file."""
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()
    return {name: " ".join((sql or "").split()) for name, sql in rows}


def make_config(tmp_path: Path, **overrides: Any) -> GH.GroupHistoryConfig:
    base: Dict[str, Any] = {
        "db_path": tmp_path / "qq_group_history.sqlite",
        "groups": frozenset({GROUP}),
        "batch_rows": 3,
        "flush_secs": 0.2,
        "queue_max": 50,
        "retention_days": GH.MIN_RETENTION_DAYS,
    }
    base.update(overrides)
    return GH.GroupHistoryConfig(**base)


def write_rows(writer: GH.GroupHistoryWriter, n: int, *, group: str = GROUP, **kw: Any) -> None:
    for i in range(n):
        writer.record(
            instance_id=kw.get("instance_id", "default"),
            group_id=group,
            sender_user_id=kw.get("sender_user_id", "1076712858"),
            sender_name=kw.get("sender_name", "某人"),
            message_id=kw.get("message_id_base", 1000) + i,
            event_time_ms=kw.get("event_time_ms"),
            text=f"{kw.get('text', 'msg')} {i}",
        )


def count_rows(path: Path) -> int:
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM group_messages").fetchone()[0])
    finally:
        conn.close()


def seed_store(path: Path, rows: List[tuple]) -> None:
    """Insert raw rows into a store, creating it with the shared schema."""
    conn = GH.connect_store(path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO group_messages (instance_id, group_id, sender_user_id, "
                "sender_name, message_id, event_time_ms, received_at_ms, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    finally:
        conn.close()


def row(
    *,
    instance_id: str = "default",
    group_id: str = GROUP,
    sender: str = "1076712858",
    name: str = "某人",
    message_id: Optional[str] = "1",
    event_ms: int = 1_700_000_000_000,
    received_ms: int = 1_700_000_000_000,
    text: str = "hello",
) -> tuple:
    return (instance_id, group_id, sender, name, message_id, event_ms, received_ms, text)


class _BrokenConnection:
    """A connection whose every statement raises, for the fail-open tests.

    ``sqlite3.Connection`` is an immutable C type, so the write failure has to
    be injected at the factory rather than by patching a method on it.
    """

    def __init__(self, message: str) -> None:
        self._exc = sqlite3.OperationalError(message)

    def execute(self, *a: Any, **k: Any):
        raise self._exc

    def executemany(self, *a: Any, **k: Any):
        raise self._exc

    def executescript(self, *a: Any, **k: Any):
        return None

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def broken_store(message: str):
    """Replacement for ``GH.connect_store`` that yields a doomed connection."""

    def _factory(path: Path):
        return _BrokenConnection(message)

    return _factory


class FakeClient:
    """Stand-in for ``OneBotClient``; records actions, touches no socket."""

    def __init__(self) -> None:
        self.actions: List[Any] = []
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0

    async def call_action(self, action, *, timeout=None):
        self.actions.append(action)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

    async def send_action(self, action):
        self.actions.append(action)

    async def close(self):
        self.connected = False


class FakeHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event):
        self.calls += 1
        return "ok"


def make_adapter(tmp_path: Path, extra: Optional[Dict[str, Any]] = None) -> A.OneBotAdapter:
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
        "group_whitelist": [GROUP],
        "group_history_enabled": True,
        "group_history_db": str(tmp_path / "qq_group_history.sqlite"),
        "group_history_batch_rows": 1,
        "group_history_flush_secs": 0.05,
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    # Replace the gateway hand-off itself (as tests/gateway/test_onebot_plugin.py
    # does): the real ``handle_message`` would reach for an agent runtime.
    handler = FakeHandler()
    ad.handle_message = handler  # type: ignore[assignment]
    ad.test_handler = handler  # type: ignore[attr-defined]
    return ad


def group_event(
    text: str = "hi",
    *,
    gid: int = int(GROUP),
    uid: int = 1076712858,
    message_id: int = 1,
    nickname: Optional[str] = "某人",
    card: Optional[str] = None,
    segments=None,
    event_time: int = 1_700_000_000,
) -> P.MessageEvent:
    return P.MessageEvent(
        self_id=100,
        message_type=P.MessageType.GROUP,
        sub_type="normal",
        group_id=gid,
        user_id=uid,
        message_id=message_id,
        message=segments if segments is not None else [P.TextSegment(text=text)],
        raw_message=text,
        time=event_time,
        sender=P.Sender(user_id=uid, nickname=nickname, card=card),
    )


def private_event(text: str = "hi", *, uid: int = 2104743984, message_id: int = 1):
    return P.MessageEvent(
        self_id=100,
        message_type=P.MessageType.PRIVATE,
        sub_type="friend",
        group_id=None,
        user_id=uid,
        message_id=message_id,
        message=[P.TextSegment(text=text)],
        raw_message=text,
        time=1_700_000_000,
        sender=P.Sender(user_id=uid, nickname="bob"),
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    A._reset_module_state()
    for key in list(os.environ):
        if key.startswith("ONEBOT_") or key == "QQ_GROUP_HISTORY_DB":
            monkeypatch.delenv(key, raising=False)
    yield
    A._reset_module_state()


@pytest.fixture()
def writer(tmp_path):
    """A started writer, always stopped afterwards (it owns a thread)."""
    made: List[GH.GroupHistoryWriter] = []

    def _make(**overrides: Any) -> GH.GroupHistoryWriter:
        w = GH.GroupHistoryWriter(make_config(tmp_path, **overrides))
        made.append(w)
        w.start()
        return w

    yield _make
    for w in made:
        w.close(timeout=5.0)


# ---------------------------------------------------------------------------
# 1. Schema fidelity — the whole point of reusing corlinman's table
# ---------------------------------------------------------------------------


class TestSchemaCompatibility:
    def test_schema_sql_is_corlinmans_verbatim(self):
        """A "tidy-up" of the DDL is a silent break of the D2 reader."""
        assert " ".join(GH.SCHEMA_SQL.split()) == " ".join(CORLINMAN_SCHEMA.split())

    def test_created_file_has_corlinmans_objects(self, tmp_path):
        path = tmp_path / "h.sqlite"
        GH.connect_store(path).close()
        objects = schema_rows(path)
        assert "group_messages" in objects
        assert "idx_group_messages_window" in objects
        assert "monitor_state" in objects
        # Column order is load-bearing: the reader SELECTs by name but the
        # digest formatter consumes the result tuple positionally.
        conn = sqlite3.connect(str(path))
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(group_messages)")]
        finally:
            conn.close()
        assert cols == [
            "id",
            "instance_id",
            "group_id",
            "sender_user_id",
            "sender_name",
            "message_id",
            "event_time_ms",
            "received_at_ms",
            "text",
        ]

    @pytest.mark.skipif(not SNAPSHOT.is_file(), reason="production snapshot not exported here")
    def test_matches_the_real_production_snapshot(self, tmp_path):
        """Compare against corlinman's actual file, not a transcription."""
        ours = tmp_path / "h.sqlite"
        GH.connect_store(ours).close()
        mine, theirs = schema_rows(ours), schema_rows(SNAPSHOT)
        for name in ("group_messages", "idx_group_messages_window", "monitor_state"):
            assert mine[name] == theirs[name], name

    def test_text_cap_matches_corlinman(self):
        assert GH.TEXT_CAP == 2000

    def test_d2_reader_reads_what_this_writer_wrote(self, tmp_path, writer):
        """The load-bearing compatibility test: D2's unmodified query
        function, pointed at a file this module created, returns our rows in
        the shape its digest formatter expects."""
        reader = load_reader()
        w = writer(batch_rows=100, flush_secs=10.0)
        now_ms = int(time.time() * 1000)
        for i in range(5):
            w.record(
                instance_id="default",
                group_id=GROUP,
                sender_user_id="1076712858",
                sender_name="某人",
                message_id=5000 + i,
                event_time_ms=now_ms,
                text=f"实时消息 {i}",
            )
        assert w.flush(timeout=10.0)

        rows = reader._qq_monitor_query(
            str(w.config.db_path),
            instance_id="default",
            group_id=GROUP,
            since_ms=now_ms - 60_000,
            until_ms=now_ms + 60_000,
            sender_ids=[],
            limit=1000,
        )
        assert len(rows) == 5
        # (received_at_ms, sender_user_id, sender_name, event_time_ms, text)
        assert rows[0][1] == "1076712858"
        assert rows[0][2] == "某人"
        assert rows[0][4] == "实时消息 0"

        lines = reader._qq_monitor_format_lines(rows, ["1076712858"], reader.ZoneInfo("Asia/Shanghai"))
        assert len(lines) == 5
        assert lines[0].startswith("★")  # focus marking still works

    def test_sender_filter_still_narrows(self, tmp_path, writer):
        """``watch_user_ids`` (qunjlu's whole mechanism) works on our rows."""
        reader = load_reader()
        w = writer(batch_rows=100, flush_secs=10.0)
        now_ms = int(time.time() * 1000)
        for uid in ("1076712858", "999"):
            w.record(
                instance_id="default",
                group_id=GROUP,
                sender_user_id=uid,
                sender_name="n",
                message_id=int(uid),
                event_time_ms=now_ms,
                text="hi",
            )
        assert w.flush(timeout=10.0)
        rows = reader._qq_monitor_query(
            str(w.config.db_path),
            instance_id="default",
            group_id=GROUP,
            since_ms=now_ms - 60_000,
            until_ms=now_ms + 60_000,
            sender_ids=["1076712858"],
            limit=1000,
        )
        assert [r[1] for r in rows] == ["1076712858"]

    def test_default_write_path_matches_the_readers_default(self, tmp_path, monkeypatch):
        """Drift guard.  ``default_db_path`` deliberately re-derives the path
        ``installer.qq_group_history_db_path`` computes rather than importing
        a cron plugin from a platform plugin; if the two ever diverge, an
        operator who unsets ``QQ_GROUP_HISTORY_DB`` at cutover silently reads
        an empty file while the writer fills another one."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.delenv(GH.DB_PATH_ENV, raising=False)
        monkeypatch.delenv("QQ_GROUP_HISTORY_DB", raising=False)
        from plugins.corlinman_jobs import installer

        assert GH.default_db_path() == installer.qq_group_history_db_path()

    def test_writer_env_is_not_the_readers_env(self):
        """Two variables, on purpose: during coexistence the reader points at
        corlinman's LIVE file, and the writer must not follow it there."""
        from plugins.corlinman_jobs import installer

        assert GH.DB_PATH_ENV != installer.QQ_GROUP_HISTORY_DB_ENV

    def test_db_path_env_override(self, tmp_path, monkeypatch):
        target = tmp_path / "elsewhere" / "h.sqlite"
        monkeypatch.setenv(GH.DB_PATH_ENV, str(target))
        assert GH.default_db_path() == target

    def test_store_stays_in_delete_journal_mode(self, tmp_path):
        """SQLite 3.40.1 on the target host carries the WAL-reset corruption
        bug and this store is multi-process (gateway writes, cron reads)."""
        path = tmp_path / "h.sqlite"
        conn = GH.connect_store(path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. Configuration — off unless asked, and never wider than the whitelist
# ---------------------------------------------------------------------------


class TestResolveConfig:
    def test_off_by_default(self):
        assert GH.resolve_config({}, frozenset({GROUP})) is None

    def test_enabled_uses_the_whitelist_when_no_explicit_groups(self, tmp_path):
        cfg = GH.resolve_config(
            {"group_history_enabled": True, "group_history_db": str(tmp_path / "h.sqlite")},
            frozenset({GROUP, OTHER_GROUP}),
        )
        assert cfg is not None
        assert cfg.groups == frozenset({GROUP, OTHER_GROUP})

    def test_explicit_groups_narrow_the_whitelist(self, tmp_path):
        cfg = GH.resolve_config(
            {
                "group_history_enabled": True,
                "group_history_groups": [GROUP],
                "group_history_db": str(tmp_path / "h.sqlite"),
            },
            frozenset({GROUP, OTHER_GROUP}),
        )
        assert cfg.groups == frozenset({GROUP})

    def test_groups_outside_the_whitelist_are_dropped(self, tmp_path):
        cfg = GH.resolve_config(
            {
                "group_history_enabled": True,
                "group_history_groups": f"{GROUP},4242",
                "group_history_db": str(tmp_path / "h.sqlite"),
            },
            frozenset({GROUP}),
        )
        assert cfg.groups == frozenset({GROUP})

    def test_all_groups_outside_the_whitelist_stays_off(self, tmp_path):
        """Same discipline as ``proactive.resolve_config``: a mistyped id must
        not fall back to "then archive everything whitelisted"."""
        assert (
            GH.resolve_config(
                {
                    "group_history_enabled": True,
                    "group_history_groups": "4242",
                    "group_history_db": str(tmp_path / "h.sqlite"),
                },
                frozenset({GROUP}),
            )
            is None
        )

    def test_absent_whitelist_is_not_permission_to_archive_everything(self):
        """``group_whitelist=None`` means "any group may talk to the bot".  It
        does not mean "record every group the account is in"."""
        assert GH.resolve_config({"group_history_enabled": True}, None) is None

    def test_empty_whitelist_stays_off(self):
        assert GH.resolve_config({"group_history_enabled": True}, frozenset()) is None

    def test_explicit_groups_without_a_whitelist_are_honoured(self, tmp_path):
        cfg = GH.resolve_config(
            {
                "group_history_enabled": True,
                "group_history_groups": [OTHER_GROUP],
                "group_history_db": str(tmp_path / "h.sqlite"),
            },
            None,
        )
        assert cfg.groups == frozenset({OTHER_GROUP})

    def test_env_switches_it_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ONEBOT_GROUP_HISTORY_ENABLED", "true")
        monkeypatch.setenv("ONEBOT_GROUP_HISTORY_GROUPS", GROUP)
        monkeypatch.setenv(GH.DB_PATH_ENV, str(tmp_path / "h.sqlite"))
        cfg = GH.resolve_config({}, frozenset({GROUP}))
        assert cfg is not None and cfg.groups == frozenset({GROUP})

    def test_retention_defaults_to_seven_days(self, tmp_path):
        cfg = GH.resolve_config(
            {"group_history_enabled": True, "group_history_db": str(tmp_path / "h.sqlite")},
            frozenset({GROUP}),
        )
        assert cfg.retention_days == GH.DEFAULT_RETENTION_DAYS == 7.0

    def test_retention_cannot_be_set_below_the_monitor_window(self, tmp_path):
        """A retention under ~1 day deletes rows the next digest still needs,
        and the failure is invisible: an empty digest simply does not send."""
        cfg = GH.resolve_config(
            {
                "group_history_enabled": True,
                "group_history_retention_days": 0.25,
                "group_history_db": str(tmp_path / "h.sqlite"),
            },
            frozenset({GROUP}),
        )
        assert cfg.retention_days == GH.MIN_RETENTION_DAYS >= 1.0

    def test_batch_and_queue_knobs_have_floors(self, tmp_path):
        cfg = GH.resolve_config(
            {
                "group_history_enabled": True,
                "group_history_db": str(tmp_path / "h.sqlite"),
                "group_history_batch_rows": 0,
                "group_history_queue_max": -5,
                "group_history_flush_secs": 0,
            },
            frozenset({GROUP}),
        )
        assert cfg.batch_rows >= 1 and cfg.queue_max >= 1 and cfg.flush_secs > 0

    def test_documented_defaults(self):
        assert (GH.DEFAULT_BATCH_ROWS, GH.DEFAULT_FLUSH_SECS) == (200, 30.0)
        assert GH.DEFAULT_QUEUE_MAX == 2000


# ---------------------------------------------------------------------------
# 3. Batching — the reason this is not one transaction per message
# ---------------------------------------------------------------------------


class TestBatching:
    def test_row_count_threshold_commits_before_the_timer(self, tmp_path, writer):
        w = writer(batch_rows=5, flush_secs=3600.0)
        write_rows(w, 5)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and count_rows(w.config.db_path) < 5:
            time.sleep(0.01)
        assert count_rows(w.config.db_path) == 5
        assert w.stats["batches"] == 1  # one transaction, not five

    def test_time_threshold_commits_a_partial_batch(self, tmp_path, writer):
        w = writer(batch_rows=1000, flush_secs=0.15)
        write_rows(w, 3)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and count_rows(w.config.db_path) < 3:
            time.sleep(0.01)
        assert count_rows(w.config.db_path) == 3
        assert w.stats["batches"] == 1

    def test_nothing_is_committed_before_either_threshold(self, tmp_path, writer):
        w = writer(batch_rows=1000, flush_secs=3600.0)
        write_rows(w, 4)
        time.sleep(0.2)
        assert count_rows(w.config.db_path) == 0
        assert w.flush(timeout=10.0)
        assert count_rows(w.config.db_path) == 4

    def test_close_flushes_what_is_still_queued(self, tmp_path):
        w = GH.GroupHistoryWriter(make_config(tmp_path, batch_rows=1000, flush_secs=3600.0))
        assert w.start()
        write_rows(w, 7)
        w.close(timeout=10.0)
        assert count_rows(w.config.db_path) == 7

    def test_blank_text_is_never_stored(self, tmp_path, writer):
        """Stickers / recalls / media-only posts.  corlinman skipped them, and
        a blank line in a digest transcript is pure noise."""
        w = writer()
        assert w.record(instance_id="default", group_id=GROUP, sender_user_id="1", text="   ") is False
        assert w.record(instance_id="default", group_id=GROUP, sender_user_id="1", text="") is False
        assert w.flush(timeout=10.0)
        assert count_rows(w.config.db_path) == 0

    def test_text_is_capped(self, tmp_path, writer):
        w = writer()
        w.record(
            instance_id="default",
            group_id=GROUP,
            sender_user_id="1",
            message_id=1,
            text="x" * (GH.TEXT_CAP + 500),
        )
        assert w.flush(timeout=10.0)
        conn = sqlite3.connect(str(w.config.db_path))
        try:
            assert conn.execute("SELECT LENGTH(text) FROM group_messages").fetchone()[0] == GH.TEXT_CAP
        finally:
            conn.close()

    def test_groups_outside_the_capture_set_are_ignored(self, tmp_path, writer):
        w = writer(groups=frozenset({GROUP}))
        assert w.record(instance_id="default", group_id=OTHER_GROUP, sender_user_id="1", text="x") is False
        assert w.flush(timeout=10.0)
        assert count_rows(w.config.db_path) == 0


# ---------------------------------------------------------------------------
# 4. Bounded memory
# ---------------------------------------------------------------------------


class TestQueueBound:
    def test_a_full_queue_drops_rows_instead_of_growing(self, tmp_path, monkeypatch):
        """The gateway's cgroup cap is 512 MB against ~105 MB steady RSS.  A
        stalled writer must cost archived rows, not the whole process.

        The worker is genuinely wedged here (its first commit blocks on an
        event), which is the shape of the failure this bound exists for."""
        released = threading.Event()
        real_commit = GH.GroupHistoryWriter._commit

        def _stalled_commit(self, rows):
            released.wait(30.0)
            return real_commit(self, rows)

        monkeypatch.setattr(GH.GroupHistoryWriter, "_commit", _stalled_commit)
        cfg = make_config(tmp_path, queue_max=8, batch_rows=2, flush_secs=0.05)
        w = GH.GroupHistoryWriter(cfg)
        assert w.start()
        try:
            accepted = sum(
                1
                for i in range(200)
                if w.record(
                    instance_id="default",
                    group_id=GROUP,
                    sender_user_id="1",
                    message_id=i,
                    text=f"m{i}",
                )
            )
            assert w.stats["queue_depth"] <= cfg.queue_max
            assert accepted + w.stats["dropped"] == 200
            assert w.stats["dropped"] > 0
            # In-flight rows are bounded by queue + the worker's own batch,
            # never by how many arrived.
            assert accepted <= cfg.queue_max + cfg.batch_rows
        finally:
            released.set()
            w.close(timeout=15.0)

    def test_the_batch_size_cannot_exceed_the_queue_bound(self, tmp_path):
        """The worker's un-committed list is bounded only by ``batch_rows``.
        Left unclamped, a batch above the queue bound would make total
        in-flight memory unbounded — exactly what the queue bound prevents."""
        w = GH.GroupHistoryWriter(make_config(tmp_path, queue_max=10, batch_rows=10_000))
        assert w.config.batch_rows == 10
        assert w.config.queue_max == 10

    def test_dropping_never_raises_into_the_caller(self, tmp_path):
        cfg = make_config(tmp_path, queue_max=1, batch_rows=1000, flush_secs=3600.0)
        w = GH.GroupHistoryWriter(cfg)
        assert w.start()
        try:
            for i in range(50):
                assert w.record(
                    instance_id="default", group_id=GROUP, sender_user_id="1",
                    message_id=i, text="x",
                ) in (True, False)
        finally:
            w.close(timeout=10.0)

    def test_a_stop_seen_while_draining_is_not_lost(self, tmp_path):
        """``flush()`` racing ``close()``: the drain consumes whatever is in
        the queue, and swallowing the stop token there would leave the writer
        thread running forever."""
        w = GH.GroupHistoryWriter(make_config(tmp_path))
        w._queue.put(GH._Stop())
        assert w._drain_nowait() == []
        assert w._stop_requested is True

    def test_flush_then_close_terminates_the_thread(self, tmp_path):
        w = GH.GroupHistoryWriter(make_config(tmp_path, batch_rows=1000, flush_secs=3600.0))
        assert w.start()
        write_rows(w, 4)
        assert w.flush(timeout=10.0)
        w.close(timeout=10.0)
        assert w._thread is None
        assert count_rows(w.config.db_path) == 4

    def test_record_before_start_and_after_close_is_a_no_op(self, tmp_path):
        w = GH.GroupHistoryWriter(make_config(tmp_path))
        assert w.record(instance_id="default", group_id=GROUP, sender_user_id="1", text="x") is False
        assert w.start()
        w.close(timeout=10.0)
        assert w.record(instance_id="default", group_id=GROUP, sender_user_id="1", text="x") is False


# ---------------------------------------------------------------------------
# 5. The event loop is never blocked
# ---------------------------------------------------------------------------


class TestDoesNotBlockTheEventLoop:
    def test_the_sqlite_connection_belongs_to_the_writer_thread(self, tmp_path, writer):
        """Structural proof, not a timing heuristic: if the connection were
        created on the caller's thread, every commit's fsync would happen on
        whatever thread called ``record`` — the event loop."""
        w = writer(batch_rows=1, flush_secs=0.05)
        write_rows(w, 1)
        assert w.flush(timeout=10.0)
        assert w._conn_thread_ident is not None
        assert w._conn_thread_ident != threading.get_ident()

    def test_record_is_a_plain_function_not_a_coroutine(self):
        assert not asyncio.iscoroutinefunction(GH.GroupHistoryWriter.record)

    def test_a_stalled_store_does_not_stall_the_inbound_path(self, tmp_path, monkeypatch):
        """The real property: with commits made pathologically slow, a burst of
        inbound messages still returns to the event loop promptly, because
        ``record`` only touches a queue."""
        slow = threading.Event()

        real_commit = GH.GroupHistoryWriter._commit

        def _slow_commit(self, rows):
            slow.set()
            time.sleep(2.0)  # a wedged disk, exaggerated
            return real_commit(self, rows)

        monkeypatch.setattr(GH.GroupHistoryWriter, "_commit", _slow_commit)

        async def _run() -> float:
            ad = make_adapter(tmp_path, {"group_history_batch_rows": 1})
            await ad.connect.__wrapped__(ad) if False else None  # never dial
            ad._start_group_history()
            assert ad._history_writer is not None
            start = time.monotonic()
            for i in range(60):
                await ad._on_message_event(group_event(f"m{i}", message_id=i))
            elapsed = time.monotonic() - start
            ad._history_writer.close(timeout=15.0)
            return elapsed

        elapsed = asyncio.run(_run())
        assert slow.is_set(), "the slow commit never ran — the test proved nothing"
        # 60 messages against a store that takes 2s per commit.  Anything near
        # 2s here means the inbound path waited on the archive.
        assert elapsed < 1.0, f"inbound path blocked for {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 6. Fail-open — an archive miss must never be a reply miss
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_an_unopenable_store_disables_archiving_without_raising(self, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("i am a file")
        w = GH.GroupHistoryWriter(make_config(tmp_path, db_path=blocker / "sub" / "h.sqlite"))
        assert w.start() is False
        assert w.stats["last_error"]
        assert w.record(instance_id="default", group_id=GROUP, sender_user_id="1", text="x") is False

    def test_a_failing_commit_is_counted_not_raised(self, tmp_path, monkeypatch):
        w = GH.GroupHistoryWriter(make_config(tmp_path, batch_rows=1, flush_secs=0.05))
        monkeypatch.setattr(GH, "connect_store", broken_store("disk I/O error"))
        assert w.start()
        try:
            write_rows(w, 3)
            assert w.flush(timeout=10.0)
            assert w.stats["failed"] == 3
            assert w.stats["written"] == 0
            assert "disk I/O error" in (w.stats["last_error"] or "")
        finally:
            w.close(timeout=10.0)

    def test_the_adapter_still_routes_when_the_archive_is_broken(self, tmp_path, monkeypatch):
        monkeypatch.setattr(GH, "connect_store", broken_store("database is locked"))

        async def _run():
            ad = make_adapter(tmp_path)
            ad._start_group_history()
            assert ad._history_writer is not None
            for i in range(3):
                await ad._on_message_event(
                    group_event(
                        "",
                        message_id=i,
                        segments=[P.AtSegment(qq="100"), P.TextSegment(text=f" 在吗 {i}")],
                    )
                )
            # The reply path spawns a turn task per accepted message; drain
            # them so "did the bot still answer" is actually observed.
            await asyncio.sleep(0.05)
            if ad._turn_tasks:
                await asyncio.gather(*list(ad._turn_tasks), return_exceptions=True)
            ad._history_writer.flush(timeout=10.0)
            failed = ad._history_writer.stats["failed"]
            ad._history_writer.close(timeout=10.0)
            return ad.test_handler.calls, failed

        calls, failed = asyncio.run(_run())
        assert failed > 0, "the archive did not actually fail — test proved nothing"
        assert calls == 3, "inbound handling stopped when the archive broke"

    def test_a_hostile_value_does_not_raise(self, tmp_path, writer):
        class Explosive:
            def __str__(self):
                raise RuntimeError("nope")

        w = writer()
        assert w.record(instance_id="default", group_id=Explosive(), sender_user_id="1", text="x") is False

    def test_a_writer_that_never_started_is_harmless_to_stop(self, tmp_path):
        w = GH.GroupHistoryWriter(make_config(tmp_path))
        w.close(timeout=1.0)  # must not raise or hang
        assert w.stats["running"] is False


# ---------------------------------------------------------------------------
# 7. Retention: DELETE, never VACUUM
# ---------------------------------------------------------------------------


class TestRetention:
    def test_prune_removes_old_rows_and_keeps_recent_ones(self, tmp_path):
        w = GH.GroupHistoryWriter(make_config(tmp_path, retention_days=2.0))
        assert w.start()
        try:
            now_ms = int(time.time() * 1000)
            seed_store(
                w.config.db_path,
                [row(message_id=str(i), received_ms=now_ms - 10 * 86_400_000, text=f"old{i}") for i in range(20)]
                + [row(message_id=str(100 + i), received_ms=now_ms - 3_600_000, text=f"new{i}") for i in range(5)],
            )
            w._prune()
            conn = sqlite3.connect(str(w.config.db_path))
            try:
                remaining = [r[0] for r in conn.execute("SELECT text FROM group_messages")]
            finally:
                conn.close()
            assert len(remaining) == 5
            assert all(t.startswith("new") for t in remaining)
            assert w.stats["pruned"] == 20
        finally:
            w.close(timeout=10.0)

    def test_prune_does_not_vacuum(self, tmp_path):
        """Freed pages stay on the freelist for reuse.  VACUUM rewrites the
        whole file, which is not a trade worth making on a 2 vCPU host to
        reclaim ~20 MB against 7.6 GB free."""
        w = GH.GroupHistoryWriter(make_config(tmp_path, retention_days=2.0))
        assert w.start()
        try:
            now_ms = int(time.time() * 1000)
            seed_store(
                w.config.db_path,
                [
                    row(message_id=str(i), received_ms=now_ms - 10 * 86_400_000, text="x" * 1500)
                    for i in range(2000)
                ],
            )
            size_before = w.config.db_path.stat().st_size
            w._prune()
            conn = sqlite3.connect(str(w.config.db_path))
            try:
                freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
            finally:
                conn.close()
            assert freelist > 0, "pages were reclaimed — something vacuumed"
            assert w.config.db_path.stat().st_size >= size_before
        finally:
            w.close(timeout=10.0)

    def test_prune_is_chunked(self, tmp_path):
        """A big backlog must not hold the single write lock in one statement:
        in DELETE mode a write blocks the monitors' readers."""
        assert GH.PRUNE_CHUNK_ROWS > 0
        w = GH.GroupHistoryWriter(make_config(tmp_path, retention_days=2.0))
        assert w.start()
        try:
            now_ms = int(time.time() * 1000)
            n = GH.PRUNE_CHUNK_ROWS + 37
            seed_store(
                w.config.db_path,
                [row(message_id=str(i), received_ms=now_ms - 10 * 86_400_000) for i in range(n)],
            )
            w._prune()
            assert count_rows(w.config.db_path) == 0
            assert w.stats["pruned"] == n
        finally:
            w.close(timeout=10.0)


# ---------------------------------------------------------------------------
# 8. "Never corlinman's file"
# ---------------------------------------------------------------------------


class TestForeignStoreGuard:
    def test_our_own_store_is_accepted(self, tmp_path):
        path = tmp_path / "h.sqlite"
        GH.connect_store(path).close()
        assert GH.foreign_wal_reason(path) is None

    def test_a_missing_file_is_accepted(self, tmp_path):
        assert GH.foreign_wal_reason(tmp_path / "nope.sqlite") is None

    def test_a_wal_store_is_refused(self, tmp_path):
        """corlinman opens its store in WAL; this module never does.  A
        WAL-mode target is therefore almost certainly corlinman's live file —
        the two-writer configuration D46-② exists to prevent."""
        path = tmp_path / "corlinman.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(GH.SCHEMA_SQL)
        conn.commit()
        conn.close()
        assert GH.foreign_wal_reason(path) is not None

    def test_start_refuses_a_wal_store(self, tmp_path):
        path = tmp_path / "corlinman.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(GH.SCHEMA_SQL)
        conn.commit()
        conn.close()
        w = GH.GroupHistoryWriter(make_config(tmp_path, db_path=path))
        assert w.start() is False
        assert "refused" in (w.stats["last_error"] or "")

    def test_a_stray_wal_sidecar_is_refused(self, tmp_path):
        path = tmp_path / "h.sqlite"
        GH.connect_store(path).close()
        path.with_name(path.name + "-wal").write_bytes(b"")
        assert GH.foreign_wal_reason(path) is not None


# ---------------------------------------------------------------------------
# 9. Adapter integration — where the rows actually come from
# ---------------------------------------------------------------------------


class TestAdapterIntegration:
    def test_archiving_is_off_unless_configured(self, tmp_path):
        ad = make_adapter(tmp_path, {"group_history_enabled": False})
        ad._start_group_history()
        assert ad._history_writer is None

    def test_a_group_message_is_archived(self, tmp_path):
        async def _run():
            ad = make_adapter(tmp_path)
            ad._start_group_history()
            await ad._on_message_event(group_event("大家好", message_id=7))
            ad._history_writer.flush(timeout=10.0)
            path = ad._history_writer.config.db_path
            ad._history_writer.close(timeout=10.0)
            return path

        path = asyncio.run(_run())
        conn = sqlite3.connect(str(path))
        try:
            got = conn.execute(
                "SELECT instance_id, group_id, sender_user_id, sender_name, message_id, text "
                "FROM group_messages"
            ).fetchall()
        finally:
            conn.close()
        assert got == [("default", GROUP, "1076712858", "某人", "7", "大家好")]

    def test_direct_messages_are_never_archived(self, tmp_path):
        """D46-⑦: the stored scope must not exceed what corlinman stored, and
        corlinman stored monitored groups only."""

        async def _run():
            ad = make_adapter(tmp_path, {"group_history_groups": [GROUP, "2104743984"]})
            ad._start_group_history()
            await ad._on_message_event(private_event("私聊内容"))
            ad._history_writer.flush(timeout=10.0)
            path = ad._history_writer.config.db_path
            ad._history_writer.close(timeout=10.0)
            return path

        assert count_rows(asyncio.run(_run())) == 0

    def test_a_group_outside_the_capture_set_is_not_archived(self, tmp_path):
        async def _run():
            ad = make_adapter(
                tmp_path,
                {"group_whitelist": [GROUP, OTHER_GROUP], "group_history_groups": [GROUP]},
            )
            ad._start_group_history()
            await ad._on_message_event(group_event("x", gid=int(OTHER_GROUP), message_id=1))
            ad._history_writer.flush(timeout=10.0)
            path = ad._history_writer.config.db_path
            ad._history_writer.close(timeout=10.0)
            return path

        assert count_rows(asyncio.run(_run())) == 0

    def test_capture_happens_before_the_reply_gate(self, tmp_path):
        """corlinman recorded EVERY inbound message from a monitored group,
        router-filtered ones included — a digest of "what did the room say"
        that only saw messages the bot answered would be worthless."""

        async def _run():
            ad = make_adapter(tmp_path, {"group_reply_policy": "mention_or_keyword"})
            ad._start_group_history()
            # No @mention, no keyword: the router drops this outright.
            await ad._on_message_event(group_event("闲聊一句", message_id=11))
            ad._history_writer.flush(timeout=10.0)
            path = ad._history_writer.config.db_path
            calls = ad.test_handler.calls
            ad._history_writer.close(timeout=10.0)
            return path, calls

        path, calls = asyncio.run(_run())
        assert calls == 0, "the router did not actually drop the message"
        assert count_rows(path) == 1

    def test_sender_name_is_blanked_when_it_is_just_the_uin(self, tmp_path):
        """Matches corlinman's own convention; otherwise the digest renders
        ``123(123)`` for every member with no nickname or group card."""

        async def _run():
            ad = make_adapter(tmp_path)
            ad._start_group_history()
            await ad._on_message_event(group_event("hi", nickname=None, message_id=3))
            ad._history_writer.flush(timeout=10.0)
            path = ad._history_writer.config.db_path
            ad._history_writer.close(timeout=10.0)
            return path

        conn = sqlite3.connect(str(asyncio.run(_run())))
        try:
            assert conn.execute("SELECT sender_name FROM group_messages").fetchone()[0] == ""
        finally:
            conn.close()

    def test_group_card_wins_over_nickname(self, tmp_path):
        async def _run():
            ad = make_adapter(tmp_path)
            ad._start_group_history()
            await ad._on_message_event(group_event("hi", nickname="nick", card="群名片", message_id=4))
            ad._history_writer.flush(timeout=10.0)
            path = ad._history_writer.config.db_path
            ad._history_writer.close(timeout=10.0)
            return path

        conn = sqlite3.connect(str(asyncio.run(_run())))
        try:
            assert conn.execute("SELECT sender_name FROM group_messages").fetchone()[0] == "群名片"
        finally:
            conn.close()

    def test_event_time_is_carried_across(self, tmp_path):
        async def _run():
            ad = make_adapter(tmp_path)
            ad._start_group_history()
            await ad._on_message_event(group_event("hi", message_id=5, event_time=1_700_000_123))
            ad._history_writer.flush(timeout=10.0)
            path = ad._history_writer.config.db_path
            ad._history_writer.close(timeout=10.0)
            return path

        conn = sqlite3.connect(str(asyncio.run(_run())))
        try:
            assert conn.execute("SELECT event_time_ms FROM group_messages").fetchone()[0] == 1_700_000_123_000
        finally:
            conn.close()

    def test_the_in_memory_proactive_buffer_still_gets_fed(self, tmp_path):
        """The two capture paths are complements.  Breaking B4's 30-row
        context buffer while adding persistence would silently degrade
        proactive speech instead of failing a test."""

        async def _run():
            ad = make_adapter(tmp_path)
            ad._start_group_history()
            await ad._on_message_event(group_event("看得见我吗", message_id=9))
            ad._history_writer.flush(timeout=10.0)
            ad._history_writer.close(timeout=10.0)
            return A.recent_group_messages(ad.instance_id, GROUP)

        buffered = asyncio.run(_run())
        assert [entry[2] for entry in buffered] == ["看得见我吗"]

    def test_reconnect_does_not_start_a_second_writer(self, tmp_path):
        """``connect()`` also runs on reconnect; two writer threads would
        append every message twice."""
        ad = make_adapter(tmp_path)
        ad._start_group_history()
        first = ad._history_writer
        ad._start_group_history()
        try:
            assert ad._history_writer is first
        finally:
            first.close(timeout=10.0)

    def test_disconnect_flushes_and_stops_the_writer(self, tmp_path):
        async def _run():
            ad = make_adapter(tmp_path, {"group_history_batch_rows": 1000, "group_history_flush_secs": 3600.0})
            ad._start_group_history()
            path = ad._history_writer.config.db_path
            await ad._on_message_event(group_event("最后一句", message_id=12))
            assert count_rows(path) == 0  # still queued
            await ad.disconnect()
            assert ad._history_writer is None
            return path

        assert count_rows(asyncio.run(_run())) == 1

    def test_a_broken_group_history_module_does_not_break_connect(self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("resolve exploded")

        monkeypatch.setattr(GH, "resolve_config", _boom)
        ad = make_adapter(tmp_path)
        ad._start_group_history()  # must not raise
        assert ad._history_writer is None


# ---------------------------------------------------------------------------
# 10. Backfill
# ---------------------------------------------------------------------------


class TestBackfill:
    def test_imports_rows_verbatim(self, tmp_path):
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id="1", event_ms=111, received_ms=222, text="a")])
        result = BF.backfill(src, dest)
        assert (result.scanned, result.inserted, result.duplicates) == (1, 1, 0)
        conn = sqlite3.connect(str(dest))
        try:
            got = conn.execute(
                "SELECT instance_id, group_id, sender_user_id, sender_name, message_id, "
                "event_time_ms, received_at_ms, text FROM group_messages"
            ).fetchone()
        finally:
            conn.close()
        # Timestamps must NOT be re-stamped: the monitors window on
        # received_at_ms, so an import-time stamp would collapse a week of
        # history onto one instant.
        assert got == ("default", GROUP, "1076712858", "某人", "1", 111, 222, "a")

    def test_running_it_three_times_inserts_nothing_extra(self, tmp_path):
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id=str(i), received_ms=1_700_000_000_000 + i) for i in range(50)])
        first = BF.backfill(src, dest)
        second = BF.backfill(src, dest)
        third = BF.backfill(src, dest)
        assert first.inserted == 50
        assert (second.inserted, second.duplicates) == (0, 50)
        assert (third.inserted, third.duplicates) == (0, 50)
        assert count_rows(dest) == 50

    def test_it_dedupes_against_our_own_live_capture(self, tmp_path):
        """The case that matters at cutover.  The same QQ message captured by
        corlinman and by our live writer carries two different
        ``received_at_ms`` values; keying on the timestamp would double every
        message in the coexistence overlap."""
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id="777", received_ms=1_700_000_000_000, text="同一条消息")])
        seed_store(dest, [row(message_id="777", received_ms=1_700_000_004_321, text="同一条消息")])
        result = BF.backfill(src, dest)
        assert (result.inserted, result.duplicates) == (0, 1)
        assert count_rows(dest) == 1

    def test_duplicates_within_the_source_collapse(self, tmp_path):
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id="5", received_ms=1), row(message_id="5", received_ms=2)])
        result = BF.backfill(src, dest)
        assert (result.inserted, result.duplicates) == (1, 1)

    def test_rows_without_a_message_id_dedupe_on_content(self, tmp_path):
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id=None, event_ms=999, text="无 id 的一条")])
        assert BF.backfill(src, dest).inserted == 1
        again = BF.backfill(src, dest)
        assert (again.inserted, again.duplicates) == (0, 1)
        assert count_rows(dest) == 1

    def test_different_groups_are_not_conflated(self, tmp_path):
        """Message ids are only unique per conversation on OneBot backends."""
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(
            src,
            [row(message_id="1", group_id=GROUP), row(message_id="1", group_id=OTHER_GROUP)],
        )
        assert BF.backfill(src, dest).inserted == 2

    def test_the_day_window_is_honoured(self, tmp_path):
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        now_ms = int(time.time() * 1000)
        seed_store(
            src,
            [row(message_id="old", received_ms=now_ms - 30 * 86_400_000)]
            + [row(message_id="new", received_ms=now_ms - 3_600_000)],
        )
        result = BF.backfill(src, dest, since_ms=now_ms - 7 * 86_400_000)
        assert result.inserted == 1
        conn = sqlite3.connect(str(dest))
        try:
            assert conn.execute("SELECT message_id FROM group_messages").fetchone()[0] == "new"
        finally:
            conn.close()

    def test_group_and_instance_filters(self, tmp_path):
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(
            src,
            [
                row(message_id="1", group_id=GROUP),
                row(message_id="2", group_id=OTHER_GROUP),
                row(message_id="3", group_id=GROUP, instance_id="second"),
            ],
        )
        assert BF.backfill(src, dest, groups=[GROUP], instance_id="default").inserted == 1

    def test_blank_rows_are_not_imported(self, tmp_path):
        """The live writer never stores a blank row; importing one would make
        the two stores disagree about what counts as a message."""
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id="1", text="   "), row(message_id="2", text="ok")])
        result = BF.backfill(src, dest)
        assert (result.inserted, result.blank) == (1, 1)

    def test_dry_run_writes_nothing(self, tmp_path):
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id=str(i)) for i in range(10)])
        result = BF.backfill(src, dest, dry_run=True)
        assert result.inserted == 10
        assert count_rows(dest) == 0

    def test_source_and_dest_may_not_be_the_same_file(self, tmp_path):
        path = tmp_path / "same.sqlite"
        seed_store(path, [row()])
        with pytest.raises(SystemExit):
            BF.backfill(path, path)

    def test_a_missing_source_is_a_clear_error(self, tmp_path):
        with pytest.raises(SystemExit):
            BF.backfill(tmp_path / "nope.sqlite", tmp_path / "dest.sqlite")

    def test_the_source_is_opened_read_only(self, tmp_path):
        """It may be corlinman's own live file."""
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id=str(i)) for i in range(5)])
        before = src.read_bytes()
        BF.backfill(src, dest)
        assert src.read_bytes() == before

    def test_dest_is_created_with_the_shared_schema(self, tmp_path):
        src, dest = tmp_path / "src.sqlite", tmp_path / "deep" / "dest.sqlite"
        seed_store(src, [row()])
        BF.backfill(src, dest)
        assert set(schema_rows(dest)) >= {"group_messages", "idx_group_messages_window", "monitor_state"}

    def test_cli_reports_counts_without_message_text(self, tmp_path, capsys):
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        seed_store(src, [row(message_id="1", text="秘密内容不要打印")])
        assert BF.main(["--source", str(src), "--dest", str(dest), "--days", "0"]) == 0
        out = capsys.readouterr().out
        assert "inserted   : 1" in out
        assert "秘密内容不要打印" not in out

    def test_backfilled_rows_are_readable_by_the_d2_reader(self, tmp_path):
        """End to end: corlinman's rows, imported, read back by the monitors'
        own query function without any change to the read path."""
        reader = load_reader()
        src, dest = tmp_path / "src.sqlite", tmp_path / "dest.sqlite"
        now_ms = int(time.time() * 1000)
        seed_store(
            src,
            [
                row(message_id=str(i), received_ms=now_ms - 1000 * i, event_ms=now_ms - 1000 * i, text=f"历史 {i}")
                for i in range(10)
            ],
        )
        BF.backfill(src, dest)
        rows = reader._qq_monitor_query(
            str(dest),
            instance_id="default",
            group_id=GROUP,
            since_ms=now_ms - 86_400_000,
            until_ms=now_ms + 1000,
            sender_ids=[],
            limit=1000,
        )
        assert len(rows) == 10

    @pytest.mark.skipif(not SNAPSHOT.is_file(), reason="production snapshot not exported here")
    def test_against_the_real_production_snapshot(self, tmp_path):
        """52k real rows, imported twice.  The second run must insert zero."""
        dest = tmp_path / "dest.sqlite"
        first = BF.backfill(SNAPSHOT, dest, since_ms=None)
        second = BF.backfill(SNAPSHOT, dest, since_ms=None)
        assert first.inserted > 0
        assert second.inserted == 0
        assert second.duplicates == first.inserted
        assert count_rows(dest) == first.inserted
