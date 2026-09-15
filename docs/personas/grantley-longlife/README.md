# 格兰长期会话、图片与自动压缩部署

后续实机核验（2026-09-06）：当前 `SnowLuma native text bridge 0.2.0` 尚未提供
图片入站或转发卡片能力。下文的模型读图测试通过不代表 QQ 收图已接通；
详见 [最新部署与限制](../../migration-corlinman/conversation-20260906/README.md)。

已部署到 `cornna-cn` 的 `hermes-cloud`，于2026-09-06 13:32:53 UTC启动。默认模型继续使用 `gemini-3.8-flash-high`。生产配置位于主机 `/opt/hermes-cloud/data/config.yaml`，容器内为 `/opt/hermes/data/config.yaml`。

## 最终行为

- 群聊：每个群一个持久会话，所有发言人和主动发言共用该群上下文，发言人随消息标注。
- 私聊：每位联系人一个持久会话，沿用原有会话和历史。绑定格兰及未绑定的私聊都使用相同压缩规则，各联系人之间仍隔离。
- 每次回答恢复该会话当前的完整有效上下文。达到 **256000 token** 自动压缩较早内容，保留最近约 **32000 token** 原文和必要的完整轮次、工具调用配对；旧内容以摘要延续。边界按模型用量／估计值检测，若切换到较小模型，75%窗口安全上限可能先触发。
- 自动重置设为 `none`；关闭每轮微压缩。压缩使用原会话ID、保留历史数据库，失败时保留原上下文并进入重试冷却。压缩是允许改变上下文缓存的边界；平常继续复用固定前缀。
- 图片：给 `providers.cornna.models.gemini-3.8-flash-high` 补充 `supports_vision: true`，`agent.image_input_mode: auto`。OneBot已有的图片下载与原生多模态路径接管后续图片。其他模型的能力元数据未被覆盖。

本次未新增核心工具、压缩管理器或并行记忆数据库。使用现有 Hermes `ContextCompressor`、`SessionDB`、`SessionStore` 和图片路由。实现原则与[Anthropic的上下文压缩](https://platform.claude.com/docs/en/build-with-claude/compaction)及[OpenAI的会话状态管理](https://developers.openai.com/api/docs/guides/conversation-state)一致：滚动传递压缩后的有效上下文；实际调用仍走当前 Gemini 兼容接口，不发送其他厂商专有的压缩参数。

## 群聊历史衔接

迁移在网关停止后执行，先用 SQLite backup API 备份。5个群共接入 **69个有效旧会话、1331条原始消息**。导入块采用现有“仅供参考”的上下文交接格式，记录原会话、发言人和时间；没有再调用模型对历史做一次有损摘要。最后一条真实助手回复维持消息角色交替，后续正常追加新消息。未复制旧系统人格到新会话。

| 群 | 旧会话数 | 旧消息数 | 导入上下文估计 token |
|---|---:|---:|---:|
| 1082225370 | 3 | 21 | 6736 |
| 183287894 | 19 | 602 | 171981 |
| 149881991 | 25 | 248 | 52752 |
| 667528618 | 11 | 329 | 47952 |
| 894800697 | 11 | 131 | 22852 |

群894800697中5个具有其他人格路由或身份歧义的会话未导入；已显式重置的历史也不重新激活。这些记录仍留在原数据库。生产原有 `sessions` 和 `messages` 每行内容经迁移前后摘要比对确认完全不变。普通网关加载器可照常清理已重置会话的过期路由入口。

## 验证与限制

- 179项测试通过：177项既有图片路由、多模态、缓存、共享会话及滚动压缩测试，加2项配置和真实SQLite迁移契约测试。见 [tests.txt](tests.txt)、[migration-tests.txt](migration-tests.txt)。
- 生产数据库私有副本上完整执行迁移并再次执行，确认恢复和幂等性。见 [preflight.txt](preflight.txt)。测试副本已删除，生产聊天文本未复制到本地。
- 部署后真实配置加载、AIAgent初始化分别核验群聊、绑定私聊、未绑定私聊，均为256000／32000、原会话压缩、失败保留原文；群内不同发言人与主动发言同键，私聊联系人不同键。5个共享会话已持久化到路由数据库并在重启后恢复。见 [verification.txt](verification.txt)。
- 真实3.8原生图片API正确识别测试图的 `ZX49-K7`、左侧红圆和右侧蓝矩形。见 [vision-api.json](vision-api.json)。
- 真实模型压缩在独立临时 `HERMES_HOME` 中使用缩小的12000／1200测试阈值，完成两轮压缩及SQLite重新打开，关键约定 `SABLE-619` 保留。两次测试均在随后主模型回答时遇到服务商HTTP 429（各经过程序默认的3次尝试），见 [real-smoke.txt](real-smoke.txt)、[real-smoke-retry.txt](real-smoke-retry.txt)。因此，压缩后真实回答和AIAgent主循环读图未通过在线验收；图片直连接口及本地链路测试已通过。没有把该失败报告成端到端通过，也未发送QQ测试消息。
- 未进行256k原始输入的付费压力测试；256k阈值的生效边界已通过实际运行对象核验。32k是近期原文预算，并非对无限历史逐字回忆的承诺；更早的原始记录留存于数据库。
- 网关已运行，Telegram连接正常。QQ部署时为 `auth_required`。用户随后完成扫码，2026-09-06 14:05:55 UTC核验原生接口在线、`get_status.online/good=true`、账号2952532060，Hermes OneBot自动恢复为 `connected`，见 [login-verification.json](login-verification.json)。没有发送测试消息；真实QQ收图与模型回复尚未验收，此前模型429限流未在此次登录检查中复测。

## 交付与恢复

[configure.py](configure.py) 是本次无损配置变换，[migrate_groups.py](migrate_groups.py) 是一次性迁移脚本；两者不进入每轮运行路径。部署步骤和结果在 [deployment.txt](deployment.txt)，真实模型测试脚本为 [real_smoke.py](real_smoke.py)。

备份位于主机 `/opt/hermes-cloud/data/.deployments/grantley-longlife-20260906/backup`，包含原配置、SQLite和路由镜像；仅服务器本地保留。部署程序遇到错误会在网关仍停止时自动恢复备份，再启动原容器。以后若需人工恢复，先备份部署后的新聊天并停止网关，再决定恢复配置还是整个数据库，避免覆盖新增记录。旧主机 `corlinman-prod` 和旧systemd网关均未启动。
