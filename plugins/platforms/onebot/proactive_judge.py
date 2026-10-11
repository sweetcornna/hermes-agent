"""A cheap second opinion before an unprompted group post.

The proactive turn already offers the persona a SKIP hatch, but the persona
is the wrong party to ask: it was just told "say something", it has a voice
to stay in, and an in-character model almost always finds something to say.
That is how a bot ends up chiming in on every lull.

This judge sits in front of that turn.  A separate auxiliary model — routed
through the core's ``auxiliary.onebot_proactive_judge`` task, so provider,
model and ``reasoning_effort`` live in ``config.yaml`` like every other side
task — reads the same recent transcript and answers one question: would a
post right now read as natural to the people in the room, or as noise?
How *often* the bot speaks is not its question — that is the daily caps'
job (``proactive_daily_max`` / ``proactive_daily_max_total``), and the
rubric says so, so the judge does not ration on top of them.

It fails **closed**.  The gate exists to keep the bot from talking too much,
so a timeout, an error or an answer in the wrong shape means "stay quiet this
beat" — the opposite of the outbound safety gate, where silence is the risk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

TASK_KEY = "onebot_proactive_judge"
TASK_DISPLAY_NAME = "QQ proactive pre-judge"
TASK_DESCRIPTION = "decides whether an unprompted QQ group post would be welcome right now"
#: Routing defaults the plugin registers; ``auxiliary.onebot_proactive_judge``
#: in config.yaml overrides any of them.
TASK_DEFAULTS = {"provider": "auto", "model": "", "timeout": 60}

DEFAULT_TIMEOUT_SECONDS = 60.0
#: Room for adaptive thinking plus a one-line JSON answer.
DEFAULT_MAX_TOKENS = 4096

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_THINK_RE = re.compile(
    r"<(think|thinking|reasoning|thought)>.*?</\1>", re.DOTALL | re.IGNORECASE
)

_SYSTEM_PROMPT = """你是 QQ 群聊里的「插话把关人」。群里有个成员（聊天记录里标成「{self_label}」的就是他），他现在想主动在群里说一句话——没人 @ 他、也没人叫他。你要判断：此刻他开口，像不像一个普通的活跃群友自然地冒个泡，还是会让人觉得多嘴、打扰、刷存在感。
{persona_block}
适合开口（满足任一即可）：
- 有人提问、求助或在找信息，他能给出有用的回答（他能上网查资料）。
- 群里正在聊的东西——游戏、动漫、AI、学校、吃喝玩乐、日常吐槽、群友晒的图和梗——他能接一句有自己味道的反应、吐槽、看法或玩笑。

不适合开口（踩中任一就不说）：
- 他能说的只有「哈哈」「确实」「+1」这类空话。
- 几个人在私下说事、吵架、倾诉，或者严肃敏感话题（家事、健康、情绪低落、政治），或者在讨论具体的投资买卖。
- 话题凉了：最后一条消息超过 30 分钟。
- 他刚说过话，之后别人还没怎么接。

他每天主动开口的次数另有硬上限，你不用替他省着：只要此刻接话自然，就可以说。

只输出一行 JSON，不要其他内容：{{"speak": true 或 false, "reason": "一句话理由"}}"""


@dataclass(frozen=True)
class JudgeSettings:
    enabled: bool = True
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: Who the bot is, in a sentence or two — without it the judge cannot
    #: tell which topics the bot actually has something to add to.
    persona: str = ""


@dataclass(frozen=True)
class JudgeVerdict:
    speak: bool
    reason: str = ""
    #: ``False`` when the model was never asked or its answer was unusable.
    consulted: bool = True
    #: Why the model was not consulted (``disabled``, ``no_context``, ...).
    cause: str = ""


def resolve_settings(extra: Optional[Mapping[str, Any]]) -> JudgeSettings:
    """Read the flat ``proactive_judge_*`` keys from the platform settings."""
    get = (extra or {}).get
    enabled = get("proactive_judge_enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"0", "false", "no", "off"}
    try:
        timeout = float(get("proactive_judge_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS
    try:
        max_tokens = int(get("proactive_judge_max_tokens", DEFAULT_MAX_TOKENS))
    except (TypeError, ValueError):
        max_tokens = DEFAULT_MAX_TOKENS
    return JudgeSettings(
        enabled=bool(enabled),
        timeout_seconds=timeout,
        max_tokens=max(256, max_tokens),
        persona=str(get("proactive_judge_persona", "") or "").strip(),
    )


def build_messages(
    lines: Sequence[str],
    *,
    self_label: str,
    now_label: str,
    idle_minutes: Optional[float],
    persona: str = "",
) -> list:
    facts = [f"现在时间：{now_label}"]
    if idle_minutes is not None:
        facts.append(f"最后一条消息是 {max(0, round(idle_minutes))} 分钟前")
    user = "\n".join(facts) + "\n\n最近的群聊记录（越靠下越新）：\n" + "\n".join(lines)
    return [
        {
            "role": "system",
            "content": _SYSTEM_PROMPT.format(
                self_label=self_label,
                persona_block=f"\n他是谁：{persona}\n" if persona else "",
            ),
        },
        {"role": "user", "content": user},
    ]


def answer_text(response: Any) -> str:
    """The reply text only.

    Unlike the core's ``extract_content_or_reasoning`` this never falls back
    to reasoning: thinking that weighs ``{"speak": true}`` before settling on
    no is not an answer, and when thinking eats the whole budget the honest
    reading is "no verdict", which keeps the bot quiet.
    """
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


def parse_verdict(raw: str) -> Optional[JudgeVerdict]:
    """Parse the JSON answer; ``None`` when it is not in the asked shape."""
    match = _JSON_RE.search(raw or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    speak = payload.get("speak")
    if not isinstance(speak, bool):
        # "speak": "true" did not answer in the shape asked for; reading a
        # truthy string as a yes is how a gate turns into a no-op.
        return None
    reason = payload.get("reason")
    return JudgeVerdict(speak=speak, reason=str(reason)[:200] if isinstance(reason, str) else "")


async def judge(
    lines: Sequence[str],
    *,
    self_label: str,
    now_label: str,
    idle_minutes: Optional[float],
    settings: Optional[JudgeSettings] = None,
) -> JudgeVerdict:
    """Ask whether an unprompted post would be welcome now.  Never raises."""
    cfg = settings or JudgeSettings()
    if not cfg.enabled:
        # Off means the gate steps aside, not that the bot goes mute.
        return JudgeVerdict(speak=True, consulted=False, cause="disabled")
    if not lines:
        # Nothing to read the room from — an opening line into an unseen room
        # is exactly the post this gate exists to stop.
        return JudgeVerdict(speak=False, consulted=False, cause="no_context")

    try:
        from agent.auxiliary_client import async_call_llm
    except Exception as exc:  # noqa: BLE001
        logger.warning("OneBot proactive judge: auxiliary client unavailable (%s)", exc)
        return JudgeVerdict(speak=False, consulted=False, cause="client_unavailable")

    messages = build_messages(
        lines,
        self_label=self_label,
        now_label=now_label,
        idle_minutes=idle_minutes,
        persona=cfg.persona,
    )
    try:
        response: Any = await asyncio.wait_for(
            async_call_llm(task=TASK_KEY, messages=messages, max_tokens=cfg.max_tokens),
            timeout=cfg.timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "OneBot proactive judge: timed out after %.0fs — staying quiet",
            cfg.timeout_seconds,
        )
        return JudgeVerdict(speak=False, consulted=False, cause="timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OneBot proactive judge: call failed (%s: %s) — staying quiet",
            type(exc).__name__,
            exc,
        )
        return JudgeVerdict(speak=False, consulted=False, cause="call_failed")

    raw = answer_text(response)
    verdict = parse_verdict(raw)
    if verdict is None:
        logger.warning(
            "OneBot proactive judge: unparseable answer (%d chars) — staying quiet",
            len(raw or ""),
        )
        return JudgeVerdict(speak=False, consulted=False, cause="unparseable")
    return verdict


__all__ = [
    "JudgeSettings",
    "JudgeVerdict",
    "TASK_DEFAULTS",
    "TASK_DESCRIPTION",
    "TASK_DISPLAY_NAME",
    "TASK_KEY",
    "answer_text",
    "build_messages",
    "judge",
    "parse_verdict",
    "resolve_settings",
]
