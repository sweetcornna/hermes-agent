# D47 — deterministic pre-reduction for the QQ monitor digests

D2 shipped the three monitors with a single model call and a
**newest-1000-messages** truncation. On `sanhu`'s source group (`980927602`,
~15k–20k messages/day) that meant the daily digest covered only the last few
hours of the day — measured on the real export, **6 of 24 hours**.

The source solved this with a parallel map-reduce (N model calls, then a
reduce turn). D47 rules that out for this host: 00-PLAN.md §18 proved every
upstream request that fails burns the (very tight) account pool, and the box
is 2 vCPU / 1.9 GB with `MemoryHigh=384M`. Multiplying calls per run is
exactly the wrong direction here.

**What this task did instead:** compress the window in *pure Python, zero
model calls*, so the one model call the scheduler already pays for can cover
the whole day. Model calls per run: **still exactly 1**.

---

## 1. What changed

No new files, no new dependencies (stdlib + `yaml`, unchanged).

| Path | Change | Lines |
|---|---|---|
| `plugins/corlinman_jobs/scripts/corlinman_jobs_lib.py` | the pre-reduction and its constants; `main_qq_monitor_digest` rewritten around it | 1067 → 1440 |
| `plugins/corlinman_jobs/prompts.py` | `QQ_MONITOR_SAMPLING_PROMPT` — tells the model the log may be a sample | 308 → 327 |
| `plugins/corlinman_jobs/plugin.yaml` | `QQ_MONITOR_DIGEST_BUDGET` documented under `optional_env` | 148 → 159 |
| `tests/plugins/corlinman_jobs/test_jobs_lib.py` | 1 obsolete test replaced, 17 added | 1160 → 1470 |
| `tests/plugins/corlinman_jobs/test_plugin_registration.py` | manifest/env cross-check now also reads the job-side library | 267 → 274 |

`installer.py` is **untouched**: the budget is resolved at run time, so an
operator retunes it without reinstalling twelve jobs, and the installed entry
scripts stay byte-identical in shape.

---

## 2. The pipeline

Every stage is a pure function of its input. No RNG, no hashing, no set
*iteration*, no wall clock. Every ordering is total — ties break on the
message's position in the (already deterministic) query result.

### 2.0 Fetch cap raised 10,000 → 40,000 — this had to happen first

`QQ_MONITOR_FETCH_CAP` was ported verbatim from the source at 10,000. That
number is **below a real day** for `sanhu`: a 24h window holds 18,136 rows, so
the query itself was silently discarding the oldest ~45% *before any*
summarisation strategy got a say. Whole-day coverage was unreachable at
10,000 no matter how good the reduction is. (The source had the same ceiling,
so its map-reduce covered ~55% of a busy day too.)

40,000 is ~2x the busiest real 24h window (20,780 rows on 2026-08-18) and
stays a pure safety valve. Measured cost: an 18,136-row window materialises at
9.0 MB; a full 40,000-row fetch extrapolates to ~20 MB. Peak of a complete
real run of the busiest day, end to end: **16.0 MB** (`tracemalloc`).

### 2.1 Focus members are lifted out first, before anything else runs

`focus_user_ids` is `jlu`'s entire mechanism — the ★ lines and the per-member
closing section the prompt demands. Focus messages **bypass the noise filter,
the dedup, the bucketing and the quota**. They are not counted against any
per-sender cap; they only reduce the budget left for everyone else.

This is unconditional, including for a focus message that is an image only, a
single character, or an exact repeat of another focus message — precisely the
shapes the crowd filter would otherwise eat.

### 2.2 Zero-content drops

Classified on the *plain* text (CQ segments removed, whitespace collapsed):

| category | rule | real 08-19 `sanhu` window |
|---|---|---|
| `media` | nothing left after removing CQ segments — image/sticker/voice only | 3,140 / 18,136 |
| `symbol` | no letters/digits/CJK left (`unicodedata.category` in `P*`/`S*`/`Z*`/`C*`) — "？", "。。。", "😭😭", kaomoji | 319 |
| `filler` | ≤1 content character — "哦", "好" | 183 |

`media` is the single biggest win and it is *free*: an image-only message
rendered as up to 300 characters of CDN URL and contributed nothing to a text
digest. Removing it is what pays for the larger line budget.

### 2.3 Echo dedup

Identical plain text keeps its **first** occurrence only. In the real data
this is copypasta, not 27 people making 27 points — the export's top repeats
are one joke pasted 27 times, a 22×-pasted rant, a 21× "要来力".

The first occurrence gets a **+30 score bonus**: a text the room went on to
repeat three or more times is a topic anchor, and it is *because* the copies
were dropped that the original must be protected.

### 2.4 Hourly buckets + quota — the whole-day guarantee

Buckets are 60 minutes wide, aligned to the window start (`since_ms`), so a
1440-minute window yields 24 of them. `_qq_monitor_allocate` splits the budget
**half egalitarian, half proportional**:

* each non-empty bucket first gets `min(size, budget // (2 * buckets))`;
* the remainder goes out in proportion to each bucket's *remaining* capacity,
  largest-remainder, ties broken by bucket index.

The floor is the point. A purely proportional split gives the export's
27-message 06:00 hour about two lines and the digest silently loses the quiet
parts of the day. With the floor, the same hour keeps all 27.

### 2.5 Within a bucket: breadth, then depth

1. **Breadth** spends 70% of the bucket quota taking each sender's single best
   message, senders ordered by that message's score. This is the anti-flood
   guarantee: it is what stops a 470-sender day collapsing onto the ten
   loudest accounts.
2. **Depth** spends the reserved 30% on the highest-scoring messages still
   unpicked — including the best message of a sender breadth never reached —
   under a per-sender ceiling of `ceil(quota * 0.2)`.

The 30% reserve is not a guess. With a pure breadth pass, busy buckets never
got past round one, so a member who made three substantive points in an hour
contributed exactly one, and the digest kept only **61%** of the busiest day's
≥40-character messages. Reserving 30% for depth lifts that to **97%** (and 88%
→ 100% on 2026-08-19) while still keeping 284 of that day's 526 senders —
about **3×** what the newest-1000 truncation managed (98).

Score (integer, deterministic):
`min(content_length, 200) + 20 if it contains a question mark + 30 if it is a
repeated-copypasta anchor`.

### 2.6 Rendering

Kept rows render through the **unchanged** `_qq_monitor_format_lines`, with
the display text substituted: CQ segments become short labels (`[图片]`,
`[表情]`, `[CQ:at,qq=N]` → `@N`) and newlines collapse, so one message is
always exactly one line. That last part also closes a latent bug — a
multi-line message previously rendered as several unattributed lines.

---

## 3. The annotation — the digest owns up to being a sample

First line stays the header the prompt tells the model to reproduce. When
**nothing** was reduced the header is byte-identical to what D2 shipped
(`群 X 最近 1 天的消息汇总（共 N 条）。`) and no annotation is printed at all.
When something was reduced, a real run prints:

```
群 980927602 最近 1 天的消息汇总（原始 18136 条，抽样保留 1500 条）。
说明：下面的聊天记录不是全部原文，而是对整个时间窗口做的确定性抽样——按小时分桶保证全时段
都有代表，并对刷屏、重复内容和单个刷屏者做了限流。请据抽到的内容归纳话题，不要推断没有出现
的内容，也不要按记录行数重新统计条数。
已归约 16636 条：图片/表情等无文字内容 3140 条、纯符号或颜文字 319 条、单字灌水 183 条、
重复刷屏 1605 条、时段配额外未抽中 11389 条；保留 1500 条，覆盖 24 个时段。
按原始条数发言最多：山寨币赌狗…(3766672257) 2134 条、我叫乖乖(381344274) 1461 条、…
（抽样后各人条数已被拉平，热度以此行为准）。
```

For a monitor with focus members it also prints
`重点关注对象（★ 标记）的 N 条消息未参与抽样，全部保留。`

**Why the "top talkers" line exists:** per-sender capping deliberately
flattens the loudest voices in the sampled log (top-sender share 11.8% → 5.1%
on 08-19). Without restating the raw ranking the digest would lose the fact
that one account dominated the room — so the flattened signal is handed back
as a fact rather than left to be inferred from line counts.

`prompts.QQ_MONITOR_SAMPLING_PROMPT` says the same thing on the instruction
side: cover the whole period, take counts from the header, do not claim to
have seen everything, do not conclude anything about what is not shown.

---

## 4. Default budget: 1,500 lines, and why

1. **It is cheaper than what it replaces.** Rendered chat-log characters on
   three real `sanhu` days: **84,692 / 96,112 / 87,036** against the old
   newest-1000 log's **93,699 / 105,522 / 94,935** — 9–16% *fewer* prompt
   characters while covering 24 hours instead of 6–8. So it does not increase
   token cost or account-pool pressure. On `jlu`'s group the gap is far wider
   (60,606 vs 105,513) because that group posts many images.
2. ~1 line per minute of a 1440-minute window, i.e. ≥60 lines per hourly
   bucket — enough for a per-topic paragraph for every part of the day.
3. It sits above a whole post-noise day for the two smaller monitors, so
   `jlu` and `qunjlu` stay effectively lossless and only `sanhu` truly
   samples.

Configurable three ways, in precedence order: the `budget=` argument to
`main_qq_monitor_digest`, the `QQ_MONITOR_DIGEST_BUDGET` env var, then the
module default. Any value is clamped to 50…20,000; a non-integer env value is
logged to stderr and ignored rather than failing a scheduled run.

---

## 5. Verification — all offline, all against the real export

`.migration-export/sqlite/qq_group_history.sqlite`, 52,649 real rows,
2026-08-16→19. No network, no SSH, no delivery.

### 5.1 Coverage and fidelity, per real window

| window | raw | kept | hours covered (raw / new / old) | distinct senders (raw / new / old) |
|---|---|---|---|---|
| `sanhu` 08-17 09:05 | 8,658 | 1,500 | 24 / **24** / 8 | 300 / **241** / 90 |
| `sanhu` 08-18 09:05 | 20,780 | 1,499 | 24 / **23** / 8 | 526 / **284** / 98 |
| `sanhu` 08-19 09:05 | 18,136 | 1,500 | 24 / **24** / 6 | 472 / **262** / 99 |
| `jlu` 08-19 10:05 | 1,389 | 1,108 | 20 / **20** / 18 | 33 / **31** / 31 |
| `qunjlu` 08-19 08:05 | 167 | 117 | 9 / **9** / 9 | 1 / 1 / 1 |

("old" = the newest-1000 truncation this replaces, computed from the same
rows.) 08-18 shows 23 of 24 because that window's 05:00 hour held exactly one
message and it was an image.

### 5.2 Is information actually preserved? Two independent recall measures

Neither measure is something the algorithm optimises for directly, and both
are computed against the *raw* window, not against the reduction's own idea
of what mattered.

**A. Substantive messages** — every distinct message with ≥40 content
characters (the ones that actually carry a point):

| window | distinct substantive messages | kept by pre-reduction | kept by old truncation |
|---|---|---|---|
| `sanhu` 08-17 | 59 | **58 (98%)** | 6 (10%) |
| `sanhu` 08-18 | 196 | **190 (97%)** | 13 (7%) |
| `sanhu` 08-19 | 92 | **92 (100%)** | 2 (2%) |
| `jlu` 08-19 | 24 | **24 (100%)** | 6 (25%) |

**B. Repeated topics** — every text repeated ≥3 times with ≥8 content
characters, i.e. the day's memes and running arguments:

| window | anchors | kept by pre-reduction |
|---|---|---|
| `sanhu` 08-17 | 31 | **31/31** |
| `sanhu` 08-18 | 189 | **189/189** |
| `sanhu` 08-19 | 140 | **140/140** |
| `jlu` 08-19 | 4 | **4/4** |

**C. Eyeball check.** Reading the selected lines for a busy hour, the
conversation still threads: the `@<id>` mentions that carry QQ's reply
structure survive, and adjacent question/answer pairs land in the same
digest because different senders participate in them.

### 5.3 Accounting balances

`kept + Σdropped == total == len(rows)` asserted for every window above and
for a sweep of 24 windows across both groups (every 6 hours, 08-17→19).
Worst hour-coverage ratio in that sweep: **88%** (14 of 16 hours, on a
low-traffic `183287894` window whose two missing hours held only images).

### 5.4 `focus_user_ids` messages are never lost — how it was checked

For each of three real `jlu` windows, every focus row was read **straight
out of SQL** (`WHERE sender_user_id='1076712858'`) and each one matched
against the ★ lines of the produced digest:

```
08-17: focus rows in window=169  ★ lines in digest=169  unmatched=0
08-18: focus rows in window=418  ★ lines in digest=418  unmatched=0
08-19: focus rows in window=168  ★ lines in digest=168  unmatched=0
```

Plus a synthetic worst case in the test suite: 3,000 crowd messages (twice
the budget) alongside six focus messages that are individually an image-only
message, a single character, a bare "？", and an exact duplicate pair — all
six survive, the image one as `[图片]` rather than a CDN URL.

### 5.5 Determinism — how it was checked

1. **Same process, five repeats**, six real windows → one distinct SHA-256
   each.
2. **Fresh interpreters under five `PYTHONHASHSEED` values** (`0`, `1`,
   `12345`, `99999`, `random`), six real windows → **one** distinct SHA-256
   per window across all runs. This is the one that matters: `set`s and
   `dict`s are used, so a hash-order dependency would show up here.
3. **A physically re-ordered copy of the database** — every row reinserted in
   reverse `id` order, giving different rowids for identical content →
   **byte-identical output** on all six windows.
4. A unit test asserts three consecutive runs produce identical stdout, and
   `_qq_monitor_allocate` is asserted to be a pure function.

### 5.6 Memory

`tracemalloc` around a complete run of the busiest real window (20,780 rows,
group 980927602): **peak 16.0 MB**, output 96,562 chars. Against
`MemoryHigh=384M`.

### 5.7 Test suite

```
.venv/bin/python -m pytest tests/plugins/corlinman_jobs -q
    → 308 passed          (D2 baseline: 291)

.venv/bin/python -m pytest tests/plugins/corlinman_jobs tests/plugins/qzone \
                          tests/plugins/grantley tests/cron -q
    → 1469 passed, 1 skipped   (baseline: 1452 passed, 1 skipped)

.venv/bin/python -m pytest tests/gateway/test_onebot_group_history.py -q
    → 81 passed               (baseline: 81 passed)
```

One pre-existing test was **replaced**, not deleted:
`test_over_the_prompt_cap_keeps_only_the_newest_and_flags_truncation` pinned
the behaviour D47 exists to remove. Its successor,
`test_over_the_budget_covers_the_whole_window_not_just_the_newest`, asserts
the opposite property — that both ends of the window are represented and the
sampling is spread rather than clumped.

---

## 6. Known defects and residual risk — stated plainly

1. **A digest is now a sample, and a sample can miss a thing that mattered.**
   Recall of substantive and repeated content is 97–100% on real data, but it
   is not 100% by construction. A single quiet, non-repeated, short but
   important message in a busy hour can be dropped by the quota. The old
   behaviour dropped ~93% of the day outright, so this is strictly better —
   but it is not lossless, and the digest says so in its own header.
2. **No conversational threading.** Selection is per-message. At 1:12
   compression no deterministic scheme can preserve whole threads; what
   survives is topics, participants and questions, and QQ's `@<id>` mentions
   happen to carry a lot of the reply structure. Adjacent-message context
   ("keep the two replies after every anchor") was considered and not built.
3. **Focus messages are unbounded by design.** The "never dropped" guarantee
   is absolute, so a focus member who sent 10,000 messages would blow the
   budget on their own (bounded only by `QQ_MONITOR_FETCH_CAP`). Real volume
   for the one configured focus user is ~40–420/day, so this is theoretical —
   but it is a real ordering: the guarantee wins over the budget, not the
   other way round.
4. **The 3-days-old data problem is unchanged.** This task did not touch D2
   §4 / D3's staleness gap: `check_qq_group_history` still cannot tell "the
   store has rows" from "nothing has written a new row in four days".
5. **`unicodedata.category`-based classification is a heuristic.** A message
   made entirely of CJK punctuation used as words (rare) classifies as
   `symbol`. Focus members are exempt; everyone else can lose such a message.
6. **The score weights (200 cap / +20 / +30) are calibrated on one group's
   four days.** They are defensible and measured, not universal. A different
   group with a different register might want different numbers; they are
   module constants, not literals, for exactly that reason.
7. **Never run against a live model.** Everything here is offline. The
   prompt-side claim — that the model will honour "this is a sample, cover the
   whole period, use the header's counts" — is asserted by construction and by
   the instruction text, **not** by an end-to-end run. D42 (agent identity)
   still blocks any real turn, so this shares the same "not proven live"
   caveat as E0 §5.
8. **`QQ_MONITOR_FETCH_CAP` raised to 40,000 is a deviation from the ported
   source constant.** Justified in §2.0 and measured, but it is a deliberate
   divergence and is recorded here rather than buried.
9. **All measurement is on macOS / SQLite 3.53.1**, like D3. The reduction
   itself is pure Python and has no SQLite-version dependency, but the
   40,000-row fetch has not been timed on the target box.
