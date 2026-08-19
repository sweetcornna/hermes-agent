"""QQ空间 (QZone) tools — publish, read the timeline, comment, list friends.

Ported from the corlinman deployment that is running these five tools in
production today against a real QQ account. See
``docs/migration-corlinman/C3-qzone-port-notes.md`` for what was ported,
how the four deferred disagreements (S14–S17) were resolved, the on-disk
state formats, and what cannot be verified without a live QQ session.

Layout::

    plugins/qzone/
    ├── plugin.yaml   kind: backend (bundled, auto-loaded)
    ├── __init__.py   register(ctx) — five ctx.register_tool calls
    ├── client.py     auth, injectable HTTP transport, response parsing
    ├── publish.py    qzone_publish
    ├── feed.py       qzone_list_feed / _get_post / _post_comment / _list_friends
    └── state.py      the three on-disk sidecars + write-outcome semantics

Why a plugin and not ``tools/*.py``: every model tool registered in the core
is sent on every API call, so the bar for core surface is high and this
family clears none of it — it is useful only on an install with a QQ account
bridged through OneBot. As a plugin the tools carry a ``check_fn``, so when
no OneBot endpoint is configured they never enter the model's schema and
cost exactly zero tokens.

They register into the ``onebot`` toolset, which the framework folds into
the auto-generated ``hermes-onebot`` toolset for the registered OneBot
platform (``toolsets.py``'s plugin-platform branch). That keeps the family
attached to the platform whose login state it borrows and needs no edit to
``toolsets.py`` — the tools are not core tools and must not become any.

Safety: ``qzone_publish`` and ``qzone_post_comment`` write to a real,
public social feed and there is no delete tool on either side of this port.
Both refuse to repeat a write whose previous attempt ended in an unknown
state; see ``state.py``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Toolset name. Matches the registered platform name so the framework's
#: ``hermes-<platform>`` auto-toolset picks these up (see module docstring).
TOOLSET = "onebot"

__all__ = ["TOOLSET", "register"]


def _check_qzone_available() -> bool:
    """Expose these tools only when a OneBot backend is configured.

    They have no credentials of their own — the QQ session is borrowed from
    NapCat / Lagrange — so without a configured endpoint there is nothing
    they could possibly do, and a schema entry would be pure token cost.
    """
    try:
        from tools.onebot_client import onebot_configured

        return bool(onebot_configured())
    except Exception:  # noqa: BLE001 — an unimportable client means unavailable
        return False


def register(ctx) -> None:
    """Register the five QZone tools. Called once by the plugin loader."""
    from .feed import (
        QZONE_GET_POST_SCHEMA,
        QZONE_GET_POST_TOOL,
        QZONE_LIST_FEED_SCHEMA,
        QZONE_LIST_FEED_TOOL,
        QZONE_LIST_FRIENDS_SCHEMA,
        QZONE_LIST_FRIENDS_TOOL,
        QZONE_POST_COMMENT_SCHEMA,
        QZONE_POST_COMMENT_TOOL,
        handle_qzone_get_post,
        handle_qzone_list_feed,
        handle_qzone_list_friends,
        handle_qzone_post_comment,
    )
    from .publish import QZONE_PUBLISH_SCHEMA, QZONE_PUBLISH_TOOL, handle_qzone_publish

    tools = (
        (QZONE_PUBLISH_TOOL, QZONE_PUBLISH_SCHEMA, handle_qzone_publish, "🐧"),
        (QZONE_LIST_FEED_TOOL, QZONE_LIST_FEED_SCHEMA, handle_qzone_list_feed, "📰"),
        (QZONE_GET_POST_TOOL, QZONE_GET_POST_SCHEMA, handle_qzone_get_post, "🔍"),
        (QZONE_POST_COMMENT_TOOL, QZONE_POST_COMMENT_SCHEMA, handle_qzone_post_comment, "💬"),
        (QZONE_LIST_FRIENDS_TOOL, QZONE_LIST_FRIENDS_SCHEMA, handle_qzone_list_friends, "👥"),
    )
    for name, schema, handler, emoji in tools:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=_check_qzone_available,
            requires_env=["ONEBOT_WS_URL"],
            emoji=emoji,
        )
