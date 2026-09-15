"""``GrantleyMemoryProvider`` — the cache-safe delivery channel for live state.

This is the load-bearing half of the caching resolution. Everything about
Grantley that changes faster than a conversation arrives through
:meth:`prefetch`, whose return value the memory manager wraps in
``<memory-context>`` and appends to the **current user message's**
``api_content`` (``agent/turn_context.py`` ``compose_user_api_content``).
That is the only injection channel in hermes that is safe to vary per turn:
it lands *after* the cached prefix instead of rewriting it.

The counterpart rule, from A3 G2: **do not implement**
:meth:`~agent.memory_provider.MemoryProvider.system_prompt_block` for
decaying content. It is frozen into the system prompt, so anything decaying
placed there re-hashes the prefix every turn. This class deliberately leaves
that hook at its inherited no-op — see :meth:`system_prompt_block`.

What prefetch emits each turn
-----------------------------
1. the ``## 此刻的我（实时状态）`` section, rendered with live placeholder
   values (fatigue as a **bucket label**, never the float);
2. a life-rhythm nudge when one trips (``go_out`` / ``wrap_outing`` /
   ``change_scene``);
3. the top-N life events by ``salience * 0.5^(age_days/half_life)``.

Speed matters: the manager runs an external provider's ``prefetch`` on a
daemon thread with a timeout and *skips the turn entirely* if a previous
call is still running. Two small indexed sqlite reads keep us well inside
that budget.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from agent.compression_policy import (
    normalize_tail_tokens,
    normalize_threshold_ratio,
    normalize_threshold_tokens,
)
from agent.memory_provider import MemoryProvider, RecallStatus

from .channel_binding import is_grantley_onebot_lane
from . import life, tools
from .persona import (
    PERSONA_ID,
    PersonaAssetError,
    PersonaDocument,
    load_persona_document,
    render_state_block,
)
from .store import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_MIN_WEIGHT,
    DEFAULT_TOP_N,
    GrantleyStore,
)

logger = logging.getLogger(__name__)

#: Plugin storage key. Also the directory name under
#: ``<hermes home>/plugin-data/``.
STORAGE_KEY: str = "grantley"

# Grantley's QQ policy is deliberately local to this plugin. The ordinary
# global compression defaults remain untouched for every other persona.
DEFAULT_AUTOCOMPACT_THRESHOLD = 0.75
DEFAULT_AUTOCOMPACT_TAIL_TOKENS = 16_000


class GrantleyMemoryProvider(MemoryProvider):
    """Persona state + decayed life-event recall, delivered per turn."""

    def __init__(
        self,
        *,
        persona_id: str = PERSONA_ID,
        config: Optional[Dict[str, Any]] = None,
        connection: Optional[sqlite3.Connection] = None,
        data_dir: Optional[Path] = None,
    ) -> None:
        cfg = dict(config or {})
        self._persona_id = str(persona_id or PERSONA_ID)
        self._half_life_days = float(
            cfg.get("event_half_life_days", DEFAULT_HALF_LIFE_DAYS)
        )
        self._top_n = int(cfg.get("event_top_n", DEFAULT_TOP_N))
        self._min_weight = float(cfg.get("event_min_weight", DEFAULT_MIN_WEIGHT))
        self._emit_nudges = bool(cfg.get("emit_life_nudges", True))
        self._config = cfg

        self._explicit_conn = connection
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._store: Optional[GrantleyStore] = None
        self._doc: Optional[PersonaDocument] = None
        self._lock = threading.RLock()
        self._last_recall_count = 0
        self._initialized = False
        self._platform = ""
        self._chat_id = ""
        self._chat_type = ""

    # -- required ABC surface ---------------------------------------------

    @property
    def name(self) -> str:
        return "grantley"

    def is_available(self) -> bool:
        """True once the ported prompt asset parses.

        Storage is not part of availability: an unreadable database is a
        degraded persona, but an unparseable prompt means we do not know
        which half of the document is volatile, and guessing there is how
        live state ends up back in the cached prefix.
        """
        try:
            self._document()
        except PersonaAssetError:
            return False
        return True

    def unavailable_reason(self) -> str:
        try:
            self._document()
        except PersonaAssetError as exc:
            return str(exc)
        return ""

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Bind the session lane; open persona storage only for bound QQ."""
        with self._lock:
            self._initialized = False
            self._platform = str(kwargs.get("platform") or "").lower()
            self._chat_id = str(kwargs.get("chat_id") or "")
            self._chat_type = str(kwargs.get("chat_type") or "").lower()
            self._last_recall_count = 0
            if not self._is_grantley_qq_lane():
                self._initialized = True
                return
            self._document()
            self._ensure_store()
            self._initialized = True

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not self._initialized or not self._is_grantley_qq_lane():
            return []
        return tools.tool_schemas()

    # -- constructor-time context policy ---------------------------------

    @staticmethod
    def _bool_setting(value: Any, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    def _is_grantley_qq_lane(self) -> bool:
        """Scope the policy to the initialized Grantley OneBot lane.

        Only an explicit ``channels.<chat_id>.persona: grantley`` entry binds
        a lane. Missing, empty, malformed, or non-matching maps fail closed.
        This is the same map the reply and proactive lanes use for framing.
        """
        return is_grantley_onebot_lane(
            platform=self._platform,
            chat_id=self._chat_id,
            chat_type=self._chat_type,
            channels=self._config.get("channels"),
        )

    def context_compression_policy(self) -> Dict[str, Any]:
        """Tune Hermes' existing rolling compressor for Grantley QQ only.

        The manager resolves this mapping once while constructing the agent.
        It therefore cannot rewrite the system prompt or churn the provider's
        cached prefix mid-session. Summaries, durable cooldowns, in-place
        SessionDB commits, iterative re-compaction, and per-session locking all
        remain owned by ``agent/`` rather than being duplicated in OneBot.
        """
        if not self._is_grantley_qq_lane():
            return {}

        raw = self._config.get("autocompact")
        if isinstance(raw, Mapping):
            config = dict(raw)
        else:
            config = {}

        enabled = self._bool_setting(config.get("enabled"), True)
        policy: Dict[str, Any] = {"enabled": enabled}
        if not enabled:
            return policy

        threshold = normalize_threshold_ratio(config.get("threshold"))
        if threshold is None:
            threshold = DEFAULT_AUTOCOMPACT_THRESHOLD

        tail_tokens = normalize_tail_tokens(config.get("tail_tokens"))
        if tail_tokens is None:
            tail_tokens = DEFAULT_AUTOCOMPACT_TAIL_TOKENS

        threshold_tokens = normalize_threshold_tokens(
            config.get("threshold_tokens")
        )

        policy.update(
            {
                "threshold": threshold,
                "threshold_tokens": threshold_tokens,
                "tail_tokens": tail_tokens,
                # Grantley must never discard original turns merely because
                # the summarizer is unavailable. The core records a durable
                # cooldown and retries after backoff.
                "abort_on_summary_failure": True,
                "in_place": True,
                "micro_compact": False,
                "min_tail_user_messages": 2,
            }
        )
        return policy

    def system_prompt_stable_across_compression(self) -> bool:
        """Grantley contributes no system block; retain cached bytes exactly."""
        return True

    # -- the cache-safe channel -------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return this turn's live-state block. Cache-safe by construction.

        The manager appends the result to the *current* user message, so a
        different value every turn costs nothing: the cached prefix is
        untouched. Returns ``""`` on any failure — a broken persona must not
        break the turn.

        Note the returned text is deliberately **not** wrapped in
        ``<memory-context>``; the manager adds that itself and logs a warning
        if a provider pre-wraps it.
        """
        if not self._is_grantley_qq_lane():
            self._last_recall_count = 0
            return ""

        try:
            doc = self._document()
            store = self._ensure_store()
        except Exception as exc:  # noqa: BLE001 - never propagate into a turn
            logger.debug("grantley prefetch unavailable: %s", exc)
            self._last_recall_count = 0
            return ""

        try:
            state = store.load_state(self._persona_id)
            extra: list[str] = []

            if self._emit_nudges:
                signals = life.compute_life_signals(
                    state.state_json.get("life"), life.now_dt()
                )
                nudge = signals.get("life_nudge")
                if isinstance(nudge, dict):
                    extra.append(
                        f"[生活节奏提醒 · {nudge.get('level', '')}] "
                        f"{nudge.get('message', '')}\n{nudge.get('suggested_action', '')}"
                    )

            events = store.retrieve(
                self._persona_id,
                top_n=self._top_n,
                half_life_days=self._half_life_days,
                min_weight=self._min_weight,
            )
            self._last_recall_count = len(events)
            if events:
                lines = ["最近还留着印象的事（越靠前越鲜明）："]
                lines += [f"- [{e.weight:.2f}] {e.text}" for e in events]
                extra.append("\n".join(lines))

            return render_state_block(doc, state, extra_lines=extra)
        except Exception as exc:  # noqa: BLE001
            logger.debug("grantley prefetch failed: %s", exc)
            self._last_recall_count = 0
            return ""

    def system_prompt_block(self) -> str:
        """Intentionally empty — see the module docstring and A3 G2.

        Everything this provider knows is time-varying. A ``system_prompt_block``
        is frozen into the system prompt at session start, so putting decaying
        content here would re-hash the cached prefix on every change. The
        persona's *stable* identity reaches the prompt through the opt-in,
        lane-gated ``grantley.identity`` section. A ``SOUL.md`` identity is
        safe only in a dedicated Grantley profile because SOUL is global to
        every surface served by that profile.
        """
        return ""

    def recall_status(self) -> Optional[RecallStatus]:
        if self._last_recall_count <= 0:
            return None
        return RecallStatus(provider_label="格兰", count=self._last_recall_count)

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs: Any
    ) -> str:
        if not self._is_grantley_qq_lane():
            return tools._err(
                "forbidden_lane",
                "Grantley persona tools are available only in a bound OneBot lane",
            )
        try:
            store = self._ensure_store()
        except Exception as exc:  # noqa: BLE001
            return tools._err("unavailable", f"grantley storage unavailable: {exc}")
        return tools.dispatch(
            tool_name,
            args or {},
            store=store,
            persona_id=self._persona_id,
            data_dir=self._resolve_data_dir(),
        )

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Deliberately a no-op.

        Grantley's event log is written by explicit tool calls and by the
        out-of-band cron jobs, not by scraping every turn. Auto-ingesting
        conversation would fill the decay window with chatter and drown the
        life beats that the retrieval is meant to surface.
        """
        return None

    def shutdown(self) -> None:
        with self._lock:
            if self._store is not None and self._explicit_conn is None:
                try:
                    self._store._conn.close()  # noqa: SLF001 - we own it
                except Exception:  # noqa: BLE001
                    pass
            self._store = None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "event_half_life_days",
                "label": "Life-event half-life (days)",
                "kind": "number",
                "default": DEFAULT_HALF_LIFE_DAYS,
                "description": (
                    "Decay half-life for the life-event log. 0 disables decay."
                ),
            },
            {
                "key": "event_top_n",
                "label": "Life events per turn",
                "kind": "number",
                "default": DEFAULT_TOP_N,
                "description": "How many decayed events ride on each user message.",
            },
            {
                "key": "emit_life_nudges",
                "label": "Emit life-rhythm nudges",
                "kind": "bool",
                "default": True,
            },
        ]

    def backup_paths(self) -> List[str]:
        try:
            from plugins.plugin_storage import plugin_data_dir

            return [str(plugin_data_dir(STORAGE_KEY))]
        except Exception:  # noqa: BLE001
            return []

    # -- internals ---------------------------------------------------------

    def _document(self) -> PersonaDocument:
        if self._doc is None:
            self._doc = load_persona_document()
        return self._doc

    def _resolve_data_dir(self) -> Optional[Path]:
        if self._data_dir is not None:
            return self._data_dir
        try:
            from plugins.plugin_storage import plugin_data_dir

            return plugin_data_dir(STORAGE_KEY)
        except Exception:  # noqa: BLE001
            return None

    def _ensure_store(self) -> GrantleyStore:
        with self._lock:
            if self._store is not None:
                return self._store
            if self._explicit_conn is not None:
                self._store = GrantleyStore(self._explicit_conn)
                return self._store
            from plugins.plugin_storage import plugin_db

            self._store = GrantleyStore(plugin_db(STORAGE_KEY))
            return self._store


__all__ = ["STORAGE_KEY", "GrantleyMemoryProvider"]
