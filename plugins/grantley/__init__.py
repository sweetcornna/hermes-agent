"""格兰特利·贝尔 (Grantley Bell) — a long-lived character persona for hermes.

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
    ├── assets/                  byte-exact ported character content
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

The persona's *stable* identity does not come from here — it belongs in the
profile's ``SOUL.md``, written by ``scripts/grantley_job.py install-profile``.
Putting identity in a runtime-registered block would be harmless today and a
cache hazard the moment someone made it dynamic; keeping the two layers
physically apart is the point.
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


def _load_plugin_config() -> Dict[str, Any]:
    """Read plugin settings from config.yaml. Never raises."""
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
    config = _load_plugin_config()

    from .memory_provider import GrantleyMemoryProvider

    provider = GrantleyMemoryProvider(config=config)
    ctx.register_memory_provider(provider)

    if config.get("inject_identity_section"):
        try:
            from .persona import load_persona_document

            ctx.register_system_prompt_section(
                "grantley.identity",
                load_persona_document().stable,
            )
        except Exception as exc:  # noqa: BLE001 - never block provider load
            logger.warning("grantley: identity section not registered (%s)", exc)


__all__ = ["register"]
