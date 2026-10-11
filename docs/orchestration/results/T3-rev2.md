# T3 rev-2 修订结果

状态：已完成（2026-08-31）。

## 实现计划

1. `docs/orchestration/tasks/T3-design.md`
   - §10.2.2 / §10.2.4 / §10.4.1：新增唯一 `maybe_open_session(...)` 判定函数，正常、gap、epoch 三类分支统一调用；补非 online 不开区间的测试要求。
   - §10.2.3 / §10.5 / §10.6：采用“可验证判定总体”口径：`account_evidence == "online"` 且 `verification_degraded == false` 的区间时长构成 `exposure_h`；`failures` 仅从同一总体计数。混合/降级整段排除，因为现有行级账本无法可靠拆出其中可验证子时长，且不能改账本契约。
   - §7.5.1：增加 `Asia/Shanghai` 本地日期键并规定跨日先清零 `attempts_today`；扩充心跳测试。
   - §6.3.1 与常量表：将 hint 单 episode 上限改为 73 次，保留 600 秒节流，覆盖从首次拉取到第 12 小时。
   - §9.0：保留“丢最旧”，明确持续故障会丢，并规定下一条成功消息携带累计丢弃数。
   - §10.5.1：`censored` 改为真实删失标志计数；`min/max` 纳入 `n<10` 防呆。
2. `docs/orchestration/results/T3-rev2.md`
   - 记录本计划、7 项逐处改动、口径理由、真实命令输出、逐条 PASS/FAIL 与未完成项。
3. 修订前创建带 UTC 时间戳的备份；不触碰 `T4-home-migration-runbook.md`，也不改修订单列出的已通过契约。

## 备份

- 已按修订单先执行 `cp -a`。
- 备份：`docs/orchestration/tasks/T3-design.md.bak-pre-rev2-20260831T112312Z`
- 备份 SHA-256：`d27f09e54f4551b8cb40390049aae5efa6670caab275d131e10423907fe59992`
- 创建时原文件与备份哈希相同。

## 必修 1：统计口径选择与理由

选择口径：**可验证的 person-time 判定总体**，在文档中命名为 `rate_rows`。

```python
rate_rows = [r for r in epoch_rows
             if r.get("account_evidence") == "online"
             and not r.get("verification_degraded", True)]
exposure_h = sum(r["duration_s"] for r in rate_rows) / 3600.0
failures = sum(1 for r in rate_rows
               if r.get("end_reason") == "kicked"
               and not r.get("end_censored"))
```

理由：

1. `verification_degraded`、`account_evidence == "mixed"` / `null` 的时段全部排除，观测者不可验证的时段不会再累积成“零失败暴露时间”。
2. `exposure_h` 与 `failures` 共享完全相同的总体资格筛选；`failures` 只是 `rate_rows` 内出现已确认 `kicked` 端点的子集，不再从更窄的 `ok` 集合计数。
3. 删失区间和当前未闭合区间仍可贡献其中已经观测、全程有 online 证据且未降级的 person-time；这正是零事件 rule-of-three 需要使用的右删失信息。
4. schema 2 只有区间级 `account_evidence` / `verification_degraded` 汇总，没有每拍可验证时长，不能可靠拆分 mixed/degraded 区间；整段排除是最保守且不改账本契约的做法。
5. 不保留“所有区间总时长”的宽口径字段，避免与判定用 `exposure_h` 混名或误用。
6. rule-of-three 公式及阈值保持原样：`λ_upper = 3 / T`、`T_needed = 6 / λ₀`。

## 改动清单与逐处理由

1. **必修 1，统计总体一致**
   - 位置：§10.2.3（约 1560–1566 行）、§10.5（约 1653–1677 行）、§10.5.1 代码片段（约 1710–1724 行）、§10.6.2（约 1779–1782 行）。
   - 改动：定义单一 `rate_rows`；`exposure_h` 与 `failures` 均由它派生；降级/mixed/null 整段排除；明确不保留同名宽口径。
   - 理由：消除分母含不可验证时段、分子又取自更窄集合的两个同向偏差。

2. **必修 2，统一开区间规则**
   - 位置：§10.2.2（约 1514–1558 行）、gap 分支（约 1582–1587 行）、epoch 分支（约 1624–1634 行）、T7 测试清单（约 1373 行）。
   - 改动：新增唯一 `maybe_open_session(state, observation, *, start_censored)`；正常转换、gap、epoch 全部调用；只有确认态 `ONLINE` 且 `account_evidence == "online"` 才能开区间；非 online 返回 `None`。
   - 理由：把资格规则集中到单点，删除各分支无条件构造区间的旁路；unknown 期间不开，首个有效 online 才决定起点。

3. **必修 3，`attempts_today` 跨日清零**
   - 位置：§7.5.1（约 1032–1052 行）、T4 测试清单（约 1370 行）。
   - 改动：新增 `attempts_local_date`；每拍先按 `Asia/Shanghai` 比较本地日期，跨日或旧状态缺字段时先把 `attempts_today` 清零，再判断 09:00 发送条件；新增跨日测试要求。
   - 理由：前一天失败 3 次不能永久封死以后每天的心跳。

4. **必修 4，hint 上限覆盖目标 episode**
   - 位置：§6.3.1（约 849–858 行）、常量总表（约 2005 行）。
   - 改动：`HINT_MAX_FETCHES_PER_EPISODE` 从 12 改为 73；保留 `HINT_MIN_INTERVAL_SECS = 600`。
   - 理由：`73 = 1 + 12 × 3600 / 600`，首次后可在第 12 小时再刷新一次，覆盖 9.7 h / 11.7 h 实测空窗，同时保留硬上限和节流。

5. **轻修 5，告警队列承认并披露丢失**
   - 位置：§9.0（约 1331–1354 行）。
   - 改动：保留溢出丢最旧；累计 `pending_alerts_dropped`；下一条成功发送的消息必须携带累计丢弃数，成功后才清零；把“唯一可能重复、绝不丢失”收窄到队列未溢出的崩溃窗口。
   - 理由：有界队列策略合理，但持续故障下的丢失不能静默，也不能继续声称绝不丢。

6. **轻修 6，`censored` 输出真值**
   - 位置：§10.5 表格（约 1655 行）、§10.5.1 片段（约 1721–1723 行）。
   - 改动：定义并计算 `start_censored || end_censored` 的真实删失区间数；删除 `len(all_) - len(ok)`。
   - 理由：`ok` 还排除证据不完整/降级记录，其差值不是删失数。

7. **轻修 7，`n<10` 覆盖 `min/max`**
   - 位置：§10.5.1（约 1691 行）、T7 测试清单（约 1373 行）。
   - 改动：防呆字段改为 `median/p25/p75/min/max` 全部输出“样本不足”；测试清单同步要求五项全部受限。
   - 理由：字段清单与统一 `summarize()` 防呆必须一致，不能从 `min/max` 旁路读取小样本结论。

## 验收标准自查

| 验收项 | 判定 | 证据 |
|---|---|---|
| 必修 1：分子分母同一总体 | PASS | `rate_rows` 同时派生 `exposure_h` / `failures`；旧 `all_` 分母和 `ok` 分子写法均已删除；合成数据验证见下 |
| 必修 1：`verification_degraded` 不进分母 | PASS | §10.2.3 明确整段不进入 rule-of-three 暴露；断言确认旧“仍计入暴露时间”文本不存在 |
| 必修 2：单一函数，三分支调用 | PASS | 直接 `start_new_session` 构造点仅函数体内一个；normal/gap/epoch 三段精确断言均通过 |
| 必修 2：非 online 不产生区间并有测试要求 | PASS | helper 同时检查确认态和 online 证据；T7 对 gap/epoch 非 online 两条均要求不得产生新区间 |
| 必修 3：跨本地日清零并补测试 | PASS | `attempts_local_date` + `Asia/Shanghai` 清零顺序已定义；T4 加跨日用例 |
| 必修 4：至少覆盖 12 小时且保留节流 | PASS | 上限 73，节流 600 秒未删；正文和常量表一致 |
| 轻修 5：队列矛盾消除且丢弃可见 | PASS | 承认持续故障会丢；下一条成功消息披露累计数 |
| 轻修 6：`censored` 是真实删失数 | PASS | 使用两个 censor 标志求和；旧差值表达式不存在 |
| 轻修 7：`min/max` 纳入防呆 | PASS | 正文与 T7 测试清单均列出五项 |
| rule-of-three 公式与 `T_needed = 6/λ₀` | PASS | 字面量断言通过，未改公式和阈值 |
| 账本契约 §12.4 | PASS | 从 `### 12.4` 到文末与备份逐字相同 |
| `test_run_path_never_probes` | PASS | 字面量断言仍存在，未改该段 |
| T4 禁改文件 | PASS | 修订前后 SHA-256 均为 `b31493f310e57c9b1bb47d5e088342d1004426df5741e956f8bad353c166a800` |
| 修订前备份 | PASS | 备份存在，且哈希为修订前原文哈希 |
| 最终 FAIL 项 | PASS | 无最终 FAIL |

## 真实验证输出

### 1. 最终验收断言

命令：`python3` 内联断言，读取设计文档与 rev-2 备份，逐项检查 7 项修复、锁定契约和 Markdown 围栏。

```text
PASS 必修1 rate_rows 单一总体
PASS 必修1 不可验证时段不进分母
PASS 必修1 旧混合口径已删除
PASS 必修2 唯一构造函数
PASS 必修2 normal/gap/epoch 全调用
PASS 必修2 非 online 测试断言
PASS 必修3 Asia/Shanghai 跨日清零
PASS 必修4 hint 至少覆盖12h
PASS 轻修5 队列丢弃可见
PASS 轻修6 censored 真值计数
PASS 轻修7 min/max 同受防呆
PASS 锁定 §12.4 账本契约逐字未变
PASS 锁定 rule-of-three 公式阈值
PASS 锁定 test_run_path_never_probes
PASS Markdown 代码围栏成对
```

### 2. 统一开区间函数的三分支精确检查

```text
PASS single_constructor
PASS normal_calls_helper
PASS gap_calls_helper
PASS epoch_calls_helper
PASS non_online_test_assertion
```

### 3. 统计总体合成数据检查

输入包含：可验证 kicked、可验证右删失、可验证起点删失 kicked、降级 kicked、mixed kicked。

```text
rate_rows=3 exposure_h=3.5 failures=2 censored=2
PASS degraded/mixed rows excluded from both exposure and failures
PASS failures are event-filtered subset of rate_rows
```

### 4. 备份与禁改文件哈希

```text
1b60f49b3c9dcb2eaf1b3445e47f715297aef9c7cb60c11a352cc1a11f41f76e  /Users/cornna/project/hermes-agent/docs/orchestration/tasks/T3-design.md
d27f09e54f4551b8cb40390049aae5efa6670caab275d131e10423907fe59992  /Users/cornna/project/hermes-agent/docs/orchestration/tasks/T3-design.md.bak-pre-rev2-20260831T112312Z
b31493f310e57c9b1bb47d5e088342d1004426df5741e956f8bad353c166a800  /Users/cornna/project/hermes-agent/docs/orchestration/tasks/T4-home-migration-runbook.md
```

修订前记录的 T4 SHA-256 同样为：

```text
b31493f310e57c9b1bb47d5e088342d1004426df5741e956f8bad353c166a800
```

### 5. 格式检查

```text
trailing_whitespace_lines= []
PASS no trailing whitespace
```

### 6. 验证过程中的一次断言误报及根因

首次辅助断言把 `maybe_open_session(` 的字面量数量错误写成 `>= 6`，输出过一项 FAIL：

```text
FAIL 必修2-normal/gap/epoch 调统一函数
```

按要求做最小复现后得到：

```text
1514:def maybe_open_session(state, observation, *, start_censored: bool):
1536:maybe_open_session(
1586:    maybe_open_session(state, observation, start_censored=True)
1628:    maybe_open_session(state, observation, start_censored=True)
1633:  `maybe_open_session(...)` 返回 `None`，不预写占位区间，后续只在新的有效 online 采样上重新判定。
literal_call_count= 5
non_online_test_phrase_count= 2
```

根因是验证器阈值算错：实际五处为函数定义、normal 调用、gap 调用、epoch 调用和一处说明文字；设计分支没有缺失。随后将验证改为分别精确匹配 normal/gap/epoch 三段，最终输出见第 2 节，全部 PASS。未对设计做猜测式补丁。

本任务只修设计文档，没有可执行实现代码，因此未运行项目 `pytest`；上述验证均为针对修订单验收条件的真实文档断言和合成统计检查。

## 未完成项

无。
