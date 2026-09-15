"""三臂采样：p2 现役 / 已验收未部署 / 本轮候选。

第三方真人文本的处理：闲聊刺激集从原始导出文件按固定种子抽取，**不落盘**；
输出里只保留每条刺激的 sha256（`--keep-messages` 默认关）。官方 cases.json 是
本仓库自撰用例，可以保留 messages。

用法:
  sample.py --mode stimuli --output stimuli.jsonl [--n 24]
  sample.py --mode cases   --output cases.jsonl --keep-messages
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import fingerprint as F  # noqa: E402
from plugins.grantley.channel_binding import render_channel_prompt  # noqa: E402
from plugins.grantley.persona import (  # noqa: E402
    load_voice_document,
    render_placeholders,
    split_persona_document,
)
from plugins.grantley.state import PersonaState  # noqa: E402

EVAL = REPO / "docs/personas/grantley-evaluation"
SSH_HOST = "root@149.118.61.165"          # cornna-p2；config 里的 cornna-p2-root 的跳板已死
HERMES_HOME = "/opt/hermes/data"

#: 固定状态，保证三臂之间唯一的变量是人格资产本身。
FIXED_STATE = {
    "mood": "还行", "fatigue": 0.3,
    "recent_topics": ["训练", "食堂"],
    "life_activity": "刚从训练场回来", "life_location": "宿舍楼下",
    "life_companions": ["亚戈"], "life_state": "at_academy",
    "life_story_arc": "新学期头几周",
}


def arms() -> dict[str, dict[str, str]]:
    """三条臂。channel 段三臂共用，唯一变量是身份/声音资产。"""
    snapshot = json.loads((EVAL / "baseline.json").read_text())["channel_snapshot"]
    channel = render_channel_prompt(snapshot)

    def doc(path: Path):
        return split_persona_document(path.read_text(encoding="utf-8"))

    p2 = doc(HERE / "before/grantley.md.p2-deployed")
    accepted = doc(HERE / "before/grantley.md")
    candidate = doc(REPO / "plugins/grantley/assets/grantley.md")
    voice = load_voice_document()
    return {
        "p2_current": {"stable": p2.stable, "volatile": p2.volatile, "channel": channel},
        "accepted": {"stable": accepted.stable, "volatile": accepted.volatile, "channel": channel},
        "candidate": {
            "stable": candidate.stable + "\n\n" + voice,
            "volatile": candidate.volatile,
            "channel": channel,
        },
    }


def stimuli(n: int, seed: int = 20260914) -> list[dict]:
    """从真实 user 侧抽短闲聊刺激。只返回文本给运行时用，不写盘。"""
    rows = [r for r in F.load_mk_observations() if F.provenance(r) == "human_chat"]
    pool: list[str] = []
    for row in rows:
        text = F.normalize(row.get("user_text") or "")
        if not (3 <= len(text) <= 20):
            continue
        if re.search(r"\[CQ:|<system>|http|说说|QQ\s*空间", text):
            continue
        pool.append(text)
    pool = sorted(set(pool))
    random.Random(seed).shuffle(pool)
    picked = pool[:n]
    return [
        {"id": f"S{i:02d}", "text": t,
         "sha256": hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]}
        for i, t in enumerate(picked, 1)
    ]


def build_jobs(mode: str, n: int, replicates: int,
               only: list[str] | None = None) -> tuple[list[dict], dict]:
    variants = arms()
    if only:
        variants = {k: v for k, v in variants.items() if k in only}
    state = PersonaState(persona_id="grantley", state_json=FIXED_STATE)
    jobs: list[dict] = []
    meta: dict = {"mode": mode, "arms": {}}
    for name, parts in variants.items():
        meta["arms"][name] = {
            "stable_chars": len(parts["stable"]),
            "stable_sha256": hashlib.sha256(parts["stable"].encode()).hexdigest(),
        }

    if mode == "stimuli":
        items = stimuli(n)
        meta["stimuli_count"] = len(items)
        meta["stimuli_set_sha256"] = hashlib.sha256(
            "\n".join(i["text"] for i in items).encode()
        ).hexdigest()
        meta["stimuli_ids"] = [{"id": i["id"], "sha256": i["sha256"]} for i in items]
        for item in items:
            for name, parts in variants.items():
                context = render_placeholders(parts["volatile"], state)
                user = (
                    f"当前发送者 id=1076712858。\n{item['text']}\n\n"
                    f"<memory-context><persona-state>\n{context}\n"
                    "</persona-state></memory-context>"
                )
                for rep in range(1, replicates + 1):
                    jobs.append({
                        "id": item["id"], "variant": name, "replicate": rep,
                        "split": "stimuli", "stimulus_sha256": item["sha256"],
                        "messages": [
                            {"role": "system",
                             "content": parts["stable"] + "\n\n" + parts["channel"]},
                            {"role": "user", "content": user},
                        ],
                    })
    else:
        cases = json.loads((EVAL / "cases.json").read_text())
        meta["cases_sha256"] = hashlib.sha256((EVAL / "cases.json").read_bytes()).hexdigest()
        for case in cases:
            case_state = PersonaState(persona_id="grantley", state_json=case.get("state", FIXED_STATE))
            for name, parts in variants.items():
                context = render_placeholders(parts["volatile"], case_state)
                user = (
                    f"当前发送者 id={case.get('speaker', 'friend')}。\n{case['prompt']}\n\n"
                    f"<memory-context><persona-state>\n{context}\n{case.get('extra', '')}"
                    "\n</persona-state></memory-context>"
                )
                jobs.append({
                    "id": case["id"], "variant": name, "replicate": 1, "split": "cases",
                    "messages": [
                        {"role": "system",
                         "content": parts["stable"] + "\n\n" + parts["channel"]},
                        {"role": "user", "content": user},
                    ],
                })
    return jobs, meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["stimuli", "cases"], required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--replicates", type=int, default=1)
    ap.add_argument("--arms", nargs="+", default=None,
                    help="只跑这些臂（重采某一臂时用，其余臂复用既有输出）")
    ap.add_argument("--keep-messages", action="store_true")
    ap.add_argument("--ssh-host", default=SSH_HOST)
    ap.add_argument("--hermes-home", default=HERMES_HOME)
    args = ap.parse_args(argv)

    jobs, meta = build_jobs(args.mode, args.n, args.replicates, args.arms)
    meta.update(created_at=datetime.now(timezone.utc).isoformat(),
                ssh_host=args.ssh_host, hermes_home=args.hermes_home,
                job_count=len(jobs), keep_messages=args.keep_messages,
                transport="SSH to configured provider; no gateway/tool execution")
    payload = {"hermes_home": args.hermes_home, "jobs": jobs,
               "keep_messages": args.keep_messages,
               "settings": {"temperature": 0.4, "max_tokens": 1200}}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(jobs)} 个请求 -> {args.output}")

    command = "python3 -c " + shlex.quote((HERE / "remote_sampler.py").read_text())
    with args.output.open("x") as out:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", args.ssh_host, command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.stdin.write(json.dumps(payload, ensure_ascii=False))
        proc.stdin.close()
        done = errors = 0
        for line in proc.stdout:
            out.write(line)
            out.flush()
            row = json.loads(line)
            done += 1
            if row.get("error"):
                errors += 1
                print(f"  ERROR {row['variant']} {row['id']}: {row['error'][:80]}")
        stderr = proc.stderr.read()
        code = proc.wait()
    print(f"完成 {done}/{len(jobs)}，错误 {errors}")
    if code:
        raise SystemExit(f"远端采样失败 (exit {code})；stderr 已隐去（{len(stderr)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
