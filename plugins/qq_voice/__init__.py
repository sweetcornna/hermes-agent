"""QQ voice tools — send a 语音消息, or speak as one of QQ's own AI characters.

Two model-callable tools over a QQ account already bridged through OneBot v11
(SnowLuma / NapCat / Lagrange / go-cqhttp):

* ``qq_send_voice`` — a native QQ voice bubble (语音消息), either synthesized
  from ``text`` with whatever TTS provider the operator configured, or an
  existing local audio file.  Private chat or group.
* ``qq_ai_voice`` — a voice message spoken by one of QQ's *built-in* AI
  characters (群 AI 声聊).  Group-only and backed by a OneBot v11 extension
  implemented by SnowLuma and NapCat; Lagrange and go-cqhttp may not provide
  it.  Called without a ``character`` it lists the ones the group offers
  instead of guessing.

Layout::

    plugins/qq_voice/
    ├── plugin.yaml    kind: backend (bundled, auto-loaded)
    ├── __init__.py    register(ctx) — two ctx.register_tool calls
    ├── common.py      id coercion, target resolution, audio vetting,
    │                  synthesis, and the live-vs-direct transport choice
    ├── quota.py       the text-length cap and the daily send budget
    ├── send_voice.py  qq_send_voice
    └── ai_voice.py    qq_ai_voice

Why a plugin and not ``tools/*.py``: every model tool registered in the core
is sent on every API call, so the bar for core surface is high and this pair
clears none of it — it is useful only on an install with a QQ account bridged
through OneBot.  As a plugin the tools carry a ``check_fn``, so when no OneBot
endpoint is configured they never enter the model's schema and cost exactly
zero tokens.

They register into the ``onebot`` toolset, which the framework folds into the
auto-generated ``hermes-onebot`` toolset for the registered OneBot platform
(``toolsets.py``'s plugin-platform branch).  That keeps the pair attached to
the platform whose login state it borrows and needs no edit to
``toolsets.py`` — these are not core tools and must not become any.

Two delivery paths, and why the difference is reported
------------------------------------------------------
When the gateway is running in this process the tools hand the send to
``plugins/platforms/onebot/voice.py``, which enforces the emergency mute, the
Tencent content policy and the self-recording into the recent-chatter buffer,
in that order.  When it is not (CLI, ``hermes cron``), none of that state
exists to bypass and the send goes out over the plain OneBot transport.  Both
outcomes report which one ran under ``delivery_path``; see ``common.py``.

Both tools are capped, and the caps are the reason this plugin is safe to
hand a model
------------------------------------------------------------------------
Every autonomous voice path here is already bounded (``proactive_daily_max``,
``proactive_call_daily_max``, ``proactive_voice_max_chars``, the adapter's
sliding-window speech cap); the model-callable tools were the one unbounded
one, and each ``qq_send_voice`` call runs a *paid* synthesis through the
configured ``tts.provider``.  ``quota.py`` closes that: a text-length cap on
both tools, and a daily send budget on ``qq_send_voice`` alone (Tencent
synthesizes ``qq_ai_voice`` at no cost to us).  Both are configured under
``plugins.entries.qq_voice.settings`` in ``~/.hermes/config.yaml`` — the
native plugin-settings path, declared in ``plugin.yaml``'s ``config_schema``
so the loader type-checks them — and either can be turned off with ``0``.
Thresholds, so config keys and not env vars; the only env this plugin reads
is the OneBot endpoint and token, which are credentials and are the platform's
rather than ours.

Safety: these drive an unofficial automation path.  They can fail on a stale
OneBot login state or when Tencent risk control intervenes, and a voice
message posted into a real group cannot be unsent from here.  Nothing in this
plugin ever silently retries a send — a double-posted voice bubble is worse
than a visible failure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Toolset name.  Matches the registered platform name so the framework's
#: ``hermes-<platform>`` auto-toolset picks these up (see module docstring).
TOOLSET = "onebot"

__all__ = ["TOOLSET", "register"]


def _check_qq_voice_available() -> bool:
    """Expose these tools only when a OneBot backend is configured.

    They have no credentials of their own — the QQ session belongs to the
  running OneBot backend — so without a configured endpoint
    there is nothing they could possibly do, and a schema entry would be pure
    token cost on every API call.
    """
    try:
        from tools.onebot_client import onebot_configured

        return bool(onebot_configured())
    except Exception:  # noqa: BLE001 — an unimportable client means unavailable
        return False


def register(ctx) -> None:
    """Register the two QQ voice tools.  Called once by the plugin loader."""
    from .ai_voice import QQ_AI_VOICE_SCHEMA, QQ_AI_VOICE_TOOL, handle_qq_ai_voice
    from .send_voice import (
        QQ_SEND_VOICE_SCHEMA,
        QQ_SEND_VOICE_TOOL,
        handle_qq_send_voice,
    )

    tools = (
        (QQ_SEND_VOICE_TOOL, QQ_SEND_VOICE_SCHEMA, handle_qq_send_voice, "🎙️"),
        (QQ_AI_VOICE_TOOL, QQ_AI_VOICE_SCHEMA, handle_qq_ai_voice, "🗣️"),
    )
    for name, schema, handler, emoji in tools:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=_check_qq_voice_available,
            requires_env=["ONEBOT_WS_URL"],
            emoji=emoji,
        )
