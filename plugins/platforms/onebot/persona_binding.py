"""Per-channel persona framing for the OneBot lane — the missing wire.

``plugins/grantley/channel_binding.py`` was written (C1) as *the* integration
point for this adapter and then never called: B3's repo-wide grep found
``resolve_channel_prompt`` with zero callers, and B4 independently found
``MessageEvent.channel_prompt`` unset on both OneBot lanes.  Same gap, two
discoveries (00-PLAN.md §18).  Until this module existed,
``plugins.entries.grantley.settings.channels`` was a pure declaration: the
operator could write ``channel_owner: "2104743984"`` and nothing would read it.

Why one module instead of two call sites
----------------------------------------
There are two lanes that speak into a QQ group — the reply lane
(``adapter._build_message_event``) and the proactive lane
(``proactive.generate``) — and B4 deliberately declined to wire only its own,
because a persona framed one way when it answers and another way when it
speaks first is a persona with two characters.  So the resolution lives here,
once, and both lanes call :func:`channel_prompt` with nothing but their live
settings mapping.  Consistency is then structural rather than a convention two
files have to keep agreeing on; a test pins it anyway.

What is actually called
-----------------------
The real signatures in ``channel_binding`` (not its prose):

* ``bindings_from_config(raw) -> dict[str, PersonaChannelBinding]`` — keyed by
  ``str(chat_id)``, malformed entries skipped rather than raised;
* ``resolve_channel_prompt(binding, *, on=None, data_dir=None) -> str | None``
  — ``None`` for "nothing to say", and it never raises by contract.

``on`` is passed explicitly here, which is the whole reason the caching below
is legitimate: the module documents its output as a **daily frozen snapshot**,
a pure function of ``(binding, date)``, so memoising on exactly that pair
cannot change a byte of what the model sees.  Without a cache every inbound
group message would re-read the seed-pack YAML off disk
(``life.resolve_seed_library`` has no cache of its own).

Degradation
-----------
Every failure mode ends at "no channel prompt", never at an exception and
never at an empty frame injected into the system message: grantley not
installed, no ``channels`` config, an unknown chat id, a corrupt entry.  The
persona still works — it just loses the per-channel owner framing, exactly as
``channel_binding``'s own contract says.

Second occupant: the sticker menu
---------------------------------
:func:`channel_prompt` returns the *whole* ephemeral frame for a turn, and
since ``plugins/grantley/assets/stickers`` landed there is a second thing that
belongs in it — the probabilistic ``## 可用表情`` list (see :mod:`.sticker`).
It is composed here because this is already the one function both lanes call
for per-turn framing; a second hook would have to be wired into both of them
and kept in agreement, which is the exact duplication this module exists to
avoid.

Two consequences worth stating, because both are load-bearing and neither is
obvious from the call site:

* the menu is appended OUTSIDE ``_channel_prompt``'s day cache.  That cache is
  legitimate only because its contents are a pure function of
  ``(binding, date)``; a per-turn dice roll folded into it would be frozen for
  the day, which is not what a probability means;
* the menu is offered even when the channel has NO binding, so this function
  no longer returns ``None`` merely because a chat id is unbound.  Stickers
  belong to the account rather than to a channel's owner framing, and gating
  them on ``channels:`` would confine the feature to configured channels for
  no reason a reader could later reconstruct.

"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from . import sticker

logger = logging.getLogger(__name__)

__all__ = [
    "EXTRA_KEY",
    "bindings_from_extra",
    "channel_prompt",
    "reset_state",
]

#: Adapter-level override, for deployments that would rather keep the QQ
#: channel map next to the rest of the QQ config than under the persona
#: plugin.  Present-but-empty is meaningful ("no bindings"), so membership is
#: tested rather than truthiness.
EXTRA_KEY = "persona_channels"

#: Where the persona plugin keeps the same map (C1 §4.1 / 00-PLAN.md §18):
#: ``plugins.entries.grantley.settings.channels``.  This is the path the
#: migration documents to operators, so it is the fallback source.
_SETTINGS_KEY = "channels"

#: The persona package imports under two names: ``plugins.grantley`` in-repo,
#: ``_hermes_user_memory.grantley`` once deployed as a user memory provider
#: (see ``plugins/memory/__init__.py``).  Try both; a deployment that has
#: neither simply gets no channel prompt.
_CANDIDATE_PACKAGES = ("plugins.grantley", "_hermes_user_memory.grantley")

_MISSING = object()

#: Lazily-resolved ``channel_binding`` module (or ``None`` once we know there
#: is none).  ``_MISSING`` means "not looked yet".
_binding_module: Any = _MISSING

#: Lazily-resolved persona-plugin settings mapping.
_plugin_settings: Any = _MISSING

#: ``(persona_id, chat_id, is_group, day) -> rendered prompt or None``.
#: Bounded by pruning every entry from a previous day on each miss, so it can
#: never grow past the number of channels the bot actually talks in.
_prompt_cache: Dict[Tuple[str, str, bool, str], Optional[str]] = {}


def reset_state() -> None:
    """Test hook — drop every cached lookup (module, settings, prompts)."""
    global _binding_module, _plugin_settings
    _binding_module = _MISSING
    _plugin_settings = _MISSING
    _prompt_cache.clear()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _load_binding_module() -> Any:
    """Import ``channel_binding`` from whichever name the persona ships under."""
    global _binding_module
    if _binding_module is not _MISSING:
        return _binding_module
    from importlib import import_module

    for package in _CANDIDATE_PACKAGES:
        try:
            _binding_module = import_module(f"{package}.channel_binding")
            return _binding_module
        except Exception:  # noqa: BLE001 — a missing persona is not an error here
            continue
    logger.debug(
        "OneBot: no grantley channel_binding module found — per-channel persona "
        "framing is off"
    )
    _binding_module = None
    return None


def _load_plugin_settings() -> Mapping[str, Any]:
    """The persona plugin's own settings block, or ``{}``.

    Read through the persona package rather than re-deriving the config path,
    so the two cannot drift apart when the path changes.
    """
    global _plugin_settings
    if _plugin_settings is not _MISSING:
        return _plugin_settings
    from importlib import import_module

    for package in _CANDIDATE_PACKAGES:
        try:
            loader = getattr(import_module(package), "load_plugin_config", None)
            if loader is None:
                continue
            settings = loader()
            if isinstance(settings, Mapping):
                _plugin_settings = settings
                return _plugin_settings
        except Exception:  # noqa: BLE001 — config is optional by contract
            continue
    _plugin_settings = {}
    return _plugin_settings


def _raw_channel_map(extra: Mapping[str, Any]) -> Any:
    """The unparsed channel map: adapter ``extra`` first, persona config next."""
    if isinstance(extra, Mapping) and EXTRA_KEY in extra:
        return extra.get(EXTRA_KEY)
    return _load_plugin_settings().get(_SETTINGS_KEY)


def bindings_from_extra(extra: Mapping[str, Any]) -> Dict[str, Any]:
    """Parsed ``{chat_id: PersonaChannelBinding}``, or ``{}``.

    Not cached: ``bindings_from_config`` is a pure dict walk over a handful of
    entries, and caching it would mean an operator's config reconcile stopped
    taking effect until restart — the one behaviour the live-``extra`` reads
    everywhere else in this adapter exist to avoid.  The expensive half (the
    daily snapshot) is what :func:`channel_prompt` memoises.
    """
    module = _load_binding_module()
    if module is None:
        return {}
    try:
        return dict(module.bindings_from_config(_raw_channel_map(extra)))
    except Exception:  # noqa: BLE001 — a typo in operator config is not fatal
        logger.warning("OneBot: persona channel bindings could not be parsed", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# The one function both lanes call
# ---------------------------------------------------------------------------


def _today() -> date:
    """Today's calendar day on the same clock ``life.now_dt()`` uses."""
    return datetime.now(timezone.utc).astimezone().date()


def channel_prompt(
    extra: Mapping[str, Any],
    *,
    chat_id: Any,
    is_group: bool,
    on: Optional[date] = None,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """The ephemeral per-channel persona frame for one chat, or ``None``.

    *chat_id* is the platform-native id (a QQ group id or a peer uin), **not**
    the adapter's ``g``-prefixed chat id — the operator writes bare ids in
    ``channels:``.

    *is_group* comes from the live event, and wins over the binding's own
    ``group:`` flag when the two disagree.  A config typo must not tell the
    persona it is in a DM while it is posting to a group; the wire knows.

    The sticker menu is appended HERE rather than inside ``_channel_prompt``,
    and the placement is load-bearing.  ``_channel_prompt`` is memoised on
    ``(persona, channel, group, day)`` because its own output is documented
    as a daily frozen snapshot; folding a per-turn dice roll into that would
    freeze the roll for the day too — stickers available all day or not at
    all, which is not what a probability means.  Composed outside the cache,
    the frame stays a daily constant and the menu stays a per-turn decision.
    """
    try:
        base = _channel_prompt(extra, chat_id=chat_id, is_group=is_group, on=on)
    except Exception:  # noqa: BLE001 — outermost guard; see the module docstring
        logger.warning("OneBot: per-channel persona frame failed", exc_info=True)
        base = None
    try:
        menu = sticker.offer_menu(extra, rng)
    except Exception:  # noqa: BLE001 — a flourish must never cost the frame
        logger.warning("OneBot: sticker menu failed", exc_info=True)
        menu = None
    if not menu:
        return base
    # Offered even when this channel has no persona binding at all: the
    # stickers belong to the account, not to a channel's owner framing, and
    # returning ``None`` here would silently confine the feature to
    # configured channels for no reason anyone could find later.
    return f"{base}\n\n{menu}" if base else menu


def _channel_prompt(
    extra: Mapping[str, Any],
    *,
    chat_id: Any,
    is_group: bool,
    on: Optional[date] = None,
) -> Optional[str]:
    key = str(chat_id)
    if not key:
        return None
    binding = bindings_from_extra(extra).get(key)
    if binding is None:
        return None

    day = on or _today()
    cache_key = (
        str(getattr(binding, "persona_id", "")),
        key,
        bool(is_group),
        day.isoformat(),
    )
    cached = _prompt_cache.get(cache_key, _MISSING)
    if cached is not _MISSING:
        return cached  # type: ignore[return-value]

    module = _load_binding_module()
    if module is None:  # pragma: no cover — bindings_from_extra already returned {}
        return None
    try:
        if bool(getattr(binding, "is_group", False)) != bool(is_group):
            from dataclasses import replace

            logger.info(
                "OneBot: channel %s is configured group=%s but the event says "
                "group=%s — trusting the event",
                key,
                getattr(binding, "is_group", None),
                bool(is_group),
            )
            binding = replace(binding, is_group=bool(is_group))
        prompt = module.resolve_channel_prompt(binding, on=day)
    except Exception:  # noqa: BLE001 — a decorative frame never costs a message
        logger.warning("OneBot: per-channel persona frame failed", exc_info=True)
        prompt = None

    # Prune yesterday before inserting today, so the cache is bounded by the
    # number of live channels rather than by uptime.
    today_str = cache_key[3]
    for stale in [k for k in _prompt_cache if k[3] != today_str]:
        _prompt_cache.pop(stale, None)
    _prompt_cache[cache_key] = prompt
    return prompt
