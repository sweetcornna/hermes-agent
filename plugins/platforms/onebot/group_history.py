"""Persistent archive of inbound QQ group messages — the writer D2 was missing.

Why this module exists
----------------------
``plugins/corlinman_jobs``'s three migrated monitors (``qunjlu`` / ``sanhu`` /
``jlu``) summarise "everything said in group X in the last 24 hours".  Their
*only* data source is a SQLite table called ``group_messages``, and until this
module landed **nothing in this port ever wrote a row into it** — the port read
corlinman's own live capture file over the coexistence window and would have
gone permanently silent, within that store's ~3-day retention, the moment
corlinman was switched off (00-PLAN.md §19, D2 notes §4).

This is that writer.  It is deliberately *not* a new feature: it captures the
same messages corlinman captured, into the same schema corlinman used, so the
already-written read path in ``corlinman_jobs`` needs no change at cutover —
only ``QQ_GROUP_HISTORY_DB`` moves from corlinman's file to ours.

The five design constraints, and where each is enforced
------------------------------------------------------
1. **corlinman's schema, verbatim.**  :data:`SCHEMA_SQL` is a character-level
   copy of ``corlinman_server/qq_group_history.py``'s ``_SCHEMA``.  Not "close
   enough": ``corlinman_jobs_lib._qq_monitor_query`` selects specific columns
   in a specific order and relies on ``idx_group_messages_window`` for its
   range scan.  ``tests/gateway/test_onebot_group_history.py`` pins the DDL
   against a copy of the source text *and* runs the real reader against a file
   this module created.

2. **Our own file, never corlinman's.**  The write path resolves
   :data:`DB_PATH_ENV` (``ONEBOT_GROUP_HISTORY_DB``) — a *different* variable
   from the reader's ``QQ_GROUP_HISTORY_DB``, precisely so that pointing the
   monitors at corlinman's live store during coexistence cannot also point the
   writer there.  Two writers on one SQLite file, one of them not even in this
   process tree, is lock contention plus a corruption window.
   :func:`foreign_wal_reason` is the mechanical backstop: corlinman opens its
   store in WAL, we never do, so a WAL-mode target is almost certainly
   corlinman's own file and we refuse to write it.

3. **Batched commits on a dedicated thread.**  The target host runs SQLite
   3.40.1, which carries the WAL-reset corruption bug (``hermes_state.py``
   L655-660 refuses to *enable* WAL on such builds), so this store stays in
   the default DELETE journal mode — where every ``COMMIT`` is an fsync of the
   whole database.  One transaction per message on a 2 vCPU box is not
   affordable, and ``sqlite3`` is blocking, so the commit cannot live on the
   event loop at all.  :meth:`GroupHistoryWriter.record` therefore only does a
   bounded ``put_nowait`` onto a :class:`queue.Queue`; a single background
   thread owns the connection and commits in batches.

4. **Fail-open, always.**  Nothing in here raises into the adapter's inbound
   path.  A failed INSERT loses an archived chat line; a raised exception
   would stop the bot answering people.  Those are not comparable costs.

5. **Bounded memory.**  The queue has a hard ``maxsize``; when it is full new
   rows are *dropped and counted*, never buffered.  The host's hermes unit is
   capped at ``MemoryHigh=384M`` / ``MemoryMax=512M`` against a ~105 MB steady
   RSS — an unbounded buffer behind a stalled disk is an OOM, i.e. the whole
   gateway, to save chat lines that the monitors would tolerate losing.

Privacy
-------
These are real people's group messages.  This module logs **counts and error
strings only** — never message text, never a sender's name.  The same rule
corlinman's own module states in its header, for the same reason.
"""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "BUSY_TIMEOUT_MS",
    "DB_PATH_ENV",
    "DEFAULT_BATCH_ROWS",
    "DEFAULT_FLUSH_SECS",
    "DEFAULT_QUEUE_MAX",
    "DEFAULT_RETENTION_DAYS",
    "MIN_RETENTION_DAYS",
    "SCHEMA_SQL",
    "TEXT_CAP",
    "GroupHistoryConfig",
    "GroupHistoryWriter",
    "connect_store",
    "default_db_path",
    "foreign_wal_reason",
    "resolve_config",
]


# ---------------------------------------------------------------------------
# Schema — a verbatim copy of corlinman's, and it must stay that way
# ---------------------------------------------------------------------------

#: Per-message text cap, identical to ``corlinman_server.qq_group_history``'s
#: ``TEXT_CAP``.  A forwarded wall of text gets truncated: the digest prompt
#: caps line length anyway, and unbounded rows let one paste balloon the store
#: (and, here, one queue entry).
TEXT_CAP = 2000

#: Character-for-character ``corlinman_server/qq_group_history.py::_SCHEMA``.
#: Changing so much as a column order here silently breaks
#: ``corlinman_jobs_lib._qq_monitor_query``, which is positional.
SCHEMA_SQL = """
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

_INSERT_SQL = (
    "INSERT INTO group_messages (instance_id, group_id, sender_user_id, "
    "sender_name, message_id, event_time_ms, received_at_ms, text) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Environment variable naming the file this module WRITES.  Deliberately not
#: ``QQ_GROUP_HISTORY_DB`` — that one names the file the monitors READ, and
#: during coexistence it points at corlinman's live store.  Cutover is: flip
#: the reader's variable onto this file.  See the module docstring, point 2.
DB_PATH_ENV = "ONEBOT_GROUP_HISTORY_DB"

#: Commit whenever this many rows have accumulated.  Bounds the burst case: a
#: flood commits by size instead of sitting in RAM until the timer fires, and
#: caps one transaction at 200 x <=2 KB of text, a blip on a 1.9 GB host.
DEFAULT_BATCH_ROWS = 200

#: ...or when the oldest un-committed row is this old, whichever comes first.
#:
#: 30 s is chosen against the measured traffic, not picked round: D2 counted
#: ~15,000 rows/day for group 980927602 and ~1,500 for 183287894, i.e. ~0.2
#: rows/s averaged and ~0.35 rows/s across waking hours.  A 30 s window
#: therefore batches ~10 rows, cutting fsyncs to <=2,880/day — about 10x fewer
#: than committing per message — while the exposure on an *unclean* kill stays
#: at <=30 s out of a 24 h, ~16,500-message digest window.  A graceful
#: shutdown flushes, so that exposure only exists for SIGKILL / power loss.
#: Going shorter buys durability nobody needs here and spends the one resource
#: (fsync on a 2 vCPU box in DELETE mode) that is actually scarce.
DEFAULT_FLUSH_SECS = 30.0

#: Hard upper bound on un-written rows held in memory.
#:
#: 2,000 rows is ~100 minutes of the busiest monitored group's traffic — the
#: queue can only approach it if the writer thread has been wedged for over an
#: hour, at which point dropping is the correct answer, not buffering.  Worst
#: case footprint is 2,000 x TEXT_CAP; realistically (~30 chars/message) it is
#: well under a megabyte.  The worker's own un-committed batch is clamped to
#: this same number (see :meth:`GroupHistoryWriter.__init__`), so the total
#: in-flight bound is 2 x this value, never more.
DEFAULT_QUEUE_MAX = 2000

#: Row retention in days (D46-⑤).  The monitors read a 24 h window; corlinman
#: kept ~3 days.  7 days x ~16,500 rows/day x ~1 KB is roughly 20 MB against
#: 7.6 GB free, so the extra margin is free and buys a week of back-diagnosis.
DEFAULT_RETENTION_DAYS = 7.0

#: Floor for a *configured* retention.  The monitors' window is 24 h; a
#: retention under that silently deletes rows the next digest still needs, and
#: that failure is invisible (an empty digest just does not send).  corlinman
#: enforced the same invariant as ``max(retention, longest_window + 1h)``.
MIN_RETENTION_DAYS = 2.0

#: How often the writer thread prunes.  Hourly: the store only has to be
#: *roughly* the configured size, and each pass takes the write lock.
PRUNE_INTERVAL_SECS = 3600.0

#: Delay before the first prune, so boot I/O and the first messages are not
#: competing with a potentially large initial delete.
PRUNE_INITIAL_DELAY_SECS = 60.0

#: Rows deleted per prune statement.  Chunked so a big backlog never holds the
#: single write lock long enough to stall a monitor's read (DELETE mode blocks
#: readers during a write).
PRUNE_CHUNK_ROWS = 5000

#: Safety stop for one prune pass — 1,000,000 rows.  Whatever is left waits
#: for the next hourly pass rather than pinning the lock.
PRUNE_MAX_CHUNKS = 200

#: Matches ``corlinman_jobs_lib._qq_monitor_query``'s own busy timeout, so a
#: writer and a reading cron subprocess wait for each other instead of one of
#: them failing instantly on ``database is locked``.
BUSY_TIMEOUT_MS = 30000

#: Log at most one drop warning per this many drops (after the first).  A
#: wedged writer must not also flood journald — retention there is the scarce
#: resource this migration already had to widen once (00-PLAN.md §6).
_DROP_LOG_EVERY = 1000


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_groups(raw: Any) -> Tuple[str, ...]:
    """``[1, "2"]`` / ``"1,2"`` → ``("1", "2")``.  Ids compare as text.

    Same shape as :func:`plugins.platforms.onebot.proactive._parse_groups`;
    duplicated rather than imported because ``proactive`` imports ``adapter``
    and ``adapter`` imports this module.
    """
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(v).strip() for v in raw if str(v).strip())
    return tuple(p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip())


def default_db_path() -> Path:
    """Where this module writes, honouring :data:`DB_PATH_ENV`.

    The default deliberately mirrors
    ``plugins.corlinman_jobs.installer.qq_group_history_db_path``'s own
    default — ``$HERMES_HOME/plugin-data/corlinman_jobs/
    qq_group_history.sqlite`` — so that an operator who simply *unsets*
    ``QQ_GROUP_HISTORY_DB`` at cutover finds reader and writer already
    agreeing on one file.  Recomputed (not imported) to keep this platform
    plugin from depending on a cron plugin; a test asserts the two stay
    equal, so the duplication cannot drift silently.
    """
    override = os.getenv(DB_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_hermes_home  # noqa: PLC0415

    return Path(get_hermes_home()) / "plugin-data" / "corlinman_jobs" / "qq_group_history.sqlite"


@dataclass(frozen=True)
class GroupHistoryConfig:
    """Resolved settings.  ``resolve_config`` returning ``None`` means off."""

    db_path: Path
    groups: FrozenSet[str]
    batch_rows: int = DEFAULT_BATCH_ROWS
    flush_secs: float = DEFAULT_FLUSH_SECS
    queue_max: int = DEFAULT_QUEUE_MAX
    retention_days: float = DEFAULT_RETENTION_DAYS


def resolve_config(
    extra: Any, group_whitelist: Optional[FrozenSet]
) -> Optional[GroupHistoryConfig]:
    """Read the flat ``group_history_*`` keys; ``None`` when archiving is off.

    **Off unless explicitly switched on.**  Capturing a group's chat to disk
    is a real, if quiet, new behaviour on a host where corlinman is still the
    live service; it does not get to start because a config file was merged.

    Which groups get captured, in order of preference:

    * ``group_history_groups`` when set — but intersected with
      ``group_whitelist``, because the whitelist is the hard gate everywhere
      else in this adapter and an archive is not the place to invent an
      exception.  An explicit list that survives the intersection empty stays
      **off**, mirroring ``proactive.resolve_config``'s fix: an operator who
      narrowed the target and mistyped an id gets nothing, not everything.
    * otherwise ``group_whitelist``.
    * a ``group_whitelist`` of ``None`` means "no whitelist — any group may
      talk to the bot".  That is emphatically *not* a licence to archive every
      group the account happens to be in, so archiving stays off and says so.

    Direct messages are never archived: nothing in this module has a code path
    for them, and the adapter only calls it from the group branch.  corlinman
    stored monitored groups only, and D46-⑦ holds the port to that scope.
    """
    get = extra.get if hasattr(extra, "get") else (lambda k, d=None: getattr(extra, k, d))

    if not _as_bool(get("group_history_enabled", os.getenv("ONEBOT_GROUP_HISTORY_ENABLED")), False):
        return None

    requested = _parse_groups(
        get("group_history_groups", os.getenv("ONEBOT_GROUP_HISTORY_GROUPS"))
    )
    if requested:
        if group_whitelist is not None:
            groups = tuple(g for g in requested if g in group_whitelist)
            outside = [g for g in requested if g not in group_whitelist]
            if outside:
                logger.warning(
                    "OneBot: group_history_groups outside group_whitelist ignored: %s",
                    outside,
                )
            if not groups:
                logger.warning(
                    "OneBot: every requested group_history_groups entry (%s) is "
                    "outside group_whitelist — group history archiving stays OFF "
                    "rather than falling back to the whole whitelist",
                    list(requested),
                )
                return None
        else:
            groups = requested
    elif group_whitelist:
        groups = tuple(sorted(str(g) for g in group_whitelist))
    else:
        logger.warning(
            "OneBot: group_history_enabled but no capture target "
            "(set group_history_groups or group_whitelist) — archiving stays OFF; "
            "an absent whitelist is not permission to archive every group"
        )
        return None

    raw_path = str(get("group_history_db", "") or "").strip()
    db_path = Path(raw_path).expanduser() if raw_path else default_db_path()

    retention = _as_float(
        get("group_history_retention_days", os.getenv("ONEBOT_GROUP_HISTORY_RETENTION_DAYS")),
        DEFAULT_RETENTION_DAYS,
    )
    if retention < MIN_RETENTION_DAYS:
        logger.warning(
            "OneBot: group_history_retention_days=%.3f is below the %.1f-day floor "
            "(the monitors read a 24h window) — using the floor",
            retention,
            MIN_RETENTION_DAYS,
        )
        retention = MIN_RETENTION_DAYS

    queue_max = max(
        1,
        _as_int(
            get("group_history_queue_max", os.getenv("ONEBOT_GROUP_HISTORY_QUEUE_MAX")),
            DEFAULT_QUEUE_MAX,
        ),
    )
    batch_rows = max(
        1,
        _as_int(
            get("group_history_batch_rows", os.getenv("ONEBOT_GROUP_HISTORY_BATCH_ROWS")),
            DEFAULT_BATCH_ROWS,
        ),
    )

    return GroupHistoryConfig(
        db_path=db_path,
        groups=frozenset(groups),
        batch_rows=batch_rows,
        flush_secs=max(
            0.1,
            _as_float(
                get("group_history_flush_secs", os.getenv("ONEBOT_GROUP_HISTORY_FLUSH_SECS")),
                DEFAULT_FLUSH_SECS,
            ),
        ),
        queue_max=queue_max,
        retention_days=retention,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def foreign_wal_reason(path: Path) -> Optional[str]:
    """Why ``path`` looks like somebody else's store, or ``None``.

    The one mechanically checkable form of D46-②'s "never write corlinman's
    file".  corlinman's ``QqGroupHistory`` opens its store with
    ``PRAGMA journal_mode = WAL``; this module never does (see the module
    docstring, point 3), so a WAL-mode target — or a stray ``-wal`` sidecar —
    is a strong signal that the path has been aimed at the live corlinman
    store, which is exactly the two-writer configuration that risks
    corruption.  Refusing costs an archive; guessing wrong costs the file
    three monitors read.

    Probing is read-only: the sidecar check touches nothing, and the pragma
    read opens ``mode=ro``, which never takes a write lock.
    """
    try:
        if not path.exists():
            return None
        if path.with_name(path.name + "-wal").exists():
            return "a -wal sidecar is present"
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        # Unreadable is the connect path's problem to report, not ours.
        return None
    mode = str(row[0]).lower() if row else ""
    return "journal_mode is WAL" if mode == "wal" else None


def connect_store(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) a ``group_messages`` store at ``path``.

    Shared with the backfill tool so both ends agree on schema and pragmas.

    ``journal_mode`` is left at SQLite's DELETE default **on purpose**: the
    target host ships SQLite 3.40.1, and ``hermes_state.py`` L655-660 declines
    to enable WAL below 3.44.6/3.50.7/3.51.3 because of the WAL-reset
    corruption bug — and this store is precisely the multi-process case that
    bug hits (this gateway writes it; a ``hermes cron`` subprocess reads it).
    ``synchronous`` is likewise left at FULL: batching already cut commits to
    a few thousand a day, so there is nothing left to buy by trading integrity
    for fsyncs, and a corrupt archive is worse than a slow one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# Control sentinels pushed through the same queue as data rows, so ordering is
# never ambiguous: a flush really does cover everything recorded before it.
class _Stop:
    __slots__ = ()


class _Flush:
    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = threading.Event()


Row = Tuple[str, str, str, str, Optional[str], int, int, str]


class GroupHistoryWriter:
    """Queue + background thread that batches rows into the archive.

    Lifecycle: :meth:`start`, then :meth:`record` from anywhere (it is
    thread-safe and non-blocking), then :meth:`close`.  ``record`` before
    ``start`` or after ``close`` is a silent no-op returning ``False`` —
    archiving is best-effort by construction, and a shutdown race must not
    surface as an exception on the inbound message path.
    """

    def __init__(self, config: GroupHistoryConfig) -> None:
        if config.batch_rows > config.queue_max:
            # The worker holds un-committed rows in a plain list, bounded only
            # by ``batch_rows`` (it commits the instant it reaches it).  A
            # batch size above the queue bound would therefore leave total
            # in-flight memory unbounded — precisely what ``queue_max`` exists
            # to prevent.  Clamped here rather than in ``resolve_config`` so
            # every construction path is covered, which makes the real
            # guarantee "at most 2 x queue_max rows in flight, ever".
            logger.warning(
                "OneBot: group history batch_rows=%d exceeds queue_max=%d; "
                "clamping to the queue bound so in-flight rows stay bounded",
                config.batch_rows,
                config.queue_max,
            )
            config = replace(config, batch_rows=config.queue_max)
        self._config = config
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=config.queue_max)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._conn: Optional[sqlite3.Connection] = None
        self._conn_thread_ident: Optional[int] = None
        self._next_prune_at = 0.0
        self._last_report_at = 0.0
        self._stop_requested = False
        self._lock = threading.Lock()
        # Counters. Ints under the GIL; the lock only guards the drop counter's
        # read-modify-write against many concurrent recorders.
        self._queued = 0
        self._written = 0
        self._dropped = 0
        self._failed = 0
        self._pruned = 0
        self._batches = 0
        self._last_error: Optional[str] = None

    # -- introspection ---------------------------------------------------

    @property
    def config(self) -> GroupHistoryConfig:
        return self._config

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stats(self) -> Dict[str, Any]:
        """Counters only — never message content.  Used by tests and logs."""
        return {
            "db_path": str(self._config.db_path),
            "groups": sorted(self._config.groups),
            "running": self._running,
            "queue_depth": self._queue.qsize(),
            "queue_max": self._config.queue_max,
            "queued": self._queued,
            "written": self._written,
            "dropped": self._dropped,
            "failed": self._failed,
            "pruned": self._pruned,
            "batches": self._batches,
            "last_error": self._last_error,
        }

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Spawn the writer thread.  ``False`` means archiving stays off.

        Never raises: a store we cannot open is a degraded archive, not a
        reason to fail ``connect()`` and leave the bot off QQ entirely.
        """
        if self._running:
            return True
        reason = foreign_wal_reason(self._config.db_path)
        if reason is not None:
            logger.error(
                "OneBot: refusing to archive into %s — %s. That is how "
                "corlinman's own live store looks, and two writers on one "
                "SQLite file risk corrupting the very file the QQ monitors "
                "read. Point %s at a file this gateway owns.",
                self._config.db_path,
                reason,
                DB_PATH_ENV,
            )
            self._last_error = f"refused: {reason}"
            return False
        try:
            # Open once here, on the caller's thread, purely to fail fast on a
            # bad path; the worker opens its own connection because sqlite3
            # objects are bound to the thread that created them.
            connect_store(self._config.db_path).close()
        except (OSError, sqlite3.Error) as exc:
            logger.error(
                "OneBot: group history archive disabled — cannot open %s (%s)",
                self._config.db_path,
                exc,
            )
            self._last_error = str(exc)
            return False

        self._running = True
        self._next_prune_at = time.monotonic() + PRUNE_INITIAL_DELAY_SECS
        self._last_report_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="onebot-group-history",
            daemon=True,  # never hold up process exit for an archive
        )
        self._thread.start()
        logger.info(
            "OneBot: group history archiving ON — db=%s groups=%s "
            "batch=%d rows / %.0fs, queue<=%d, retention=%.1fd",
            self._config.db_path,
            sorted(self._config.groups),
            self._config.batch_rows,
            self._config.flush_secs,
            self._config.queue_max,
            self._config.retention_days,
        )
        return True

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until everything recorded so far is committed.

        For graceful shutdown and for tests.  Never called from the event
        loop without a thread hop.
        """
        if not self._running:
            return True
        token = _Flush()
        try:
            self._queue.put(token, timeout=timeout)
        except queue.Full:
            return False
        return token.done.wait(timeout)

    def close(self, timeout: float = 10.0) -> None:
        """Stop the thread after committing whatever is still queued."""
        if not self._running:
            return
        self._running = False
        try:
            self._queue.put(_Stop(), timeout=timeout)
        except queue.Full:  # pragma: no cover — the worker always drains
            pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():  # pragma: no cover — needs a wedged disk
                logger.warning(
                    "OneBot: group history writer did not stop within %.0fs; "
                    "leaving it as a daemon thread", timeout
                )
        self._thread = None
        logger.info(
            "OneBot: group history archiving stopped — written=%d dropped=%d "
            "failed=%d pruned=%d",
            self._written,
            self._dropped,
            self._failed,
            self._pruned,
        )

    # -- producer side (event loop) --------------------------------------

    def record(
        self,
        *,
        instance_id: str,
        group_id: Any,
        sender_user_id: Any,
        sender_name: str = "",
        message_id: Any = None,
        event_time_ms: Optional[int] = None,
        text: str = "",
    ) -> bool:
        """Enqueue one group message.  Non-blocking, never raises.

        Returns ``True`` only when the row was actually queued.  ``False``
        covers every "not archived" case — not started, group not captured,
        blank text, queue full — because the caller's correct response to all
        of them is identical: carry on handling the message.

        This is the function that runs on the asyncio event loop, so it does
        exactly two things that can cost anything: a set membership test and a
        ``put_nowait``.  No I/O, no lock held across a syscall, no ``await``.
        """
        try:
            if not self._running:
                return False
            gid = str(group_id)
            if gid not in self._config.groups:
                return False
            body = (text or "").strip()
            if not body:
                # Blank rows (stickers, recalls, media-only posts) are noise in
                # a rendered digest; corlinman's ``record`` skipped them too.
                return False
            now_ms = int(time.time() * 1000)
            row: Row = (
                str(instance_id),
                gid,
                str(sender_user_id),
                str(sender_name or ""),
                None if message_id is None else str(message_id),
                int(event_time_ms) if event_time_ms is not None else now_ms,
                now_ms,
                body[:TEXT_CAP],
            )
            try:
                self._queue.put_nowait(row)
            except queue.Full:
                self._note_drop()
                return False
            self._queued += 1
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open is the whole point
            self._last_error = str(exc)
            logger.warning("OneBot: group history record failed (%s)", exc)
            return False

    def _note_drop(self) -> None:
        """Count a dropped row and log about it, rarely.

        Dropping the *newest* row (rather than evicting the oldest) is
        deliberate: the worker is draining oldest-first, so keeping the
        backlog contiguous means whatever does get written is a coherent
        stretch of the conversation rather than a shuffled sample.  A queue
        this full also means the writer has been stalled for well over an
        hour, at which point the digest is already compromised and preserving
        *some* ordering beats preserving the most recent lines.
        """
        with self._lock:
            self._dropped += 1
            count = self._dropped
        if count == 1 or count % _DROP_LOG_EVERY == 0:
            logger.warning(
                "OneBot: group history queue full (%d rows) — dropped %d "
                "message(s) so far; the archive is behind, the bot is not",
                self._config.queue_max,
                count,
            )

    # -- consumer side (worker thread) -----------------------------------

    def _run(self) -> None:
        """Batch-commit loop.  Owns ``self._conn`` for its whole lifetime."""
        pending: List[Row] = []
        deadline: Optional[float] = None
        try:
            while True:
                now = time.monotonic()
                wait = self._next_prune_at - now
                if deadline is not None:
                    wait = min(wait, deadline - now)
                try:
                    item = self._queue.get(timeout=max(0.0, wait))
                except queue.Empty:
                    item = None

                if isinstance(item, _Stop):
                    self._stop_requested = True
                    pending.extend(self._drain_nowait())
                elif isinstance(item, _Flush):
                    # ``_drain_nowait`` may swallow a _Stop that a racing
                    # ``close()`` pushed; it records that on the flag rather
                    # than dropping it, or the loop would never exit.
                    pending.extend(self._drain_nowait())
                    self._commit(pending)
                    pending.clear()
                    deadline = None
                    item.done.set()
                    if not self._stop_requested:
                        continue
                elif item is not None:
                    pending.append(item)
                    if deadline is None:
                        deadline = time.monotonic() + self._config.flush_secs

                now = time.monotonic()
                if pending and (
                    self._stop_requested
                    or len(pending) >= self._config.batch_rows
                    or (deadline is not None and now >= deadline)
                ):
                    self._commit(pending)
                    pending.clear()
                    deadline = None

                if self._stop_requested:
                    break
                if time.monotonic() >= self._next_prune_at:
                    self._prune()
                    self._next_prune_at = time.monotonic() + PRUNE_INTERVAL_SECS
                    self._report()
        except Exception as exc:  # noqa: BLE001 — a dead thread must be loud
            self._last_error = str(exc)
            logger.exception("OneBot: group history writer thread died (%s)", exc)
        finally:
            self._running = False
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
                self._conn = None

    def _drain_nowait(self) -> List[Row]:
        """Everything queued right now; control tokens consumed, not lost.

        A ``_Stop`` found here is remembered on :attr:`_stop_requested` rather
        than discarded — dropping it would leave the thread running forever
        after a ``flush()`` that raced a ``close()``.
        """
        out: List[Row] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return out
            if isinstance(item, _Stop):
                self._stop_requested = True
            elif isinstance(item, _Flush):
                item.done.set()
            else:
                out.append(item)

    def _ensure_conn(self) -> Optional[sqlite3.Connection]:
        if self._conn is not None:
            return self._conn
        try:
            self._conn = connect_store(self._config.db_path)
            self._conn_thread_ident = threading.get_ident()
        except (OSError, sqlite3.Error) as exc:
            self._last_error = str(exc)
            logger.warning(
                "OneBot: group history store unavailable (%s) — rows dropped", exc
            )
            return None
        return self._conn

    def _commit(self, rows: List[Row]) -> None:
        """One transaction for the whole batch.  Swallows every failure.

        A failed batch is discarded rather than retried: the rows are chat
        archive, the queue is bounded, and a retry loop against a broken store
        would spend the box's I/O budget re-failing.  The error is counted and
        logged (without content) so the loss is visible.
        """
        if not rows:
            return
        conn = self._ensure_conn()
        if conn is None:
            self._failed += len(rows)
            return
        try:
            with conn:  # BEGIN ... COMMIT, or ROLLBACK on exception
                conn.executemany(_INSERT_SQL, rows)
        except sqlite3.Error as exc:
            self._failed += len(rows)
            self._last_error = str(exc)
            logger.warning(
                "OneBot: group history batch of %d row(s) failed (%s)", len(rows), exc
            )
            # A connection that errored may be in an unusable state; drop it so
            # the next batch reopens rather than inheriting the problem.
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
            self._conn = None
            return
        self._written += len(rows)
        self._batches += 1

    def _prune(self) -> None:
        """Delete rows past the retention horizon.  DELETE only — no VACUUM.

        VACUUM rewrites the entire file; on a 2 vCPU box with one spindle's
        worth of I/O budget, paying that to reclaim ~20 MB is a bad trade.
        The freed pages get reused by tomorrow's inserts, so the file settles
        at its steady-state size instead of growing without bound.
        """
        conn = self._ensure_conn()
        if conn is None:
            return
        cutoff = int((time.time() - self._config.retention_days * 86400.0) * 1000)
        removed = 0
        try:
            for _ in range(PRUNE_MAX_CHUNKS):
                with conn:
                    cur = conn.execute(
                        "DELETE FROM group_messages WHERE id IN ("
                        "SELECT id FROM group_messages WHERE received_at_ms < ? "
                        "LIMIT ?)",
                        (cutoff, PRUNE_CHUNK_ROWS),
                    )
                    n = cur.rowcount or 0
                removed += n
                if n < PRUNE_CHUNK_ROWS:
                    break
        except sqlite3.Error as exc:
            self._last_error = str(exc)
            logger.warning("OneBot: group history prune failed (%s)", exc)
            return
        if removed:
            self._pruned += removed
            logger.info(
                "OneBot: group history pruned %d row(s) older than %.1f day(s)",
                removed,
                self._config.retention_days,
            )

    def _report(self) -> None:
        """Hourly heartbeat so "the archive quietly stopped" is detectable."""
        logger.info(
            "OneBot: group history — written=%d queued=%d dropped=%d failed=%d "
            "depth=%d",
            self._written,
            self._queued,
            self._dropped,
            self._failed,
            self._queue.qsize(),
        )
