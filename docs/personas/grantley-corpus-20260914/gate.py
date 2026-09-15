"""机械门禁：在**采样出的模型输出**上判定，不在提示词字节上判定。

三级纪律（见本目录 README.md §P5）：
  二级 绝对门（blocking）—— 人格文档自己已声明的规则，转成可执行断言：
       M4 禁用式命中 = 0、M5 示范逐字泄漏 = 0、M3 [LONG_REPLY] 用对。
  一级 漂移带（blocking，单向棘轮）—— 阈值来自**当前已验收版本**的实测分布 + 容差。
       它是漂移探测器，不主张审美理想，也不可能让定义它的那一版失败。
  三级 只报告（永不 blocking）—— 人类语域锚点与前任模型分布并列打印，供人判断。

与被删掉的旧断言（tests/plugins/grantley/test_persona_rendering.py:104-105 的注释）
的区别是结构性的：旧断言钉的是提示词字节，作者合法改一句话就红；这里量的是行为分布，
只有分布变了才红。所以本文件**不进 pytest**——pytest 只测尺子本身。

用法:
  gate.py --samples new.jsonl --baseline accepted.jsonl [--freeze thresholds.json]
  gate.py --samples new.jsonl --thresholds thresholds.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))

import fingerprint as F  # noqa: E402

VOICE_ASSET = HERE.parents[2] / "plugins/grantley/assets/voice.md"

#: 一级漂移带的容差。方向全部是"不许比已验收版更差"，不设理想值。
TOLERANCE = {
    "reply_p90_max_factor": 1.5,      # 不许显著更长
    "bubbles_ge2_share_drop": 0.15,   # 不许进一步塌缩成单气泡
    "modal_per_100_drop": 0.0,        # 语气词密度不许再低（单向棘轮）
    "tail_period_share_rise": 0.05,   # 句号收尾率不许再升
}


def load_outputs(path: Path, variant: str | None = None) -> list[str]:
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if variant and row.get("variant") != variant:
            continue
        if row.get("output"):
            out.append(row["output"])
    return out


def demo_replies() -> list[str]:
    """从 voice.md 抽出示范回复，用于 M5 逐字泄漏检测。"""
    text = VOICE_ASSET.read_text(encoding="utf-8")
    return [m.group(1).strip() for m in re.finditer(r"^我：(.+)$", text, re.M)]


def thresholds_from(samples: list[str], source: str) -> dict:
    """从已验收版本的采样现算一级漂移带。

    同时记下**现任自己**在二级绝对门上的违规数。二级是"人格文档已声明的规则"，
    不因现任违规而放宽；把它记在阈值文件里，是为了让读报告的人知道这道门当下
    对现任也是红的，而不是让候选背锅。
    """
    m = F.measure(samples, "baseline").to_dict()
    return {
        "thresholds_source": source,
        "n": m["n"],
        "incumbent_tier2_violations": {k: v for k, v in m["banned"].items() if v},
        "reply_p90": m["reply_p90"],
        "bubbles_ge2_share": m["bubbles_ge2_share"],
        "modal_per_100": m["modal_per_100"],
        "tail_period_share": m["tail_period_share"],
        "tolerance": TOLERANCE,
    }


def evaluate(samples: list[str], th: dict) -> tuple[list[str], list[str], dict]:
    """返回 (二级失败, 一级失败, 实测)。"""
    m = F.measure(samples, "candidate").to_dict()
    demos = demo_replies()

    tier2: list[str] = []
    banned = {k: v for k, v in m["banned"].items() if v}
    if banned:
        tier2.append(f"M4 禁用式命中非零: {banned}")
    if m["unmarked_longform_share"]:
        bad = [t for t in samples if F.is_unmarked_longform(t)]
        tier2.append(
            f"M3 长/结构化回复未标 [LONG_REPLY]: {len(bad)}/{len(samples)} 条"
        )
    leaks = [t for t in samples if F.leaks_demo(t, demos)]
    if leaks:
        tier2.append(f"M5 示范逐字泄漏 {len(leaks)}/{len(samples)} 条（>=8 字连续子串）")

    tol = th.get("tolerance", TOLERANCE)
    tier1: list[str] = []
    cap = th["reply_p90"] * tol["reply_p90_max_factor"]
    if m["reply_p90"] > cap:
        tier1.append(f"M1 整条 p90 {m['reply_p90']} > 上限 {cap:.0f}（基线 {th['reply_p90']}）")
    floor = th["bubbles_ge2_share"] - tol["bubbles_ge2_share_drop"]
    if m["bubbles_ge2_share"] < floor:
        tier1.append(
            f"M2 >=2 气泡占比 {m['bubbles_ge2_share']:.1%} < 下限 {floor:.1%}"
            f"（基线 {th['bubbles_ge2_share']:.1%}）"
        )
    mfloor = th["modal_per_100"] - tol["modal_per_100_drop"]
    if m["modal_per_100"] < mfloor:
        tier1.append(
            f"M-语气词 {m['modal_per_100']} < 下限 {mfloor}（基线 {th['modal_per_100']}）"
        )
    pcap = th["tail_period_share"] + tol["tail_period_share_rise"]
    if m["tail_period_share"] > pcap:
        tier1.append(
            f"M-句号收尾 {m['tail_period_share']:.1%} > 上限 {pcap:.1%}"
            f"（基线 {th['tail_period_share']:.1%}）"
        )
    return tier2, tier1, m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, required=True)
    ap.add_argument("--variant", default=None, help="只取该 variant 的 output")
    ap.add_argument("--baseline", type=Path, help="已验收版本的采样，用于现算阈值")
    ap.add_argument("--baseline-variant", default="after")
    ap.add_argument("--thresholds", type=Path, help="读取已冻结的阈值 json")
    ap.add_argument("--freeze", type=Path, help="把本次算出的阈值写出以便冻结")
    args = ap.parse_args(argv)

    samples = load_outputs(args.samples, args.variant)
    if not samples:
        print("没有可评的样本", file=sys.stderr)
        return 2

    if args.thresholds:
        th = json.loads(args.thresholds.read_text(encoding="utf-8"))
    elif args.baseline:
        th = thresholds_from(
            load_outputs(args.baseline, args.baseline_variant), str(args.baseline)
        )
    else:
        print("需要 --baseline 或 --thresholds", file=sys.stderr)
        return 2
    if args.freeze:
        args.freeze.write_text(json.dumps(th, ensure_ascii=False, indent=2), encoding="utf-8")

    tier2, tier1, m = evaluate(samples, th)

    print(f"== 候选样本 n={m['n']}  阈值来源: {th['thresholds_source']} (n={th['n']}) ==")
    incumbent = th.get("incumbent_tier2_violations") or {}
    if incumbent:
        print(f"   ⚠ 现任自身也违反二级绝对门: {incumbent}（门禁不因此放宽）")
    print(f"   整条 p50/p90      {m['reply_p50']}/{m['reply_p90']}")
    print(f"   每气泡 p50/p90    {m['bubble_p50']}/{m['bubble_p90']}")
    print(f"   >=2 气泡占比      {m['bubbles_ge2_share']:.1%}")
    print(f"   句号收尾 / 无尾标点 {m['tail_period_share']:.1%} / {m['tail_no_punct_share']:.1%}")
    print(f"   语气词 /100字      {m['modal_per_100']}")
    print(f"   [LONG_REPLY] 占比  {m['long_reply_share']:.1%}")

    print("\n== 三级 · 只报告（不参与判定）==")
    print("   人类锚点 群183287894: 整条 p50/p90=8/21  无尾标点=90.9%  语气词=1.138")
    print("   真人对格兰说话:       整条 p50/p90=11/30 无尾标点=49.4%  语气词=1.92")
    print("   前任模型·聊天切片:   整条 p50/p90=44/113 >=2气泡=91.5% 语气词=0.949")
    print(f"   M6 追问率={m['question_tail_share']:.1%}  说教词/100条={m['preachy_per_100']}")
    print(f"   M7 签名词命中（只报告，绝不设下限）: {m['signature_sample_share']}")

    ok = True
    print("\n== 二级 · 绝对门（blocking）==")
    if tier2:
        ok = False
        for line in tier2:
            print(f"   FAIL {line}")
    else:
        print("   PASS 禁用式 0 命中、示范 0 泄漏")

    print("\n== 一级 · 漂移带（blocking，单向棘轮）==")
    if tier1:
        ok = False
        for line in tier1:
            print(f"   FAIL {line}")
    else:
        print("   PASS 未在任何已测轴上比已验收版更差")

    print(f"\n结论: {'PASS' if ok else 'FAIL'}（机械门禁只是预筛；最终 blocking 门是匿名 A/B 盲评）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
