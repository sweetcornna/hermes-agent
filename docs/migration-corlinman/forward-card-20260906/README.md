# QQ 折叠卡片补齐

已在生产 `cornna-cn` 上启用原生桥 **0.3.0** 的 `forward` 能力。
正式启用后的 Hermes 启动时间为 **2026-09-06 16:38:56 UTC**；后续只补充了失败时禁止重试的明确标记。
QQ、Hermes 和 Telegram 均恢复在线。Telegram 的 **Corn Agents** 多 Topic 群也已恢复直接对话，
详见 [Telegram 恢复记录](../telegram-restore-20260906/README.md)。

## 用户可见行为

- 日常继续按内容发 1–3 条，通常一条；同一发送者安静 3 秒后合并输入再回复。
- 复杂完整解答使用 `[LONG_REPLY]` 请求可展开聊天记录卡片；超过既有 1000 字阈值也自动折叠。
- 代码、下划线、乘方符号、换行和来源链接保留。已确认未发出的卡片失败可退回完整文字；
  结果不明时停止重发，避免卡片与全文重复。
- 2104743984 的亚戈关系、新人格、256000／32000 自动压缩和现有历史保留。

## 实现

桥接层接受群聊和私聊的转发请求，把文本节点以已确认消息存入机器人自己的会话，
再用 `multiForwardMsgWithComment` 完成上传和发送。每次实际发送均受原有 SQLite 限速及去重保护。
仅允许使用机器人自己的 UIN 生成文本卡片，正文最多 60000 UTF-8 字节；内部草稿按字节安全分段，
不会丢失 Unicode 字符。草稿保留在机器人自己的会话中。

接口依据是 [QQ 原生消息接口的公开说明](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/apis/msg.ts)
和实际 Linux QQ 3.2.31-51102 的回调。`buildMultiForwardMsg` 的预览可被发送确认，但没有正文资源，
因此不能作为成功标准。最终实现要求收到同账号、同接收方的新卡片成功回调，且卡片有有效资源。
卡片按接收方限制并发；当原生接口不回传请求关联值时，采用新卡片、发送时间、接收方与会话代次共同匹配。

Hermes 将转发操作纳入已有操作 ID 与禁止未知重发的路径，等待原生确认，返回真实消息 ID。
未新增核心模型工具、未改变会话键或人格缓存前缀。此项未处理图片入站缺口。

## 实机证据

- [self-test-r5.txt](self-test-r5.txt)：卡片发送到认证机器人自身，获得真实消息 ID。
- [readback-r5.txt](readback-r5.txt)：通过 QQ 原生读取接口展开，1182 字原文 SHA-256 完全一致，
  首尾标记均正确，正文资源存在。
- [hermes-e2e.txt](hermes-e2e.txt)：真实安装的 Hermes OneBot 适配器发送短于阈值的显式卡片，
  经完整原生链路成功，消息 ID 为 `76417699905199`。
- [verification-final.json](verification-final.json)：再次读回 Hermes 发出的卡片，正文哈希一致；
  `forward` 能力公开，Telegram 与 QQ 都 connected；3 秒、3 气泡上限及 256k／32k 配置保持。
- UI 已实际点开卡片并检查首尾：[开头截图](card-expanded-top.png)、[末尾截图](card-expanded-bottom.png)。
  截图仅截取合成测试正文窗口。
- 群聊线路通过带认证 WebSocket、真实 Unix 管道和原生回调夹具验证；未向真实群组或其他联系人测试发送。

## 验证与边界

原生桥最终 30 项相关测试通过，Linux 运行结果见 [linux-tests-final.txt](linux-tests-final.txt)。
Hermes 原有本次相关测试 100 项通过，另新增 1 项明确禁止不确定卡片结果重试的测试；
[hermes-retry-tests.txt](hermes-retry-tests.txt) 中该文件最终 15 项通过。
两侧共 **131 个不同测试**，不重复累计 Linux 或其他重跑次数。

早期验证中的失败记录保留：文件写入权限的测试准备错误；预览结构被误当作最终卡片；
以及已发送预览无法读回正文。最终实际发送及读回均通过后才设置 `forwardEnabled: true`。
测试过程中数次重启 QQ 并使用已有登录恢复，没有要求用户重新扫码；未清除账号状态、聊天历史或旧会话。

## 部署与恢复

最终源文件哈希见 [manifest-final.json](manifest-final.json)，代码副本在 `final/`。
服务器备份为 `/opt/hermes-cloud/data/.deployments/forward-card-20260906/backup`，包含原插件文件、
安装前完整原生运行目录和私有配置，只保留在服务器。恢复时只回滚相应代码／能力配置，不能覆盖新增聊天数据库。
文件已写入当前容器的持久可写层，常规容器或主机重启保留；重新创建 QQ 容器时须应用保存的原生代码副本。

机器人自身会话中保留了合成测试草稿和测试卡片，包括前期空预览；没有把它们当作业务消息或官方人物经历。
