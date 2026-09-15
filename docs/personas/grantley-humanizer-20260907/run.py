"""Paired current-baseline evaluation, using the existing isolated remote sampler.

No QQ messages, live sessions or production configuration are changed.
"""
import argparse
import hashlib
import json
import random
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from plugins.grantley.persona import load_persona_document, split_persona_document, render_state_block
from plugins.grantley.channel_binding import render_channel_prompt
from plugins.grantley.state import PersonaState


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "holdout", "regression"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--scenarios", type=Path, default=HERE / "scenarios.json")
    parser.add_argument("--variants", nargs="+", choices=["before", "after"], default=["before", "after"])
    args = parser.parse_args()
    saved = json.loads((HERE / "baseline.json").read_text())
    variants = {"before": split_persona_document(saved["files"]["plugins/grantley/assets/grantley.md"]["text"]),
                "after": load_persona_document()}
    if args.split == "regression":
        cases = json.loads(saved["files"]["docs/personas/grantley-evaluation/cases.json"]["text"])
        for case in cases:
            case.update(id=f"R{case['id']:02}", turns=[case["prompt"]], split="regression")
    else:
        cases = [c for c in json.loads(args.scenarios.read_text()) if c["split"] == args.split]
    jobs = []
    channel = render_channel_prompt({"is_group": True, "channel_owner_id": "owner"})
    for case in cases:
        sender = case.get("speaker", "17")
        # Historical owner/friend labels are deliberately NOT Argo's real ID.
        sender = {"owner": "18", "friend": "17"}.get(sender, sender)
        for variant in args.variants:
            doc = variants[variant]
            state = PersonaState(persona_id="grantley", state_json=case.get("state", {}))
            sidecar = render_state_block(doc, state)
            turns = [f"[Current QQ sender ID: {sender}]\n[New message]\n{text}\n\n"
                     f"<memory-context>\n{sidecar}\n{case.get('context', '')}\n{case.get('extra', '')}\n</memory-context>"
                     for text in case["turns"]]
            for replicate in range(1, 2 if args.split == "regression" else 4):
                jobs.append({"id": case["id"], "split": args.split, "variant": variant,
                             "replicate": replicate,
                             "messages": [{"role": "system", "content": doc.stable + "\n\n" + channel}],
                             "turns": turns})
    random.Random(90726).shuffle(jobs)
    payload = {"hermes_home": "/opt/hermes/data", "expected_model": "gpt-6-astra",
               "settings": {"temperature": 0.4, "max_tokens": 1800},
               "workers": args.workers, "jobs": jobs}
    metadata = {"created_at": datetime.now(timezone.utc).isoformat(), "cases": cases,
                "variants": {v: {"stable": variants[v].stable, "source": variants[v].source,
                                   "sha256": hashlib.sha256(variants[v].source.encode()).hexdigest()}
                             for v in args.variants}, "payload": payload,
                "transport": "SSH/docker exec; configured provider; no QQ or session writes"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        args.output.with_suffix(".inputs.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        remote = (HERE.parent / "grantley-evaluation/remote_runner.py").read_text()
        command = shlex.join(["docker", "exec", "-i", "hermes-cloud", "python", "-c", remote])
        process = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                    "cornna-cn", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        process.stdin.write(json.dumps(payload))
        process.stdin.close()
        for line in process.stdout:
            row = json.loads(line)
            output.write(line)
            output.flush()
            print(f"{row['variant']} {row['id']} sample={row['replicate']} turn={row['turn']}: "
                  f"{row.get('error', 'ok')} ({row['elapsed_seconds']}s)", flush=True)
        stderr = process.stderr.read()
        if process.wait():
            raise SystemExit(f"Remote sampler failed; stderr suppressed ({len(stderr)} chars)")


if __name__ == "__main__":
    main()
