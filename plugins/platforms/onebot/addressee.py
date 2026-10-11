"""Noticing when someone in a group is talking to the bot without naming it.

The router only answers an @mention or a configured keyword.  People do not
talk like that: they reply to what the bot just said, ask "那只老虎呢",
say "问问 AI 呗", or turn to it mid-thread with a bare "你觉得呢".  None of
those reach the model, and the persona looks deaf.

This module watches the messages the router let fall.  Per group it collects
them until the room pauses (``quiet_seconds``, bounded by ``max_wait_seconds``),
then asks one auxiliary model — the core's ``auxiliary.onebot_address_judge``
task, routed like every other side task — whether any of them is aimed at the
bot.  A yes hands that message back to the adapter as if the bot had been
addressed, and the ordinary reply path (speech cap, reply window, the turn)
takes over from there.

Only rooms the bot is part of are watched — within
``recent_self_minutes`` of its own last post there — and calls are paced
(``min_interval_seconds`` between judgements per group, a daily ceiling per
group) and the judge fails **closed**: an error, a timeout
or an answer in the wrong shape means the bot was not addressed.  Missing an
indirect address is the old behaviour; answering a conversation that was not
for it is the failure this feature must not introduce.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import protocol

logger = logging.getLogger(__name__)

TASK_KEY = "onebot_address_judge"
TASK_DISPLAY_NAME = "QQ implicit-address judge"
TASK_DESCRIPTION = "decides whether unmentioned QQ group messages are aimed at the bot"
TASK_DEFAULTS = {"provider": "auto", "model": "", "timeout": 30}

SELF_LABEL = "你自己"
_MAX_CANDIDATES = 12
_ROOM_LINES = 20
_LINE_CHARS = 200
_PLACEHOLDER_RE = re.compile(r"\[[^\[\]]{1,8}\]")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_THINK_RE = re.compile(
    r"<(think|thinking|reasoning|thought)>.*?</\1>", re.DOTALL | re.IGNORECASE
)

_SYSTEM_PROMPT = """你是 QQ 群聊里的「点名识别器」。群里有个成员（聊天记录里标成「{self_label}」的就是他）{persona_clause}。下面这几条新消息里没有人 @ 他，也没人直接叫他的名字。你要判断：其中有没有哪一条其实是在跟他说话、想找他聊。

算是在找他：
- 接着他刚才说的话往下说、回应他、反驳他、追问他。
- 用别的称呼或代称指他（比如外号、「那只老虎」「bot」「机器人」「AI」），或者明显在等他回答（「你觉得呢」「问问他」）。
- 话里提到他、想拉他进话题、想听他的看法。

不算：
- 群友之间互相说话、回复别人、@别人。
- 自言自语、对全群的泛泛感叹或提问（没有指向他）。
- 只是在聊和他人设相关的话题，但并没有冲着他说。

拿不准就不算。

只输出一行 JSON，不要其他内容：{{"to_him": true 或 false, "index": 那条消息的编号或 null, "reason": "一句话理由"}}"""


@dataclass(frozen=True)
class AddressSettings:
    enabled: bool = False
    quiet_seconds: float = 8.0
    max_wait_seconds: float = 20.0
    min_interval_seconds: float = 15.0
    daily_max_checks: int = 300
    #: Judge only within this many minutes of the bot's own last post in the
    #: group; ``0`` judges every lull.
    recent_self_minutes: float = 10.0
    timeout_seconds: float = 30.0
    max_tokens: int = 2048
    persona: str = ""


def _float(get: Callable[..., Any], key: str, default: float) -> float:
    try:
        value = float(get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default


def _int(get: Callable[..., Any], key: str, default: int) -> int:
    try:
        return max(0, int(get(key, default)))
    except (TypeError, ValueError):
        return default


def resolve_settings(extra: Optional[Mapping[str, Any]]) -> AddressSettings:
    """Read the flat ``implicit_address_*`` keys; off unless enabled."""
    get = (extra or {}).get
    enabled = get("implicit_address_enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    timeout = _float(get, "implicit_address_timeout_seconds", 30.0) or 30.0
    persona = get("implicit_address_persona", None) or get("proactive_judge_persona", "")
    return AddressSettings(
        enabled=bool(enabled),
        quiet_seconds=_float(get, "implicit_address_quiet_seconds", 8.0),
        max_wait_seconds=_float(get, "implicit_address_max_wait_seconds", 20.0),
        min_interval_seconds=_float(get, "implicit_address_min_interval_seconds", 15.0),
        daily_max_checks=_int(get, "implicit_address_daily_max_checks", 300),
        recent_self_minutes=_float(get, "implicit_address_recent_self_minutes", 10.0),
        timeout_seconds=timeout,
        max_tokens=max(256, _int(get, "implicit_address_max_tokens", 2048)),
        persona=str(persona or "").strip(),
    )


def worth_judging(text: str) -> bool:
    """Whether a message has any words in it once media placeholders go."""
    return bool(_PLACEHOLDER_RE.sub("", text or "").strip())


@dataclass
class Candidate:
    """One unaddressed group message, with the room as it stood before it."""

    event: protocol.MessageEvent
    sender_name: str
    text: str
    room_before: List[Tuple[float, str, str, bool]]
    at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class AddressVerdict:
    to_him: bool
    index: Optional[int] = None
    reason: str = ""
    consulted: bool = True
    cause: str = ""


def _local_tz() -> Any:
    """Hermes's configured timezone, or ``None`` for the host zone."""
    try:
        import hermes_time

        return hermes_time.get_timezone()
    except Exception:  # noqa: BLE001 — timestamps are context, never a gate
        return None


def _clock(stamp: float) -> str:
    try:
        return datetime.fromtimestamp(stamp, _local_tz()).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "--:--"


def build_messages(candidates: Sequence[Candidate], *, persona: str = "") -> list:
    room = candidates[0].room_before[-_ROOM_LINES:] if candidates else []
    before = [
        f"[{_clock(ts)}] {SELF_LABEL if is_self else (name or '某人')}: {text.strip()[:_LINE_CHARS]}"
        for ts, name, text, is_self in room
        if (text or "").strip()
    ]
    fresh = [
        f"{i}. [{_clock(c.event.time or time.time())}] {c.sender_name or '某人'}: "
        f"{c.text.strip()[:_LINE_CHARS]}"
        for i, c in enumerate(candidates, start=1)
    ]
    user = (
        "之前的聊天记录（越靠下越新）：\n"
        + ("\n".join(before) if before else "（没有）")
        + "\n\n新消息（带编号）：\n"
        + "\n".join(fresh)
    )
    clause = f"，他是：{persona}" if persona else ""
    return [
        {
            "role": "system",
            "content": _SYSTEM_PROMPT.format(self_label=SELF_LABEL, persona_clause=clause),
        },
        {"role": "user", "content": user},
    ]


def answer_text(response: Any) -> str:
    """Reply text only — never the reasoning, which may weigh a yes it dropped."""
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "") if isinstance(part, Mapping) else str(part)
            for part in content
        )
    return _THINK_RE.sub("", content if isinstance(content, str) else "").strip()


def parse_verdict(raw: str, count: int) -> Optional[AddressVerdict]:
    """Parse the JSON answer; ``None`` unless it is in the asked shape."""
    match = _JSON_RE.search(raw or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("to_him"), bool):
        return None
    reason = payload.get("reason")
    reason = str(reason)[:200] if isinstance(reason, str) else ""
    if not payload["to_him"]:
        return AddressVerdict(to_him=False, reason=reason)
    index = payload.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= count:
        # A yes that cannot say which message is not an answer we can act on.
        return None
    return AddressVerdict(to_him=True, index=index, reason=reason)


async def judge(
    candidates: Sequence[Candidate], settings: Optional[AddressSettings] = None
) -> AddressVerdict:
    """Ask whether any candidate is aimed at the bot.  Never raises."""
    cfg = settings or AddressSettings()
    if not candidates:
        return AddressVerdict(to_him=False, consulted=False, cause="empty")
    try:
        from agent.auxiliary_client import async_call_llm
    except Exception as exc:  # noqa: BLE001
        logger.warning("OneBot address judge: auxiliary client unavailable (%s)", exc)
        return AddressVerdict(to_him=False, consulted=False, cause="client_unavailable")
    messages = build_messages(candidates, persona=cfg.persona)
    try:
        response: Any = await asyncio.wait_for(
            async_call_llm(task=TASK_KEY, messages=messages, max_tokens=cfg.max_tokens),
            timeout=cfg.timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("OneBot address judge: timed out after %.0fs", cfg.timeout_seconds)
        return AddressVerdict(to_him=False, consulted=False, cause="timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OneBot address judge: call failed (%s: %s)", type(exc).__name__, exc
        )
        return AddressVerdict(to_him=False, consulted=False, cause="call_failed")
    raw = answer_text(response)
    verdict = parse_verdict(raw, len(candidates))
    if verdict is None:
        logger.warning("OneBot address judge: unparseable answer (%d chars)", len(raw))
        return AddressVerdict(to_him=False, consulted=False, cause="unparseable")
    return verdict


Judge = Callable[[Sequence[Candidate], AddressSettings], Awaitable[AddressVerdict]]
OnAddressed = Callable[[List[Candidate], int, AddressVerdict], Awaitable[None]]


@dataclass
class _Room:
    pending: List[Candidate] = field(default_factory=list)
    task: Optional[asyncio.Task] = None
    last_check: float = float("-inf")
    day: str = ""
    checks: int = 0


class AddressWatcher:
    """Per-group: gather unaddressed messages, judge once per lull."""

    def __init__(self, on_addressed: OnAddressed, *, judge_fn: Judge = judge) -> None:
        self._on_addressed = on_addressed
        self._judge = judge_fn
        self._rooms: Dict[Any, _Room] = {}

    def observe(self, group_id: Any, candidate: Candidate, settings: AddressSettings) -> None:
        room = self._rooms.setdefault(group_id, _Room())
        room.pending.append(candidate)
        del room.pending[:-_MAX_CANDIDATES]
        if room.task is None or room.task.done():
            room.task = asyncio.create_task(
                self._settle(group_id, room, settings), name="onebot-address-watch"
            )

    def forget(self, group_id: Any) -> None:
        """Drop what is waiting — the bot was addressed directly meanwhile."""
        room = self._rooms.get(group_id)
        if room is not None:
            room.pending.clear()

    async def close(self) -> None:
        tasks = [r.task for r in self._rooms.values() if r.task and not r.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._rooms.clear()

    async def _settle(self, group_id: Any, room: _Room, settings: AddressSettings) -> None:
        try:
            while room.pending:
                now = time.monotonic()
                first, last = room.pending[0].at, room.pending[-1].at
                due = min(last + settings.quiet_seconds, first + settings.max_wait_seconds)
                due = max(due, room.last_check + settings.min_interval_seconds)
                if due > now:
                    await asyncio.sleep(due - now)
                    continue
                batch, room.pending = room.pending, []
                day = datetime.now(_local_tz()).strftime("%Y-%m-%d")
                if room.day != day:
                    room.day, room.checks = day, 0
                if settings.daily_max_checks and room.checks >= settings.daily_max_checks:
                    continue
                room.checks += 1
                room.last_check = time.monotonic()
                verdict = await self._judge(batch, settings)
                if not verdict.to_him or verdict.index is None:
                    continue
                logger.info(
                    "OneBot: implicit address group=%s by=%s reason=%s",
                    group_id,
                    batch[verdict.index - 1].sender_name,
                    verdict.reason,
                )
                await self._on_addressed(batch, verdict.index - 1, verdict)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a watcher bug must never cost the channel
            logger.exception("OneBot: implicit address watcher failed group=%s", group_id)


__all__ = [
    "AddressSettings",
    "AddressVerdict",
    "AddressWatcher",
    "Candidate",
    "TASK_DEFAULTS",
    "TASK_DESCRIPTION",
    "TASK_DISPLAY_NAME",
    "TASK_KEY",
    "answer_text",
    "build_messages",
    "judge",
    "parse_verdict",
    "resolve_settings",
    "worth_judging",
]
