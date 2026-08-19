"""Preflight checks for the migrated corlinman job set.

Every check answers one question that, left unasked, produces a silent wrong
answer rather than an error — the failure mode that cost corlinman 1803
no-op decay runs and would cost this migration a duplicate public post.

The checks are plain data (:class:`Check`) so the installer can refuse to
proceed, the dry-run planner can print them, and the tests can assert on
them without parsing text.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from .specs import ALL_SPECS, PERSONA_ID, TIMEZONE, JobSpec

#: Checks at this level block an install / enable.
FAIL = "fail"
#: Worth knowing, does not block.
WARN = "warn"
OK = "ok"


@dataclass(frozen=True)
class Check:
    """One preflight verdict."""

    key: str
    level: str
    message: str
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.level == FAIL


def _configured_timezone() -> tuple[Optional[str], str]:
    """The zone hermes cron will actually use, and where it came from.

    Mirrors ``hermes_time._resolve_timezone_name``'s order without importing
    the module's cache, so a test can vary the environment freely.
    """
    env = os.getenv("HERMES_TIMEZONE", "").strip()
    if env:
        return env, "HERMES_TIMEZONE"
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config() or {}
    except Exception:  # noqa: BLE001 — config is optional here
        cfg = {}
    value = cfg.get("timezone") if isinstance(cfg, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip(), "config.yaml: timezone"
    return None, "unset (hermes would use the host's local zone)"


def check_timezone(specs: tuple[JobSpec, ...] = ALL_SPECS) -> Check:
    """D8: the declared per-job zone must be the zone cron evaluates in.

    hermes cron has no per-job timezone — ``cron/jobs.py`` compares
    ``next_run_at`` against ``hermes_time.now()``, one process-wide clock. The
    specs all declare ``Asia/Shanghai``; the production host's local zone is
    ``Asia/Tokyo``. Installing without pinning the zone would shift every
    schedule by an hour with no error anywhere.
    """
    declared = sorted({spec.timezone for spec in specs})
    configured, source = _configured_timezone()
    if len(declared) != 1:
        return Check(
            "timezone",
            FAIL,
            f"jobs declare more than one timezone: {', '.join(declared)}",
            "hermes cron can only honour one; split them across profiles or align them",
        )
    want = declared[0]
    if configured is None:
        return Check(
            "timezone",
            FAIL,
            f"hermes has no timezone configured, but every job declares {want}",
            f"set HERMES_TIMEZONE={want} (or `timezone: {want}` in config.yaml); "
            f"currently {source}",
        )
    if configured != want:
        return Check(
            "timezone",
            FAIL,
            f"hermes timezone is {configured!r} but every job declares {want!r}",
            f"resolved from {source}; every schedule would fire at the wrong wall-clock time",
        )
    return Check("timezone", OK, f"hermes timezone is {configured} (from {source})")


def check_croniter() -> Check:
    """Cron expressions need croniter, which hermes already depends on."""
    try:
        import croniter  # noqa: F401, PLC0415
    except ImportError:
        return Check(
            "croniter",
            FAIL,
            "croniter is not importable; every cron-expression job would be rejected",
            "croniter is a core hermes dependency — the venv is incomplete",
        )
    return Check("croniter", OK, "croniter is importable")


def check_parallelism() -> Check:
    """P1: the stagger assumes ``cron.max_parallel_jobs`` stays small."""
    env = os.getenv("HERMES_CRON_MAX_PARALLEL", "").strip()
    configured: Any = None
    if env:
        configured, source = env, "HERMES_CRON_MAX_PARALLEL"
    else:
        try:
            from hermes_cli.config import read_raw_config

            cfg = read_raw_config() or {}
        except Exception:  # noqa: BLE001
            cfg = {}
        cron_cfg = cfg.get("cron") if isinstance(cfg, dict) else None
        if isinstance(cron_cfg, dict) and cron_cfg.get("max_parallel_jobs") is not None:
            configured, source = cron_cfg["max_parallel_jobs"], "config.yaml: cron.max_parallel_jobs"
    if configured is None:
        return Check(
            "max_parallel_jobs",
            WARN,
            "cron.max_parallel_jobs is unset — hermes will run jobs unbounded",
            "set it to 2; the stagger was designed around that ceiling and "
            "raising it is not the fix for SQLite contention (P1)",
        )
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return Check(
            "max_parallel_jobs",
            WARN,
            f"cron.max_parallel_jobs is not an integer: {configured!r}",
            source,
        )
    if value > 2:
        return Check(
            "max_parallel_jobs",
            WARN,
            f"cron.max_parallel_jobs is {value}; the stagger assumes 2",
            f"from {source}. Raising parallelism to paper over SQLite DELETE-mode "
            "contention is explicitly not the remedy (P1)",
        )
    return Check("max_parallel_jobs", OK, f"cron.max_parallel_jobs is {value} (from {source})")


def check_qzone_state() -> Check:
    """The QQ dedup ledgers must be present *before* any QQ job is enabled.

    ``plugins/qzone`` keys its idempotency off three sidecar files. If the
    production copies were not migrated, or the persona id resolves to
    ``default`` instead of ``grantley``, the ledgers read empty and the very
    first run re-replies to comments that were already answered — visible,
    public, and not undoable.
    """
    try:
        from plugins.qzone import state as qzone_state
    except Exception as exc:  # noqa: BLE001
        return Check(
            "qzone_state",
            FAIL,
            f"plugins.qzone.state is not importable: {exc}",
            "the QQ jobs cannot run without it",
        )

    persona = qzone_state.resolve_persona_id()
    root = qzone_state.state_root()
    details = [f"persona={persona}", f"root={root}"]

    if persona != PERSONA_ID:
        return Check(
            "qzone_state",
            FAIL,
            f"qzone persona resolves to {persona!r}, not {PERSONA_ID!r}",
            f"set QZONE_PERSONA_ID={PERSONA_ID}; otherwise the dedup ledgers are "
            "read from the wrong file and the first run repeats old replies. "
            + ", ".join(details),
        )

    posts = qzone_state.post_log_entries(persona)
    friends = qzone_state.friend_comment_seen(persona)
    seen = qzone_state.seen_comment_map(persona)
    details.extend(
        [
            f"post_log={len(posts)}",
            f"seen_comment_tids={len(seen)}",
            f"friend_comments={len(friends)}",
        ]
    )
    if not posts and not friends and not seen:
        return Check(
            "qzone_state",
            FAIL,
            "all three qzone ledgers are empty",
            "copy qzone_post_log/, qzone_seen_comments/ and qzone_friend_comments/ "
            "out of /opt/corlinman/execution-state/ first (C3 §8); production had "
            "19 / 2 / 37 entries. " + ", ".join(details),
        )
    return Check("qzone_state", OK, "qzone ledgers present: " + ", ".join(details))


def check_onebot() -> Check:
    """The QQ jobs borrow their session from a configured OneBot backend."""
    for var in ("ONEBOT_WS_URL", "ONEBOT_HTTP_URL"):
        if os.getenv(var, "").strip():
            return Check("onebot", OK, f"{var} is set")
    return Check(
        "onebot",
        FAIL,
        "neither ONEBOT_WS_URL nor ONEBOT_HTTP_URL is set",
        "without one the qzone tools are gated out of the model's schema and the "
        "QQ jobs would run with no tools at all",
    )


def check_telegram() -> Check:
    """D16: say which bot's token has to be in this profile.

    The destination was unreachable while the migration was being written
    and is reachable now (re-verified 2026-08-19: ``getChat`` ok, forum
    supergroup ``Corn Agents``, bot an administrator, topics 11/12/13/680 all
    valid) — but only for ``@sweetcornna2_bot`` (8720715962). corlinman's
    ``@Cornna_bot`` (5420007505) is still not a member of that chat. So this
    stays a WARN: a token is configured, and nothing here can tell *which*
    bot it belongs to.
    """
    from .specs import TELEGRAM_CHAT_ID

    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return Check(
            "telegram",
            WARN,
            "TELEGRAM_BOT_TOKEN is not set; the four Telegram jobs cannot deliver",
            f"content still generates; delivery to {TELEGRAM_CHAT_ID} fails",
        )
    return Check(
        "telegram",
        WARN,
        f"Telegram delivery to {TELEGRAM_CHAT_ID} needs @sweetcornna2_bot's token specifically",
        "that chat was re-verified reachable on 2026-08-19 (forum supergroup "
        "'Corn Agents', bot is an administrator, topics 11/12/13/680 valid) — but "
        "corlinman's @Cornna_bot is not a member of it and never was. Confirm the "
        "configured token is 8720715962's before enabling the four Telegram jobs",
    )


def check_qq_group_history() -> Check:
    """D2: the three QQ monitors' only data source.

    Unlike :func:`check_qzone_state`, an empty-but-reachable store is not a
    FAIL: a monitor with no messages in its window is a normal day (all
    three migrated monitors have ``send_when_empty=false`` — a quiet day
    means no digest, not an error), whereas an empty qzone ledger means
    "not migrated yet" and is unsafe to write dedup decisions against. A
    missing or wrong-schema store, on the other hand, means the monitors
    have no data at all and cannot possibly do their job.
    """
    from .installer import qq_group_history_db_path

    path = qq_group_history_db_path()
    if not path.is_file():
        return Check(
            "qq_group_history",
            FAIL,
            f"qq_group_history.sqlite not found at {path}",
            "sanhu/jlu/qunjlu have no other data source (A1 §4); point "
            "QQ_GROUP_HISTORY_DB at a migrated copy, or at corlinman's own "
            "live file during the coexistence window",
        )
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM group_messages").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check(
            "qq_group_history",
            FAIL,
            f"qq_group_history.sqlite at {path} is unreadable or has the wrong schema",
            f"{exc}; expected the group_messages table from "
            "corlinman_server.qq_group_history",
        )
    count = int(row[0]) if row else 0
    if count == 0:
        return Check(
            "qq_group_history",
            WARN,
            f"qq_group_history.sqlite at {path} is reachable but has 0 rows",
            "every monitor will see an empty window (and, with "
            "send_when_empty=false, produce no digest) until this store has "
            "rows — copy the migrated snapshot in, or point "
            "QQ_GROUP_HISTORY_DB at corlinman's live file",
        )
    return Check(
        "qq_group_history",
        OK,
        f"qq_group_history.sqlite reachable, {count} row(s)",
        str(path),
    )


def check_scripts_installed() -> Check:
    """Scripts must be present in ``$HERMES_HOME/scripts/`` and match the tree."""
    from .installer import script_drift

    drift = script_drift()
    missing = [name for name, status in drift.items() if status == "missing"]
    stale = [name for name, status in drift.items() if status == "stale"]
    if missing:
        return Check(
            "scripts",
            FAIL,
            f"{len(missing)} job script(s) not installed: {', '.join(sorted(missing))}",
            "run `install` to copy them into $HERMES_HOME/scripts/",
        )
    if stale:
        return Check(
            "scripts",
            WARN,
            f"{len(stale)} installed script(s) differ from the repository copy: "
            f"{', '.join(sorted(stale))}",
            "re-run `install` to refresh them",
        )
    return Check("scripts", OK, f"{len(drift)} job script(s) installed and current")


def run_checks(
    *,
    include_qq: bool = True,
    include_qzone: Optional[bool] = None,
    include_qq_history: Optional[bool] = None,
    include_scripts: bool = True,
) -> list[Check]:
    """All applicable checks, most important first.

    Three different QQ-shaped failure modes used to share one flag
    (``include_qq``): "no onebot connectivity", "qzone dedup ledgers not
    migrated" and — new for D2 — "the monitors' qq_group_history.sqlite is
    missing". They stayed coupled fine as long as every QQ job also used
    the qzone toolset; the three monitors don't (they carry no toolset at
    all — delivery is cron's own, not a tool call), so a plan selecting
    only e.g. ``sanhu`` must require onebot connectivity and the history
    store without also demanding the (irrelevant) qzone ledgers.

    ``include_qzone`` and ``include_qq_history`` default to mirroring
    ``include_qq`` when left unspecified, so every existing caller that only
    ever set ``include_qq`` keeps behaving exactly as before; the installer
    passes all three explicitly, computed per selected spec set (see
    ``installer.needs_qq`` / ``needs_qzone`` / ``needs_qq_history``).
    """
    if include_qzone is None:
        include_qzone = include_qq
    if include_qq_history is None:
        include_qq_history = include_qq
    checks = [check_timezone(), check_croniter(), check_parallelism()]
    if include_scripts:
        checks.append(check_scripts_installed())
    if include_qq:
        checks.append(check_onebot())
    if include_qzone:
        checks.append(check_qzone_state())
    if include_qq_history:
        checks.append(check_qq_group_history())
    checks.append(check_telegram())
    return checks


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.blocking]


__all__ = [
    "FAIL",
    "OK",
    "WARN",
    "Check",
    "blocking",
    "check_croniter",
    "check_onebot",
    "check_parallelism",
    "check_qq_group_history",
    "check_qzone_state",
    "check_scripts_installed",
    "check_telegram",
    "check_timezone",
    "run_checks",
]
