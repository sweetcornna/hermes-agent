"""宿主机侧采样：只读 config.yaml / .env，绝不修改主机任何状态。

与 grantley-evaluation/remote_runner.py 的唯一差别：不依赖 python-dotenv
（容器外没装），改用 stdlib 解析 .env。输出 schema 保持一致，所以
gate.py 与 grade.py 都能直接吃。凭据始终留在主机上。
"""
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

payload = json.load(sys.stdin)
root = Path(payload["hermes_home"])
config = yaml.safe_load((root / "config.yaml").read_text())
provider = config["model"]["provider"]
model = config["model"]["default"]
provider_config = config["providers"][provider]


def read_env(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


secrets = read_env(root / ".env")
key_name = provider_config.get("api_key_env") or f"{provider.upper()}_API_KEY"
api_key = provider_config.get("api_key") or secrets[key_name]
url = provider_config["base_url"].rstrip("/") + "/chat/completions"
settings = payload.get("settings", {"temperature": 0.4, "max_tokens": 1200})
if payload.get("expected_model") and model != payload["expected_model"]:
    raise SystemExit("Configured model changed; refusing unmatched comparison")
keep_messages = bool(payload.get("keep_messages", True))


def request_sample(job):
    messages = job["messages"]
    body = {"model": model, "messages": messages, **settings}
    result = {
        "id": job["id"], "variant": job["variant"], "model": model,
        "provider": provider, "settings": settings,
        "replicate": job.get("replicate", 1), "split": job.get("split", "regression"),
        "stimulus_sha256": job.get("stimulus_sha256"),
        "attempts": [],
    }
    if keep_messages:
        result["messages"] = messages
    start = time.monotonic()
    for attempt in range(2):
        try:
            request = urllib.request.Request(
                url, json.dumps(body).encode(),
                {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.load(response)
            choice = data["choices"][0]
            result.update(output=choice["message"].get("content"),
                          finish_reason=choice.get("finish_reason"),
                          returned_model=data.get("model"), usage=data.get("usage"))
            if not isinstance(result["output"], str) or not result["output"].strip():
                raise ValueError("empty response")
            if choice.get("finish_reason") == "length":
                raise ValueError("truncated response")
            result["attempts"].append({"attempt": attempt + 1, "ok": True})
            break
        except Exception as error:
            result["attempts"].append({"attempt": attempt + 1, "ok": False,
                                       "error": f"{type(error).__name__}: {error}"[:200]})
            if attempt:
                result["error"] = result["attempts"][-1]["error"]
            else:
                time.sleep(2)
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    return result


with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    for result in pool.map(request_sample, payload["jobs"]):
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
