"""Isolated API sampling on the configured host; no Hermes/gateway tools run.

Input: JSON payload on stdin. Output: one JSON record per API request.
Credentials remain on the host. This script never changes its config or state.
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
model_config = config["model"]
provider = model_config["provider"]
model = model_config["default"]
provider_config = config["providers"][provider]
from dotenv import dotenv_values

secrets = dotenv_values(root / ".env")
key_name = provider_config.get("api_key_env") or f"{provider.upper()}_API_KEY"
api_key = provider_config.get("api_key") or secrets[key_name]
url = provider_config["base_url"].rstrip("/") + "/chat/completions"
settings = payload.get("settings", {"temperature": 0.4, "max_tokens": 1200})
if payload.get("expected_model") and model != payload["expected_model"]:
    raise SystemExit("Configured model changed; refusing unmatched comparison")


def request_sample(job, messages, turn):
    body = {"model": model, "messages": messages, **settings}
    result = {"id": job["id"], "variant": job["variant"], "model": model,
              "provider": provider, "settings": settings,
              "replicate": job.get("replicate", 1), "turn": turn,
              "messages": messages, "split": job.get("split", "regression"),
              "attempts": []}
    start = time.monotonic()
    for attempt in range(2):
        try:
            request = urllib.request.Request(
                url, json.dumps(body).encode(),
                {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
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
            # Never print raw headers, URLs with credentials, or provider error bodies.
            result["error"] = type(error).__name__
            if isinstance(error, urllib.error.HTTPError):
                result["http_status"] = error.code
            result["attempts"].append({"attempt": attempt + 1, "ok": False,
                                      "error": result["error"],
                                      "http_status": result.get("http_status"),
                                      "output": result.get("output")})
            if attempt == 0:
                time.sleep(3)
    else:
        result["output"] = None
    if result.get("output"):
        result.pop("error", None)
        result.pop("http_status", None)
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    return result


def sample(job):
    messages = list(job["messages"])
    rows = []
    # Each continuation receives that variant's actual previous response.
    for turn, user_text in enumerate(job.get("turns", [None]), 1):
        if user_text is not None:
            messages.append({"role": "user", "content": user_text})
        result = request_sample(job, list(messages), turn)
        rows.append(result)
        if not result.get("output"):
            break
        messages.append({"role": "assistant", "content": result["output"]})
    return rows


with concurrent.futures.ThreadPoolExecutor(max_workers=payload.get("workers", 2)) as pool:
    jobs = [pool.submit(sample, job) for job in payload["jobs"]]
    for future in concurrent.futures.as_completed(jobs):
        for row in future.result():
            print(json.dumps(row, ensure_ascii=False), flush=True)
