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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
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
#: Verbatim from service.py:_QQ_MONITOR_FETCH_CAP.
QQ_MONITOR_FETCH_CAP = 10_000

#: Safety cap on how many formatted lines go into ONE agent turn.
#:
#: The source split anything over 1,000 messages (service.py:
#: _QQ_MONITOR_CHUNK_MESSAGES) into 1,000-message chunks, summarised those
#: chunks with PARALLEL chat turns (asyncio.gather over its own chat
#: service), then ran one more turn to merge the partial summaries
#: (_qq_monitor_summarize). A hermes cron job gets exactly one model call —
#: there is no seam here for a script to launch several concurrent LLM
#: turns of its own outside that call, and doing so would mean this job
#: quietly starts making its own paid model calls from Python instead of
#: through the one the scheduler already accounts for. That is a bigger
#: architectural step than "port a job onto the established pattern", so it
#: is not done.
#:
#: Consequence: on a day where a monitor's window holds more than this many
#: messages — expected most days for "sanhu" (group 980927602 alone
#: produced 45,578 of the 52,649-row export, roughly 15k/day) — this port
#: keeps only the NEWEST QQ_MONITOR_PROMPT_MESSAGE_CAP messages and marks
#: the digest as covering "仅展示最新一部分" rather than the whole day. This
#: is a real, documented fidelity gap versus the source (which covered
#: every message via map-reduce); see
#: docs/migration-corlinman/D2-qq-monitor-port-notes.md §3.
QQ_MONITOR_PROMPT_MESSAGE_CAP = 1000


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
) -> int:
    """Emit one monitor's chat window as digest material.

    Ports the fetch half of ``_qq_monitor_run_once`` plus the header/chat-log
    half of ``_qq_monitor_compose_prompt`` (the single-turn path — see
    QQ_MONITOR_PROMPT_MESSAGE_CAP for why the map-reduce path is not
    reproduced). The style instructions themselves live in
    ``prompts.qq_monitor_digest`` — this function only ever prints material,
    matching every other job in this library.

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

    truncated = False
    if len(rows) > QQ_MONITOR_PROMPT_MESSAGE_CAP:
        rows = rows[-QQ_MONITOR_PROMPT_MESSAGE_CAP:]
        truncated = True

    window_desc = _qq_monitor_window_desc(int(window_minutes))
    tail = "，仅展示最新一部分，更早的消息未纳入本次汇总" if truncated else ""
    print(f"群 {group_id} {window_desc}的消息汇总（共 {len(rows)} 条{tail}）。")
    if focus_user_ids:
        print("重点关注：" + "、".join(focus_user_ids))
    print()
    print("聊天记录（越靠下越新）：")
    print("\n".join(_qq_monitor_format_lines(rows, focus_user_ids, tz)))
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
