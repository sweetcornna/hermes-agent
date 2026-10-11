"""Persistent failover with independent hourly probes, using isolated profiles."""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agent import fallback_recovery as recovery
from agent.error_classifier import FailoverReason
from hermes_constants import get_hermes_home


@pytest.fixture
def settings(monkeypatch):
    config = {
        "model": {"provider": "primary", "default": "claude-test"},
        "providers": {
            "primary": {"base_url": "https://primary.invalid", "api_mode": "anthropic_messages", "key_env": "TEST_PRIMARY_KEY"},
            "backup": {"base_url": "https://backup.invalid/v1", "api_mode": "chat_completions", "key_env": "TEST_BACKUP_KEY"},
        },
        "fallback_providers": [{"provider": "backup", "model": "gemini-test", "api_mode": "chat_completions"}],
        "fallback_recovery": {"enabled": True, "probe_interval_seconds": 3600, "probe_timeout_seconds": 5},
    }
    monkeypatch.setenv("TEST_PRIMARY_KEY", "primary-test-key")
    monkeypatch.setenv("TEST_BACKUP_KEY", "backup-test-key")
    save(config)
    return config


def save(config):
    get_hermes_home().mkdir(parents=True, exist_ok=True)
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(config))


def agent_stub():
    route = recovery._policy()[0]
    agent = SimpleNamespace(**route, requested_provider=route["provider"],
                            _fallback_activated=False, _fallback_index=0,
                            _fallback_chain=[{"provider": "backup", "model": "gemini-test"}],
                            reasoning_config={"effort": "medium"})
    agent._primary_runtime = dict(route, requested_provider=route["provider"])
    def activate():
        agent.model = "gemini-test"
        agent.provider = "backup"
        agent._fallback_activated = True
        return True
    agent._try_activate_fallback = MagicMock(side_effect=activate)
    return agent


def state():
    with recovery._store() as db:
        row = db.execute("SELECT * FROM recovery WHERE id=1").fetchone()
        return dict(row) if row else None


@pytest.mark.parametrize("reason", [FailoverReason.billing, FailoverReason.rate_limit, FailoverReason.upstream_rate_limit,
                                    FailoverReason.timeout, FailoverReason.server_error, FailoverReason.overloaded,
                                    FailoverReason.auth, FailoverReason.model_not_found])
def test_outage_latches_and_routes_new_agents(settings, reason):
    agent = agent_stub()
    recovery.record_primary_failure(agent, reason)
    newcomer = agent_stub()
    assert recovery.keep_fallback_for_turn(newcomer)
    assert newcomer.model == "gemini-test"
    assert recovery.keep_fallback_for_turn(newcomer)
    newcomer._try_activate_fallback.assert_called_once()
    assert state()["reason"] == reason.value


@pytest.mark.parametrize("reason", [None, FailoverReason.content_policy_blocked, FailoverReason.format_error,
                                    FailoverReason.context_overflow, FailoverReason.thinking_signature])
def test_request_specific_errors_do_not_latch(settings, reason):
    recovery.record_primary_failure(agent_stub(), reason)
    assert state() is None


def test_disabled_policy_does_not_create_state(settings):
    agent = agent_stub()
    settings["fallback_recovery"]["enabled"] = False
    save(settings)
    recovery.record_primary_failure(agent, FailoverReason.billing)
    assert not recovery.keep_fallback_for_turn(agent)
    assert recovery.probe_due() == "disabled"
    assert not (get_hermes_home() / "fallback_recovery.db").exists()


def test_probe_only_hourly_and_only_restore_after_valid_response(settings, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: now[0])
    agent = agent_stub()
    recovery.record_primary_failure(agent, FailoverReason.billing)
    recovery.keep_fallback_for_turn(agent)
    agent._rate_limited_until = float("inf")
    with patch.object(recovery, "_probe", side_effect=[False, True]) as probe:
        now[0] += 3599
        assert recovery.probe_due() == "waiting"
        probe.assert_not_called()
        now[0] += 1
        assert recovery.probe_due() == "unavailable"
        assert recovery.keep_fallback_for_turn(agent)
        now[0] += 3599
        assert recovery.probe_due() == "waiting"
        now[0] += 1
        assert recovery.probe_due() == "recovered"
        assert not recovery.keep_fallback_for_turn(agent)
        assert agent._rate_limited_until == 0
        assert not recovery.keep_fallback_for_turn(agent_stub())
        assert recovery.probe_due() == "primary"
        assert probe.call_count == 2


def test_probes_survive_exceptions_without_early_retry(settings, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: now[0])
    recovery.record_primary_failure(agent_stub(), FailoverReason.timeout)
    now[0] += 3600
    with patch.object(recovery, "_probe", side_effect=TimeoutError):
        assert recovery.probe_due() == "unavailable"
        assert recovery.probe_due() == "waiting"
    assert not state()["healthy"]


def test_concurrent_schedulers_claim_only_one_probe(settings, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: now[0])
    recovery.record_primary_failure(agent_stub(), FailoverReason.billing)
    now[0] += 3600
    with patch.object(recovery, "_probe", return_value=True) as probe, ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: recovery.probe_due(), range(4)))
    assert results.count("recovered") == 1
    probe.assert_called_once()


def test_new_failure_supersedes_inflight_success(settings, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: now[0])
    agent = agent_stub()
    recovery.record_primary_failure(agent, FailoverReason.billing)
    now[0] += 3600
    def probe(*_):
        recovery.record_primary_failure(agent, FailoverReason.server_error)
        return True
    with patch.object(recovery, "_probe", side_effect=probe):
        assert recovery.probe_due() == "superseded"
    assert not state()["healthy"]


def test_explicit_model_and_profile_overrides_are_isolated(settings, monkeypatch, tmp_path):
    primary = agent_stub()
    other = agent_stub()
    other.model = other._primary_runtime["model"] = "other-model"
    recovery.record_primary_failure(other, FailoverReason.billing)
    assert state() is None
    recovery.record_primary_failure(primary, FailoverReason.billing)
    assert not recovery.keep_fallback_for_turn(other)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-profile"))
    save(settings)
    assert not recovery.keep_fallback_for_turn(agent_stub())


def test_repeated_failure_does_not_postpone_probe(settings, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: now[0])
    recovery.record_primary_failure(agent_stub(), FailoverReason.billing)
    due = state()["next_probe"]
    now[0] += 500
    recovery.record_primary_failure(agent_stub(), FailoverReason.billing)
    assert state()["next_probe"] == due


def test_multiplex_without_profile_scope_fails_closed(settings, monkeypatch):
    from agent.secret_scope import UnscopedSecretError

    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    monkeypatch.setattr(recovery, "get_hermes_home_override", lambda: None)
    with pytest.raises(UnscopedSecretError):
        recovery.probe_due()
    assert not (get_hermes_home() / "fallback_recovery.db").exists()


@pytest.fixture
def endpoint(settings):
    calls = []
    response_state = {"status": 200, "text": "OK"}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body, dict(self.headers)))
            status = response_state["status"] if body["model"] == "claude-test" else 200
            if status != 200:
                payload = {"type": "error", "error": {
                    "type": "rate_limit_error" if status == 429 else "api_error",
                    "message": "quota exhausted" if status == 429 else "upstream unavailable"}}
            elif self.path == "/v1/messages":
                payload = {"id": "msg_probe", "type": "message", "role": "assistant", "model": body["model"],
                           "content": [{"type": "text", "text": response_state["text"]}], "stop_reason": "end_turn",
                           "usage": {"input_tokens": 5, "output_tokens": 1}}
            else:
                payload = {"id": "chat_probe", "object": "chat.completion", "created": 0, "model": body["model"],
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": response_state["text"]}, "finish_reason": "stop"}]}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    settings["providers"]["primary"]["base_url"] = url
    settings["providers"]["backup"]["base_url"] = url + "/v1"
    save(settings)
    try:
        yield calls, response_state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions"])
def test_real_resolver_sdk_and_wire_probe(settings, endpoint, mode):
    settings["providers"]["primary"]["api_mode"] = mode
    if mode == "chat_completions":
        settings["providers"]["primary"]["base_url"] += "/v1"
    save(settings)
    route = recovery._policy()[0]
    calls, responses = endpoint
    assert recovery._probe(route, 5)
    assert len(calls) == 1
    assert calls[0][1]["model"] == "claude-test"
    assert calls[0][1]["messages"] == [{"role": "user", "content": "Reply with OK."}]
    responses["text"] = ""
    assert not recovery._probe(route, 5)
    responses["status"] = 429
    with pytest.raises(Exception):
        recovery._probe(route, 5)
    assert len(calls) == 3  # SDK retries are disabled for probes.


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("reasoning", [{"effort": "medium"}, None])
def test_real_agent_fallback_and_restore(settings, endpoint, monkeypatch, fresh, reasoning):
    from run_agent import AIAgent
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested="primary", target_model="claude-test")
    with patch("run_agent.get_tool_definitions", return_value=[]), patch("run_agent.check_toolset_requirements", return_value={}):
        agent = AIAgent(model="claude-test", provider=runtime["provider"],
                        requested_provider=runtime["requested_provider"],
                        api_key=runtime["api_key"], base_url=runtime["base_url"], api_mode=runtime["api_mode"],
                        quiet_mode=True, skip_memory=True, skip_context_files=True,
                        reasoning_config=reasoning, fallback_model=settings["fallback_providers"])
    now = [10000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: now[0])
    prompt_before = agent._cached_system_prompt
    if fresh:
        recovery.record_primary_failure(agent_stub(), FailoverReason.billing)
        assert not agent._restore_primary_runtime()
    else:
        assert agent._try_activate_fallback(reason=FailoverReason.billing)
    assert agent.model == "gemini-test"
    assert not agent._restore_primary_runtime()
    assert agent.model == "gemini-test"
    now[0] += 3600
    assert recovery.probe_due() == "recovered"
    assert agent._restore_primary_runtime()
    assert agent.model == "claude-test"
    assert agent.api_mode == "anthropic_messages"
    assert agent.reasoning_config == reasoning
    assert agent._cached_system_prompt == prompt_before


@pytest.mark.parametrize("status", [429, 500, 503, 401])
def test_chat_error_to_gemini_then_probe_and_claude(settings, endpoint, monkeypatch, status):
    """Real HTTP + SDKs + error classifier + conversation loop, no upstreams."""
    from run_agent import AIAgent
    from hermes_cli.runtime_provider import resolve_runtime_provider

    settings["agent"] = {"api_max_retries": 1}
    save(settings)
    runtime = resolve_runtime_provider(requested="primary", target_model="claude-test")
    with patch("run_agent.get_tool_definitions", return_value=[]), patch("run_agent.check_toolset_requirements", return_value={}):
        agent = AIAgent(model="claude-test", provider=runtime["provider"],
                        requested_provider=runtime["requested_provider"],
                        api_key=runtime["api_key"], base_url=runtime["base_url"], api_mode=runtime["api_mode"],
                        quiet_mode=True, skip_memory=True, skip_context_files=True,
                        fallback_model=settings["fallback_providers"])
    agent._disable_streaming = True
    agent.compression_enabled = False
    agent._cached_system_prompt = "Keep the same system prompt."
    calls, responses = endpoint
    responses["status"] = status
    now = [10000.0]
    monkeypatch.setattr(recovery.time, "time", lambda: now[0])
    result = agent.run_conversation("hello")
    assert result["final_response"] == "OK", result
    assert agent.model == "gemini-test"
    assert not state()["healthy"]
    before = len(calls)
    assert agent.run_conversation("hello again")["final_response"] == "OK"
    assert all(body["model"] == "gemini-test" for _, body, _ in calls[before:])
    now[0] += 3600
    responses["status"] = 200
    assert recovery.probe_due() == "recovered"
    before = len(calls)
    assert agent.run_conversation("hello after recovery")["final_response"] == "OK"
    assert all(body["model"] == "claude-test" for _, body, _ in calls[before:])
    assert agent._cached_system_prompt == "Keep the same system prompt."
