# T3 修订单（打回重修）

**处置：打回。** 审计结论见 `docs/orchestration/audits/T3.md`（gpt-auditor 异构第二意见，已含主线程复核）。

## 必修（阻断验收）

F-01 / F-02 / F-04 / F-04b / F-05 / F-06 / F-07 / F-08 / F-09。逐条按审计文件的「修」段处理。

其中主线程已独立在代码里确认为事实、不得辩驳的三条：

- **F-02** — `tools/onebot_client.py:57-65` 的 `onebot_base_url()` 确实优先读 `ONEBOT_HTTP_URL`，
  再回落 `ONEBOT_WS_URL`。设计里若假定它只读 WS，必须改。
- **F-04** — `gateway/status.py:1178-1188` `runtime_status_pid_is_live()` 在任一 start_time 为 `None`
  时跳过比对并 `return True`。不能把它当成可靠的存活判据。
- **F-04b** — `gateway/status.py:1716-1722` `_marker_is_stale()` 用 `age = (now - written_dt).total_seconds()`
  然后 `age > ttl_s`；**未来时间戳得到负 age，被判为「新鲜」**。marker 新鲜度检查必须同时处理负 age。

**F-01 额外强调**：Hermes 已 active 时不得再建第二条 OneBot WebSocket。T4 的审计独立发现了同一类错误
（`audits/T4.md` F16），说明这条铁律不能停在口头约定——**在设计里写成可测试断言**：
探针建连前必须检查 Hermes 运行态，Hermes active 时拒绝建连并返回明确错误码。

## 降级

- **F-03** — 主线程下调为措辞问题，改文字即可，不需要改设计。

## 轻修

F-10 ~ F-13 按审计给的改法直接改（成本低）；F-14 是外观项，可改可不改。

## 契约锁定（T4 依赖）

`tasks/T3-design.md` 的账本与报告契约是**权威**，T4 必须适配它，不是反过来：
`epoch=datacenter|home_broadband`、session 记录含 `schema`/`duration_s`/`epoch`、
CLI 为 `report --compare` 与 `report --epoch ...`、JSONL append-only。
**本次修订不得改动这套契约的字段名和命令形态**；若你认为必须改，先停下来在结果文件里说明理由，不要直接改。

## 交付

就地修订 `docs/orchestration/tasks/T3-design.md`；修订前先
`cp -a docs/orchestration/tasks/T3-design.md docs/orchestration/tasks/T3-design.md.bak-pre-rev-$(date -u +%Y%m%dT%H%M%SZ)`。
另写 `docs/orchestration/results/T3-rev.md`，逐条列出每个 finding 的处置（已修 / 已降级 / 不修+理由）与对应改动位置。
边写边落盘，不要攒到最后一次性输出。
