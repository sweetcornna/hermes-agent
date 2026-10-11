# 格兰自然回复、连续输入与部署记录

已于 **2026-09-06 15:34:20 UTC** 部署到 `cornna-cn/hermes-cloud`。
网关 running，OneBot connected，QQ 客户端未重启。生产结果见
[deployment.txt](deployment.txt)，安装校验见 [production-verification.jsonl](production-verification.jsonl)。

## 已上线

- 新人格继续将 QQ **2104743984** 识别为**亚戈、同寝舍友及暗恋对象**。
- 日常通常一条，按内容自然选择 1–3 条，不凑数、不轮换、不重复追加关心。
- 复杂解答保留完整代码、步骤、依据、结果；不受闲聊简短规则约束。
- `platforms.onebot.extra.reply_debounce_seconds: 3`：同一账号、会话、发送者
  最后一次被接受的输入之后安静 3 秒才开始回复。连续文字和已支持的媒体一起构建消息，
  已触发群聊的同一发送者不必在每条补充中重新 @。其他发送者不重置他的计时器。
- 共享群会话按发言人分别保存待处理输入，轮到该会话时再次检查静默窗口。
  下载过程中到达的补充也会保留。控制命令仍立即处理，澄清或审批答复不会死锁。
- 源码及安装目录人格逐字一致，稳定人格保持 4000 字符以内。
  六条已绑定会话新建 `grantley-conversation-20260906` 分支承接有效历史；
  旧系统前缀、旧会话和历史记录不改写。256000／32000 自动压缩设定继续使用。

## 卡片与图片的真实限制

Hermes 已支持 `[LONG_REPLY]` 指定完整解答走合并转发卡片，短于 1000 字也可选择；
自动长度阈值保留，卡片失败时完整降级，不丢代码操作符、变量下划线或来源链接。
群聊、私聊和主动发送共用这条发送路径。

**后续已补齐并启用转发卡片，详见 [最终卡片交付](../forward-card-20260906/README.md)。**
以下为当时发现的限制及后续修复背景。
2026-09-06 实际 `get_version_info` 返回 `SnowLuma native text bridge 0.2.0`，
能力只有 `text, at, reply, recall`。它的 `kernel-operations.cjs` 未实现
`send_group_forward_msg` / `send_private_forward_msg`，Hermes 如实使用全文文字回退。
不能只把 `forward` 加入能力声明来冒充实现；现有公开原生接口声明也不足以证明
当前 Linux QQ 的自定义合并转发流程可用。当前未更改 QQ 内核接入或登录状态。

该桥也没有图片入站转换，原生图片元素仍转成 `native_unsupported`。
前一阶段验证的 Gemini 读图和 Hermes 图片链路继续存在，但不能据此声称
当前 QQ 图片已经端到端接通。此次连续输入测试覆盖的是 OneBot 已提供图片字段时的链路。

## 验证

- 本次主要回归 [tests-second.txt](tests-second.txt) 302 项通过；随后补充真实 3 秒计时、
  真实 BasePlatformAdapter 共享会话处理和澄清等待测试，
  [tests-batch-final.txt](tests-batch-final.txt) 的同一文件增至 18 项全部通过。
  这七个文件合计 **305 个不同测试**，未把重复运行相加。
- 第一批的另外三个文件（人格构建、群聊上下文、路由）通过；首次失败属于新增测试
  调用拼写、取消任务的测试收尾，以及旧代码围栏规范化预期，均有记录且已修正。
- 相同 `gemini-3.8-flash-high` 配置、temperature 0.4、max_tokens 1800，
  6 个输入分别做修改前后探测，共 12 个输出，见 [model-pairs.jsonl](model-pairs.jsonl)。
  服务商返回模型名为 `gemini-3.8-flash`。
- 五个日常样例：修改前气泡数 `2,2,2,2,3`，修改后 `1,1,1,2,2`。
  这只能证明本次样例表现，不能承诺每个未来回答都严格遵守提示词。
- 专业样例由 1068 字、3 气泡变为 1578 字完整解答并正确请求卡片；
  修改前后代码均通过 3234 个边界及重复元素输入，见 [code-check.json](code-check.json)。
  卡片路由测试使用假客户端，没有向任何 QQ 联系人发送测试消息。

待处理的 3 秒输入保存在内存；等待窗口内突然崩溃仍可能丢失尚未持久化的输入。
按配置重启时会停止等待任务；维护脚本先等待网关闲置，备份只保留在服务器。

## 恢复

服务器备份目录 `/opt/hermes-cloud/data/.deployments/conversation-20260906/backup`。
部署按 [manifest.json](manifest.json) 比对前后哈希，源文件只涉及 OneBot 插件与格兰资产，
未修改核心会话或模型工具集合。部署后若发生新消息，不可用旧数据库覆盖新增历史。
MCP 继续使用已修复的新镜像和固定依赖；未重新创建 QQ 登录环境。
