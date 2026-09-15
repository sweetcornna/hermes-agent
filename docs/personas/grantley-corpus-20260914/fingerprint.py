"""格兰特利语料指纹：纯测量函数 + 报告生成。

设计边界（对应本轮计划 P5）：
  * 本模块不发任何 API 请求。它只读已有的 ndjson / jsonl / sqlite。
  * 所有指标都作用在**模型输出文本**或**人类消息文本**上，绝不断言提示词字节。
    被删除的旧断言（test_persona_rendering.py:104-105 的注释）是对提示词做
    substring/sha 快照，作者合法改一句话就红；这里量的是行为分布。
  * pytest 只应测「尺子本身」：给定人造文本，计数器返回 N。模型行为的判定
    发生在评测（gate.py 读采样 jsonl），不在单元测试里。

CLI:
  fingerprint.py report [--json out.json]   # 量全部语料源，打印三级对照表
  fingerprint.py verify                     # 重算并与 FINGERPRINT.md 内嵌数字比对
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

BUBBLE_SEPARATOR = "[MSG_BREAK]"      # plugins/platforms/onebot/adapter.py:165
LONG_REPLY_MARKER = "[LONG_REPLY]"    # plugins/platforms/onebot/adapter.py:166

_CQ = re.compile(r"\[CQ:[^\]]*\]")
_ZERO_WIDTH = re.compile(r"[​-‏﻿]")

# --------------------------------------------------------------------------
# 归一化与切分
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """剥掉平台控制符与不可见字符，保留 [MSG_BREAK] 供切分。"""
    if not text:
        return ""
    out = text.replace(LONG_REPLY_MARKER, "")
    out = _CQ.sub("", out)
    out = _ZERO_WIDTH.sub("", out)
    return out.strip()


def bubbles(text: str) -> list[str]:
    """按 [MSG_BREAK] 切气泡，丢弃空气泡。"""
    return [b for b in (p.strip() for p in normalize(text).split(BUBBLE_SEPARATOR)) if b]


def is_long_reply(text: str) -> bool:
    return (text or "").lstrip().startswith(LONG_REPLY_MARKER)


# --------------------------------------------------------------------------
# 禁用式（M4）—— 词表顺序由语料实测命中率决定，不凭想象
# --------------------------------------------------------------------------

#: 任何回复都不许命中。
BANNED_PATTERNS: dict[str, str] = {
    "本大爷": r"本大爷",
    "句首哈？": r"(?:^|\[MSG_BREAK\])\s*哈[?？]",
    "客服开场": r"有什么(?:可以|能)(?:帮|为)|随时(?:叫我|找我|告诉我)|乐意为你",
}

#: 只在**闲聊**回复上算。人格明文规定复杂/专业/执行类问题以 [LONG_REPLY] 开头、
#: 完整作答并保留对方指定格式——那种场合的标题与列表是交付物，不是 AI 腔。
#: 实测教训：不分场合地卡 Markdown，会把一条正确标记了 [LONG_REPLY] 的公司分析
#: 判成 12 处违规（见 README.md「门禁自身的一次误判」）。
CASUAL_ONLY_BANNED_PATTERNS: dict[str, str] = {
    # (?<!\*) / (?!\*) 排除 **加粗**——加粗是排版，不是角色扮演旁白。
    "动作旁白": r"(?<!\*)\*[^*\n]{1,20}\*(?!\*)",
    "闲聊Markdown标题": r"(?:^|\n)#{1,6} ",
    "闲聊Markdown列表": r"(?:^|\n)\s*(?:[-*•]\s+|\d+[.)]\s+)",
}

#: M9 元语言出戏。2026-09-14 盲评查出的唯一失分类别：候选碰到"未核实"时会滑进
#: 考据者口吻（"二代官方介绍"、"一手场景/设定文本"、"官方都没提过"），评委判为
#: 严重出戏。他活在那个学院里，不会主动提这些词。
#:
#: ⚠️ 必须**提问感知**。人格明文规定"问到现实身份、出处或实际执行情况就如实回答"，
#: 所以对方自己问"这是官设吗""官方声优是谁"时，答话里出现该词是正确的，回避才是闪躲。
#: 不分提问地卡这些词，会把 4/24 条正确回答判成违规——与 M4 卡 Markdown 同一个错。
#: 用 meta_leak_hits(text, prompt) 而不是直接把它塞进 banned_hits。
META_LEAK_PATTERNS: dict[str, str] = {
    "元语言出戏": r"官方|官设|设定文本|角色卡|原作|一手(?:场景|资料|文本)|二代介绍|没(?:写过|提过)|考据|资料记载",
}

#: 超过这个字数且无 [LONG_REPLY] 标记，视为"该标却没标"（M3）。
#: 阈值来自人格自己的规格而非语料：闲聊上限三条短气泡，量级远低于此。
LONGFORM_CHARS = 200

# M7 只报告，绝不设下限：给签名词设下限就是奖励复读（语料里 404 次「哈？」的成因）
SIGNATURE_PATTERNS: dict[str, str] = {
    "走/走吧": r"走[吧，。！]|走，",
    "一起": r"一起",
    "来不来": r"来不来|去不去|要不要",
    "训练/剑": r"训练|剑术|练剑",
    "吃饭": r"吃饭|食堂|饿",
}

_PREACHY = re.compile(r"其实|因为.{0,12}所以|重要的是|关键在于|说到底|归根结底")

# CCL 2023 / HC3 口径的第二条指标：语气词密度（论文记 AI 约为人类 1/5）。
# 这里用正则近似而非分词，绝对值不可与论文直接比较，只用同一把尺子做组间比较。
# 来源与限定见 RESEARCH-HUMANIZER.md。
MODAL_PARTICLES = re.compile(r"[吧嘛呢啊呀哦喔噢欸诶嗯哼吗啦咯喽嘿哎唉呗咧嘞哒鸭]")

# 句尾"标点"集合。中文 IM 里真人多半根本不打句尾标点（实测绑定群 90.9%），
# 所以"无尾标点率"是比"尾。占比"更直接的口语度指标。
TAIL_PUNCT = set("。！？!?…~～、，,.;；:：)）】」’”\"'")

# 均质书面腔的词汇层测试（humanizer-chinese #19）：书面词 vs 口语词。
COLLOQUIAL_PAIRS: tuple[tuple[str, str], ...] = (
    ("购买", "买"), ("使用", "用"), ("如何", "怎么"), ("是否", "有没有"),
    ("进行", "做"), ("以及", "还有"), ("非常", "特别"), ("立即", "马上"),
    ("需要", "得"), ("能够", "能"), ("前往", "去"), ("获取", "拿"),
    ("查看", "看"), ("由于", "因为"),
)


def banned_hits(text: str) -> dict[str, int]:
    """M4。闲聊专属模式在 [LONG_REPLY] 回复上不计。"""
    norm = normalize(text)
    hits = {name: len(re.findall(pat, norm)) for name, pat in BANNED_PATTERNS.items()}
    if not is_long_reply(text):
        hits.update(
            {name: len(re.findall(pat, norm))
             for name, pat in CASUAL_ONLY_BANNED_PATTERNS.items()}
        )
    return hits


def meta_leak_hits(text: str, prompt: str = "") -> list[str]:
    """M9：回答里出现、而对方提问里没有的元语言词。

    prompt 为空时退化为"任何元词都算"，只适合没有提问上下文的闲聊刺激集。
    """
    if is_long_reply(text):
        return []
    norm = normalize(text)
    found: list[str] = []
    for pat in META_LEAK_PATTERNS.values():
        found.extend(h for h in re.findall(pat, norm) if h not in (prompt or ""))
    return found


def is_unmarked_longform(text: str) -> bool:
    """M3：长且结构化，却没有 [LONG_REPLY] 标记。"""
    if is_long_reply(text):
        return False
    norm = normalize(text)
    structured = bool(re.search(r"(?:^|\n)(?:#{1,6} |\s*(?:[-*•]\s+|\d+[.)]\s+))", norm))
    return len(norm) >= LONGFORM_CHARS or structured


def signature_hits(text: str) -> dict[str, int]:
    norm = normalize(text)
    return {name: len(re.findall(pat, norm)) for name, pat in SIGNATURE_PATTERNS.items()}


#: M5 的窗口长度。实测校准：min_run=8 时，从未见过示范的 p2 现役臂有 1/24 误判
#: （命中 "刚从训练场回来，"——它同时出现在注入的实时状态里，模型独立生成）。
#: 10 起误判归零。改尺子而非改示范：那句话本来就是他该说的，为指标改它是过拟合。
DEMO_LEAK_MIN_RUN = 10


def leaks_demo(text: str, demo_replies: list[str], min_run: int = DEMO_LEAK_MIN_RUN) -> bool:
    """M5：生成文本是否含任一示范回复的 >=min_run 字连续子串。

    按气泡正文比对，先剔掉 ``[MSG_BREAK]``——否则一个跨到控制符上的窗口
    （``"[MSG_BRE"``）会把任何分了气泡的回复都判成泄漏。
    """
    hay = " ".join(bubbles(text))
    if not hay:
        return False
    for demo in demo_replies:
        for piece in bubbles(demo):
            for i in range(0, max(0, len(piece) - min_run + 1)):
                if piece[i : i + min_run] in hay:
                    return True
    return False


# --------------------------------------------------------------------------
# 分布测量（M1/M2/M3/M6）
# --------------------------------------------------------------------------


def _pct(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * q))
    return sorted_vals[idx]


@dataclass
class Measurement:
    label: str
    n: int = 0
    reply_p50: int = 0
    reply_p90: int = 0
    bubble_p50: int = 0
    bubble_p90: int = 0
    bubble_count_dist: dict[int, int] = field(default_factory=dict)
    bubbles_ge2_share: float = 0.0
    tail_punct: dict[str, int] = field(default_factory=dict)
    tail_period_share: float = 0.0
    tail_no_punct_share: float = 0.0
    tail_entropy: float = 0.0
    modal_per_100: float = 0.0
    colloquial_share: float = 0.0
    long_reply_share: float = 0.0
    unmarked_longform_share: float = 0.0
    question_tail_share: float = 0.0
    preachy_per_100: float = 0.0
    banned: dict[str, int] = field(default_factory=dict)
    banned_sample_share: dict[str, float] = field(default_factory=dict)
    signature_sample_share: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["bubble_count_dist"] = {str(k): v for k, v in sorted(self.bubble_count_dist.items())}
        return d


def measure(texts: list[str], label: str = "") -> Measurement:
    """对一批文本量全部分布指标。纯函数。"""
    kept = [t for t in texts if normalize(t)]
    m = Measurement(label=label, n=len(kept))
    if not kept:
        return m

    reply_lens = sorted(len(normalize(t)) for t in kept)
    m.reply_p50, m.reply_p90 = int(statistics.median(reply_lens)), _pct(reply_lens, 0.9)

    all_bubbles: list[int] = []
    counts: Counter[int] = Counter()
    tails: Counter[str] = Counter()
    ge2 = 0
    long_reply = 0
    unmarked_longform = 0
    q_tail = 0
    no_punct_tail = 0
    preachy = 0
    banned_total: Counter[str] = Counter()
    banned_samples: Counter[str] = Counter()
    sig_samples: Counter[str] = Counter()

    for t in kept:
        bs = bubbles(t)
        counts[len(bs)] += 1
        if len(bs) >= 2:
            ge2 += 1
        all_bubbles.extend(len(b) for b in bs)
        last = bs[-1] if bs else ""
        if last:
            tails[last[-1]] += 1
            if last[-1] in "？?":
                q_tail += 1
            if last[-1] not in TAIL_PUNCT:
                no_punct_tail += 1
        if is_long_reply(t):
            long_reply += 1
        if is_unmarked_longform(t):
            unmarked_longform += 1
        preachy += len(_PREACHY.findall(normalize(t)))
        for name, hits in banned_hits(t).items():
            banned_total[name] += hits
            if hits:
                banned_samples[name] += 1
        for name, hits in signature_hits(t).items():
            if hits:
                sig_samples[name] += 1

    bl = sorted(all_bubbles)
    m.bubble_p50, m.bubble_p90 = int(statistics.median(bl)), _pct(bl, 0.9)
    m.bubble_count_dist = dict(counts)
    m.bubbles_ge2_share = round(ge2 / len(kept), 4)
    m.tail_punct = dict(tails.most_common(8))
    m.tail_period_share = round(tails.get("。", 0) / len(kept), 4)
    m.tail_no_punct_share = round(no_punct_tail / len(kept), 4)
    total_tail = sum(tails.values()) or 1
    m.tail_entropy = round(
        -sum((c / total_tail) * math.log2(c / total_tail) for c in tails.values()), 3
    )
    body = "".join(normalize(t).replace(BUBBLE_SEPARATOR, "") for t in kept)
    body_len = len(body) or 1
    m.modal_per_100 = round(len(MODAL_PARTICLES.findall(body)) / body_len * 100, 3)
    formal = sum(body.count(a) for a, _ in COLLOQUIAL_PAIRS)
    casual = sum(body.count(b) for _, b in COLLOQUIAL_PAIRS)
    m.colloquial_share = round(casual / (formal + casual), 4) if (formal + casual) else 0.0
    m.long_reply_share = round(long_reply / len(kept), 4)
    m.unmarked_longform_share = round(unmarked_longform / len(kept), 4)
    m.question_tail_share = round(q_tail / len(kept), 4)
    m.preachy_per_100 = round(preachy / len(kept) * 100, 1)
    m.banned = dict(banned_total)
    m.banned_sample_share = {k: round(v / len(kept), 4) for k, v in banned_samples.items()}
    m.signature_sample_share = {k: round(v / len(kept), 4) for k, v in sig_samples.items()}
    return m


# --------------------------------------------------------------------------
# log-odds 签名词（A5）—— 字符 1/2/3-gram，+0.5 平滑，双基线
# --------------------------------------------------------------------------

_NON_CJK_RUN = re.compile(r"[^一-鿿぀-ヿ]+")


def char_ngrams(texts: list[str], nmax: int = 3) -> Counter[str]:
    """字符 n-gram 计数。非中日文字符切段，避免跨标点/跨语言成 gram。"""
    c: Counter[str] = Counter()
    for t in texts:
        for seg in _NON_CJK_RUN.split(normalize(t).replace(BUBBLE_SEPARATOR, " ")):
            if not seg:
                continue
            for n in range(1, nmax + 1):
                for i in range(len(seg) - n + 1):
                    c[seg[i : i + n]] += 1
    return c


def log_odds(
    target: Counter[str],
    baseline: Counter[str],
    *,
    min_target_count: int = 12,
    smoothing: float = 0.5,
    top: int = 30,
) -> list[tuple[str, float, int, int]]:
    """log(p_target / p_baseline)，只保留目标出现 >=min_target_count 的项。

    沿用 xiaohuihui-EVIDENCE.md §A6 的口径。双基线各算一遍的意义在 §A6.1：
    单看频次会把「这个群的方言」或「模型的通用腔」误判成个人指纹。
    """
    t_total = sum(target.values()) + smoothing * len(target)
    b_total = sum(baseline.values()) + smoothing * len(baseline)
    out: list[tuple[str, float, int, int]] = []
    for gram, tc in target.items():
        if tc < min_target_count:
            continue
        bc = baseline.get(gram, 0)
        pt = (tc + smoothing) / t_total
        pb = (bc + smoothing) / b_total
        out.append((gram, round(math.log(pt / pb), 3), tc, bc))
    out.sort(key=lambda r: r[1], reverse=True)
    return out[:top]


# --------------------------------------------------------------------------
# 语料源加载（非纯，只在 CLI 用）
# --------------------------------------------------------------------------

MK_OBS = REPO / ".migration-export/json/mk_observations.grantley.ndjson"
GROUP_DB = REPO / ".migration-export/sqlite/qq_group_history.sqlite"
GRANTLEY_GROUP = "183287894"   # 格兰的绑定频道（B3-hermes-config.yaml.example:266）
CONTRAST_GROUP = "980927602"   # 不同人群对照
PERSONAS_DIR = REPO / "docs/personas"


# ---- 用途切分（provenance）------------------------------------------------
# mk_observations 的 user_text 不全是真人输入：8% 是主动发言 job 的提示词（内嵌整段
# 群聊记录 + "只回复 SKIP"），另有一批是 QQ 空间 job 的提示词（"写一条 200 字以内的
# 说说"）。这些任务天然产出长句、句号收尾的正文，混进来会稀释聊天语域的指纹。
# 这是 xiaohuihui-EVIDENCE.md §C3「非本人产出的文本要剔除出统计」在本语料上的对应物
# ——只是这里剔的不是"非本人"，而是"非聊天用途"。
_PROACTIVE_JOB = re.compile(r"以下是这个群最近的聊天记录|只回复\s*SKIP")
_QZONE_JOB = re.compile(r"QQ\s*空间|说说|qzone", re.I)


def provenance(row: dict) -> str:
    """把一行 mk_observation 归到 human_chat / proactive_job / qzone_job / unknown。"""
    user_text = row.get("user_text") or ""
    channel_user_id = str(row.get("channel_user_id") or "")
    if channel_user_id == "proactive" or _PROACTIVE_JOB.search(user_text):
        return "proactive_job"
    if _QZONE_JOB.search(user_text):
        return "qzone_job"
    if channel_user_id.isdigit():
        return "human_chat"
    return "unknown"


def split_by_provenance(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(provenance(row), []).append(row)
    return out


def load_mk_observations() -> list[dict]:
    if not MK_OBS.exists():
        return []
    return [json.loads(line) for line in MK_OBS.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_group_messages(group_id: str, limit: int | None = None) -> list[str]:
    if not GROUP_DB.exists():
        return []
    uri = f"file:{GROUP_DB}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as conn:
        sql = "SELECT text FROM group_messages WHERE group_id = ? AND text IS NOT NULL AND text != ''"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [row[0] for row in conn.execute(sql, (group_id,))]


def load_eval_outputs(variant: str) -> list[str]:
    """收集所有评测 jsonl 里指定变体的 output。"""
    out: list[str] = []
    for path in sorted(PERSONAS_DIR.glob("grantley-*/final-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("variant") == variant and row.get("output"):
                out.append(row["output"])
    return out


def build_report() -> dict:
    rows = load_mk_observations()
    split = split_by_provenance(rows)
    chat_rows = split.get("human_chat", [])
    old_replies = [r.get("reply_text") or "" for r in chat_rows]
    user_turns = [r.get("user_text") or "" for r in chat_rows]
    accepted = load_eval_outputs("after")
    generic = load_eval_outputs("before")
    human_lane = load_group_messages(GRANTLEY_GROUP)
    human_contrast = load_group_messages(CONTRAST_GROUP)

    report: dict = {
        "provenance": {k: len(v) for k, v in sorted(split.items(), key=lambda kv: -len(kv[1]))},
        "sources": {
            "old_chat_replies": len([t for t in old_replies if t.strip()]),
            "real_user_turns_to_grantley": len([t for t in user_turns if t.strip()]),
            "accepted_eval_outputs": len(accepted),
            "generic_eval_outputs": len(generic),
            f"human_lane_{GRANTLEY_GROUP}": len(human_lane),
            f"human_contrast_{CONTRAST_GROUP}": len(human_contrast),
        },
        "measurements": {
            "old_production": measure(old_replies, "老线上格兰·聊天切片（前任模型输出）").to_dict(),
            "old_proactive_job": measure(
                [r.get("reply_text") or "" for r in split.get("proactive_job", [])],
                "老线上格兰·主动发言 job（非聊天用途，仅对照）",
            ).to_dict(),
            "old_qzone_job": measure(
                [r.get("reply_text") or "" for r in split.get("qzone_job", [])],
                "老线上格兰·空间说说 job（非聊天用途，仅对照）",
            ).to_dict(),
            "accepted": measure(accepted, "现验收版（评测输出）").to_dict(),
            "generic_model": measure(generic, "通用模型语域（before 变体）").to_dict(),
            "human_lane": measure(human_lane, f"人类语域锚点 群{GRANTLEY_GROUP}").to_dict(),
            "human_contrast": measure(human_contrast, f"不同人群对照 群{CONTRAST_GROUP}").to_dict(),
            "real_user_turns": measure(
                user_turns, "真人对格兰说话（第三方真人，不入提示词、不入公开 docs）"
            ).to_dict(),
        },
    }

    tgt = char_ngrams(old_replies)
    report["log_odds"] = {
        "vs_human_lane": log_odds(tgt, char_ngrams(human_lane)),
        "vs_generic_model": log_odds(tgt, char_ngrams(generic)),
    }
    accepted_grams = char_ngrams(accepted)
    report["log_odds_accepted"] = {
        "vs_human_lane": log_odds(accepted_grams, char_ngrams(human_lane), min_target_count=6),
    }
    return report


def _fmt_measure(m: dict) -> str:
    dist = ", ".join(f"{k}条 {v}" for k, v in m["bubble_count_dist"].items())
    return (
        f"n={m['n']}  整条 p50/p90={m['reply_p50']}/{m['reply_p90']}  "
        f"气泡 p50/p90={m['bubble_p50']}/{m['bubble_p90']}  "
        f">=2条={m['bubbles_ge2_share']:.1%}  尾。={m['tail_period_share']:.1%}  "
        f"无尾标点={m['tail_no_punct_share']:.1%}  尾熵={m['tail_entropy']}\n"
        f"      语气词/100字={m['modal_per_100']}  口语词占比={m['colloquial_share']:.1%}  "
        f"LONG_REPLY={m['long_reply_share']:.1%}  "
        f"该标未标={m['unmarked_longform_share']:.1%}\n"
        f"      气泡分布: {dist}\n"
        f"      禁用式命中样本占比: {m['banned_sample_share'] or '{}'}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["report", "verify"])
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    report = build_report()

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("== 用途切分（provenance）==")
    for k, v in report["provenance"].items():
        print(f"  {k}: {v}")
    print("\n== 语料源 ==")
    for k, v in report["sources"].items():
        print(f"  {k}: {v}")
    print("\n== 分布测量 ==")
    for key, m in report["measurements"].items():
        print(f"\n  [{key}] {m['label']}")
        print("      " + _fmt_measure(m))
    print("\n== log-odds 签名（老语料 vs 双基线，取交集才算指纹）==")
    for base, items in report["log_odds"].items():
        print(f"\n  vs {base}:")
        for gram, lo, tc, bc in items[:15]:
            print(f"      {gram!r:12s} {lo:+7.3f}  目标{tc:5d}  基线{bc:6d}")

    if args.action == "verify":
        md = HERE / "FINGERPRINT.md"
        if not md.exists():
            print("\nFINGERPRINT.md 不存在，无法比对", file=sys.stderr)
            return 1
        text = md.read_text(encoding="utf-8")
        checks: list[tuple[str, str]] = []
        old = report["measurements"]["old_production"]
        acc = report["measurements"]["accepted"]
        hum = report["measurements"]["human_lane"]
        checks.append(("聊天切片 >=2 气泡占比", f"{old['bubbles_ge2_share']:.1%}"))
        checks.append(("真人对格兰语气词密度", str(report["measurements"]["real_user_turns"]["modal_per_100"])))
        checks.append(("老语料气泡 p50", str(old["bubble_p50"])))
        checks.append(("老语料气泡 p90", str(old["bubble_p90"])))
        checks.append(("现版整条 p50", str(acc["reply_p50"])))
        checks.append(("现版整条 p90", str(acc["reply_p90"])))
        checks.append(("人类锚点 p50", str(hum["reply_p50"])))
        checks.append(("人类锚点 p90", str(hum["reply_p90"])))
        checks.append(("人类无尾标点率", f"{hum['tail_no_punct_share']:.1%}"))
        checks.append(("现版无尾标点率", f"{acc['tail_no_punct_share']:.1%}"))
        checks.append(("人类语气词密度", str(hum["modal_per_100"])))
        checks.append(("现版语气词密度", str(acc["modal_per_100"])))
        missing = [name for name, val in checks if val not in text]
        if missing:
            print(f"\nVERIFY FAIL: FINGERPRINT.md 缺少这些实测值: {missing}", file=sys.stderr)
            return 1
        print("\nVERIFY OK: FINGERPRINT.md 内嵌数字与重算一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
