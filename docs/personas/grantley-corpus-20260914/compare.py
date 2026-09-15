"""三臂对照：把每条臂放在同一把尺子上，并列人类锚点。

只读采样 jsonl，不发请求。判定权在 gate.py（机械门禁）与 grade.py（匿名盲评）；
本脚本只负责把差异看清楚。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fingerprint as F  # noqa: E402

ANCHORS = {
    "真人·他的绑定群": {"reply_p50": 8, "reply_p90": 21, "tail_no_punct_share": 0.909,
                        "modal_per_100": 1.138, "bubbles_ge2_share": None},
    "真人·对格兰说话": {"reply_p50": 11, "reply_p90": 30, "tail_no_punct_share": 0.494,
                        "modal_per_100": 1.92, "bubbles_ge2_share": None},
    "前任模型·聊天切片": {"reply_p50": 44, "reply_p90": 113, "tail_no_punct_share": 0.001,
                          "modal_per_100": 0.949, "bubbles_ge2_share": 0.915},
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, nargs="+", required=True)
    args = ap.parse_args(argv)

    by_variant: dict[str, list[str]] = collections.defaultdict(list)
    errors: collections.Counter = collections.Counter()
    for path in args.samples:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("error") or not row.get("output"):
                errors[row.get("variant", "?")] += 1
                continue
            by_variant[row["variant"]].append(row["output"])

    cols = ["n", "整条p50", "整条p90", "泡p50", "≥2泡", "尾。", "无尾标点", "语气词", "LONG_REPLY", "该标未标"]
    print(f"{'臂':22s}" + "".join(f"{c:>10s}" for c in cols))
    print("-" * (22 + 10 * len(cols)))
    order = ["p2_current", "accepted", "candidate"]
    for name in [n for n in order if n in by_variant] + [n for n in by_variant if n not in order]:
        m = F.measure(by_variant[name], name).to_dict()
        print(f"{name:22s}"
              f"{m['n']:>10d}{m['reply_p50']:>10d}{m['reply_p90']:>10d}{m['bubble_p50']:>10d}"
              f"{m['bubbles_ge2_share']:>9.0%} {m['tail_period_share']:>9.0%}"
              f"{m['tail_no_punct_share']:>10.0%}{m['modal_per_100']:>10.3f}"
              f"{m['long_reply_share']:>10.0%}{m['unmarked_longform_share']:>10.0%}")
        banned = {k: v for k, v in m["banned"].items() if v}
        if banned:
            print(f"{'':22s}  ⚠ 禁用式: {banned}")
    print("-" * (22 + 10 * len(cols)))
    for name, a in ANCHORS.items():
        ge2 = f"{a['bubbles_ge2_share']:>9.0%} " if a["bubbles_ge2_share"] is not None else f"{'—':>10s}"
        print(f"{name:22s}{'—':>10s}{a['reply_p50']:>10d}{a['reply_p90']:>10d}{'—':>10s}"
              f"{ge2}{'—':>10s}{a['tail_no_punct_share']:>10.0%}{a['modal_per_100']:>10.3f}{'—':>10s}")
    if errors:
        print(f"\n采样错误: {dict(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
