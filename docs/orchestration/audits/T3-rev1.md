# T3 rev-1 聚焦复审

审计员：gpt-auditor（只读）。落盘由主线程代写。范围限定三问，非全量重审。
**总判定：打回**（主线程复核后同意，并追加一条审计员未发现的问题）。

## Q1 — 14 个 finding 的落地情况

| Finding | 判定 |
|---|---|
| F-01 | 已修。`run` 路径自动探针已删；手动 `probe` 建连前查三份物证，Hermes 非 DEAD 时拒绝并退出码 3 |
| F-02 | 已修。收窄为唯一 `get_status`，`prefer_live=False`，拒绝 `ONEBOT_HTTP_URL` |
| F-03 | 已按主线程指令降级。静态扫描重新定位为防回归检查，不再声称构成权限边界 |
| F-04 | 已修。`LIVE/DEAD/UNVERIFIABLE` 三值 + `kind`/`argv`/start_time 校验；`PermissionError` 不再当作进程不存在 |
| F-04b | 已修。有符号 age + `IMPLAUSIBLE_FUTURE` 分类，按陈旧处理并触发 A4 |
| F-05 | 已修。闩锁绑定 `(writer_pid, writer_start_time)` 代际 |
| F-06 | 已修。按确认后的状态转换切段，单拍 `connecting` 不切段 |
| **F-07** | **修得不对**。§10.2.2 同时规定「unknown 期间不开区间」与「区间从 unknown 起步时取第一个 online 并置 `start_censored`」——两条不能同时作为实现规则 |
| **F-08** | **修得不对**。gap / epoch 分支**无条件** `start_new_session(start_censored=True)`，与「只能从 `account_evidence=="online"` 开始」冲突；切换时若处于 offline/AUTH_REQUIRED/LINK_DOWN 会写出无有效在线起点的区间 |
| F-09 | 已修。pilot 闸门与统计结论分离，零失败走 rule-of-three，30 天必须出结论 |
| **F-10** | **声称已修但实际没有**。`n<10` 防呆只列了 `median/p25/p75`，而同一节的字段清单包含 `min/max` |
| F-11 | 已修。恢复确认 10 拍（抖动后 30），冷却升级 3 窗口，汇总与恢复通知均计入全局上限 |
| **F-12** | **声称已修但实际没有**。`attempts_today` 无跨日清零 |
| **F-13** | **修得不对**。`HINT_MAX_FETCHES_PER_EPISODE = 12` 只覆盖约 2 小时，而目标场景是 9.7–11.7 小时的 episode |
| F-14 | 已修。改为对 9.7h / 11.7h 逐条断言消息序列与时刻 |

## Q2 — 新增约 940 行引入的矛盾

1. **告警队列自相矛盾**（`:1298-1346`）：`PENDING_ALERT_QUEUE_MAX = 10` 溢出丢最旧，
   同一节又声称「唯一可能的错是重复而非丢失」。队列超 10 条时确实会丢告警。
2. **unknown 开区间规则冲突**（`:1502-1505`）：见 F-07。
3. **gap/epoch 事务与开区间前提冲突**（`:1502-1503, 1538-1545, 1581-1587`）：见 F-08。
4. **`censored` 输出值不符合自己的定义**（定义 `:1608-1612`，片段 `:1638-1660`）：
   定义是「删失区间数」，片段打印 `len(all_) - len(ok)`；而 `ok` 还排除了
   `account_evidence != "online"` 与 `verification_degraded`，差值是「所有未进入完整统计的记录数」。
5. **`n<10` 防呆与字段清单不一致**（`:1610-1629`）：见 F-10。
6. **`exposure_h` 口径与降级排除口径不一致**（`:1506-1520, 1608-1612, 1660-1737`）：
   `exposure_h` 含所有区间，`n_complete` 却排除 `verification_degraded` 与非 online 证据区间；
   而 `exposure_h` 正是 rule-of-three 的分母 ⇒ 观测者无法验证的时段仍在增加暴露时间，
   使零失败判定**提前**得出「显著改善」。

## Q3 — 账本契约

**未被破坏。** `epoch` 取值、`schema`/`duration_s`/`epoch` 字段名与语义、
`report --compare`/`--epoch` 命令形态、JSONL append-only 全部保持。
`schema` 1→2 为纯新增，未发现改名或语义重定义。
`end_reason` 新增 `sample_gap`/`epoch_change` 属值域扩展，且报告只把 `kicked` 计为 failure，
按修订后的读取逻辑不会误判为 kicked。

---

## 主线程复核

**同意打回。** 逐条验证结果：

- **Q2-6 证实，且比审计员陈述的更严重。** 文档表格明写
  `exposure_h` = 「该 epoch 下所有区间时长之和（**含删失区间与当前未闭合区间**）」，
  而 `n_complete` 排除 `verification_degraded` 与非 online 证据区间；
  代码片段 `exposure_h = sum(... for r in all_)` 确实取自 `all_`。
- **主线程追加发现（审计员未提）：分子与分母取自不同总体。**
  同一段代码里 `failures = sum(1 for r in ok if r.get('end_reason') == 'kicked')` —— 取自 `ok`；
  而表格对 `failures` 的定义是「`end_reason == "kicked"` **且非删失**」，
  这是比 `ok` 宽的集合（`ok` 还额外排除 online 证据与 degraded）。
  ⇒ **failures 在比 exposure 更窄的总体上计数**，系统性低估失败率，
  与 Q2-6 同向叠加，双重推动 λ 看起来更好。
  这是整个迁移成败判定所依赖的唯一计算，必须让分子分母口径严格一致。
- **F-12 证实。** 全文 `attempts_today` 仅 3 处，**没有任何一处是清零规则**；
  失败时 `attempts_today += 1`，条件是 `attempts_today < 3`。
  ⇒ 某天连续失败 3 次后该字段永久 ≥3，每日心跳**永久停止**。
  心跳恰恰是「系统还活着」的信号，它的静默永久死亡正是心跳机制存在的意义所在。
- **F-13 证实为实质问题。** hint 上限只覆盖约 2 小时，而 H1 记录的空窗是 9.7h / 11.7h ——
  上限恰好在这套系统存在的理由（长时间掉线）中失效，重新制造了 F-13 本要修的失败模式。
- Q2-1 / Q2-4 / F-10 属实但成本低，直接改即可。

**判定 F-07 / F-08 是同一个根因**：账本缺少一条统一的、可判定的「区间何时可以开启」规则，
于是各分支各自为政。修的时候应该先把这条规则写成单一判定函数，再让所有分支调用它，
而不是逐个分支打补丁。
