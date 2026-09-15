"""格兰特利·贝尔 (Grantly Bell) — a long-lived character persona for hermes.

Ported from the corlinman system. See
``docs/migration-corlinman/C1-grantley-port-notes.md`` for what was ported,
what was deliberately changed, and what data must be carried at cutover.

Layout::

    plugins/grantley/
    ├── plugin.yaml              kind: exclusive (memory-provider activation)
    ├── __init__.py              register(ctx)
    ├── persona.py               prompt load + cache-safe split + placeholders
    ├── state.py                 PersonaState, fatigue bucketing, topic format
    ├── decay.py                 ported decay math (pure, two clocks)
    ├── life.py                  life doc, seed library, life-beat draw, signals
    ├── store.py                 sqlite: persona_state + append-only life_events
    ├── tools.py                 persona_life_* model-facing tools
    ├── jobs.py                  no-LLM decay + life-advance jobs
    ├── channel_binding.py       per-channel persona (OneBot integration point)
    ├── assets/                  evidence-backed character content
    └── scripts/grantley_job.py  cron entry point (no_agent)

Activation. This is a memory provider, so it is selected by name through
``memory.provider`` rather than ``plugins.enabled``::

    memory:
      provider: grantley

The directory is authored in-tree for review and tests; the deployment form
is a copy at ``$HERMES_HOME/plugins/grantley/``, which
``plugins/memory/__init__.py`` scans as a user-installed provider. Every
intra-package import is relative so the package works under both the
in-repo ``plugins.grantley`` name and the deployed
``_hermes_user_memory.grantley`` name.

When the plugin manages the persona's *stable* identity, it comes from the
lane-specific ``grantley.identity`` prompt section. A production profile that
also serves CLI, Telegram, or unrelated OneBot channels must keep its default
``SOUL.md`` neutral; a Grantley-wide ``SOUL.md`` is profile-global and cannot
be lane-gated by this plugin.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: Config keys checked in order for this plugin's settings.
_CONFIG_PATHS = (
    ("plugins", "entries", "grantley", "settings"),
    ("plugins", "grantley"),
)


def load_plugin_config() -> Dict[str, Any]:
    """Read plugin settings from config.yaml. Never raises.

    Public because the OneBot adapter needs the same block to find
    ``settings.channels`` (the per-channel persona bindings). Re-deriving the
    config path over there would be a second source of truth for it.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        config = load_config()
        for path in _CONFIG_PATHS:
            value = cfg_get(config, *path, default=None)
            if isinstance(value, dict) and value:
                return dict(value)
    except Exception as exc:  # noqa: BLE001 - config is optional
        logger.debug("grantley: config unavailable (%s)", exc)
    return {}


def register(ctx) -> None:
    """Plugin entry point.

    Registers the memory provider, which carries the persona's volatile state
    and its ``persona_life_*`` tools. Optionally registers the *stable*
    identity as a frozen system-prompt section for single-profile
    deployments that would rather not manage a ``SOUL.md``; off by default
    because ``SOUL.md`` is the correct layer and registering both would pay
    for the identity twice.
    """
    config = load_plugin_config()

    from .memory_provider import GrantleyMemoryProvider

    provider = GrantleyMemoryProvider(config=config)
    ctx.register_memory_provider(provider)

    def _register_lane_section(section_id: str, loader) -> None:
        """Register one frozen, lane-gated prompt section.

        Both sections share a single gate (``is_grantley_onebot_lane``) so a
        non-Grantley lane cannot receive either half, and both are captured
        once at registration: the content is a pure function of the asset, so
        it belongs in the cached prefix, not in the per-turn sidecar.
        """
        try:
            from .channel_binding import is_grantley_onebot_lane

            content = loader()

            def _bound(session_info) -> str:
                if not is_grantley_onebot_lane(
                    platform=session_info.get("platform"),
                    chat_id=session_info.get("chat_id"),
                    chat_type=session_info.get("chat_type"),
                    channels=config.get("channels"),
                ):
                    return ""
                return content

            ctx.register_system_prompt_section(section_id, _bound)
        except Exception as exc:  # noqa: BLE001 - never block provider load
            logger.warning("grantley: %s section not registered (%s)", section_id, exc)

    if config.get("inject_identity_section"):
        from .persona import load_persona_document

        _register_lane_section(
            "grantley.identity", lambda: load_persona_document().stable
        )

    if config.get("inject_voice_section"):
        from .persona import load_voice_document

        _register_lane_section("grantley.voice", load_voice_document)


#: Back-compat alias for the pre-E0 private name.
_load_plugin_config = load_plugin_config

__all__ = ["load_plugin_config", "register"]
