# P1 机器人：Claude 故障回退与小时恢复检测

适用目标：P1 `172.245.54.136`，`hermes.service`，`HERMES_HOME=/opt/hermes/data`。
主路由保持 `cornna-claude / claude-opus-5-5`；备用为
`cornna / gemini-3.8-flash-high`，沿用 `.env` 中的 `CORNNA_API_KEY`。

## 行为

- 额度/限流、认证、上游服务错误或连接故障触发现有 fallback 链。
- 配置默认主路由发生故障后，在该 profile 的 `fallback_recovery.db` 中记录状态。
  缓存会话、新建会话及网关重启后的会话都继续使用备用模型。
- 首次失败后 3600 秒进行第一次上游探测；失败后再等 3600 秒。
  systemd 每分钟检查到期时间，因此实际发请求最多有约一分钟调度延迟。
  主模型正常时不会发送定时请求。
- 探测只发送 `Reply with OK.`，最多输出 32 tokens；不发送会话历史、人格或工具。
  每次仅请求一次，禁用 SDK 自动重试。有效文本回复才视为恢复。
- 恢复后，下一轮回复通过现有 restore 路径切回 Claude，同时恢复原推理配置。
  已进行中的 Gemini 回复不被打断。
- 单条提示的内容过滤、上下文超限、请求格式问题不标记全局上游故障。
  显式选择其他模型的任务不受默认路由的故障状态影响。
- SQLite 事务防止多个检测进程重复请求；探测期间的新失败会使旧探测结果失效。
  多 profile 模式缺少作用域时拒绝读取默认 profile 状态。

## 配置

```yaml
fallback_providers:
  - provider: cornna
    model: gemini-3.8-flash-high
    base_url: https://api.cornna.xyz/v1
    api_mode: chat_completions
    key_env: CORNNA_API_KEY
fallback_recovery:
  enabled: true
  probe_interval_seconds: 3600
  probe_timeout_seconds: 30
```

恢复检测支持配置了明确端点的命名 provider，协议为 `anthropic_messages` 或
`chat_completions`。默认关闭；启用时需要同时运行独立调度器。
P1 使用本目录的 service/timer，其他部署需修改单元的用户与路径。

## 准备与部署

`prepare.py PAYLOAD_DIRECTORY STAGE_DIRECTORY` 在私有目录中准备变更：
对生产文件按唯一文本锚点打补丁，保留线上其他改动，编译检查所有 Python 文件，
追加 YAML 配置并保存前后 SHA256。它不修改线上文件、不重启服务。
payload 包含 `agent/fallback_recovery.py` 及本目录的两个 systemd 单元。

2026-10-08 经用户确认后已在 P1 部署：

- payload：`/tmp/hermes-fallback-payload-20261008-v2`
- stage：`/tmp/hermes-fallback-stage-20261008-v2`
- 后续补丁：`/tmp/hermes-fallback-stage-20261008-v3`，补齐新建会话和默认
  `reasoning_config: null` 的恢复；两个版本都在 `/opt/hermes/backups/` 留有备份。

首次部署使用以下命令（已经执行，请勿重复执行旧 stage）：

```sh
/opt/hermes/venv/bin/python \
  /tmp/hermes-fallback-payload-20261008-v2/apply.py \
  /tmp/hermes-fallback-stage-20261008-v2
```

`apply.py` 在网关状态为 running 且 active_agents=0 时才能执行；再次检查线上
文件的 SHA256，备份 stage 到 `/opt/hermes/backups/`，停服、应用、启动网关和
timer。命令失败会恢复已有文件并重启原网关；新增模块和停用的单元保留但不生效。
上线后还必须检查新的 `gateway_state.json` PID/时间、平台连接和 timer 状态。

手动回滚：停用 timer 并停网关，按备份的 `manifest.json` 将各 `.before`
内容恢复到原路径，保留原文件所有者/权限，执行 `systemctl daemon-reload` 并
重启 `hermes.service`。恢复旧配置后检测功能关闭；故障状态库可保留。

## 验证记录

- `scripts/run_tests.sh`：14 个相关测试文件，134 passed，0 failed。
- 新建会话推理设置补丁后，重跑 4 个受影响文件：75 passed，0 failed。
- 最终新增 31 项用例包含真实本地 HTTP + SDK + provider 解析 + conversation loop：
  Claude 429/500/503/401 → Gemini → 后续消息保持 Gemini → 小时探测 → Claude。
- 覆盖小时边界、失败探测、空回复、并发检测、过时成功结果、跨 profile 隔离、
  显式模型覆盖、推理设置恢复和系统提示保持。
- Ruff 与 `git diff --check` 通过；P1 `systemd-analyze verify` 通过。
- P1 实际凭据及接口的小请求：Claude `RateLimitError / 429`；
  `gemini-3.8-flash-high` 返回有效文本。未向聊天平台发送测试消息。
- 用户已批准部署并重启；线上配置与预备文件 SHA256 一致，timer 已启用。
- 最终网关 PID `1267540`，状态 running；OneBot 与 Telegram 均 connected，
  `NRestarts=0`。检测 service 正常退出，timer 持续 waiting。
- 上线后再次发真实 Claude 小请求仍为 429，已通过 `record_primary_failure`
  记录这次实际失败，立即启用备用路由；不必等下一条聊天再次触发 429。
