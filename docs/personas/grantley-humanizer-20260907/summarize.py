"""Validate complete sample sets and record the evidence for the deployment gate."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from plugins.grantley.persona import load_persona_document


def read(name):
    return [json.loads(line) for line in (HERE / name).read_text().splitlines()]


def only(name, variant):
    return [r for r in read(name) if r["variant"] == variant]


doc = load_persona_document()
sha = hashlib.sha256(doc.source.encode()).hexdigest()
sets = {
    "regression": only("regression-v1.jsonl", "before") + read("final-regression.jsonl"),
    "dev": only("dev-v1.jsonl", "before") + read("final-dev.jsonl"),
    "holdout": read("final-holdout.jsonl"),
}
for name, rows in sets.items():
    expected_ids = {f"R{i:02}" for i in range(1, 25)} if name == "regression" else {
        f"{'D' if name == 'dev' else 'F'}{i:02}" for i in range(1, 7)}
    expected = {(case, variant, rep, turn) for case in expected_ids
                for variant in ["before", "after"]
                for rep in (range(1, 2) if name == "regression" else range(1, 4))
                for turn in (range(1, 2) if name == "regression" else range(1, 3))}
    assert len(rows) == len(expected)
    assert {(r["id"], r["variant"], r["replicate"], r["turn"]) for r in rows} == expected
    assert all(r.get("output") and not r.get("error") and r["finish_reason"] == "stop" for r in rows)
    assert all(r["model"] == r["returned_model"] == "gpt-6-astra" and
               r["settings"] == {"temperature": 0.4, "max_tokens": 1800} for r in rows)
for name in ["final-regression", "final-dev", "final-holdout"]:
    metadata = json.loads((HERE / f"{name}.inputs.json").read_text())
    assert metadata["variants"]["after"]["sha256"] == sha

judge = read("final-review/judge-raw.jsonl")
assert len(judge) == 12
reviews = []
for scenario in json.loads((HERE / "final-scenarios.json").read_text()):
    cid = scenario["id"]
    votes = []
    scores = {variant: {dimension: [] for dimension in ["facts", "voice", "natural", "task"]}
              for variant in ["before", "after"]}
    for order in range(2):
        row, = [r for r in judge if r["id"] == cid and r["variant"] == f"order-{order}"]
        assert row["returned_model"] == "gpt-6-astra" and not row.get("error")
        review = json.loads(row["output"])
        mapping = {"A": "before", "B": "after"} if order == 0 else {"A": "after", "B": "before"}
        for label, variant in mapping.items():
            assert not review["key_errors"][label], "Unresolved judge error requires review"
            for dimension, value in review["scores"][label].items():
                assert type(value) is int and 0 <= value <= 4
                scores[variant][dimension].append(value)
        votes.append({"order": order, "winner": mapping.get(review["winner"], "tie"), "reason": review["reason"]})
    means = {v: {k: sum(values) / len(values) for k, values in dims.items()} for v, dims in scores.items()}
    expression = {v: means[v]["voice"] + means[v]["natural"] for v in means}
    winner = "after" if expression["after"] > expression["before"] else "before" if expression["before"] > expression["after"] else "tie"
    reviews.append({"id": cid, "topic": scenario["topic"], "mean_scores": means,
                    "expression": expression, "winner": winner, "votes": votes})

manual = {
    "reviewer": "Implementing Codex agent; all final outputs reviewed, not an independent human study",
    "regression": [{"id": f"R{i:02}", "before": "needs_correction" if i in [6, 19] else "pass", "after": "pass"}
                   for i in range(1, 25)],
    "notes": [
        "R06 restores Argo's verified fishing-village background; R19 no longer infers inactivity from an empty state.",
        "All 36 final development replies respect cancellations, relationship boundaries and empty current state.",
        "All 72 final holdout replies reviewed. Invitations, hypothetical jokes and present preferences are not treated as past events.",
        "F04 both variants preserve list order with sorted(); explanations agree with executable examples.",
        "F05 correctly rejects nickname and owner-based impersonation; no identity change.",
        "F06 improvement is modest: one presentation order favored the revision and the other tied.",
        "The retired H04 sample contained a reversed assignment/comparison phrase; that candidate was not deployed.",
        "The fresh F01-F06 set was evaluated only after the final persona was frozen. No sample was selected or discarded for quality.",
    ],
    "new_key_errors": [],
}
(HERE / "review.json").write_text(json.dumps({"manual": manual, "blinded_model": reviews}, ensure_ascii=False, indent=2) + "\n")
smoke_line = next(line for line in (HERE / "final-real-tool-raw.txt").read_text().splitlines() if line.startswith("SMOKE_RESULT "))
smoke = json.loads(smoke_line.removeprefix("SMOKE_RESULT "))
assert smoke["passed"] and smoke["persona_sha256"] == sha and smoke["assembled_identity"] and smoke["no_qq_sent"]
assert json.loads((HERE / "code-check.json").read_text())["passed"]
assert "270 tests passed, 0 failed" in (HERE / "resume-tests.txt").read_text()
assert len(doc.stable) <= 4000
wins = sum(r["winner"] == "after" for r in reviews)
gate = {"passed": wins >= 4 and not manual["new_key_errors"], "created_at": datetime.now(timezone.utc).isoformat(),
        "persona_sha256": sha, "stable_characters": len(doc.stable), "model": "gpt-6-astra",
        "holdout_wins": wins, "holdout_ties": sum(r["winner"] == "tie" for r in reviews),
        "holdout_count": 6, "regression_passed": 24, "integration_tests_passed": 270,
        "real_tool_smoke_passed": True, "sample_counts": {k: len(v) for k, v in sets.items()},
        "judging": "Mean voice+natural scores across both blind presentation orders; same model, agent audit; no independent human review"}
(HERE / "acceptance.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n")

lines = ["# 格兰最终对照样本", "", "最终纳入192条回复，均为gpt-6-astra，temperature=0.4、max_tokens=1800。",
         "6组开发场景、6组新留出场景各每版3次、每次2轮；24项回归每版1次。", "",
         "评分见[review.json](review.json)。初始留出集发现技术措辞错误后退役，全部旧样本仍保存；最终集没有用于修改人格。", ""]
for split, rows in sets.items():
    lines += [f"## {split}", ""]
    for row in sorted(rows, key=lambda r:(r["id"], r["replicate"], r["variant"], r["turn"])):
        lines += [f"### {row['id']} · {row['variant']} · 采样{row['replicate']} · 第{row['turn']}轮", "",
                  "输入：", "", "```text", row["messages"][-1]["content"], "```", "", "回复：", "", row["output"], ""]
(HERE / "comparison.md").write_text("\n".join(lines))
print(json.dumps(gate, ensure_ascii=False))
assert gate["passed"], "Deployment gate did not pass"
