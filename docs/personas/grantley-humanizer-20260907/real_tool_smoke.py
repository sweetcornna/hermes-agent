"""Real AIAgent/provider/file+terminal execution in a temporary HERMES_HOME.

Credentials are read only on the existing server. No messaging tools/adapters
are started. The only output sink is this script's JSON report.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml
from dotenv import dotenv_values

payload = json.load(sys.stdin)
production = Path("/opt/hermes/data")
source = Path("/opt/hermes/repo")
cfg = yaml.safe_load((production / "config.yaml").read_text())
provider = cfg["providers"][cfg["model"]["provider"]]
key = provider.get("api_key") or dotenv_values(production / ".env")[provider.get("api_key_env", "CORNNA_API_KEY")]
model = cfg["model"]["default"]
assert model == "gpt-6-astra"
sys.path.insert(0, str(source))

with tempfile.TemporaryDirectory(prefix="grantley-humanizer-e2e-") as temporary:
    home = Path(temporary)
    work = home / "work"
    work.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    for name in list(os.environ):
        if any(part in name for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(name, None)
    test_cfg = {
        "model": {"default": model, "provider": "cornna", "context_length": 200000},
        "providers": {"cornna": {"base_url": provider["base_url"], "api_mode": "chat_completions"}},
        "memory": {"provider": "grantley"},
        "terminal": {"backend": "local", "cwd": str(work)},
        "compression": {"enabled": False},
        "plugins": {"enabled": [], "entries": {"grantley": {"settings": {
            "inject_identity_section": True,
            "channels": {"90726001": {"persona": "grantley", "group": False}},
        }}}},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(test_cfg, allow_unicode=True))
    shutil.copytree(production / "plugins/grantley", home / "plugins/grantley",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (home / "plugins/grantley/assets/grantley.md").write_text(payload["persona"])
    os.chdir(work)
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    db = SessionDB(db_path=home / "state.db")
    session_id = "humanizer-real-tool-smoke"
    agent = AIAgent(
        api_key=key, base_url=provider["base_url"], provider="custom", requested_provider="cornna",
        api_mode="chat_completions", model=model, session_db=db, session_id=session_id,
        platform="onebot", chat_id="90726001", chat_type="dm", user_id="90726001",
        quiet_mode=True, skip_context_files=True, skip_background_review=True,
        enabled_toolsets=["file", "terminal"], max_iterations=8, max_tokens=2200,
    )
    # Check the final persona is actually in the assembled prompt, not merely on disk.
    prompt = agent._build_system_prompt()
    assert payload["stable"] in prompt, "New identity did not reach the real agent"
    target = work / "checked.txt"
    text = "GRAN-90726\n"
    task = (f"[Current QQ sender ID: 90726001]\n这是一项隔离验证的真实文件任务。只在{work}中操作。"
            f"请实际创建{target}，内容精确为GRAN-90726加一个换行，随后调用工具读取文件并计算SHA-256。"
            "最后回复实际字节数和哈希；不要只给示例，不使用网络、消息或其他目录。")
    result = agent.run_conversation(task)
    messages = result.get("messages", [])
    calls = [call for m in messages for call in (m.get("tool_calls") or [])]
    actual = target.read_bytes()
    expected = hashlib.sha256(text.encode()).hexdigest()
    output = result.get("final_response", "")
    assert actual == text.encode()
    assert expected in output and str(len(actual)) in output
    assert calls and any(m.get("role") == "tool" for m in messages)
    assert not any("send_message" in json.dumps(call) for call in calls)
    print("SMOKE_RESULT " + json.dumps({
        "passed": True, "model": model, "persona_sha256": hashlib.sha256(payload["persona"].encode()).hexdigest(),
        "assembled_identity": True, "bytes": len(actual), "sha256": expected,
        "tool_names": [c["function"]["name"] for c in calls],
        "output": output, "messages": messages, "no_qq_sent": True,
    }, ensure_ascii=False), flush=True)
    db.close()
