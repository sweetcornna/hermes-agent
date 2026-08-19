"""On-disk state for the QZone tools: what was posted, and what was answered.

Three sidecars, ported so that the files production is *already writing* stay
readable and writable without conversion. Losing or ignoring them causes
duplicate posts and duplicate replies on the first run after a cutover, on a
real person's real social feed, so the formats here are reproduced from the
originals rather than redesigned.

Layout — ``<state root>/<store>/[<qq instance>/]<persona id>.json``. The
``<qq instance>`` segment is present only when the instance is not the
literal ``"default"``, exactly as in the source; a single-account install
therefore lands on the unqualified path the production files already use.

The state root is :func:`plugins.plugin_storage.plugin_data_dir` — i.e.
``<hermes home>/plugin-data/qzone/`` — because the plugin *install*
directory is destroyed by ``hermes plugins remove/update``. Set
``QZONE_STATE_DIR`` to point at an already-populated directory instead
(that is the migration path for the existing ``/opt/corlinman/execution-state``
files).

Schemas
-------
``qzone_post_log/<persona>.json`` — publish history + anti-repeat corpus::

    {"version": 1,
     "posts": [{"ts": "2026-08-17T23:00:04+09:00",   # local ISO-8601, seconds
                "job": "hermes.qzone_daily",          # "" when interactive
                "tid": "1cbe3d3c72aa6c6a01750700",    # null when unknown
                "qzone_url": "https://user.qzone.qq.com/<uin>/mood/<tid>",
                "text": "<body, capped at 500 chars>",
                "outcome": "sent"},                   # ADDED — see below
               ...]}                                  # last 30, oldest first

``outcome`` is the one field this port adds. It is absent from the 19 real
production entries and absent from anything corlinman writes; readers must
treat a missing value as ``"sent"``, which is what those entries are. It
exists because of disagreement S17: a write that failed *in transport* may
still have landed, and a log that cannot say so lets a retry double-post.

``qzone_seen_comments/<persona>.json`` — reply dedup, keyed by 说说::

    {"version": 2,
     "seen": {"<tid>": ["<identity>:<unix ts>", ...]}}

``identity`` is ``id:<comment id>`` when the source comment had a stable id,
``sha256:<hex>`` of the comment body when it did not, and bare ``uin:<qq>``
in the oldest records. Capped at 200 identities per tid and 100 tids, both
rolling off least-recently-updated.

``qzone_friend_comments/<persona>.json`` — dedup for comments left on
*friends'* posts::

    {"version": 1, "seen": ["<friend uin>:<tid>", ...]}

A flat list, oldest first. This one has no writer anywhere in corlinman —
production's ``hermes.qzone_friends`` job predates it — so the format is
reproduced from the real 37-entry file on the box, and the 500-entry cap is
this port's choice.

Concurrency
-----------
Every mutation is a read-modify-write, and losing one costs a duplicate
public comment. Each store is therefore guarded by an ``flock``'d sidecar
(the pattern already used by ``tools/memory_tool.py`` and
``tools/skill_usage.py``) plus an in-process lock, and written with
``utils.atomic_json_write``. The originals used a bare tmp+rename, which is
crash-safe but not concurrent-safe; this is a deliberate upgrade, not a
format change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "OUTCOME_FAILED",
    "OUTCOME_SENT",
    "OUTCOME_UNKNOWN",
    "comment_identity",
    "friend_comment_seen",
    "is_recorded_comment",
    "mark_comment",
    "post_log_entries",
    "record_publish",
    "resolve_persona_id",
    "resolve_qq_instance_id",
    "state_root",
    "unknown_publish_guard",
    "valid_slug",
]

# Store directory names — identical to the production directories so an
# existing tree can be copied in as-is.
_POST_LOG_DIR = "qzone_post_log"
_SEEN_DIR = "qzone_seen_comments"
_FRIEND_DIR = "qzone_friend_comments"

_POST_LOG_VERSION = 1
_POST_LOG_MAX = 30
_POST_LOG_TEXT_CAP = 500

_SEEN_VERSION = 2
_SEEN_PER_TID_MAX = 200
_SEEN_TIDS_MAX = 100

_FRIEND_VERSION = 1
#: No upstream writer exists to copy a cap from; production sits at 37
#: entries. 500 keeps the file trivially small while covering years of the
#: current rate.
_FRIEND_MAX = 500

#: Terminal states of a write. ``unknown`` is the important one (S17): the
#: request failed before we saw a reply, so the post may be public.
OUTCOME_SENT = "sent"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"

#: How long an ``unknown`` publish blocks an identical re-publish. Long
#: enough to cover a daily job's retry window, short enough that a genuine
#: repeat a day later is still allowed.
UNKNOWN_PUBLISH_GUARD_SECS = 6 * 3600

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def valid_slug(value: str) -> bool:
    """True iff ``value`` is safe to splice into a path.

    Mirrors the source rule: stripping ``_`` and ``-`` must leave a non-empty
    ASCII alphanumeric run, so ``..``, ``/`` and ``\\`` are all rejected
    before the id reaches the filesystem.
    """
    if not value:
        return False
    stripped = value.replace("_", "").replace("-", "")
    return bool(stripped) and stripped.isascii() and stripped.isalnum() and bool(
        _SLUG_RE.match(value)
    )


def state_root() -> Path:
    """The directory holding the three sidecar stores."""
    override = os.getenv("QZONE_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("qzone")


def resolve_persona_id(explicit: Optional[str] = None) -> str:
    """Which persona's sidecars to use.

    Explicit argument, then ``QZONE_PERSONA_ID``, then ``"default"``. The
    production files are keyed ``grantley``; that value is supplied by the
    job, not guessed here.
    """
    for candidate in (explicit, os.getenv("QZONE_PERSONA_ID")):
        text = (candidate or "").strip()
        if text:
            return text
    return "default"


def resolve_qq_instance_id() -> str:
    """The account namespace. ``"default"`` means an unqualified path."""
    return (os.getenv("QZONE_QQ_INSTANCE_ID", "").strip() or "default")


def _store_path(store: str, persona_id: str, instance_id: str) -> Optional[Path]:
    if not valid_slug(persona_id) or not valid_slug(instance_id):
        logger.debug("qzone: refusing unsafe sidecar slug %r/%r", instance_id, persona_id)
        return None
    root = state_root() / store
    if instance_id == "default":
        return root / f"{persona_id}.json"
    return root / instance_id / f"{persona_id}.json"


# ---------------------------------------------------------------------------
# Locked read-modify-write
# ---------------------------------------------------------------------------


def _process_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


@contextmanager
def _exclusive(path: Path):
    """Hold both an in-process and a cross-process lock for ``path``.

    The cross-process half is a sidecar ``.lock`` file so the data file
    itself stays free to be replaced atomically. Degrades to the in-process
    lock alone where neither ``fcntl`` nor ``msvcrt`` exists.
    """
    with _process_lock(path):
        lock_path = path.with_suffix(path.suffix + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            yield
            return
        if fcntl is None and msvcrt is None:  # pragma: no cover - exotic platform
            yield
            return
        try:
            handle = open(lock_path, "a+", encoding="utf-8")
        except OSError:  # pragma: no cover - unwritable state dir
            yield
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            handle.close()


def _read_json(path: Path) -> Dict[str, Any]:
    """Read a sidecar. A missing or corrupt file reads as empty.

    Total by design: a malformed sidecar must degrade the dedup guarantee,
    never block the job that depends on it.
    """
    try:
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("qzone: ignoring unreadable sidecar %s", path)
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(path: Path, payload: Dict[str, Any]) -> bool:
    try:
        from utils import atomic_json_write

        atomic_json_write(path, payload, indent=2)
    except Exception as exc:  # noqa: BLE001 — state is best-effort
        logger.warning("qzone: could not write sidecar %s (%s)", path, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Post log
# ---------------------------------------------------------------------------


def _read_posts(path: Path) -> List[Dict[str, Any]]:
    raw = _read_json(path)
    posts = raw.get("posts")
    if not isinstance(posts, list):
        return []
    return [p for p in posts if isinstance(p, dict)]


def post_log_entries(
    persona_id: Optional[str] = None,
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Recent publish records, oldest first. Empty on any problem."""
    path = _store_path(_POST_LOG_DIR, resolve_persona_id(persona_id), resolve_qq_instance_id())
    if path is None:
        return []
    posts = _read_posts(path)
    return posts[-limit:] if limit else posts


def record_publish(
    *,
    persona_id: Optional[str],
    text: str,
    tid: Optional[str],
    qzone_url: Optional[str],
    outcome: str,
    job: str = "",
) -> None:
    """Append one publish record, keeping the last 30.

    Called for ``sent`` *and* ``unknown`` outcomes. Recording an ``unknown``
    is the whole point of S17: the body is in the anti-repeat corpus (so
    tomorrow's post does not repeat it) and :func:`unknown_publish_guard`
    can see that an identical retry is unsafe.
    """
    path = _store_path(_POST_LOG_DIR, resolve_persona_id(persona_id), resolve_qq_instance_id())
    if path is None:
        return
    entry = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "job": job or "",
        "tid": tid,
        "qzone_url": qzone_url,
        "text": (text or "")[:_POST_LOG_TEXT_CAP],
        "outcome": outcome,
    }
    with _exclusive(path):
        posts = _read_posts(path)
        posts.append(entry)
        _write_json(
            path, {"version": _POST_LOG_VERSION, "posts": posts[-_POST_LOG_MAX:]}
        )


def unknown_publish_guard(
    text: str,
    persona_id: Optional[str] = None,
    *,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Return the blocking record when re-publishing ``text`` is unsafe.

    An earlier attempt with the *same body* failed in transport, so it may
    already be public. Re-sending would put the same 说说 on a real feed
    twice — the exact failure mode a cron retry produces. Returns ``None``
    when there is nothing to worry about.

    Only identical text within :data:`UNKNOWN_PUBLISH_GUARD_SECS` blocks; a
    genuinely new post is never held up by a stale unknown.
    """
    candidate = (text or "")[:_POST_LOG_TEXT_CAP]
    if not candidate:
        return None
    cutoff = (now if now is not None else time.time()) - UNKNOWN_PUBLISH_GUARD_SECS
    for entry in reversed(post_log_entries(persona_id)):
        if entry.get("outcome") != OUTCOME_UNKNOWN:
            continue
        if (entry.get("text") or "") != candidate:
            continue
        if _entry_epoch(entry) < cutoff:
            continue
        return entry
    return None


def _entry_epoch(entry: Dict[str, Any]) -> float:
    """Best-effort timestamp of a post-log entry. Unparseable reads as now.

    Treating a broken timestamp as "just happened" keeps the guard on the
    safe side: an unreadable clock must not silently unblock a re-publish.
    """
    raw = entry.get("ts")
    if not isinstance(raw, str) or not raw:
        return time.time()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return time.time()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# Comment ledgers
# ---------------------------------------------------------------------------


def comment_identity(
    *, reply_to_comment_id: str = "", reply_to_comment_content: str = ""
) -> str:
    """The stable identity of the comment being answered.

    ``id:<comment id>`` when the feed gave one, otherwise a digest of the
    comment body, otherwise ``""`` (a top-level comment, which dedups by
    post rather than by comment). Ported verbatim: changing the derivation
    would invalidate every identity already on disk.
    """
    if reply_to_comment_id:
        return f"id:{reply_to_comment_id}"
    if reply_to_comment_content:
        digest = hashlib.sha256(reply_to_comment_content.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"
    return ""


def _read_seen_map(path: Path) -> Dict[str, List[str]]:
    raw = _read_json(path)
    seen = raw.get("seen")
    if not isinstance(seen, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for tid, entries in seen.items():
        if not isinstance(tid, str) or not isinstance(entries, list):
            continue
        clean = [e for e in entries if isinstance(e, str) and e]
        if clean:
            out[tid] = clean
    return out


def _identity_of(entry: str) -> str:
    """Strip the trailing ``:<unix ts>`` from a stored record."""
    identity = entry.rsplit(":", 1)[0]
    if identity.startswith(("id:", "sha256:", "uin:")):
        return identity
    return f"uin:{identity}"


def _read_friend_list(path: Path) -> List[str]:
    raw = _read_json(path)
    seen = raw.get("seen")
    if not isinstance(seen, list):
        return []
    return [e for e in seen if isinstance(e, str) and e]


def _friend_key(owner_uin: str, tid: str) -> str:
    return f"{owner_uin}:{tid}"


def is_recorded_comment(
    *,
    owner_uin: str,
    tid: str,
    identity: str,
    actor_uin: str,
    persona_id: Optional[str] = None,
) -> bool:
    """Whether this exact comment was already written (or possibly written).

    Routes to the same ledger the writer will use: replies on our own posts
    go to ``qzone_seen_comments`` keyed by ``(tid, identity)``; comments on a
    friend's post go to ``qzone_friend_comments`` keyed by ``(owner, tid)``,
    which is coarser on purpose — the friends job leaves at most one comment
    per post.
    """
    persona = resolve_persona_id(persona_id)
    instance = resolve_qq_instance_id()
    if owner_uin == actor_uin:
        path = _store_path(_SEEN_DIR, persona, instance)
        if path is None or not identity:
            return False
        return any(_identity_of(e) == identity for e in _read_seen_map(path).get(tid, []))
    path = _store_path(_FRIEND_DIR, persona, instance)
    if path is None:
        return False
    return _friend_key(owner_uin, tid) in set(_read_friend_list(path))


def mark_comment(
    *,
    owner_uin: str,
    tid: str,
    identity: str,
    actor_uin: str,
    persona_id: Optional[str] = None,
) -> None:
    """Record that this comment was written — or may have been.

    Callers invoke this for ``sent`` **and** ``unknown`` outcomes and skip it
    for ``failed``. That asymmetry is S17 in one line: QZone rejecting the
    comment means it definitely is not public (retry freely), while a
    transport error means nobody knows (never retry automatically).
    """
    persona = resolve_persona_id(persona_id)
    instance = resolve_qq_instance_id()
    if owner_uin == actor_uin:
        _mark_own_post_reply(persona, instance, tid=tid, identity=identity)
    else:
        _mark_friend_comment(persona, instance, owner_uin=owner_uin, tid=tid)


def _mark_own_post_reply(persona: str, instance: str, *, tid: str, identity: str) -> None:
    path = _store_path(_SEEN_DIR, persona, instance)
    if path is None or not tid or not identity:
        return
    with _exclusive(path):
        seen = _read_seen_map(path)
        # Re-inserting at the tail makes the tid cap roll off the
        # least-recently-updated post rather than an arbitrary one.
        entries = seen.pop(tid, [])
        if not any(_identity_of(e) == identity for e in entries):
            entries.append(f"{identity}:{int(time.time())}")
        seen[tid] = entries[-_SEEN_PER_TID_MAX:]
        if len(seen) > _SEEN_TIDS_MAX:
            for stale in list(seen.keys())[: len(seen) - _SEEN_TIDS_MAX]:
                seen.pop(stale, None)
        _write_json(path, {"version": _SEEN_VERSION, "seen": seen})


def _mark_friend_comment(persona: str, instance: str, *, owner_uin: str, tid: str) -> None:
    path = _store_path(_FRIEND_DIR, persona, instance)
    if path is None or not owner_uin or not tid:
        return
    key = _friend_key(owner_uin, tid)
    with _exclusive(path):
        entries = _read_friend_list(path)
        if key in entries:
            return
        entries.append(key)
        _write_json(path, {"version": _FRIEND_VERSION, "seen": entries[-_FRIEND_MAX:]})


def friend_comment_seen(persona_id: Optional[str] = None) -> List[str]:
    """The raw ``"<uin>:<tid>"`` markers, oldest first."""
    path = _store_path(_FRIEND_DIR, resolve_persona_id(persona_id), resolve_qq_instance_id())
    return _read_friend_list(path) if path is not None else []


def seen_comment_map(persona_id: Optional[str] = None) -> Dict[str, List[str]]:
    """The reply ledger as ``{tid: ["<identity>:<ts>", ...]}``."""
    path = _store_path(_SEEN_DIR, resolve_persona_id(persona_id), resolve_qq_instance_id())
    return _read_seen_map(path) if path is not None else {}


def store_paths(persona_id: Optional[str] = None) -> Dict[str, Optional[Path]]:
    """Resolved sidecar paths — for diagnostics and the migration task."""
    persona = resolve_persona_id(persona_id)
    instance = resolve_qq_instance_id()
    return {
        "post_log": _store_path(_POST_LOG_DIR, persona, instance),
        "seen_comments": _store_path(_SEEN_DIR, persona, instance),
        "friend_comments": _store_path(_FRIEND_DIR, persona, instance),
    }
