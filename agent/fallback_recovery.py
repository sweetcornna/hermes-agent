"""Opt-in, profile-wide fallback latch and independent primary health probe.

The agent uses its existing fallback/restore paths. A scheduler invokes
``python -m agent.fallback_recovery`` independently of incoming chat messages.
Only the configured default route participates; explicit model overrides do not.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import math
import sqlite3
import time
import uuid

from hermes_constants import get_hermes_home, get_hermes_home_override

logger = logging.getLogger(__name__)
_OUTAGES = {
    "auth", "auth_permanent", "billing", "rate_limit", "upstream_rate_limit",
    "overloaded", "server_error", "timeout", "ssl_cert_verification",
    "model_not_found", "provider_policy_blocked", "unknown",
}


def _policy():
    from agent.secret_scope import is_multiplex_active, UnscopedSecretError
    from hermes_cli.config import load_config

    if is_multiplex_active() and get_hermes_home_override() is None:
        raise UnscopedSecretError("fallback recovery requires an explicit profile scope")
    config = load_config()
    settings = config.get("fallback_recovery") or {}
    if settings.get("enabled") is not True:
        return None
    model = config.get("model") or {}
    if not isinstance(model, dict):
        raise ValueError("fallback_recovery requires model.provider and model.default")
    provider = str(model.get("provider") or "").removeprefix("custom:")
    entry = (config.get("providers") or {}).get(provider) or {}
    route = {
        "provider": provider,
        "model": str(model.get("default") or ""),
        "base_url": str(model.get("base_url") or entry.get("base_url") or "").rstrip("/"),
        "api_mode": str(model.get("api_mode") or entry.get("api_mode") or entry.get("transport") or "chat_completions"),
    }
    if not all(route.values()) or route["api_mode"] not in {"anthropic_messages", "chat_completions"}:
        raise ValueError("fallback_recovery requires a named Anthropic or OpenAI-compatible endpoint")
    interval = float(settings.get("probe_interval_seconds", 3600))
    timeout = float(settings.get("probe_timeout_seconds", 30))
    if not math.isfinite(interval) or interval < 60 or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("Invalid fallback_recovery probe interval or timeout")
    return route, interval, timeout


def _route_key(route):
    return json.dumps(route, sort_keys=True)


def _matches(runtime, route):
    return (
        str(runtime.get("requested_provider") or runtime.get("provider") or "").removeprefix("custom:") == route["provider"]
        and runtime.get("model") == route["model"]
        and str(runtime.get("base_url") or "").rstrip("/") == route["base_url"]
        and runtime.get("api_mode") == route["api_mode"]
    )


@contextmanager
def _store():
    # No credentials or conversation contents are persisted here. SQLite makes
    # cross-process claims atomic and survives gateway restarts.
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / "fallback_recovery.db", timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS recovery (
                id INTEGER PRIMARY KEY CHECK (id = 1), route TEXT NOT NULL,
                revision TEXT NOT NULL, healthy INTEGER NOT NULL,
                next_probe REAL NOT NULL, reason TEXT NOT NULL)""")
        with db:
            yield db
    finally:
        db.close()


def record_primary_failure(agent, reason):
    """Latch actual upstream failures, never prompt-specific rejection errors."""
    if getattr(reason, "value", reason) not in _OUTAGES:
        return
    policy = _policy()
    if policy is None:
        return
    route, interval, _ = policy
    current = {k: getattr(agent, k, None) for k in ("model", "provider", "requested_provider", "base_url", "api_mode")}
    if not _matches(current, route) or not getattr(agent, "_fallback_chain", None):
        return
    # Restore the original reasoning setting along with the original model.
    runtime = getattr(agent, "_primary_runtime", None)
    if runtime is not None and _matches(runtime, route):
        runtime["reasoning_config"] = getattr(agent, "reasoning_config", None)
    key = _route_key(route)
    with _store() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM recovery WHERE id = 1").fetchone()
        next_probe = old["next_probe"] if old and old["route"] == key and not old["healthy"] else time.time() + interval
        db.execute("INSERT OR REPLACE INTO recovery VALUES (1, ?, ?, 0, ?, ?)",
                   (key, uuid.uuid4().hex, next_probe, getattr(reason, "value", reason)))
    logger.warning("Primary %s unavailable; fallback latched until a successful health probe", route["model"])


def keep_fallback_for_turn(agent):
    """Apply shared outage state to both cached and newly constructed agents."""
    policy = _policy()
    if policy is None:
        return False
    route, _, _ = policy
    runtime = getattr(agent, "_primary_runtime", None) or {}
    if not _matches(runtime, route):
        return False
    with _store() as db:
        state = db.execute("SELECT * FROM recovery WHERE id = 1").fetchone()
    if not state or state["route"] != _route_key(route):
        return False
    if state["healthy"]:
        if getattr(agent, "_fallback_activated", False):
            agent._rate_limited_until = 0
        return False
    if not getattr(agent, "_fallback_activated", False):
        # A fresh agent can enter fallback directly from persisted outage
        # state, without record_primary_failure ever seeing that instance.
        runtime["reasoning_config"] = getattr(agent, "reasoning_config", None)
        agent._fallback_index = 0
        if not agent._try_activate_fallback():
            raise RuntimeError("Primary is unavailable and no configured fallback could be activated")
    return True


def _probe(route, timeout):
    """Use the real configured credential resolver; never send chat history."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested=route["provider"], target_model=route["model"])
    if not _matches({**runtime, "model": route["model"]}, route):
        raise ValueError("Primary route changed during health probe")
    messages = [{"role": "user", "content": "Reply with OK."}]
    if route["api_mode"] == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client

        client = build_anthropic_client(runtime["api_key"], route["base_url"], timeout=timeout)
        try:
            response = client.with_options(max_retries=0).messages.create(
                model=route["model"], max_tokens=32, messages=messages,
            )
            return any(getattr(block, "text", "").strip() for block in response.content)
        finally:
            client.close()
    from openai import OpenAI

    with OpenAI(api_key=runtime["api_key"], base_url=route["base_url"],
                default_headers=runtime.get("extra_headers"), timeout=timeout, max_retries=0) as client:
        response = client.chat.completions.create(model=route["model"], max_tokens=32, messages=messages)
        return bool(response.choices and (response.choices[0].message.content or "").strip())


def probe_due():
    """Claim at most one real probe per interval, including across processes."""
    policy = _policy()
    if policy is None:
        return "disabled"
    route, interval, timeout = policy
    key = _route_key(route)
    now = time.time()
    revision = uuid.uuid4().hex
    with _store() as db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute("SELECT * FROM recovery WHERE id = 1").fetchone()
        if not state or state["route"] != key or state["healthy"]:
            return "primary"
        if state["next_probe"] > now:
            return "waiting"
        db.execute("UPDATE recovery SET revision = ?, next_probe = ? WHERE id = 1", (revision, now + interval))
    try:
        healthy = _probe(route, timeout)
    except Exception as exc:
        # Exception bodies may contain API keys or upstream response text.
        logger.warning("Primary health probe failed (%s); staying on fallback", type(exc).__name__)
        return "unavailable"
    if not healthy:
        return "unavailable"
    with _store() as db:
        changed = db.execute("UPDATE recovery SET healthy = 1, reason = '' WHERE id = 1 AND route = ? AND revision = ?",
                             (key, revision)).rowcount
    if not changed:
        return "superseded"
    logger.info("Primary %s recovered; subsequent turns will restore it", route["model"])
    return "recovered"


def main():
    # This entry point is a separate profile-scoped process. Credentials come
    # from that profile's .env through the existing scope-aware resolver.
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope

    logging.basicConfig(level=logging.INFO)
    token = set_secret_scope(build_profile_secret_scope(get_hermes_home()))
    try:
        print(probe_due())
    finally:
        reset_secret_scope(token)


if __name__ == "__main__":
    main()
