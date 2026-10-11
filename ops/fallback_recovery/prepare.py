"""Prepare a narrow patch against the actual P1 files, without changing them.

Run with the production venv: prepare.py PAYLOAD_DIRECTORY STAGE_DIRECTORY.
Payload contains agent/fallback_recovery.py and the two systemd unit files.
The stage is private because its config backup may contain credentials.
"""

import hashlib
import json
from pathlib import Path
import sys

import yaml


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError("Source anchor missing or ambiguous; refuse to overwrite production")
    return text.replace(old, new, 1)


def prepare(payload, stage):
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = Path("/opt/hermes/repo")
    patches = {
        "agent/chat_completion_helpers.py": [
            ('    if reason in {FailoverReason.rate_limit, FailoverReason.billing, FailoverReason.upstream_rate_limit}:',
             '    from agent.fallback_recovery import record_primary_failure\n'
             '    record_primary_failure(agent, reason)\n\n'
             '    if reason in {FailoverReason.rate_limit, FailoverReason.billing, FailoverReason.upstream_rate_limit}:')],
        "agent/agent_runtime_helpers.py": [
            ('    if not agent._fallback_activated:',
             '    from agent.fallback_recovery import keep_fallback_for_turn\n'
             '    if keep_fallback_for_turn(agent):\n'
             '        return False\n\n'
             '    if not agent._fallback_activated:'),
            ('        if saved_reasoning is not None:\n'
             '            agent.reasoning_config = dict(saved_reasoning)',
             '        if "reasoning_config" in rt:\n'
             '            agent.reasoning_config = dict(saved_reasoning) if saved_reasoning is not None else None')],
        "agent/conversation_loop.py": [
            ('                            agent._buffer_status(f"⚠️ Non-retryable error (HTTP {status_code}) — trying fallback...")\n'
             '                    if agent._try_activate_fallback():',
             '                            agent._buffer_status(f"⚠️ Non-retryable error (HTTP {status_code}) — trying fallback...")\n'
             '                    if agent._try_activate_fallback(reason=classified.reason):'),
            ('                        agent._buffer_status(f"⚠️ Max retries ({max_retries}) exhausted — trying fallback...")\n'
             '                    if agent._try_activate_fallback():',
             '                        agent._buffer_status(f"⚠️ Max retries ({max_retries}) exhausted — trying fallback...")\n'
             '                    if agent._try_activate_fallback(reason=classified.reason):')],
    }
    manifest = []

    def add(target, content):
        existing = target.read_bytes() if target.exists() else None
        slot = str(len(manifest))
        if existing is not None:
            (stage / (slot + ".before")).write_bytes(existing)
        (stage / (slot + ".after")).write_bytes(content)
        manifest.append({"path": str(target), "slot": slot,
                         "before_sha256": hashlib.sha256(existing).hexdigest() if existing is not None else None,
                         "after_sha256": hashlib.sha256(content).hexdigest()})

    for name, replacements in patches.items():
        target = root / name
        text = target.read_text(encoding="utf-8")
        for old, new in replacements:
            text = replace_once(text, old, new)
        compile(text, str(target), "exec")
        add(target, text.encode())
    module = payload / "agent/fallback_recovery.py"
    compile(module.read_text(encoding="utf-8"), str(module), "exec")
    target = root / "agent/fallback_recovery.py"
    if target.exists():
        raise RuntimeError("Recovery module already exists; review deployment first")
    add(target, module.read_bytes())
    config_path = Path("/opt/hermes/data/config.yaml")
    text = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    assert config["model"]["default"] == "claude-opus-5-5"
    assert config["model"]["provider"] == "cornna-claude"
    assert config["providers"]["cornna"]["base_url"] == "https://api.cornna.xyz/v1"
    assert not any(config.get(k) for k in ("fallback_model", "fallback_providers", "fallback_recovery"))
    # Preserve existing YAML comments and ordering; reject duplicate keys.
    assert all(k not in config for k in ("fallback_providers", "fallback_recovery"))
    text += """
# Claude outage recovery: actual upstream probes are limited to once per hour.
fallback_providers:
  - provider: cornna
    model: gemini-3.8-flash-high
    base_url: https://api.cornna.xyz/v1
    api_mode: chat_completions
    key_env: CORNNA_API_KEY
fallback_recovery:
  enabled: true
  probe_interval_seconds: 3600
  probe_timeout_seconds: 30
"""
    yaml.safe_load(text)
    add(config_path, text.encode())
    for name in ("hermes-fallback-recovery.service", "hermes-fallback-recovery.timer"):
        target = Path("/etc/systemd/system") / name
        if target.exists():
            raise RuntimeError("Existing recovery unit requires review")
        add(target, (payload / name).read_bytes())
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"stage": str(stage), "files": [row["path"] for row in manifest], "live_files_changed": False}, indent=2))


if __name__ == "__main__":
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
