#!/usr/bin/env python3
"""Job-side logic for the migrated corlinman scheduler jobs.

This file is **copied verbatim** into ``$HERMES_HOME/scripts/`` by
``plugins.corlinman_jobs.installer`` — hermes only runs cron scripts that live
inside that directory (``cron/scheduler.py::_run_job_script`` resolves and
containment-checks every path against it). The per-job entry scripts installed
next to it are three lines each: import this module and call one ``main_*``
function with the job's parameter bag baked in.

Two consequences shape the code:

* **Standard library only, and no imports from the hermes tree** — except
  ``yaml`` (a base hermes dependency) and, for one function, an explicitly
  passed repository root. A copy in ``$HERMES_HOME/scripts/`` has no package
  context.
* **stdout is the product.** For an agent job the text lands in the prompt
  under ``## Script Output``; for a ``no_agent`` job it *is* the delivered
  message. Diagnostics therefore go to stderr, never stdout.

One hermes behaviour to keep in mind while reading: a script that prints
nothing makes the scheduler skip the model call and the delivery entirely
(``cron/scheduler.py``: "Script produced no output — nothing to report, skip
AI call"), while a script that fails does **not** abort the run — its stderr
is injected as a ``## Script Error`` block and the agent runs anyway. Both are
used deliberately below.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Ported constants — byte-identical to corlinman_private_jobs/personal.py
# ---------------------------------------------------------------------------

MAX_MESSAGE_CHARS = 6000
MAX_TOTAL_CHARS = 180_000
NOISE_PREFIXES = ("[CONTEXT COMPACTION", "--- END OF CONTEXT SUMMARY")

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*[^\s,;，；]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{20,}"),
)

ANALYSIS_KEYWORDS = (
    "分析",
    "研究",
    "策略",
    "结论",
    "证据",
    "风险",
    "调研",
    "方案",
    "analysis",
    "research",
    "strategy",
)

#: ``hermes_state``'s sentinel for JSON-encoded (multimodal) message content.
CONTENT_JSON_PREFIX = "\x00json:"

#: Printed by the analysis job's script when the keyword filter matches
#: nothing. The prompt keys off this exact line to emit the source's fixed
#: "no analysis" sentence.
NO_ANALYSIS_MARKER = "（过去 24 小时没有命中任何分析/研究/策略记录。）"

#: Printed by the diary job's script when the day yielded no user messages.
#: Verbatim from ``personal.py::_diary_summary_action``.
NO_DIARY_MATERIAL = "（今天没有采集到用户消息。）"


def log(message: str) -> None:
    """Diagnostics go to stderr — stdout is the job's payload."""
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# hermes locations
# ---------------------------------------------------------------------------


def hermes_home() -> Path:
    """The active profile home.

    ``cron/scheduler.py`` builds the script's environment through
    ``tools.environments.local.build_subprocess_env``, which propagates
    ``HERMES_HOME`` (that is the documented contract of the scrub path), so
    reading the variable here resolves the same profile the scheduler ran
    under. The fallback matches ``hermes_constants.get_hermes_home``'s default
    and only fires when a human runs the script by hand.
    """
    raw = os.environ.get("HERMES_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".hermes"


def job_state_dir() -> Path:
    """Durable per-job state, outside any install directory.

    Mirrors ``plugins/plugin_storage.plugin_data_dir("corlinman_jobs")``
    without importing it (this file has no package context once installed).
    """
    path = hermes_home() / "plugin-data" / "corlinman_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def job_record(job_name: str) -> Optional[dict]:
    """Read one job out of ``$HERMES_HOME/cron/jobs.json``. Read-only.

    Used by the watermark job to find its own id and its previous run's
    outcome. Writes to that file go through ``cron.jobs`` only — never from
    here.
    """
    raw = _read_json(hermes_home() / "cron" / "jobs.json")
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    if not isinstance(jobs, list):
        return None
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == job_name:
            return job
    return None


def latest_output(job_id: str) -> Optional[tuple[str, str]]:
    """Newest saved output for a job: ``(filename, text)`` or None.

    hermes writes every run's result to
    ``$HERMES_HOME/cron/output/<job_id>/<timestamp>.md``. Filenames are
    timestamps, so lexical order is chronological.
    """
    directory = hermes_home() / "cron" / "output" / job_id
    try:
        files = sorted(p for p in directory.iterdir() if p.is_file())
    except OSError:
        return None
    if not files:
        return None
    newest = files[-1]
    try:
        return newest.name, newest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def previous_run_delivered(job: Optional[dict]) -> bool:
    """True when the job's last run both succeeded and delivered.

    The source persisted its watermark only on
    ``delivery.ok and not shadow``. hermes records the same two facts
    separately: ``last_status`` covers the agent run and
    ``last_delivery_error`` covers the send, so a job whose content was
    perfect but whose Telegram send failed does **not** advance the
    watermark — which is exactly the current production situation.
    """
    if not isinstance(job, dict):
        return False
    return job.get("last_status") == "ok" and not job.get("last_delivery_error")


# ---------------------------------------------------------------------------
# Text handling — ported from corlinman_private_jobs/personal.py
# ---------------------------------------------------------------------------


def redact(text: str) -> str:
    """Strip API keys / tokens / passwords. Ported verbatim."""
    out = text or ""
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)(api"):
            out = pattern.sub(lambda match: match.group(1) + "=[REDACTED]", out)
        elif pattern.pattern.startswith("(?i)(bearer"):
            out = pattern.sub(lambda match: match.group(1) + "[REDACTED]", out)
        else:
            out = pattern.sub("[REDACTED]", out)
    return out


def decode_content(raw: Any) -> str:
    """Flatten a ``messages.content`` cell to plain text.

    hermes stores multimodal turns as a sentinel-prefixed JSON list
    (``hermes_state._encode_content``); everything else is a plain string.
    """
    if not isinstance(raw, str):
        return "" if raw is None else str(raw)
    if not raw.startswith(CONTENT_JSON_PREFIX):
        return raw
    try:
        decoded = json.loads(raw[len(CONTENT_JSON_PREFIX):])
    except ValueError:
        return raw
    if isinstance(decoded, list):
        parts = [
            str(part.get("text") or "")
            for part in decoded
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return " ".join(p for p in parts if p).strip()
    if isinstance(decoded, str):
        return decoded
    return ""


# ---------------------------------------------------------------------------
# Journal material (hermes.diary_summary / hermes.analysis_digest)
# ---------------------------------------------------------------------------


def _state_db() -> Path:
    return hermes_home() / "state.db"


def query_messages(
    *,
    start_epoch: float,
    end_epoch: float,
    roles: Sequence[str],
    channels: Optional[Sequence[str]],
    user_id: Optional[str],
    limit: int,
) -> list[dict]:
    """Read user/assistant turns out of hermes's own conversation store.

    This replaces corlinman's ``app_state.corlinman_journal.query_messages``.
    The mapping is direct: ``sessions.source`` is the channel,
    ``sessions.user_id`` the user, ``messages.timestamp`` an epoch float.

    Read-only, and opened ``mode=ro`` with a generous ``busy_timeout``: the
    target host's SQLite is 3.40.1, so hermes runs the store in DELETE mode
    where a concurrent writer holds an exclusive lock across its fsync (P1).
    """
    db = _state_db()
    if not db.is_file():
        raise SystemExit(f"hermes state database not found: {db}")

    role_list = [r for r in roles if r]
    if not role_list:
        return []
    where = [
        "m.role IN (%s)" % ",".join("?" * len(role_list)),
        "m.active = 1",
        "(m.display_kind IS NULL OR m.display_kind = '')",
        "m.timestamp >= ?",
        "m.timestamp <= ?",
    ]
    args: list[Any] = [*role_list, start_epoch, end_epoch]
    if channels:
        where.append("s.source IN (%s)" % ",".join("?" * len(channels)))
        args.extend(channels)
    if user_id:
        where.append("s.user_id = ?")
        args.append(str(user_id))
    args.append(int(limit))

    sql = (
        "SELECT m.timestamp AS ts, m.role AS role, m.content AS content "
        "FROM messages m JOIN sessions s ON s.id = m.session_id "
        "WHERE " + " AND ".join(where) + " ORDER BY m.timestamp ASC LIMIT ?"
    )
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def main_diary_material(
    *,
    user_id: Optional[str],
    channels: Sequence[str],
    timezone: str,
    limit: int = 10000,
    now: Optional[datetime] = None,
) -> int:
    """Emit today's user messages as diary material. Ports ``_diary_summary_action``.

    Everything up to the model call is reproduced: same window (midnight →
    now in the declared zone), same role/channel/user filters, same noise
    prefixes, same redaction, same per-message and total character caps, same
    ``【HH:MM】`` framing, same de-duplication on ``(timestamp, text)``.
    """
    tz = ZoneInfo(timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)

    rows = query_messages(
        start_epoch=start.timestamp(),
        end_epoch=moment.timestamp(),
        roles=["user"],
        channels=list(channels) if channels else None,
        user_id=user_id,
        limit=limit,
    )

    material: list[str] = []
    seen: set[tuple[float, str]] = set()
    total = 0
    for row in rows:
        text = decode_content(row.get("content")).strip()
        if not text or text.startswith(NOISE_PREFIXES):
            continue
        text = redact(text)
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "\n…（单条消息过长，已截断）"
        stamp = float(row.get("ts") or 0.0)
        key = (stamp, text)
        if key in seen:
            continue
        seen.add(key)
        if total + len(text) > MAX_TOTAL_CHARS:
            break
        total += len(text)
        at = datetime.fromtimestamp(stamp, tz).strftime("%H:%M")
        material.append(f"【{at}】{text}")

    print(f"日期：{moment:%Y-%m-%d}（{timezone}）")
    print(f"采集到用户消息：{len(material)} 条")
    print()
    print("材料：")
    print("\n\n".join(material) if material else NO_DIARY_MATERIAL)
    return 0


def main_analysis_material(
    *,
    user_id: Optional[str],
    channels: Sequence[str],
    timezone: str,
    hours: int = 24,
    limit: int = 4000,
    now: Optional[datetime] = None,
) -> int:
    """Emit the keyword-filtered 24h analysis material. Ports ``_analysis_digest_action``.

    The source ran the keyword filter in Python and skipped the model call
    entirely when nothing matched. Here the filter still runs in Python, but
    the no-match case prints :data:`NO_ANALYSIS_MARKER` rather than nothing,
    because a silent script would make hermes skip delivery too — and the
    source *did* deliver its fixed "no analysis" sentence.
    """
    tz = ZoneInfo(timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    start = moment - timedelta(hours=hours)

    rows = query_messages(
        start_epoch=start.timestamp(),
        end_epoch=moment.timestamp(),
        roles=["user", "assistant"],
        channels=list(channels) if channels else None,
        user_id=user_id,
        limit=limit,
    )

    selected: list[str] = []
    total = 0
    for row in rows:
        text = redact(decode_content(row.get("content")).strip())
        if not text or not any(word in text.casefold() for word in ANALYSIS_KEYWORDS):
            continue
        text = text[:MAX_MESSAGE_CHARS]
        if total + len(text) > MAX_TOTAL_CHARS:
            break
        total += len(text)
        selected.append(f"[{row.get('role', '')}] {text}")

    print(f"窗口：{start:%Y-%m-%d %H:%M} → {moment:%Y-%m-%d %H:%M}（{timezone}）")
    if not selected:
        print(NO_ANALYSIS_MARKER)
        return 0
    print(f"命中记录：{len(selected)} 条")
    print()
    print("\n\n".join(selected))
    return 0


# ---------------------------------------------------------------------------
# YouTube watermark (hermes.youtube_daily)
# ---------------------------------------------------------------------------

#: Per-item id line the prompt asks the model to emit.
VIDEO_ID_LINE_RE = re.compile(r"^\s*视频ID[：:]\s*([A-Za-z0-9_-]{11})\s*$", re.MULTILINE)
#: Legacy trailer from the source implementation, still accepted.
YOUTUBE_STATE_MARKER = "YOUTUBE_STATE:"

WATERMARK_CAP = 1000
WATERMARK_PROMPT_WINDOW = 200


def extract_video_ids(text: str) -> list[str]:
    """Pull the video ids out of one run's output, order-preserving.

    Accepts both shapes: the per-item ``视频ID：<id>`` lines the migrated
    prompt asks for, and the source's trailing
    ``YOUTUBE_STATE:{"new_video_ids": [...]}`` line if some model still emits
    one.
    """
    found: list[str] = []
    for match in VIDEO_ID_LINE_RE.finditer(text or ""):
        found.append(match.group(1))
    if YOUTUBE_STATE_MARKER in (text or ""):
        _, tail = text.rsplit(YOUTUBE_STATE_MARKER, 1)
        first_line = tail.strip().splitlines()[0] if tail.strip() else ""
        try:
            parsed = json.loads(first_line)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            values = parsed.get("new_video_ids")
            if isinstance(values, list):
                found.extend(str(v) for v in values if isinstance(v, str) and v)
    return list(dict.fromkeys(found))


def _watermark_path(state_file: str) -> Path:
    """Resolve the watermark file, refusing to escape the state directory."""
    rel = Path(state_file)
    if rel.is_absolute() or ".." in rel.parts:
        raise SystemExit(f"invalid state_file {state_file!r}")
    return job_state_dir() / rel


def main_youtube_state(
    *,
    job_name: str,
    channels: Sequence[str],
    state_file: str,
) -> int:
    """Harvest the previous run's ids, then emit this run's context block.

    corlinman persisted the watermark inline, right after a successful
    delivery. hermes cron has no post-run hook, so the persistence moves to
    the *front* of the next run: read the previous run's saved output, take
    its video ids, and merge them — but only when that run actually
    delivered (:func:`previous_run_delivered`), which is the same condition
    the source used. Each output file is harvested at most once, recorded by
    filename, so a manual re-run cannot double-count or re-harvest.
    """
    path = _watermark_path(state_file)
    state = _read_json(path)
    if not isinstance(state, dict):
        state = {}
    seen: list[str] = [v for v in state.get("seen_video_ids", []) if isinstance(v, str)]
    harvested_from = state.get("harvested_output")

    job = job_record(job_name)
    job_id = str(job.get("id")) if isinstance(job, dict) and job.get("id") else ""
    if job_id:
        previous = latest_output(job_id)
        if previous is None:
            log(f"{job_name}: no previous output to harvest")
        elif previous[0] == harvested_from:
            log(f"{job_name}: previous output {previous[0]} already harvested")
        elif not previous_run_delivered(job):
            log(
                f"{job_name}: previous run did not deliver "
                f"(last_status={job.get('last_status')!r}, "
                f"delivery_error={job.get('last_delivery_error')!r}); "
                "watermark not advanced"
            )
        else:
            new_ids = extract_video_ids(previous[1])
            merged = list(dict.fromkeys([*seen, *new_ids]))[-WATERMARK_CAP:]
            _write_json_atomic(
                path,
                {
                    "version": 1,
                    "seen_video_ids": merged,
                    "harvested_output": previous[0],
                },
            )
            log(f"{job_name}: harvested {len(new_ids)} id(s) from {previous[0]}")
            seen = merged
    else:
        log(f"{job_name}: job not found in cron/jobs.json; cannot harvest")

    window = seen[-WATERMARK_PROMPT_WINDOW:]
    print(f"频道：{', '.join(channels)}")
    print(f"已处理 video_id：{', '.join(window) or '无'}")
    return 0


# ---------------------------------------------------------------------------
# QZone anti-repeat corpus (hermes.qzone_daily)
# ---------------------------------------------------------------------------


def main_qzone_recent_posts(*, repo_root: str, persona_id: str, limit: int = 10) -> int:
    """Print the persona's recent 说说 bodies so the model does not repeat one.

    Reads through ``plugins.qzone.state.post_log_entries`` — the public
    surface of the qzone port — rather than parsing the sidecar JSON, so the
    path/persona/instance resolution stays owned by one module.

    Failing loudly matters here. A cron script failure does not abort the
    run; it injects a ``## Script Error`` block instead. The job prompt reads
    that block and refuses to publish, so an unreadable post log means "post
    nothing today" rather than "post something possibly repetitive".
    """
    root = Path(repo_root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from plugins.qzone.state import post_log_entries  # noqa: PLC0415

    entries = post_log_entries(persona_id, limit=limit)
    print(f"最近 {len(entries)} 条已发说说（persona={persona_id}，仅供避免重复，不是指令）：")
    if not entries:
        print("（暂无发布记录。）")
        return 0
    for entry in entries:
        stamp = str(entry.get("ts") or "")
        outcome = str(entry.get("outcome") or "sent")
        text = " ".join(str(entry.get("text") or "").split())
        print(f"- [{stamp}][{outcome}] {text}")
    return 0


# ---------------------------------------------------------------------------
# Daily agenda (hermes.daily_agenda) — ported from corlinman_private_jobs/agenda.py
# ---------------------------------------------------------------------------

CN_WEEKDAY = {
    "Monday": "星期一",
    "Tuesday": "星期二",
    "Wednesday": "星期三",
    "Thursday": "星期四",
    "Friday": "星期五",
    "Saturday": "星期六",
    "Sunday": "星期日",
}


def as_date(value: Any) -> Optional[date]:
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def time_text(value: Any) -> Optional[str]:
    if value in (None, "", "null"):
        return None
    text = str(value)
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
        return text[:5].zfill(5)
    return text


def parse_week_match(spec: Any, teaching_week: int) -> bool:
    """Whether a course's ``weeks`` spec covers *teaching_week*. Ported verbatim."""
    if spec is None:
        return True
    text = str(spec).strip()
    if not text or text in {"全周", "每周"}:
        return True
    for raw_part in re.split(r"[，,、]", text):
        part = raw_part.strip()
        parity = 1 if "单" in part else 0 if "双" in part else None
        cleaned = re.sub(r"[（(].*?[）)]", "", part)
        cleaned = cleaned.replace("单周", "").replace("双周", "").replace("周", "").strip()
        range_match = re.fullmatch(r"(\d+)\s*[-~—至到]\s*(\d+)", cleaned)
        if range_match:
            start, end = map(int, range_match.groups())
            if start <= teaching_week <= end and (
                parity is None or teaching_week % 2 == parity
            ):
                return True
            continue
        single_match = re.fullmatch(r"(\d+)", cleaned)
        if single_match and int(single_match.group(1)) == teaching_week and (
            parity is None or teaching_week % 2 == parity
        ):
            return True
    return False


def daily_items(today: date, data: dict) -> tuple[list, list, list, int]:
    """Courses / tasks / exams for *today* plus the teaching week. Ported verbatim."""
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    semester_start = as_date(settings.get("semester_start_date"))
    teaching_week = (
        ((today - semester_start).days // 7) + 1
        if semester_start is not None and today >= semester_start
        else 0
    )
    weekday = today.strftime("%A")
    courses = [
        dict(item)
        for item in data.get("courses", [])
        if isinstance(item, dict)
        and item.get("weekday") == weekday
        and teaching_week > 0
        and parse_week_match(item.get("weeks"), teaching_week)
    ]
    courses.sort(
        key=lambda item: (
            time_text(item.get("start_time")) or "99:99",
            str(item.get("name") or ""),
        )
    )
    tasks = [
        dict(item)
        for item in data.get("tasks", [])
        if isinstance(item, dict)
        and str(item.get("status") or "pending").lower()
        not in {"done", "completed", "cancelled", "canceled"}
        and as_date(item.get("date")) == today
    ]
    tasks.sort(
        key=lambda item: (
            time_text(item.get("start_time")) or "99:99",
            str(item.get("title") or ""),
        )
    )
    horizon = today + timedelta(days=7)
    exams = [
        dict(item)
        for item in data.get("exams", [])
        if isinstance(item, dict)
        and str(item.get("status") or "").lower() not in {"cancelled", "canceled"}
        and (exam_date := as_date(item.get("date"))) is not None
        and today <= exam_date <= horizon
    ]
    exams.sort(
        key=lambda item: (
            as_date(item.get("date")) or date.max,
            time_text(item.get("start_time")) or "99:99",
        )
    )
    return courses, tasks, exams, teaching_week


def text_agenda(
    today: date,
    courses: list,
    tasks: list,
    exams: list,
    teaching_week: int,
) -> str:
    """Render the agenda card as text. Ported verbatim."""
    lines = [
        f"## 今日课表与日程｜{today.month}月{today.day}日 {CN_WEEKDAY[today.strftime('%A')]}",
        f"第 {teaching_week} 教学周" if teaching_week > 0 else "当前不在已配置学期内。",
        "\n### 今日课程",
    ]
    if courses:
        lines.extend(
            f"- {time_text(item.get('start_time')) or '待确认'}"
            f"-{time_text(item.get('end_time')) or '待确认'}"
            f"｜{item.get('name') or '未命名课程'}"
            f"｜{item.get('location') or '待确认'}"
            for item in courses
        )
    else:
        lines.append("- 今天暂无课程安排。")
    lines.append("\n### 今日任务")
    lines.extend(f"- {item.get('title') or '未命名任务'}" for item in tasks)
    if not tasks:
        lines.append("- 暂无已登记任务。")
    lines.append("\n### 近期考试")
    lines.extend(
        f"- {(as_date(item.get('date')) or today):%m月%d日}｜{item.get('name') or '未命名考试'}"
        for item in exams
    )
    if not exams:
        lines.append("- 近 7 天暂无已登记考试。")
    lines.append("\n课程与考试请以教务或任课教师最终通知为准。")
    return "\n".join(lines)


def render_svg(text: str, path: Path) -> None:
    """Write the agenda card as SVG. Ported verbatim."""
    escaped = [html.escape(line) for line in text.splitlines() if line.strip()]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1240" height="1754">',
        '<rect width="100%" height="100%" fill="#F7F1E8"/>',
        '<style>text{font-family:"Noto Sans CJK SC","Noto Sans SC",sans-serif;'
        'fill:#302820;font-size:28px} .title{font-size:48px;font-weight:700}</style>',
    ]
    y = 90
    for index, line in enumerate(escaped):
        cls = ' class="title"' if index == 0 else ""
        parts.append(f'<text x="70" y="{y}"{cls}>{line}</text>')
        y += 58 if index == 0 else 43
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def main_daily_agenda(
    *,
    agenda_path: str,
    timezone: str,
    render_card: bool = True,
    today: Optional[date] = None,
) -> int:
    """Render today's timetable. ``no_agent`` job — stdout IS the message.

    The source delivered a PNG card with a short caption when
    ``rsvg-convert`` was available and fell back to the text agenda
    otherwise. Same here, expressed in hermes's terms: a ``MEDIA:<abs path>``
    tag makes ``cron/scheduler.py`` send the file as a native attachment and
    deliver the remaining text as the caption.
    """
    rel = Path(agenda_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise SystemExit(f"invalid agenda_path {agenda_path!r}")
    path = job_state_dir() / rel
    if not path.is_file():
        raise SystemExit(f"agenda_data_missing: {path}")

    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - yaml is a base dependency
        raise SystemExit(f"agenda_data_unreadable: {exc}") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"agenda_data_invalid: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise SystemExit("agenda_data_invalid")

    day = today if today is not None else datetime.now(ZoneInfo(timezone)).date()
    courses, tasks, exams, teaching_week = daily_items(day, data)
    text = text_agenda(day, courses, tasks, exams, teaching_week)

    png = None
    if render_card:
        converter = shutil.which("rsvg-convert")
        if converter is not None:
            cache = job_state_dir() / "scheduler_cache" / "agenda"
            svg = cache / f"daily_agenda_{day.isoformat()}.svg"
            candidate = cache / f"daily_agenda_{day.isoformat()}.png"
            try:
                render_svg(text, svg)
                subprocess.run(
                    [converter, "-w", "1240", "-h", "1754", "-o", str(candidate), str(svg)],
                    check=True,
                    timeout=60,
                )
                png = candidate if candidate.is_file() else None
            except (OSError, subprocess.SubprocessError) as exc:
                log(f"agenda card render failed, falling back to text: {exc}")
                png = None
        else:
            log("rsvg-convert not on PATH; delivering the text agenda")

    if png is not None:
        print(f"MEDIA:{png}")
        print(f"{day:%Y-%m-%d} 今日课表与日程")
    else:
        print(text)
    return 0


# ---------------------------------------------------------------------------
# QQ group monitor digests (sanhu / jlu / qunjlu)
#
# Ported from corlinman-channels/src/corlinman_channels/service.py's
# ``_qq_monitor_run_once`` (the fetch step) and ``_qq_monitor_compose_prompt``
# (the header + chat-log rendering — the single-turn path). Read directly
# off the production checkout over a read-only SSH session during this task;
# not from an exported file, since these are private module functions never
# handed to this migration in any document.
# ---------------------------------------------------------------------------

#: Per-line text cap inside the digest. Verbatim from
#: service.py:_QQ_MONITOR_LINE_CAP.
QQ_MONITOR_LINE_CAP = 300

#: Per-monitor DB fetch ceiling — newest rows win when a window holds more.
#:
#: The source set this to 10_000 (service.py:_QQ_MONITOR_FETCH_CAP). That
#: number is **below a real day** for the busiest migrated monitor: group
#: 980927602 carries 15k-20k messages per 24h window, so a literal 10_000
#: silently clipped the oldest ~45% of the day *before* any summarisation
#: strategy got a say. Since D47's whole purpose is whole-day coverage,
#: keeping the source's value would have capped the achievable coverage at
#: ~55% no matter how good the reduction below is.
#:
#: 40_000 is ~2.2x the busiest 24h window observed in the real export
#: (20,780 rows on 2026-08-18) and is still a pure safety valve. Measured
#: cost of the raise on the real snapshot: a 18,136-row window materialises
#: at 9.0 MB and a full 40_000-row fetch extrapolates to ~20 MB — against
#: this host's MemoryHigh=384M, and the rows are dropped again as soon as
#: the reduction below has classified them.
QQ_MONITOR_FETCH_CAP = 40_000

#: How many chat lines the ONE agent turn is allowed to carry.
#:
#: The source split anything over 1,000 messages into 1,000-message chunks,
#: summarised each chunk with a PARALLEL chat turn and merged the partial
#: summaries with one more turn (service.py:_qq_monitor_summarize). D47
#: rules that path out for this host: it multiplies the model calls per run
#: by N, and 00-PLAN.md §18 proved every upstream request that fails burns
#: the (very tight) account pool. So this port keeps **exactly one** model
#: call and instead spends pure CPU up front — see
#: :func:`_qq_monitor_prereduce` — to make a whole day fit inside it.
#:
#: 1500 rather than the old newest-1000 truncation, because:
#:
#: * **It is cheaper than what it replaces.** Dropping image/sticker CQ
#:   blobs (each rendered up to QQ_MONITOR_LINE_CAP chars of URL) more than
#:   pays for the extra 500 lines. Measured on three real ``sanhu`` windows,
#:   the rendered chat log shrinks 84,479 / 89,113 / 84,495 chars against
#:   the old newest-1000 log's 94,935 / 105,522 / 93,699 — 10-16% *fewer*
#:   prompt characters while covering 24 hours instead of 5-8.
#: * It is ~1 line per minute of a 1440-minute window, i.e. >=60 lines per
#:   hourly bucket — enough for the model to write a per-topic paragraph
#:   for every part of the day.
#: * It sits above a whole post-noise day for the two smaller monitors
#:   (``jlu``'s group runs ~1.4k/day), so those two stay effectively
#:   lossless and only ``sanhu`` actually samples.
#:
#: Overridable per run — see :func:`_qq_monitor_budget`.
QQ_MONITOR_DIGEST_BUDGET = 1500

#: Env var an operator can set to retune the budget without a code change.
QQ_MONITOR_BUDGET_ENV = "QQ_MONITOR_DIGEST_BUDGET"

#: Clamp applied to any budget from any source. The floor keeps a digest
#: from degenerating into a handful of lines; the ceiling keeps a mis-set
#: env var from posting a multi-megabyte prompt at the tight account pool.
QQ_MONITOR_BUDGET_MIN = 50
QQ_MONITOR_BUDGET_MAX = 20_000

#: Time-bucket width for the coverage guarantee. One hour over the 1440-min
#: window all three monitors use gives 24 buckets — coarse enough that a
#: bucket still holds a conversation, fine enough that "the whole day is
#: represented" is a real guarantee rather than a hope.
QQ_MONITOR_BUCKET_MINUTES = 60

#: No single sender may take more than this share of one bucket's quota
#: (floor of 1 message). Real measurement of why: in the export's busiest
#: hour one account sent 272 of 1,119 messages (24%).
QQ_MONITOR_SENDER_BUCKET_SHARE = 0.2

#: Share of a bucket's quota reserved for the "depth" pass — the
#: highest-scoring messages regardless of how many senders are still
#: unrepresented. 0.3 measured against the real export; see
#: :func:`_qq_monitor_pick_bucket` for the numbers.
QQ_MONITOR_BUCKET_DEPTH_SHARE = 0.3

#: Messages whose *content* (letters/digits/CJK, after stripping CQ codes,
#: punctuation and emoji) is this short or shorter are filler ("哦", "好").
QQ_MONITOR_FILLER_MAX_CHARS = 1

#: A normalised text repeated at least this many times in one window is a
#: copypasta/复读 meme. Its FIRST occurrence is scored up as a topic anchor;
#: the later ones are dropped as ``echo``.
QQ_MONITOR_ANCHOR_REPEATS = 3

#: How many "who talked most" entries the annotation carries. Per-sender
#: capping deliberately flattens the loudest voices in the sampled log, so
#: the raw ranking is restated in the header or the digest would lose the
#: fact that one account dominated the room.
QQ_MONITOR_TOP_TALKERS = 5

#: CQ segment -> short human label. A digest reader gains nothing from 300
#: chars of CDN URL, but "someone posted an image" is real information, so
#: the segment collapses to a marker instead of vanishing.
QQ_MONITOR_CQ_LABELS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "bface": "[表情]",
    "sface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[转发]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "share": "[分享]",
    "music": "[音乐]",
    "reply": "[回复]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "redbag": "[红包]",
    "contact": "[名片]",
    "location": "[位置]",
}

_QQ_CQ_RE = re.compile(r"\[CQ:([A-Za-z]+)((?:,[^\]]*)?)\]")
_QQ_AT_QQ_RE = re.compile(r"(?:^|,)qq=([^,\]]+)")
_QQ_WS_RE = re.compile(r"\s+")


def _qq_monitor_window_desc(window_minutes: int) -> str:
    """Ported verbatim from service.py:_qq_monitor_window_desc."""
    if window_minutes % 1440 == 0:
        days = window_minutes // 1440
        return f"最近 {days} 天"
    if window_minutes % 60 == 0:
        return f"最近 {window_minutes // 60} 小时"
    return f"最近 {window_minutes} 分钟"


def _qq_monitor_collection_ids(
    watch_user_ids: Sequence[str], focus_user_ids: Sequence[str]
) -> tuple[str, ...]:
    """Ported verbatim from service.py:_QqMonitorSource.collection_ids.

    Empty return means "no sender filter — everyone", not "filter to
    nobody": the source only narrows by sender when watch_user_ids is
    non-empty. focus_user_ids alone never narrows the query — a focus
    member is always collected, even when watch_user_ids narrows the scope
    to someone else entirely; here it only controls the ★ markers below.
    """
    if not watch_user_ids:
        return ()
    return tuple(dict.fromkeys((*watch_user_ids, *focus_user_ids)))


def _qq_monitor_query(
    db_path: str,
    *,
    instance_id: str,
    group_id: str,
    since_ms: int,
    until_ms: int,
    sender_ids: Sequence[str],
    limit: int,
) -> list[tuple[int, str, str, int, str]]:
    """Newest *limit* rows in ``[since_ms, until_ms)``, returned oldest-first.

    Mirrors ``QqGroupHistory.list_window``'s SQL and its "when capped, keep
    the newest rows" contract exactly
    (corlinman_server/qq_group_history.py). Opened ``mode=ro``: this
    database is corlinman's own capture store (or a migrated copy of it)
    and is never written from here — this port has no equivalent of the
    dispatch loop that populates ``group_messages`` in the first place, see
    the D2 port notes.
    """
    path = Path(db_path)
    if not path.is_file():
        raise SystemExit(f"qq_group_history_unavailable: {path}")
    sql = (
        "SELECT received_at_ms, sender_user_id, sender_name, event_time_ms, text "
        "FROM group_messages "
        "WHERE instance_id = ? AND group_id = ? "
        "AND received_at_ms >= ? AND received_at_ms < ?"
    )
    params: list[Any] = [instance_id, group_id, int(since_ms), int(until_ms)]
    senders = [str(s) for s in sender_ids if str(s).strip()]
    if senders:
        sql += f" AND sender_user_id IN ({','.join('?' * len(senders))})"
        params.extend(senders)
    sql += " ORDER BY received_at_ms DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"qq_group_history_query_failed: {exc}") from exc
    finally:
        conn.close()
    return list(reversed(rows))


def _qq_monitor_format_lines(
    rows: Sequence[tuple[int, str, str, int, str]],
    focus_user_ids: Sequence[str],
    tz: ZoneInfo,
) -> list[str]:
    """Render store rows as ``[MM-DD HH:MM] name(id): text`` prompt lines.

    Ported from service.py:_qq_monitor_format_lines. ★-prefixes the focus
    members' lines, identically to the source.
    """
    focus = set(focus_user_ids)
    lines: list[str] = []
    for received_at_ms, sender_id, sender_name, _event_time_ms, text in rows:
        stamp = datetime.fromtimestamp(received_at_ms / 1000.0, tz).strftime("%m-%d %H:%M")
        name = str(sender_name or "")
        sid = str(sender_id or "")
        who = f"{name}({sid})" if name and sid else (name or sid)
        marker = "★" if sid in focus else ""
        lines.append(f"{marker}[{stamp}] {who}: {str(text)[:QQ_MONITOR_LINE_CAP]}")
    return lines


# --- deterministic pre-reduction (D47) -------------------------------------
#
# Why this exists: `sanhu`'s source group carries ~15k-20k messages a day and
# one agent turn cannot hold them. The source solved that with a parallel
# map-reduce (N model calls); D47 rules that out for this host — the upstream
# account pool is tight and every failed request burns it (00-PLAN.md §18),
# and the box is 2 vCPU / 1.9 GB with MemoryHigh=384M. So the whole reduction
# happens in **pure Python, zero model calls**, and the run still makes
# exactly one.
#
# Everything below is deterministic: same rows in, same rows out. No RNG, no
# hashing, no set iteration order, no wall clock. Every ordering is total —
# ties break on the message's position in the (already deterministic) query
# result. That is what makes the digest reproducible and the behaviour
# testable.


def _qq_monitor_plain_text(text: Any) -> str:
    """Message text with every CQ segment removed and whitespace collapsed.

    This is the *classification* view: what a human would call the words in
    the message. An image-only message plains to ``""``.
    """
    return _QQ_WS_RE.sub(" ", _QQ_CQ_RE.sub(" ", str(text or ""))).strip()


def _qq_monitor_display_text(text: Any) -> str:
    """Message text as it should appear in the digest.

    CQ segments collapse to a short label (``[图片]``) instead of the 100-300
    chars of CDN URL they actually carry, and newlines collapse so that one
    message stays one line — the chat-log rendering below is line-oriented
    and a multi-line message used to silently break that contract.
    """

    def _label(match: "re.Match[str]") -> str:
        kind = match.group(1).lower()
        if kind == "at":
            who = _QQ_AT_QQ_RE.search(match.group(2) or "")
            target = html.unescape(who.group(1)).strip() if who else ""
            return f" @{target} " if target else " [@] "
        return " " + QQ_MONITOR_CQ_LABELS.get(kind, f"[{kind}]") + " "

    return _QQ_WS_RE.sub(" ", _QQ_CQ_RE.sub(_label, str(text or ""))).strip()


def _qq_monitor_per_mille(share: float) -> int:
    """``share`` as an integer per-mille, so every quota is integer maths."""
    return max(0, min(1000, int(round(float(share) * 1000))))


def _qq_monitor_content_chars(text: str) -> str:
    """Just the letters/digits/CJK — punctuation, emoji and spaces removed.

    ``unicodedata.category`` puts emoji in ``So`` and every flavour of
    punctuation in ``P*``, so this one rule covers "？", "。。。", "😭😭😭"
    and kaomoji alike without a hand-maintained character list.
    """
    return "".join(ch for ch in text if unicodedata.category(ch)[0] not in "PSZC")


class _QqMonitorMessage(NamedTuple):
    """One candidate line, pre-classified."""

    index: int
    """Position in the query result — the total, deterministic tie-breaker."""
    received_at_ms: int
    sender_id: str
    sender_name: str
    event_time_ms: int
    display: str
    plain: str
    weight: int
    """Content-character count: the information proxy used for ranking."""
    focus: bool


class _QqMonitorReduction(NamedTuple):
    """Result of one pre-reduction pass, plus everything the digest must own up to."""

    rows: list[tuple[int, str, str, int, str]]
    total: int
    kept: int
    focus_kept: int
    dropped: "dict[str, int]"
    top_talkers: list[tuple[str, str, int]]
    buckets: int
    budget: int
    fetch_capped: bool


#: Drop reasons, in the order the annotation lists them.
QQ_MONITOR_DROP_LABELS = (
    ("media", "图片/表情等无文字内容"),
    ("symbol", "纯符号或颜文字"),
    ("filler", "单字灌水"),
    ("echo", "重复刷屏"),
    ("quota", "时段配额外未抽中"),
)


def _qq_monitor_budget(budget: Optional[int] = None) -> int:
    """Resolve the line budget: explicit argument > env var > module default.

    Always clamped to [QQ_MONITOR_BUDGET_MIN, QQ_MONITOR_BUDGET_MAX]; a
    garbage env value is reported on stderr and ignored rather than crashing
    a scheduled run.
    """
    raw: Any = budget
    if raw is None:
        env = os.environ.get(QQ_MONITOR_BUDGET_ENV, "").strip()
        if env:
            try:
                raw = int(env)
            except ValueError:
                log(f"{QQ_MONITOR_BUDGET_ENV}={env!r} is not an integer — using default")
                raw = None
    if raw is None:
        raw = QQ_MONITOR_DIGEST_BUDGET
    value = int(raw)
    return max(QQ_MONITOR_BUDGET_MIN, min(QQ_MONITOR_BUDGET_MAX, value))


def _qq_monitor_allocate(sizes: Sequence[int], budget: int) -> list[int]:
    """Split *budget* across time buckets holding *sizes* candidates each.

    Half egalitarian, half proportional, and nothing else:

    * every non-empty bucket first gets ``min(size, budget // (2 * buckets))``
      — this is the whole-day coverage guarantee. Without it a proportional
      split gives a 27-message hour ~2 lines and the digest silently loses
      the quiet parts of the day;
    * the rest is handed out in proportion to each bucket's *remaining*
      capacity, largest-remainder, ties broken by bucket index.

    Never allocates more than a bucket holds; returns exactly ``min(budget,
    sum(sizes))`` in total.
    """
    n = len(sizes)
    if n == 0:
        return []
    if budget <= 0:
        return [0] * n
    if budget >= sum(sizes):
        return [int(s) for s in sizes]
    floor = max(1, budget // (2 * n))
    quota = [min(int(sizes[i]), floor) for i in range(n)]
    left = budget - sum(quota)
    # Bounded loop rather than `while left`: each pass hands out at least one
    # line, so n + 2 passes is already unreachable — the bound only exists so
    # a future edit cannot turn a scheduled job into a spin.
    for _ in range(n + 2):
        if left <= 0:
            break
        spare = [int(sizes[i]) - quota[i] for i in range(n)]
        total_spare = sum(spare)
        if total_spare <= 0:
            break
        add = [min(spare[i], left * spare[i] // total_spare) for i in range(n)]
        rem = left - sum(add)
        if rem > 0:
            order = sorted(
                range(n), key=lambda i: (-((left * spare[i]) % total_spare), i)
            )
            for i in order:
                if rem <= 0:
                    break
                if add[i] < spare[i]:
                    add[i] += 1
                    rem -= 1
        given = sum(add)
        if given <= 0:
            break
        for i in range(n):
            quota[i] += add[i]
        left -= given
    return quota


def _qq_monitor_pick_bucket(
    bucket: Sequence[_QqMonitorMessage],
    quota: int,
    *,
    scores: "dict[int, int]",
    sender_share: float = QQ_MONITOR_SENDER_BUCKET_SHARE,
    depth_share: float = QQ_MONITOR_BUCKET_DEPTH_SHARE,
) -> list[_QqMonitorMessage]:
    """Choose *quota* of one bucket's messages: breadth first, then depth.

    1. **Breadth** takes ``quota * (1 - depth_share)`` lines, one per sender,
       each sender's single best message. This is the anti-flood guarantee
       the brief asks for: it is what keeps a 470-sender day from collapsing
       onto the ten loudest accounts.
    2. **Depth** spends the reserved remainder on the highest-scoring
       messages still unpicked — including the best message of any sender
       breadth never reached — subject to a per-sender ceiling of
       ``ceil(quota * sender_share)``.

    Why depth needs a *reserved* share rather than just the leftovers:
    measured on the real export's busiest day (2026-08-18, 20,780 rows), a
    pure breadth pass never got past round one in any busy bucket, so a
    member who made three substantive points that hour contributed exactly
    one and the digest kept only 61% of the day's >=40-char messages. A 30%
    depth reserve lifts that to 97% (and 88% -> 100% on 2026-08-19) while
    still keeping 284 of the day's 526 senders — roughly three times what
    the newest-1000 truncation this replaces managed (98).

    Senders are ordered by their best message's score, so when a bucket has
    more senders than quota the substantive voices are the ones that make
    it in. Every comparison ends in ``index``, so the result is a pure
    function of the input.
    """
    if quota >= len(bucket):
        return list(bucket)
    if quota <= 0:
        return []
    by_sender: "dict[str, list[_QqMonitorMessage]]" = {}
    for msg in bucket:
        by_sender.setdefault(msg.sender_id, []).append(msg)
    for msgs in by_sender.values():
        msgs.sort(key=lambda m: (-scores[m.index], m.index))
    senders = sorted(
        by_sender,
        key=lambda s: (-scores[by_sender[s][0].index], by_sender[s][0].index),
    )
    # ceil()/floor() in integer arithmetic — no float rounding anywhere on
    # the deterministic path.
    cap = max(1, -(-quota * _qq_monitor_per_mille(sender_share) // 1000))
    breadth_limit = quota - (quota * _qq_monitor_per_mille(depth_share) // 1000)

    taken: list[_QqMonitorMessage] = []
    used: "dict[str, int]" = {}
    for sender in senders:
        if len(taken) >= breadth_limit:
            break
        taken.append(by_sender[sender][0])
        used[sender] = 1
    if len(taken) < quota:
        rest = [
            msg
            for sender in senders
            for msg in by_sender[sender][1 if sender in used else 0 :]
        ]
        rest.sort(key=lambda m: (-scores[m.index], m.index))
        for msg in rest:
            if len(taken) >= quota:
                break
            if used.get(msg.sender_id, 0) >= cap:
                continue
            taken.append(msg)
            used[msg.sender_id] = used.get(msg.sender_id, 0) + 1
    return taken


def _qq_monitor_prereduce(
    rows: Sequence[tuple[int, str, str, int, str]],
    *,
    focus_user_ids: Sequence[str],
    since_ms: int,
    budget: int,
    bucket_minutes: int = QQ_MONITOR_BUCKET_MINUTES,
    sender_share: float = QQ_MONITOR_SENDER_BUCKET_SHARE,
    depth_share: float = QQ_MONITOR_BUCKET_DEPTH_SHARE,
    filler_max_chars: int = QQ_MONITOR_FILLER_MAX_CHARS,
    fetch_capped: bool = False,
) -> _QqMonitorReduction:
    """Compress a whole window down to *budget* lines, deterministically.

    Order of operations, and the reason for each:

    1. **Focus members are lifted out first and are never subject to
       anything below.** ``focus_user_ids`` is ``jlu``'s entire mechanism —
       the ★ lines and the per-member closing section the prompt asks for.
       A reduction that could drop them would quietly break that monitor, so
       they bypass the noise filter, the dedup, the buckets and the quota.
    2. **Zero-content drops** (``media``/``symbol``/``filler``). An
       image-only message renders as 300 chars of CDN URL and contributes
       nothing to a text digest; "？" and "😭" the same. Measured on the real
       export: 3,140 of 18,136 rows in one ``sanhu`` day are media-only.
    3. **Echo dedup.** Identical normalised text keeps its FIRST occurrence
       only. In the real export this is copypasta — one joke pasted 27 times
       — not 27 people making 27 points.
    4. **Hourly buckets + quota** so every part of the day is represented,
       then per-bucket selection (see :func:`_qq_monitor_pick_bucket`).

    Returns the surviving rows in the same shape and chronological order the
    query produced, with the display text substituted, plus the counters the
    digest header has to own up to.
    """
    focus = {str(u) for u in focus_user_ids if str(u).strip()}
    dropped = {key: 0 for key, _label in QQ_MONITOR_DROP_LABELS}
    total = len(rows)
    talkers: "dict[str, list[Any]]" = {}
    candidates: list[_QqMonitorMessage] = []
    plain_counts: "dict[str, int]" = {}

    for index, row in enumerate(rows):
        received_at_ms, sender_id, sender_name, event_time_ms, text = row
        sid = str(sender_id or "")
        name = str(sender_name or "")
        entry = talkers.setdefault(sid, [name, 0, index])
        entry[1] += 1
        if name and not entry[0]:
            entry[0] = name
        plain = _qq_monitor_plain_text(text)
        is_focus = sid in focus
        content = _qq_monitor_content_chars(plain)
        if not is_focus:
            if not plain:
                dropped["media"] += 1
                continue
            if not content:
                dropped["symbol"] += 1
                continue
            if len(content) <= filler_max_chars:
                dropped["filler"] += 1
                continue
        plain_counts[plain] = plain_counts.get(plain, 0) + 1
        candidates.append(
            _QqMonitorMessage(
                index=index,
                received_at_ms=int(received_at_ms),
                sender_id=sid,
                sender_name=name,
                event_time_ms=int(event_time_ms or 0),
                display=_qq_monitor_display_text(text),
                plain=plain,
                weight=len(content),
                focus=is_focus,
            )
        )

    seen: "set[str]" = set()
    surviving: list[_QqMonitorMessage] = []
    for msg in candidates:
        if not msg.focus:
            if msg.plain in seen:
                dropped["echo"] += 1
                continue
            seen.add(msg.plain)
        surviving.append(msg)

    anchors = {
        plain
        for plain, count in plain_counts.items()
        if count >= QQ_MONITOR_ANCHOR_REPEATS
    }
    scores: "dict[int, int]" = {}
    for msg in surviving:
        score = min(msg.weight, 200)
        if "?" in msg.plain or "？" in msg.plain:
            # A question is what a digest reader most wants resolved.
            score += 20
        if msg.plain in anchors:
            # The first posting of the copypasta everyone then repeated is a
            # topic marker, precisely because the copies were dropped above.
            score += 30
        scores[msg.index] = score

    kept = [msg for msg in surviving if msg.focus]
    pool = [msg for msg in surviving if not msg.focus]
    remaining = max(0, budget - len(kept))

    bucket_ms = max(1, int(bucket_minutes) * 60_000)
    buckets: "dict[int, list[_QqMonitorMessage]]" = {}
    for msg in pool:
        buckets.setdefault((msg.received_at_ms - since_ms) // bucket_ms, []).append(msg)
    keys = sorted(buckets)
    quotas = _qq_monitor_allocate([len(buckets[k]) for k in keys], remaining)
    for key, quota in zip(keys, quotas):
        bucket = buckets[key]
        taken = _qq_monitor_pick_bucket(
            bucket,
            quota,
            scores=scores,
            sender_share=sender_share,
            depth_share=depth_share,
        )
        dropped["quota"] += len(bucket) - len(taken)
        kept.extend(taken)
    kept.sort(key=lambda m: m.index)

    # Counted off what actually survived, not off the buckets the quota was
    # split over: a bucket may hold nothing but focus messages.
    covered = len({(m.received_at_ms - since_ms) // bucket_ms for m in kept})
    ordered_ids = sorted(talkers, key=lambda s: (-talkers[s][1], talkers[s][2]))
    top_talkers = [
        (talkers[s][0], s, talkers[s][1]) for s in ordered_ids[:QQ_MONITOR_TOP_TALKERS]
    ]

    return _QqMonitorReduction(
        rows=[
            (m.received_at_ms, m.sender_id, m.sender_name, m.event_time_ms, m.display)
            for m in kept
        ],
        total=total,
        kept=len(kept),
        focus_kept=sum(1 for m in kept if m.focus),
        dropped={k: v for k, v in dropped.items() if v},
        top_talkers=top_talkers,
        buckets=covered,
        budget=budget,
        fetch_capped=bool(fetch_capped),
    )


def _qq_monitor_reduction_notes(
    reduction: _QqMonitorReduction, focus_user_ids: Sequence[str]
) -> list[str]:
    """The lines that tell the digest's reader the log has been compressed.

    Non-negotiable per D47: a summariser handed a sample must not be allowed
    to believe it saw everything. These lines name the original count, the
    kept count, and every drop category with its exact number.
    """
    if not reduction.dropped and not reduction.fetch_capped:
        return []
    notes = [
        "说明：下面的聊天记录不是全部原文，而是对整个时间窗口做的确定性抽样——"
        "按小时分桶保证全时段都有代表，并对刷屏、重复内容和单个刷屏者做了限流。"
        "请据抽到的内容归纳话题，不要推断没有出现的内容，也不要按记录行数重新统计条数。"
    ]
    if reduction.dropped:
        parts = [
            f"{label} {reduction.dropped[key]} 条"
            for key, label in QQ_MONITOR_DROP_LABELS
            if reduction.dropped.get(key)
        ]
        notes.append(
            f"已归约 {sum(reduction.dropped.values())} 条："
            + "、".join(parts)
            + f"；保留 {reduction.kept} 条，覆盖 {reduction.buckets} 个时段。"
        )
    if reduction.fetch_capped:
        notes.append(
            f"注意：该时间窗口的消息量超过取数上限 {QQ_MONITOR_FETCH_CAP} 条，"
            "更早的消息没有进入本次抽样。"
        )
    if focus_user_ids:
        notes.append(
            f"重点关注对象（★ 标记）的 {reduction.focus_kept} 条消息未参与抽样，全部保留。"
        )
    if reduction.top_talkers:
        ranked = "、".join(
            f"{name}({sid}) {count} 条" if name else f"{sid} {count} 条"
            for name, sid, count in reduction.top_talkers
        )
        notes.append("按原始条数发言最多：" + ranked + "（抽样后各人条数已被拉平，热度以此行为准）。")
    return notes


def main_qq_monitor_digest(
    *,
    db_path: str,
    instance_id: str,
    group_id: str,
    watch_user_ids: Sequence[str],
    focus_user_ids: Sequence[str],
    window_minutes: int,
    timezone: str,
    monitor_id: str,
    now: Optional[datetime] = None,
    budget: Optional[int] = None,
) -> int:
    """Emit one monitor's chat window as digest material.

    Ports the fetch half of ``_qq_monitor_run_once`` plus the header/chat-log
    half of ``_qq_monitor_compose_prompt``, with the source's map-reduce
    replaced by :func:`_qq_monitor_prereduce` — a deterministic, zero-model
    compression that lets ONE agent turn cover the whole window instead of
    its newest slice (D47; see QQ_MONITOR_DIGEST_BUDGET). The style
    instructions themselves live in ``prompts.qq_monitor_digest`` — this
    function only ever prints material, matching every other job in this
    library.

    ``budget`` overrides the line budget for this run; ``None`` falls back to
    the ``QQ_MONITOR_DIGEST_BUDGET`` env var and then the module default.

    Prints nothing when the window is empty. All three migrated monitors
    have ``send_when_empty=false`` verbatim (A1 §4), and an empty stdout is
    hermes's own way of skipping the model call and the delivery entirely
    (cron/scheduler.py: "Script produced no output — nothing to report") —
    the same optimisation the source made by returning before calling
    ``_qq_monitor_summarize`` at all when ``count == 0``.
    """
    tz = ZoneInfo(timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    until_ms = int(moment.timestamp() * 1000)
    since_ms = until_ms - int(window_minutes) * 60_000
    sender_ids = _qq_monitor_collection_ids(watch_user_ids, focus_user_ids)

    rows = _qq_monitor_query(
        db_path,
        instance_id=instance_id,
        group_id=group_id,
        since_ms=since_ms,
        until_ms=until_ms,
        sender_ids=sender_ids,
        limit=QQ_MONITOR_FETCH_CAP,
    )
    if not rows:
        log(f"{monitor_id}: no messages in the window — nothing to report")
        return 0

    reduction = _qq_monitor_prereduce(
        rows,
        focus_user_ids=focus_user_ids,
        since_ms=since_ms,
        budget=_qq_monitor_budget(budget),
        fetch_capped=len(rows) >= QQ_MONITOR_FETCH_CAP,
    )
    del rows  # a busy sanhu window is ~9 MB; do not hold it while rendering
    if not reduction.rows:
        # Only reachable when the whole window was media/symbol/filler and
        # no focus member spoke. Nothing to summarise; same silent skip as an
        # empty window (send_when_empty=false).
        log(
            f"{monitor_id}: {reduction.total} message(s) in the window but none "
            "carried any text — nothing to report"
        )
        return 0

    window_desc = _qq_monitor_window_desc(int(window_minutes))
    if reduction.kept == reduction.total:
        # Nothing was compressed — keep the source's header byte-for-byte.
        print(f"群 {group_id} {window_desc}的消息汇总（共 {reduction.total} 条）。")
    else:
        print(
            f"群 {group_id} {window_desc}的消息汇总"
            f"（原始 {reduction.total} 条，抽样保留 {reduction.kept} 条）。"
        )
    if focus_user_ids:
        print("重点关注：" + "、".join(focus_user_ids))
    for note in _qq_monitor_reduction_notes(reduction, focus_user_ids):
        print(note)
    log(
        f"{monitor_id}: prereduced {reduction.total} -> {reduction.kept} line(s) "
        f"across {reduction.buckets} bucket(s), budget={reduction.budget}, "
        f"dropped={reduction.dropped}"
    )
    print()
    print("聊天记录（越靠下越新）：")
    print("\n".join(_qq_monitor_format_lines(reduction.rows, focus_user_ids, tz)))
    return 0


# ---------------------------------------------------------------------------
# grantley decay (persona.decay)
# ---------------------------------------------------------------------------


def main_grantley_decay(*, script_path: str, persona_id: Optional[str] = None) -> int:
    """Run ``plugins/grantley/scripts/grantley_job.py decay`` in-process.

    That script resolves its package by walking up from its own ``__file__``,
    so it cannot simply be copied into ``$HERMES_HOME/scripts/``. ``runpy``
    executes it at its real location with ``__file__`` intact, which keeps
    plugins/grantley the single owner of the decay implementation — this
    module adds no decay logic of its own.
    """
    import runpy  # noqa: PLC0415

    target = Path(script_path)
    if not target.is_file():
        raise SystemExit(f"grantley job script not found: {target}")
    argv = [str(target), "decay"]
    if persona_id:
        argv[1:1] = ["--persona", persona_id]
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = saved
    return 0


__all__ = [
    "ANALYSIS_KEYWORDS",
    "CONTENT_JSON_PREFIX",
    "MAX_MESSAGE_CHARS",
    "MAX_TOTAL_CHARS",
    "NOISE_PREFIXES",
    "NO_ANALYSIS_MARKER",
    "NO_DIARY_MATERIAL",
    "QQ_MONITOR_FETCH_CAP",
    "QQ_MONITOR_LINE_CAP",
    "QQ_MONITOR_PROMPT_MESSAGE_CAP",
    "SECRET_PATTERNS",
    "WATERMARK_CAP",
    "WATERMARK_PROMPT_WINDOW",
    "YOUTUBE_STATE_MARKER",
    "as_date",
    "daily_items",
    "decode_content",
    "extract_video_ids",
    "hermes_home",
    "job_record",
    "job_state_dir",
    "latest_output",
    "main_analysis_material",
    "main_daily_agenda",
    "main_diary_material",
    "main_grantley_decay",
    "main_qq_monitor_digest",
    "main_qzone_recent_posts",
    "main_youtube_state",
    "parse_week_match",
    "previous_run_delivered",
    "query_messages",
    "redact",
    "render_svg",
    "text_agenda",
    "time_text",
]
