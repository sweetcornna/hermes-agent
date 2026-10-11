# T3 rev-2 修订单（第二次打回 → 已升级到 Sol xhigh）

**范围极窄。文档 1975 行里绝大部分已通过验收，不要重写、不要重构、不要"顺手改进"。**
只修下面 7 项。改动越小越好；每一处都要能在 diff 里指着说"这就是那条 finding"。

审计原文：`docs/orchestration/audits/T3-rev1.md`（含主线程复核与一条追加发现）。

## 已通过、不许动的部分

F-01 / F-02 / F-03 / F-04 / F-04b / F-05 / F-06 / F-09 / F-11 / F-14 全部判定已修。
账本契约（`epoch` 取值、`schema`/`duration_s`/`epoch` 字段、`report --compare`/`--epoch`、
JSONL append-only、`schema` 1→2 纯新增）已通过 Q3 验收，**一个字都不要改**。
`test_run_path_never_probes`、rule-of-three 的公式与阈值（`T_needed = 6/λ₀`）均已复核正确，保留。

---

## 必修 1（最高优先）— 统计口径：分子与分母必须取自同一总体

这是整个迁移成败判定所依赖的**唯一**计算，目前有两个同向叠加的偏差，都让结果看起来更好。

**问题 A（审计 Q2-6）**：`exposure_h` 定义为「该 epoch 下**所有**区间时长之和（含删失与未闭合）」，
而 `n_complete` 排除了 `verification_degraded` 与 `account_evidence != "online"` 的区间。
`exposure_h` 又是 rule-of-three 的分母 ⇒ **观测者根本无法验证的时段仍在累积"无失败的暴露时间"**，
使零失败判定提前得出「显著改善」。

**问题 B（主线程追加发现，审计员未提）**：§10.5.1 的代码片段里
`failures = sum(1 for r in ok if r.get('end_reason') == 'kicked')` —— 取自 `ok`；
而 §10.5 表格对 `failures` 的定义是「`end_reason == "kicked"` 且非删失」，是比 `ok` **更宽**的集合。
⇒ **failures 在比 exposure 更窄的总体上计数**，系统性低估失败率，与问题 A 同向叠加。

**要求**：显式定义 rule-of-three 用的那一对 `(exposure, failures)` 取自**同一个明确总体**，
并在表格定义与代码片段两处保持一致。你来选口径并写明理由，但必须满足：
- 观测者不可验证（`verification_degraded`）的时段**不得**计入 rule-of-three 的分母；
- 分子的筛选条件必须是分母的子集，不能反过来；
- 若你保留一个"宽口径 exposure"用于展示事实，必须与判定用的那个**分开命名**，不要复用同一个字段名。

## 必修 2 — 「区间何时可以开启」缺一条统一规则（审计 F-07 + F-08，同一根因）

现状三处互相冲突：
- §10.2.2 `:1502-1503`：区间只能从 `account_evidence == "online"` 开始
- §10.2.2 `:1504-1505`：区间「从 unknown 起步」时取第一个 online 并置 `start_censored`
- §10.2.4 `:1538-1545` 与 §10.4.1 `:1581-1587`：gap / epoch 分支**无条件**
  `start_new_session(start_censored=True)`

⇒ 切换发生在 offline / AUTH_REQUIRED / LINK_DOWN 期间时，会写出没有有效在线起点的虚假区间，污染统计。

**要求**：**先写一个单一判定函数**（例如 `can_open_session(state) -> bool` 或
`maybe_open_session(...) -> Session | None`），把规则集中到一处，
**再让 gap 分支、epoch 分支、正常转换分支全部调用它**。
不要逐个分支打补丁——审计明确指出逐分支修补正是这个 bug 的成因。
补一条测试断言：切换发生在非 online 状态时不产生区间。

## 必修 3 — `attempts_today` 无跨日清零，会让每日心跳永久停止（F-12）

全文 `attempts_today` 仅 3 处，**没有任何清零规则**。失败时 `+= 1`，判据是 `< 3`。
⇒ 某天连续失败 3 次后该字段永久 ≥3，条件永不成立，**每日心跳永久静默死亡**。
心跳恰恰是"系统还活着"的信号，它的静默永久死亡是最坏的失败模式。
**要求**：写明跨本地日期的清零规则（`Asia/Shanghai`，文档已说明无 DST），并补测试。

## 必修 4 — hint 刷新上限恰好在目标场景中失效（F-13）

`HINT_MAX_FETCHES_PER_EPISODE = 12` 只覆盖约 2 小时，而 H1 记录的实际空窗是 **9.7h / 11.7h**。
上限在这套系统存在的理由（长时间掉线）中失效，重新制造了 F-13 本要修的失败模式。
**要求**：让上限与目标 episode 时长匹配（覆盖至少 12 小时），或改为随 episode 时长退避而非硬上限。
保留原有的 `HINT_MIN_INTERVAL_SECS` 节流意图，不要把它一起删掉。

## 轻修 5 — 告警队列自相矛盾（Q2-1）

`PENDING_ALERT_QUEUE_MAX = 10` 溢出丢最旧，同一节又称「唯一可能的错是重复而非丢失」。
二选一：改措辞承认持续故障下会丢，或改策略。**推荐改措辞**（丢最旧对告警是合理策略），
但必须写明"丢弃时下一条成功发送的消息要带上丢弃计数"，否则丢失是静默的。

## 轻修 6 — `censored` 输出值不符合自己的定义（Q2-4）

定义是「删失区间数」，片段打印 `len(all_) - len(ok)`，而 `ok` 还排除了
`account_evidence != "online"` 与 `verification_degraded`。改成真正的删失计数，
或把这一栏改名为它实际表示的含义。

## 轻修 7 — `n<10` 防呆漏了 `min/max`（F-10）

防呆只列 `median/p25/p75`，字段清单却包含 `min/max`。把 `min/max` 纳入同一防呆。

---

## 交付

- 修订前先 `cp -a docs/orchestration/tasks/T3-design.md docs/orchestration/tasks/T3-design.md.bak-pre-rev2-$(date -u +%Y%m%dT%H%M%SZ)`
- 就地修订 `docs/orchestration/tasks/T3-design.md`
- 另写 `docs/orchestration/results/T3-rev2.md`：逐条列出 7 项的改动位置，
  并**明确回答**必修 1 你选了哪个口径、为什么。
- 不要动 `T4-home-migration-runbook.md`（另一个 agent 在改）。
- 边写边落盘。
