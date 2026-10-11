# LuckyLilliaBot 生产切换与旧桥清理

后续按用户要求已改为 SnowLuma + PMHQ 有头模式，本文件保留 LLBot 阶段的历史记录。
当前独立源码与生产说明位于本机 `/Users/cornna/project/snowluma-pmhq/deployment/README.md`。
LLBot 容器已下线；Hermes 继续使用相同标准 OneBot 端口。

2026-09-08 UTC。生产服务器为 SSH 别名 `cornna-cn`，Hermes 运行于
`hermes-cloud`。原 OneBot 端口在本轮开始前已离线。

**部署与代码清理已完成，QQ 登录和真实消息验收尚未完成。** 新服务返回
`hasToken=false`、`online=false`，用户确认 LuckyLillia 官方授权需要提交申请。
不能把管理面板启动、模拟链路通过或 Hermes 启动初期的 connected 标记当成 QQ 在线。

## 当前部署

- 登录服务：容器 `luckylilliabot`，LLBot 8.1.10。
- 镜像固定为 `linyuchen/llbot:8.1.10@sha256:067159e742b529dedba5f0b2987290fb34b4627a1f605b83119e36ef76a81467`。
- 模式：direct；尚无 Auth Token，没有启动 QQ 登录。
- WebUI：[qq.cornna.xyz](https://qq.cornna.xyz/)，宿主 loopback 6099 → 容器 3080。
- OneBot：`ws://127.0.0.1:3301` → 容器 3001；正向 WS、array 消息格式、沿用 Hermes 的 OneBot token。
- 数据：`/opt/llbot/data`；配置：`/opt/llbot/config/default_config.json`。
- 容器使用 UID/GID 991/988、无附加 capabilities、no-new-privileges、512 MiB 内存限制。
- 不自动选择账号；预期机器人账号为 2952532060。

OneBot token、WebUI 密码、LuckyLillia 官方 Auth Token 是三份不同凭据。
面板密码保存在本机私有文件 `~/.hermes/llbot-production-access.txt`；没有写入仓库。

## 已应用的修改

生产仅局部修改 `adapter.py`、`client.py`、`README.md`，部署前后均校验 SHA-256，
未用本地整套未提交内容覆盖生产。详见 [生产增量](production-cleanup.patch) 与
[哈希清单](production-cleanup-manifest.json)。

移除 native/desktop QQ 特殊传输与管理接线，使用标准 OneBot v11。
Hermes 配置只移除了 `native_qq`/`desktop_qq` 和 `qq-native` 插件启用项；
与部署前配置逐项比较通过。人设绑定、群白名单、三秒输入合并及其他配置保留。

本地删除六个旧桥模块、四个专用测试，并移除了五个独立旧实现目录。
生产删除三个旧 QQ 验证容器、旧网络、13 个旧镜像标签、NapCat 原生服务/health
定时器/脚本、旧桥源码目录及 `qq-native` 用户插件。账号与聊天状态保存在
`/opt/llbot/private/retired-state`，两个旧 QQ Docker 数据卷保留且无容器挂载。
本地数据归档位于 `~/.hermes/retired-qq-20260908`。

`main_window.py` 是通用 QQ 群/私聊权限模块，并非桌面自动化实现，因此保留。
生产备份和历史迁移记录也保留；不会覆盖新会话数据库来回退代码。

## 验证与限制

- [工程测试](tests.txt)：35 个文件，1571 passed，0 failed。
- 生产候选文件通过本机 loopback WebSocket 集成验证：真实适配器/客户端导入、
  鉴权、后端识别、账号状态、群/私聊入站、图片/群文本/合并转发出站。
  对端为测试协议服务，没有向真实联系人发送消息。
- 生产修改后的源码哈希、配置变更范围、旧路径删除结果均核对通过。
- Hermes 已重新启动，Telegram connected；QQ 为 onebot_link_offline，与 LLBot
  未获授权、未登录的状态一致。
- LLBot 入站文档若只提供后端本机 file:// 路径，Hermes 暂不能下载；不能把它
  描述为已经通过所有附件的真实验收。

## 为什么编译公开源码不能直接免申请登录

LLBot 的公开 TypeScript 会调用预编译 `sign-proxy.*.node`，并把 manager
Auth Token 传入原生组件。该组件提供 `signRequest` 以及包含多步 QQ 交互的
`acquireSignToken`。公开仓库没有这些服务端签名能力的完整实现；删除界面/启动
检查不能生成 QQ 所需签名。

来源：[固定版本签名调用](https://github.com/LLOneBot/LuckyLilliaBot/blob/v8.1.10/src/main/qqProtocol/direct-lib/sign.ts)、
[原生接口](https://github.com/LLOneBot/LuckyLilliaBot/blob/v8.1.10/src/main/qqProtocol/direct-lib/sign-proxy/index.ts)、
[构建配置](https://github.com/LLOneBot/LuckyLilliaBot/blob/v8.1.10/vite.config.ts)。

PMHQ 的[官方配置文档](https://github.com/linyuchen/PMHQ/blob/main/doc/config.md)
同样要求 Auth Token，它的发布流程从非公开的 PMHQ.Rust 获取原生制品。
Lagrange V2 的[官方签名服务](https://github.com/LagrangeDev/SignApiGuide)也需要白名单申请，
不能把改接它宣称为免申请的自托管实现。

后续需要有效的官方授权，或者完整、兼容且可自行部署的签名实现，才能继续 QQ 登录验收。
本轮没有伪造授权、修改服务端鉴权或发送无签名登录请求。

本目录三个 Python 文件记录本次已执行的一次性生产操作，包含部署前状态断言，
不应重复作为日常启动脚本使用。
