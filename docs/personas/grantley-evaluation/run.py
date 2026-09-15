"""Capture paired samples with identical model/settings on an existing SSH host.

Usage: .venv/bin/python docs/personas/grantley-evaluation/run.py \
    --ssh-host corlinman-prod --hermes-home /opt/hermes/data --output samples.jsonl
Use a fresh output filename for a rerun; saved samples are never overwritten.
"""
import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from plugins.grantley.channel_binding import render_channel_prompt
from plugins.grantley.persona import load_persona_document, render_placeholders
from plugins.grantley.state import PersonaState

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--ssh-host", required=True)
parser.add_argument("--hermes-home", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
base = json.loads((HERE / "baseline.json").read_text())
cases = json.loads((HERE / "cases.json").read_text())
current = load_persona_document()
snapshot = base["channel_snapshot"]
variants = {
    "before": {"stable": base["stable"], "volatile": base["volatile"], "channel": base["channel_prompt"]},
    "after": {"stable": current.stable, "volatile": current.volatile, "channel": render_channel_prompt(snapshot)},
}
jobs = []
for case in cases:
    state = PersonaState(persona_id="grantley", state_json=case.get("state", {}))
    for variant, parts in variants.items():
        context = render_placeholders(parts["volatile"], state)
        user = (f"当前发送者 id={case.get('speaker', 'friend')}。\n{case['prompt']}\n\n"
                f"<memory-context><persona-state>\n{context}\n{case.get('extra', '')}"
                "\n</persona-state></memory-context>")
        jobs.append({"id": case["id"], "variant": variant, "messages": [
            {"role": "system", "content": parts["stable"] + "\n\n" + parts["channel"]},
            {"role": "user", "content": user},
        ]})
payload = {"hermes_home": args.hermes_home, "jobs": jobs}
meta = {"created_at": datetime.now(timezone.utc).isoformat(), "variants": variants,
        "cases_sha256": hashlib.sha256((HERE / "cases.json").read_bytes()).hexdigest(),
        "jobs": jobs, "transport": "SSH to configured provider; no gateway/tool execution"}
args.output.parent.mkdir(parents=True, exist_ok=True)
# Exclusive creation makes accidental loss of the original comparison impossible.
with args.output.open("x") as output:
    args.output.with_suffix(".inputs.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    command = "python3 -c " + shlex.quote((HERE / "remote_runner.py").read_text())
    process = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                args.ssh_host, command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    process.stdin.write(json.dumps(payload, ensure_ascii=False))
    process.stdin.close()
    for line in process.stdout:
        output.write(line)
        output.flush()
        row = json.loads(line)
        print(f"{row['variant']} {row['id']:02}: {'ERROR '+row['error'] if row.get('error') else 'ok'}", flush=True)
    stderr = process.stderr.read()
    code = process.wait()
    if code:
        # Remote traceback may contain configuration text: keep it out of outputs.
        raise SystemExit(f"Remote evaluator failed (exit {code}); stderr omitted ({len(stderr)} chars)")
