# Telegram 多 Topic 群恢复

已恢复 **Corn Agents（-1003990634877）**。真实 Bot API 验证机器人
`@sweetcornna2_bot` 仍为该 forum supergroup 的管理员，具有管理 Topic 权限。
连接此前已正常，拦住普通话题消息的是 `require_mention: true`。

本次只将原群加入 `free_response_chats`，群内各 Topic 可以直接对话，
其他群继续要求 @。原用户与管理权限名单、各 Topic 的会话键、历史与任务计划不变。
没有重新启用已停用的定时任务，也未向 Telegram 发送测试消息。

真实配置加载和 Telegram 适配器验证了 Topic 11、12、13、680：修改前普通消息
全部被触发门拦截，修改后全部通过；四个 Topic 保持不同会话键。
这些是既有 Topic 样例，本次配置适用于原群中的其他 Topic。
见 [preflight.txt](preflight.txt)。

2026-09-06 15:49:57 UTC 完成配置部署与 Hermes 重启，Telegram 与 QQ 当时均 connected，
见 [deployment.txt](deployment.txt)。后续 QQ 卡片工作另外更新 QQ 进程，Telegram 的恢复配置继续保留。
备份只保留在服务器 `/opt/hermes-cloud/data/.deployments/telegram-restore-20260906/config-before.yaml`。

## 2026-09-07：补齐群聊准入

用户反馈普通 Topic 消息仍不回复。日志确认 Topic 12 的消息已通过免 @ 触发，
但随后在网关被拒绝为 `Unauthorized user: 7711246035`。
原配置已有该账号的 `allow_from`、`allow_admin_from` 和
`group_allow_admin_from`；缺少独立的 `group_allow_from`。
管理员命令权限不会自动授予群聊准入，前一次仅验证触发层的检查不够完整。

本次通过 Hermes 原生配置写入
`platforms.telegram.extra.group_allow_from: ["1114483029", "7711246035"]`，
只补齐既有管理员账号的群聊准入，其他配置和核心代码不变。
当前生产版本会读取 `extra` 中的该项；预检发现仅写在平台顶层不能生效。

在生产容器内，用临时 `HERMES_HOME`、真实配置加载器、Telegram 消息对象、
适配器及网关授权方法验证四个 Topic（11、12、13、680）：修改前均为
“触发通过、授权拒绝”，修改后两层均通过；账号 `7711246035` 对所有已注册
Hermes 管理命令均获准。Topic 会话键保持独立，未列入名单的账号仍被拒绝，
其他群的普通消息仍要求 @。没有发送任何测试消息。

相关原有测试通过 `scripts/run_tests.sh` 运行：**96 passed，0 failed**。
空闲时重启了 Hermes，Telegram 与 OneBot 均恢复 connected，QQ 客户端未重启。
默认模型继续为 `gpt-6-astra`，思考强度 `high`。
详情见 [配置结果](group-auth-result-20260907.json)和
[部署结果](group-auth-deployment-20260907.json)。被拒绝的历史输入需要用户重新发送。
