"""One-shot, idempotent import of corlinman's QQ group history into ours.

Why
---
:mod:`~plugins.platforms.onebot.group_history` starts archiving from the
moment it is switched on.  The three migrated monitors, however, summarise a
**24-hour** window, and ``sanhu``/``jlu`` want that window populated on the
very first morning after cutover.  Without this tool the switchover day looks
like an outage: ``QQ_GROUP_HISTORY_DB`` moves onto a file that only holds the
few hours since the writer started, every digest comes back nearly empty, and
``send_when_empty=false`` makes that *silent* (00-PLAN.md §21, D46-⑥).

So: copy corlinman's rows across, then flip the reader.

Idempotency
-----------
The tool must be safe to run repeatedly — before cutover to seed, again at
cutover to catch up, once more if something needed a retry — without ever
producing a duplicate row.  Duplicates are not cosmetic here: they inflate
every count the digest prompt reports and waste the 1,000-line prompt cap on
repeats.

Dedup is by **message identity**, not by row identity:

* ``(instance_id, group_id, message_id)`` when the source row carries a
  message id — this is the QQ-assigned id, so a message captured *both* by
  corlinman and by our own live writer during the overlap collapses to one
  row even though the two writers stamped different ``received_at_ms``.
  Keying on the timestamp instead would silently double every message in the
  coexistence overlap, which is precisely the window this tool exists for.
* ``(instance_id, group_id, sender_user_id, event_time_ms, blake2b(text))``
  for the rare row with a NULL/blank ``message_id``, so those are deduped too
  rather than being re-inserted on every run.

The destination's existing keys are read into a set before inserting.  That is
bounded by retention (7 days ~ 115k rows ~ tens of MB in a short-lived CLI
process), and it keeps the check exact without adding an index to — and
therefore changing the schema of — a file whose schema compatibility with
corlinman's is the whole point.

``received_at_ms`` and ``event_time_ms`` are copied **verbatim**.  The
monitors window on ``received_at_ms``, so re-stamping imported rows with the
import time would collapse a week of history onto one instant.

Usage
-----
::

    python -m plugins.platforms.onebot.group_history_backfill \\
        --source /opt/corlinman/execution-state/qq_group_history.sqlite \\
        --dest   "$HERMES_HOME/plugin-data/corlinman_jobs/qq_group_history.sqlite" \\
        --days 7

``--dest`` defaults to whatever :func:`group_history.default_db_path` resolves
(i.e. ``ONEBOT_GROUP_HISTORY_DB``, else the shared default path).  The source
is opened ``mode=ro`` and is never written — it may well be corlinman's own
live file, which this process must not touch.

Privacy: like the writer, this prints counts only.  No message text, no sender
names, on stdout or in any log line.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

if __package__ in (None, ""):  # pragma: no cover — direct `python <path>` run
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from plugins.platforms.onebot.group_history import (  # noqa: E402
        TEXT_CAP,
        connect_store,
        default_db_path,
    )
else:
    from .group_history import TEXT_CAP, connect_store, default_db_path

__all__ = [
    "BackfillResult",
    "backfill",
    "dedup_key",
    "main",
]

#: Rows per INSERT transaction.  Large enough that a 50k-row import is a few
#: dozen commits, small enough that one transaction never holds the write lock
#: (DELETE mode blocks readers) for long.
INSERT_BATCH = 1000

#: Rows per source fetch.  Bounds peak memory independently of table size.
SCAN_BATCH = 5000

_UNIT = "\x1f"  # ASCII unit separator: cannot occur in an id or a digest


def dedup_key(
    instance_id: str,
    group_id: str,
    message_id: Optional[str],
    sender_user_id: str,
    event_time_ms: int,
    text: str,
) -> str:
    """Message identity, stable across writers.  See the module docstring."""
    mid = (message_id or "").strip()
    if mid:
        return _UNIT.join(("m", instance_id, group_id, mid))
    digest = hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=16).hexdigest()
    return _UNIT.join(
        ("h", instance_id, group_id, sender_user_id, str(event_time_ms), digest)
    )


class BackfillResult:
    """Counts from one run.  Deliberately not a dataclass of the rows."""

    __slots__ = ("scanned", "inserted", "duplicates", "filtered", "blank")

    def __init__(self) -> None:
        self.scanned = 0
        self.inserted = 0
        self.duplicates = 0
        self.filtered = 0
        self.blank = 0

    def as_dict(self) -> Dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"BackfillResult({self.as_dict()})"


def _existing_keys(conn: sqlite3.Connection) -> Set[str]:
    """Every message identity already in the destination."""
    keys: Set[str] = set()
    cur = conn.execute(
        "SELECT instance_id, group_id, message_id, sender_user_id, "
        "event_time_ms, text FROM group_messages"
    )
    while True:
        rows = cur.fetchmany(SCAN_BATCH)
        if not rows:
            break
        for instance_id, group_id, message_id, sender_user_id, event_time_ms, text in rows:
            keys.add(
                dedup_key(
                    str(instance_id),
                    str(group_id),
                    message_id,
                    str(sender_user_id),
                    int(event_time_ms),
                    str(text),
                )
            )
    cur.close()
    return keys


def _iter_source(
    conn: sqlite3.Connection,
    *,
    since_ms: Optional[int],
    instance_id: Optional[str],
    groups: Sequence[str],
) -> Iterator[Tuple[Any, ...]]:
    sql = (
        "SELECT instance_id, group_id, sender_user_id, sender_name, message_id, "
        "event_time_ms, received_at_ms, text FROM group_messages"
    )
    where: List[str] = []
    params: List[Any] = []
    if since_ms is not None:
        where.append("received_at_ms >= ?")
        params.append(int(since_ms))
    if instance_id:
        where.append("instance_id = ?")
        params.append(str(instance_id))
    if groups:
        where.append(f"group_id IN ({','.join('?' * len(groups))})")
        params.extend(str(g) for g in groups)
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Ascending id: the destination's own AUTOINCREMENT then preserves the
    # source's arrival order, which is what a digest reads back.
    sql += " ORDER BY id ASC"
    cur = conn.execute(sql, params)
    while True:
        rows = cur.fetchmany(SCAN_BATCH)
        if not rows:
            break
        for row in rows:
            yield tuple(row)
    cur.close()


def backfill(
    source: Path,
    dest: Path,
    *,
    since_ms: Optional[int] = None,
    instance_id: Optional[str] = None,
    groups: Sequence[str] = (),
    dry_run: bool = False,
) -> BackfillResult:
    """Import ``source``'s rows into ``dest``, skipping ones already there.

    ``source`` is opened read-only; ``dest`` is created with the shared schema
    if it does not exist yet.  Returns the counts; raises only for a source
    that cannot be opened or read, which is an operator error worth surfacing
    (unlike the live writer, this runs interactively).
    """
    result = BackfillResult()
    if not source.is_file():
        raise SystemExit(f"backfill: source not found: {source}")
    if source.resolve() == dest.resolve():
        raise SystemExit("backfill: --source and --dest are the same file")

    dest_conn = connect_store(dest)
    try:
        src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        dest_conn.close()
        raise SystemExit(f"backfill: cannot open source read-only: {exc}") from exc

    try:
        src_conn.execute("PRAGMA busy_timeout = 30000")
        seen = _existing_keys(dest_conn)
        batch: List[Tuple[Any, ...]] = []
        wanted = {str(g) for g in groups}

        for row in _iter_source(
            src_conn, since_ms=since_ms, instance_id=instance_id, groups=list(wanted)
        ):
            result.scanned += 1
            (
                r_instance,
                r_group,
                r_sender_id,
                r_sender_name,
                r_message_id,
                r_event_ms,
                r_received_ms,
                r_text,
            ) = row
            r_instance = str(r_instance)
            r_group = str(r_group)
            if wanted and r_group not in wanted:  # pragma: no cover — SQL filters
                result.filtered += 1
                continue
            body = (str(r_text) if r_text is not None else "").strip()
            if not body:
                # The live writer never stores a blank row; importing one would
                # make the two stores disagree about what a message even is.
                result.blank += 1
                continue
            body = body[:TEXT_CAP]
            key = dedup_key(
                r_instance,
                r_group,
                r_message_id,
                str(r_sender_id),
                int(r_event_ms),
                body,
            )
            if key in seen:
                result.duplicates += 1
                continue
            # Added before the INSERT lands so duplicates *within* the source
            # collapse too, and so a re-run of an interrupted import stays
            # exact rather than merely close.
            seen.add(key)
            batch.append(
                (
                    r_instance,
                    r_group,
                    str(r_sender_id),
                    str(r_sender_name or ""),
                    None if r_message_id is None else str(r_message_id),
                    int(r_event_ms),
                    int(r_received_ms),
                    body,
                )
            )
            if len(batch) >= INSERT_BATCH:
                result.inserted += _flush(dest_conn, batch, dry_run)
                batch.clear()
        result.inserted += _flush(dest_conn, batch, dry_run)
    finally:
        src_conn.close()
        dest_conn.close()
    return result


def _flush(conn: sqlite3.Connection, batch: List[Tuple[Any, ...]], dry_run: bool) -> int:
    if not batch:
        return 0
    if dry_run:
        return len(batch)
    with conn:
        conn.executemany(
            "INSERT INTO group_messages (instance_id, group_id, sender_user_id, "
            "sender_name, message_id, event_time_ms, received_at_ms, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
    return len(batch)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="group_history_backfill",
        description=(
            "Idempotently import corlinman's qq_group_history.sqlite rows into "
            "the archive this gateway writes, so the three QQ monitors do not "
            "see an empty 24h window on cutover day."
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        help="corlinman's qq_group_history.sqlite (opened read-only, never written)",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="this gateway's archive (default: ONEBOT_GROUP_HISTORY_DB, else the shared default path)",
    )
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="only import rows received in the last N days (0 = everything). Default 7, matching retention.",
    )
    parser.add_argument(
        "--instance-id",
        default=None,
        help="only import rows for this instance_id (default: every instance in the source)",
    )
    parser.add_argument(
        "--groups",
        default="",
        help="comma-separated group ids to import (default: every group in the source, which by construction is corlinman's monitored set)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be imported without writing anything",
    )
    args = parser.parse_args(argv)

    import time as _time

    since_ms = None if args.days <= 0 else int((_time.time() - args.days * 86400.0) * 1000)
    dest = Path(args.dest).expanduser() if args.dest else default_db_path()
    groups = [g.strip() for g in str(args.groups).replace(";", ",").split(",") if g.strip()]

    result = backfill(
        Path(args.source).expanduser(),
        dest,
        since_ms=since_ms,
        instance_id=args.instance_id,
        groups=groups,
        dry_run=bool(args.dry_run),
    )
    print(f"source     : {args.source}")
    print(f"dest       : {dest}{' (DRY RUN — nothing written)' if args.dry_run else ''}")
    print(f"window     : {'all rows' if since_ms is None else f'last {args.days:g} day(s)'}")
    print(f"scanned    : {result.scanned}")
    print(f"inserted   : {result.inserted}")
    print(f"duplicates : {result.duplicates}   (already present — re-runs are safe)")
    print(f"blank      : {result.blank}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
