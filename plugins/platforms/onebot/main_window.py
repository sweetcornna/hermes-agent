"""主窗口 — the one chat that is allowed to drive this gateway.

The rule this module enforces
-----------------------------
**A QQ group is never the gateway's console.**  Reminders, cron deliveries and
gateway notifications belong in one private chat, and slash commands are only
honoured there.  A group is a place the persona *talks*; it is not a place
anybody operates the agent from.

Two separate things were wide open before this module existed, and both were a
group-membership away from being someone else's:

1. **Delivery.**  ``deliver=onebot`` (bare platform target) and every gateway
   notification resolve to the platform's ``home_channel``.  Nothing stopped
   that from being ``g183287894`` — at which point restart notices, drain
   warnings and every cron digest land in a group chat of strangers.
2. **Control.**  ``router.looks_like_command`` treats a leading ``/`` in a
   group as an *explicit summons* (it bypasses the keyword filter and the
   cooldown, exactly like an @mention), and ``gateway/slash_access.py``
   disables slash gating entirely for a scope whose ``allow_admin_from`` is
   unset — which is the staged production config's state for the group scope.
   Both defaults are individually reasonable and together they mean: flip
   ``group_replies_enabled`` on and every member of a whitelisted group can
   run every registered slash command.

Why the fix is structural, not another config flag
--------------------------------------------------
This migration has already hit "config says off, behaviour says on" five times
(see ``specs.py``'s note on ``qunjlu``), so the group lock does not depend on
an operator remembering to write ``group_allow_admin_from``.  Groups lose
gateway control *by construction*: :func:`gateway_control_allowed` returns
``False`` for every group event, and the adapter stamps that onto
``MessageEvent.allow_gateway_control`` — the existing core field whose whole
job is "this text is conversation, never a command"
(``gateway/platforms/base.py:2388``).  With it ``False``,
``MessageEvent.is_command()`` is ``False``, so the run-loop's command paths are
not merely *denied*, they are never entered.

``group_gateway_control_enabled: true`` is the escape hatch, off by default and
loud in the README.  It exists so this is a decision rather than a wall.

The main window itself
----------------------
``main_window_chat_id`` (or ``ONEBOT_MAIN_WINDOW_CHAT_ID``) names the private
peer that owns this gateway; unset, it falls back to the configured
``home_channel``, which is what bare-platform delivery already targets — one
id, so "where reminders land" and "who may operate the agent" cannot drift
apart.  A **group id is rejected** in either slot: a group cannot become the
main window by config typo.

With no main window configured at all, DM behaviour is unchanged (every DM the
platform-level ``allow_from`` already admitted keeps its commands).  The group
lock does not depend on it — it holds either way.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

__all__ = [
    "MAIN_WINDOW_ENV",
    "gateway_control_allowed",
    "group_control_enabled",
    "home_channel_chat_id",
    "is_group_chat_id",
    "is_main_window",
    "normalize_peer_id",
    "resolve_main_window",
    "settings_view",
]

#: Env form of ``main_window_chat_id``.  YAML wins when both are set.
MAIN_WINDOW_ENV = "ONEBOT_MAIN_WINDOW_CHAT_ID"

#: Accepted input spellings for a *private* chat id, mirroring the ones
#: ``adapter.parse_chat_id`` takes.  ``g<digits>`` and ``group:<digits>`` are
#: the group spellings and are refused here.
_PRIVATE_PREFIXES = ("private:", "user:", "dm:")


def is_group_chat_id(raw: Any) -> bool:
    """Whether *raw* names a QQ **group** in any accepted spelling."""
    text = str(raw or "").strip().lower()
    if not text:
        return False
    if text.startswith("group:"):
        return True
    return text[0] == "g" and text[1:].isdigit()


def normalize_peer_id(raw: Any) -> str:
    """Canonical bare uin for a private chat id, or ``""`` if it is not one.

    ``"2104743984"``, ``"private:2104743984"`` and ``"user:2104743984"`` all
    normalise to ``"2104743984"``.  A group id normalises to ``""`` — callers
    read that as "there is no main window here", never as "allow it".
    """
    text = str(raw or "").strip()
    if not text or is_group_chat_id(text):
        return ""
    lowered = text.lower()
    for prefix in _PRIVATE_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    return text if text.isdigit() else ""


def home_channel_chat_id(extra: Mapping[str, Any]) -> str:
    """``home_channel``'s chat id, tolerating both shapes it travels in.

    The core loader turns ``platforms.onebot.home_channel`` into a
    :class:`gateway.config.HomeChannel`, while ``_env_enablement`` seeds it as
    a plain dict.  Either can be what is sitting in ``extra`` at call time.
    """
    home = extra.get("home_channel")
    if isinstance(home, Mapping):
        return str(home.get("chat_id") or "")
    chat_id = getattr(home, "chat_id", None)
    if chat_id is not None:
        return str(chat_id)
    return str(home or "") if isinstance(home, str) else ""


def settings_view(
    extra: Optional[Mapping[str, Any]], home_channel: Any = None
) -> Mapping[str, Any]:
    """``extra`` with the PlatformConfig's ``home_channel`` folded in.

    YAML ``platforms.onebot.home_channel`` is parsed by the core loader into
    ``PlatformConfig.home_channel`` and never lands in ``extra``
    (``gateway/config.py:709``), while ``_env_enablement``'s seed *does* arrive
    as an ``extra`` dict.  Without this view the home-channel fallback would
    work for an env-configured deployment and silently not for a YAML one —
    the sort of split that makes a security default look enabled while it
    isn't.  An explicit ``extra["home_channel"]`` still wins.
    """
    settings: Mapping[str, Any] = extra if isinstance(extra, Mapping) else {}
    if home_channel is None or settings.get("home_channel"):
        return settings
    merged = dict(settings)
    merged["home_channel"] = home_channel
    return merged


def resolve_main_window(
    extra: Optional[Mapping[str, Any]],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The main window's bare uin, or ``None`` when none is configured.

    Precedence: ``main_window_chat_id`` → ``ONEBOT_MAIN_WINDOW_CHAT_ID`` →
    ``home_channel``'s chat id.  A group id in any of those slots is *skipped*
    rather than accepted, and a config that names nothing but a group resolves
    to ``None``.
    """
    settings: Mapping[str, Any] = extra if isinstance(extra, Mapping) else {}
    environ: Mapping[str, str] = env if env is not None else os.environ
    for candidate in (
        settings.get("main_window_chat_id"),
        environ.get(MAIN_WINDOW_ENV),
        home_channel_chat_id(settings),
    ):
        peer = normalize_peer_id(candidate)
        if peer:
            return peer
    return None


def is_main_window(
    extra: Optional[Mapping[str, Any]],
    *,
    chat_id: Any,
    is_group: bool,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Whether this chat *is* the main window.  A group never is."""
    if is_group:
        return False
    main = resolve_main_window(extra, env=env)
    if not main:
        return False
    return normalize_peer_id(chat_id) == main


def group_control_enabled(extra: Optional[Mapping[str, Any]]) -> bool:
    """The escape hatch: whether groups may run gateway commands after all.

    Off unless explicitly written.  Env is deliberately *not* consulted — this
    switch decides whether a room full of people can operate the agent, so it
    lives in exactly one reviewable place, the same argument ``proactive_*``
    and ``voice_call_*`` make for being YAML-only.
    """
    settings: Mapping[str, Any] = extra if isinstance(extra, Mapping) else {}
    raw = settings.get("group_gateway_control_enabled")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def gateway_control_allowed(
    extra: Optional[Mapping[str, Any]],
    *,
    chat_id: Any,
    is_group: bool,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Value for ``MessageEvent.allow_gateway_control`` on an inbound event.

    * group → ``False``, unless :func:`group_control_enabled`;
    * DM, main window configured → only that peer;
    * DM, nothing configured → ``True``, i.e. today's behaviour untouched.
    """
    if is_group:
        return group_control_enabled(extra)
    main = resolve_main_window(extra, env=env)
    if not main:
        return True
    return normalize_peer_id(chat_id) == main
