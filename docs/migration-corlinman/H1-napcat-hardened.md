# H1 — QQ 后端最终方案：NapCat（真客户端）+ 反风控加固

状态：**已上线：账号重新认证完成，Hermes/tunnel/NapCat 稳定**（2026-08-30）

## 0. 结论

QQ 后端定为 **NapCat**（真 QQ 客户端注入，OneBot v11 原生）。
SnowLuma、Yogurt/Milky 两条路都已排除并删除，理由见下。

## 1. 为什么排除 SnowLuma

原生钩子从不确认登录就绪。frame 级插桩证据：

- 原生**确实**发了 op=7 loginState，但内容是 `status=0 flags=0 value0=0 msg="0"`（未登录、uin 0）
- 同一时刻 QQ 进程握着 67 个 `nt_qq_*` fd、GUI 聊天列表是活的
- hello 帧 `flags=0`，说明不是 TypeScript 侧丢了 loggedIn 位
- 强行伪造 `loggedIn:true` 后 3001 端口能起来，但任何真实包都返回
  `The QQ connection changed. Please restart QQ and try again. (retcode=100)`

已排除：注入时机（三种表现完全一致）、dev 镜像（原生二进制 md5 相同）、QQ 静默热更、
ptrace 权限（`cap_sys_ptrace=ep`、`ptrace_scope=0`）、uid 不匹配、QQ 版本降级。

**改源码解决不了**：fork 内 `*.c/*.cpp/*.rs/binding.gyp/CMakeLists/Cargo.toml` 命中数为 0；
`SnowLumaNative` 的 workflow 显示原生源码在**私有仓库 `SnowLuma/nnphook`**；许可证 §5 禁止逆向。
坏掉的部件是闭源二进制，既看不到也不许碰。

## 2. 为什么排除 Yogurt / Milky（协议重实现）

一条硬规律：**凡不跑真 QQ 客户端的方案，都必须外挂第三方签名服务** ——
签 NTQQ 的包需要只有腾讯客户端才有的密钥。

Lagrange 签名服务的实测特征：

- 白名单制，需 GPG + QQ 群机器人自助申请
- **不支持自建**（维护者 issue #570 明确回复；`SignProxyUrl` 只是代理到官方）
- **停过服**（issue #904 返 521，仅在 Telegram 通知）

即命门在第三方手里且可被吊销。LLBot/PMHQ 同理（`auth.luckylillia.com` 无 token 拒绝启动，
PMHQ 仓库无源码）。

真客户端方案里，健在且**无外部鉴权依赖**的只剩 NapCat（2026-08-29 推送、★10437、
六个鉴权关键词命中数全为 0）。

## 3. 掉线根因调查（推翻了原假设）

看门狗采集的 3035 条心跳 + 事件记录给出：

```
连续在线区间：
  10:56:45 -> 12:58:47   持续 2:02:02
  22:42:36 -> 00:39:15   持续 1:56:39   ← 人工扫码后的全新会话
```

两次日志原文均为 `[KickedOffLine] 你的账号当前登录已失效，请重新登录。`

- **QZone 假设被推翻**：两次掉线前 30 分钟 QZone 调用均为 **0**
- **"cookie 先空"是误读**：cookie 空与被踢发生在同一秒，没有预警提前量
- **真实模式**：约 2 小时固定会话寿命，且掉线时快速登录票据同时失效
  （两次 `quick_login_failed`），每次都要人工扫码 —— 9.7h + 11.7h 的空窗全在等人

结论：这与用哪个框架无关，换 Milky 不会改变这个数字。

## 4. 最终生产拓扑（2026-08-30）

最终确认原假设中的“NapCat 和 Hermes 必须在同一台 corlinman-prod”不成立。
权威 QQ 会话一直在 SSH 别名 `cornna-cn`，而 Hermes 主服务切到 `corlinman-prod`：

| 层 | 主机与实现 |
|---|---|
| QQ / NapCat | `cornna-cn`；`napcat.service` + `mlikiowa/napcat-docker:v4.18.19` |
| 设备身份 | `cornna-cn:/opt/napcat/ntqq` → `/app/.config/QQ`，约 173 MiB、1,691 个文件 |
| NapCat config | `cornna-cn:/opt/napcat/config` → `/app/napcat/config` |
| OneBot | `cornna-cn:127.0.0.1:3011` → 容器 3001；不向公网开放 |
| WebUI | `https://qq.cornna.xyz/webui/`；活动 nginx vhost 在 `cornna-cn`，反代本机 6099 |
| 安全隧道 | `corlinman-prod:onebot-tunnel.service`，只监听 `127.0.0.1:3011`，经已有 `napxfer` SSH key 和固定 host key 转发到 `cornna-cn:127.0.0.1:3011` |
| Hermes | `corlinman-prod:hermes.service`；仍使用 `ws://127.0.0.1:3011`，token 与权威 NapCat 配置一致 |

`cornna-cn` 原先也运行一份 Hermes。切主前发现两条持久 WS 后，立即停止刚启动的
corlinman Hermes；完成两机配置核对后，把 `cornna-cn:hermes.service` 备份并
stop/disable，再启动 corlinman Hermes。最终只有一个 gateway 进程和一个持久 OneBot 客户端。

`corlinman-prod` 上新建但未登录成功的 staged NapCat 已 stop/disable，容器、设备目录、
config 和备份均保留，未删除、未复制设备身份，也不会与权威实例并行登录。

### 4.1 corlinman staged 实例（已停用）的加固内容

| 措施 | 实现 |
|---|---|
| **设备指纹持久化** | 复用既有 `/opt/corlinman/napcat/ntqq`，挂载到 `/app/.config/QQ`。**绝不能删、复制到并行实例或重建** —— 删掉会被视为新设备并增加验证风险 |
| **只使用官方快速登录契约** | `/etc/napcat/env`（root:root，600）只保存 `ACCOUNT`、固定 UID/GID 和时区。当前镜像 entrypoint 只明确支持 `ACCOUNT`；不使用未受支持的 `NAPCAT_QUICK_PASSWORD`，失败后转人工扫码 |
| **凭据落盘最小化** | OneBot token 从 Hermes 既有 `.env` 原子同步到通用及当前账号配置；WebUI 沿用既有 token。NapCat config 目录 700、含凭据文件 600，文档、命令输出和 journal 检查均不记录值 |
| **容器痕迹处理** | 官方镜像 entrypoint 会尝试删除容器标识并伪造部分 cgroup/cpuset 视图；这是上游实现细节，不把它当作登录成功保证 |
| **出口 IP 保持不变** | 仍走阿里云广州。**不建议**改走境外代理节点：对国内 QQ 号更可疑，且频繁换 IP 本身是风控信号 |
| **启动限流** | `StartLimitIntervalSec=600` / `StartLimitBurst=8`（放在 `[Unit]` 段 —— 放 `[Service]` 会被 systemd 静默忽略） |
| **单一生命周期管理** | `napcat.service` 使用 `Restart=on-failure`、`RestartSec=45s`；Docker 明确 `restart=no`，避免双重无限重启。AUTH_REQUIRED 时容器保持运行、health 为 unhealthy，不触发重启 |

## 5. corlinman staged 部署历史（已停用）

- 部署时间：`2026-08-30T12:39:46Z`
- unit：`/etc/systemd/system/napcat.service`，已 enable
- 镜像 tag：`mlikiowa/napcat-docker:v4.18.19`
- 运行时固定引用：`mlikiowa/napcat-docker@sha256:1336a777f9a4f1f8cb89fef42f7548deacd3645919a067a50df5b66b5e77390e`
- 本地 image ID：`sha256:b132563daa43114c9796540fc5ed25843d62c78676e8bef7f9bfa639d1c1467c`
- 版本复核：容器日志只提取版本号，命中 `4.18.19`；未输出原始日志
- 设备卷：`/opt/corlinman/napcat/ntqq` → `/app/.config/QQ`
- 配置卷：`/opt/corlinman/napcat/config` → `/app/napcat/config`
- 端口：`127.0.0.1:3011` → 容器 3001（OneBot v11 WS）；`127.0.0.1:6099` → 容器 6099（WebUI）。两项均只绑定 loopback
- OneBot：Hermes 保持连接 `ws://127.0.0.1:3011`；token 使用 Hermes 既有秘密来源，不在 unit 或本文出现
- WebUI：目标容器只暴露 `127.0.0.1:6099`。原文记录的
  `https://qq.cornna.xyz/webui/` 返回的是另一份 NapCat WebUI：公网 index 为 2,303 bytes，
  本容器 index 为 1,365 bytes，内容哈希不同；目标主机 nginx 也没有该域名的 vhost。
  因此该公网入口不能作为本容器登录或健康证据
- 旧容器没有删除，已改名为 `corlinman-napcat-legacy-20260830T123501Z`，原 container ID 保持不变，用于回滚

### 5.1 部署前证据与备份

部署前复核结果：旧 `corlinman-napcat` 自 `2026-08-19T10:02:56Z` 起
`Exited(137)`，`OOMKilled=false`、Docker `restart=no`；NapCat/OneBot 端口无监听，
`hermes.service` 为 inactive。旧镜像是可变的 `latest`，digest 为
`sha256:d8098fdabedfe5cbc570b994aebb685c3096a2bebbf4ea43ffb21655a4758e63`。

备份目录：`/opt/corlinman/backups/napcat-20260830T123501Z`（root:root，700）。其中包含：

- 旧容器、旧镜像、旧 config volume 的完整 inspect
- 旧 NapCat/Hermes systemd unit 与部署前状态
- 旧 NapCat config 的完整 tar 包及 SHA-256
- 设备目录 4,674 个文件的逐文件 SHA-256、权限/属主/大小/mtime 清单
- 新 unit、post-change config、目标镜像和部署后状态快照

设备目录约 1.8 GiB；部署前根分区只剩 6.2 GiB，还需拉取约 1.45 GB 的目标镜像。
为避免备份本身耗尽生产磁盘，没有复制第二份设备树，而是保留原目录不动并生成全量校验清单。
这不是异机灾备；后续应在维护窗口把该目录加密复制到独立存储。

**不要在这个 vhost 上加 `auth_basic`**：WebUI 的二维码刷新走 WebSocket，
浏览器不会给 WS 升级请求自动附带 Basic 凭据，会导致反复弹认证框（已踩过）。

## 6. staged 实例调查与误判纠正

下表是 staged 实例在关闭前的历史快照，不是最终生产状态：

| 层 | 结果 | 证据 |
|---|---|---|
| systemd | 通过 | `napcat.service` active/running、enabled、`NRestarts=0`；`Restart=on-failure`、45 秒退避、600 秒内最多 8 次 |
| Docker 生命周期 | 通过 | 容器 running、`RestartCount=0`、`OOMKilled=false`、Docker `restart=no` |
| 镜像固定 | 通过 | tag、registry digest、本地 image ID 和容器实际 image ID 一致；运行日志版本号为 4.18.19 |
| 持久化 | 通过 | 设备目录和 config 目录挂载目标正确；未复制或删除设备身份 |
| 端口暴露 | 通过 | 宿主 3011/6099 均只绑定 `127.0.0.1`；WebUI 容器内 6099 已打开 |
| OneBot transport | **待认证** | 宿主端口已发布，但容器内 3001 尚未打开；Docker health 为 unhealthy |
| QQ account | **AUTH_REQUIRED** | QQ 进程存活，日志分类检测到扫码/二维码标记、无 login-success/kicked/fatal；目标身份应为 `2952532060`，但本容器尚不能用 `get_login_info` 确认 |
| Hermes | 安全停机 | `hermes.service` 保持 inactive，部署后 `connect_failed` 新增为 0；未发送任何 QQ 消息 |

当前不是“服务崩溃”：NapCat 和 QQ 稳定运行，WebUI 可达，只是在等待一次人工扫码。
不要重启容器，不要自动尝试密码，也不要把二维码、WebUI token 或 OneBot token 发到聊天里。

### 6.1 首次扫码后续验尝试（未放行）

用户在 `2026-08-30T13:35Z` 前报告“已经登录”，但生产实例没有产生账号就绪证据：

- `13:35:42Z`、`13:36:31Z`、`13:37:46Z`、`13:39:20Z`、`13:40:09Z`
  的只读检查中，NapCat 一直 active、容器一直 running，systemd/Docker 重启计数均为 0；
- 容器内 6099 始终打开，但 3001 始终关闭，health 始终为 unhealthy；宿主 3011
  只是 Docker 端口代理，不能据此判定 OneBot 健康；
- 二维码文件在 `13:36:55Z` 和 `13:38:56Z` 仍有刷新，日志分类没有
  login-success、kicked 或 fatal 标记，说明该生产实例仍在等待手机端授权落地；
- 因 WS 服务未建立，没有伪造 `get_status` / `get_login_info` 结果，也没有启动 Hermes。
  `hermes.service` 仍为 inactive/disabled，部署后的 `connect_failed` 计数仍为 0。

结论仍为 `AUTH_REQUIRED`。此前把过期 `webui.json:autoLoginAccount` 的尾号当作预期身份
是错误的：该字段不是当前账号事实。用户提供的截图经主线程原图复核，显示
NapCat WebUI 绿色在线、账号 grantly、UIN `2952532060`、NapCat 4.18.19、
QQ 3.2.30-50969；但截图本身没有证明它属于 `corlinman-prod` 的目标容器。

`2026-08-30T15:18Z` 再次复核时，目标容器仍每分钟刷新二维码、容器内 3001 关闭，
而按旧文档访问的公网 WebUI 与本机 6099 页面指纹不同。由此可知“截图在线”和“目标容器
AUTH_REQUIRED”并不矛盾：它们不是同一个可验证实例。禁止据另一实例截图启动 Hermes，
也禁止让同一个 QQ 账号在两个 NapCat 实例并行登录。

在不重启当前 QQ 进程的前提下，已做最小持久配置修正：

- `/etc/napcat/env` 的 `ACCOUNT` 改为 `2952532060`；
- `webui.json:autoLoginAccount` 改为 `2952532060`；
- `onebot11_2952532060.json` 的启用 WS server 保持 `0.0.0.0:3001`，token 从
  Hermes 既有秘密来源同步，值不进入命令输出或本文；
- 三个文件权限均保持 600。修正备份位于
  `/opt/corlinman/backups/napcat-20260830T123501Z/identity-correction-20260830T152019Z`。

NapCat 没有对这些文件变更热加载，3001 仍关闭；修正只保证下次受控启动使用正确身份，
不代表当前会话已经登录。未经新的明确授权，不得为了应用修正而重启 QQ。

远端 Hermes 实际从 `/opt/hermes/repo` import，远端 commit 为
`8911e2e0edf750b104edbdc106d63d6cdac88524`。该目录里的 OneBot 插件和
`tools/onebot_client.py` 是未纳入远端 git 的文件，且不含本地工作区尚未提交的
`ACCOUNT_AUTH_REQUIRED_MARKER`、`account_probe_secs` 和独立 account-state 追踪。
本轮没有覆盖远端代码；先恢复现有服务，账号状态加固应在本地改动完成审查、测试和版本化部署后
另行上线。

### 6.2 staged 实例人工扫码说明（当前不要执行）

1. 用 SSH 本地转发访问目标实例，例如
   `ssh -L 6099:127.0.0.1:6099 corlinman-prod`，再在本机打开
   `http://127.0.0.1:6099/webui/`；不要使用旧文档中的公网入口。
2. 用已有 WebUI token 登录，确认页面对应目标主机且待登录账号为 `2952532060`，
   只扫描当前二维码一次。
3. 等待容器 health 变为 healthy；不要通过重启“刷新二维码”。
4. 使用本机短生命周期探针连接 `ws://127.0.0.1:3011`，token 从
   `/opt/hermes/data/.env` 进程内读取，不放到命令行或输出中；只调用
   `get_status` 和 `get_login_info`。验收条件是 WS 握手成功、`online=true`、
   `good=true`、`get_login_info` 成功且 UIN 为 `2952532060`。
5. 仅在以上全部通过后执行 `sudo systemctl start hermes.service`；随后确认
   Hermes active，观察 journal 中 `connect_failed` 计数不再增长。禁止发送测试消息。

## 7. 最终上线验证

### 7.1 权威后端确认

只在本机 `~/.ssh/config` 已保存的主机别名内做回环检查；没有扫描未知 IP。
在所有可达别名中，只有 `cornna-cn` 同时满足：

- 活动 `qq.cornna.xyz` nginx vhost → `127.0.0.1:6099`；
- NapCat 4.18.19 容器、`/opt/napcat` 持久卷、loopback 3011/6099；
- 容器日志版本号为 NapCat 4.18.19、QQ 3.2.30-50969；
- 容器内 3001/6099 均打开，NapCat 和 Docker 重启计数均为 0。

短生命周期 WS 探针从目标账号专属 OneBot 配置进程内读取 token，不回显值，只调用
`get_status` 和 `get_login_info`，结果：

```text
WS_LINK=true
GET_STATUS_OK=true
ACCOUNT_ONLINE=true
ACCOUNT_GOOD=true
GET_LOGIN_INFO_OK=true
UIN=2952532060
```

这与用户截图（主线程原图复核：绿色在线、grantly、UIN 2952532060、NapCat 4.18.19、
QQ 3.2.30-50969）一致。截图不嵌入本文，也不保存二维码或 credential。

### 7.2 Hermes 切主

- `onebot-tunnel.service` 于 `2026-08-30T15:30Z` 部署，active/enabled，重启 0；
- 经隧道再次执行 `get_status` / `get_login_info`，结果与权威主机本地探针一致；
- `cornna-cn` 旧 Hermes 备份至
  `/opt/hermes/backups/qq-cutover-20260830T153446Z` 后 inactive/disabled；
- `corlinman-prod` Hermes 于 `2026-08-30T15:34:51Z` 切为 active/enabled；
- 隧道部署备份位于
  `/opt/corlinman/backups/napcat-20260830T123501Z/hermes-tunnel-20260830T153009Z`；
- 两机 `.env` key 集合相同、OneBot token 相同；`config.yaml` 只在
  `platforms.onebot.home_channel` 的 name/chat_id/user_id 上不同，这是新主的预期路由差异；
- 没有发送任何 QQ 测试消息。

### 7.3 稳定性观察

从切主基线 `15:34:51Z` 到最后严格样本 `15:44:13Z`，并在
`15:36:24Z`、`15:37:40Z`、`15:41:58Z`、`15:43:12Z`、`15:44:13Z`
取得只读快照：

| 指标 | 趋势 |
|---|---|
| corlinman Hermes | active，`NRestarts=0`，gateway 进程恒为 1 |
| SSH tunnel | active，`NRestarts=0`，持久 WS 恒为 1 |
| cornna-cn 旧 Hermes | inactive/disabled，本地 Hermes OneBot 客户端恒为 0 |
| 权威 NapCat | active，systemd/Docker restart 恒为 0 |
| 账号探针 | 每次 `online=true`、`good=true` |
| 失败指标 | `connect_failed=0`、kicked=0、fatal=0、QR=0 |

### 7.4 尚未部署的本地加固

`corlinman-prod` 实际从 `/opt/hermes/repo` import，remote commit 为
`8911e2e0edf750b104edbdc106d63d6cdac88524`；远端 OneBot adapter/client
与本地工作区哈希不同，且不含本地未提交的 `ACCOUNT_AUTH_REQUIRED_MARKER`、
`account_probe_secs` 和独立 account-state 追踪。本次恢复没有覆盖远端代码；这些改动必须在
本地完成审查、测试和版本化部署后另行上线。

本节的“上线”状态在 `2026-08-30T15:58Z` 前成立；随后发生新的
`KickedOffLine`，最新状态见下一节。

## 8. 独立复审修复与账号事件

### 8.1 NapCat config 权限

权威容器的 `NAPCAT_UID=0`、`NAPCAT_GID=0`，在线 QQ 进程也实际运行于 0:0；
`/opt/napcat/config` 原为 root:root 755，7 个文件均为 root:root 644，非 root
本机用户可读取 `webui.json`。修复前只保存元数据、权限与 ACL 清单，不复制或输出文件内容：

`/opt/hermes/backups/qq-cutover-20260830T153446Z/napcat-config-perms-20260830T155447Z`

在不重启 NapCat 的情况下完成：

- config 根和全部子目录 root:root 700；
- 全部文件 root:root 600；
- `nobody` 对 `webui.json` 的读取断言为 false；
- NapCat/systemd/Docker 重启计数保持 0，WebUI 返回 200；
- 修复后的 `get_status`、`get_login_info` 仍成功，身份为 2952532060。

### 8.2 SSH tunnel 失败策略

原 unit 的风险组合是 `RestartSec=5s`、`StartLimitIntervalSec=600`、
`StartLimitBurst=8`，且没有 SSH `ConnectTimeout`。持续网络故障会快速耗尽 8 次启动额度，
随后永久停在 failed，必须人工 reset。

现有 `/etc/systemd/system/onebot-tunnel.service` 已原子更新并 daemon-reload，运行中的
生产隧道没有中断：

| 项 | 修改前 | 修改后 |
|---|---|---|
| Restart | `on-failure` | `on-failure` |
| RestartSec | 5 秒 | 30 秒 |
| StartLimitIntervalSec | 600 秒 | 0（禁用启动限流） |
| StartLimitBurst | 8 | 8（interval=0 时不锁死） |
| SSH ConnectTimeout | 无 | 10 秒 |
| 其他安全项 | StrictHostKey、BatchMode、keepalive、loopback | 保持不变 |

备份与证据：

`/opt/corlinman/backups/napcat-20260830T123501Z/hermes-tunnel-20260830T153009Z/restart-hardening-20260830T155529Z`

### 8.3 无损恢复演练

演练只使用 `/run/systemd/system` 下的临时 test unit 和未使用的
`127.0.0.1:39998`，不经过生产 SSH endpoint 或 OneBot：

1. test unit 复刻 `Restart=on-failure`、`RestartSec=30s`、
   `StartLimitIntervalSec=0`，endpoint 初始关闭；
2. 连续失败后 `NRestarts=11`，状态仍为 `activating/auto-restart`，没有
   start-limit-hit；
3. 启动临时 loopback endpoint，没有调用 `reset-failed`；下一次自动重试使
   `NRestarts=12`、`ActiveState=active`、`Result=success`；
4. 保存 unit、show、journal 证据后停止并删除两个临时 unit，39998 无监听。

这证明持续失败超过旧 8 次门槛后不会锁死，endpoint 恢复时能自行恢复。

### 8.4 watchdog 收敛

`/usr/local/sbin/napcat-watch.py` 会在 link/account 异常时直接调用
`systemctl restart napcat.service`，即使有 20 分钟冷却与失败上限，仍是第二个会消耗
登录尝试的生命周期管理器。unit 没有影响 NapCat 的 ExecStop，因此已在备份后安全
stop/disable：

`/opt/hermes/backups/qq-cutover-20260830T153446Z/napcat-watch-disable-20260830T155600Z`

关闭 watchdog 没有重启 NapCat；容器与 3001 保持运行。现在只有
`napcat.service` 管理容器生命周期。

### 8.5 KickedOffLine 与最终观察

复审演练期间，账号出现新的真实掉线，与权限或服务重启无关：NapCat/systemd/Docker
重启计数均为 0，但连续状态探针从 online/good 转为：

```text
WS_LINK=true
STATUS_ENVELOPE_OK=true
ONLINE=false
GOOD=true
LOGIN_ENVELOPE_OK=true
UIN=2952532060
```

NapCat 最近日志分类计数 `KickedOffLine=2`、fatal=0、QR=0。发现后立即
stop/disable corlinman Hermes，避免旧版 adapter 在账号离线时继续工作；没有重启
NapCat、没有尝试密码、没有发送 QQ 消息。SSH tunnel 保持 active，便于人工认证后复核。

`16:03:35Z`–`16:08:42Z` 六个分钟样本全部一致：

- Hermes inactive/disabled，`NRestarts=0`；
- tunnel active，`NRestarts=0`，`connect_failed=0`；
- NapCat active，systemd/Docker restart 均为 0；
- watchdog inactive/disabled；
- 状态 API 始终成功返回 `online=false, good=true`；
- kicked 固定为 2，fatal/QR 固定为 0，没有新增长或自动登录重试。

该观察窗口当时的结论是：两项复审加固通过，账号上线门槛打回，等待人工重新认证；
该状态随后已由 8.9 节的重新认证解除。

### 8.6 stale half-login 与密码拒绝

用户在 WebUI 输入密码后看到“账号已经登录，无法重复登录”。没有读取或记录密码；服务日志
只显示“正在密码登录”后触发 kernel 的重复登录 guard。用 WebUI 自身的短生命周期 Credential
只读调用官方 API，得到：

```text
CheckLoginStatus: code=0
isLogin=false
isOffline=false
qrcode_present=true
loginError=duplicate_login_guard

GetQQLoginInfo: code=0
uin=2952532060
online=false
```

这确认是 stale half-login：NapCat 的 WebUI runtime 已认为未登录，OneBot 也明确 offline，
但 QQ kernel 仍持有同 UIN 的 login handle。v4.18.19 bundle 中的
`onUserLoggedIn` 正是写入该 guard 的位置；`PasswordLogin` route 本身使用正确 POST，
guard 没有提供独立数值错误码，而是写入 `CheckLoginStatus.data.loginError`。

部署 bundle 和官方源码均没有 QQ logout/offline/clear-current-session WebUI API；
`/api/auth/logout` 只撤销 WebUI Credential，不会注销 QQ。官方同版本公开 issue
`NapNeko/NapCatQQ#2027` 也记录了被踢后出现相同重复登录 guard、只能重启恢复的缺陷。
因此没有调用私有 kernel 方法、没有删除设备目录/GUID/config，也没有继续尝试密码。

### 8.7 405 根因与 nginx 修复

脱敏 access/error 时间线表明：浏览器在 `00:16:40 +0800` 先成功 POST
`/api/Process/Restart`（HTTP 200），随后 127.0.0.1:6099 在内部进程重启窗口 reset/close。
正确使用 POST 的 `CheckLoginStatus`、`GetQQLoginInfo`、`RefreshQRcode` 随后收到 405。

405 不是 NapCat route 的 Method Not Allowed。旧 vhost 使用：

```nginx
error_page 502 503 504 = @down;
location @down {
    try_files /502.html =502;
}
```

named location 保留原 POST 方法，Nginx 静态文件 handler 因而返回误导性的 405。
现已备份并改成内部 URI 的明确 503：

```nginx
error_page 502 503 504 =503 /qqconsole-down.html;
location = /qqconsole-down.html {
    internal;
    try_files /502.html =503;
    add_header Retry-After 10 always;
}
```

备份：`/opt/hermes/backups/qq-cutover-20260830T153446Z/nginx-405-fallback-20260830T161941Z`。
`nginx -t`、reload 均通过，WebSocket Upgrade 配置保持不变。独立 closed-upstream 测试中
POST/GET 均返回 503 且带 `Retry-After: 10`；在线时公网/本机 OPTIONS=204、POST=200，
WebUI=200。

该次 `/api/Process/Restart` 由已有 WebUI 会话发起，不是本 runbook 的自动动作。
容器没有重启，但 QQ/NapCat 内部进程重新初始化；之后日志只生成 QR，没有 quick/password
回退、duplicate guard 或 fatal。

### 8.8 当前认证入口

重启后的首次只读状态为 `isLogin=false`、`isOffline=false`、无 loginError、但没有可用 QR。
确认 v4.18.19 的 `RefreshQRcode` 代码只会清空 QQLoginError 并调用
`getQRCodePicture`，不会删除设备身份后，执行官方 `RefreshQRcode` **一次**：

```text
RefreshQRcode: code=0
CheckLoginStatus: code=0
isLogin=false
isOffline=false
loginError_present=false
qrcode_present=true
```

没有调用 PasswordLogin、quick-login 或 Restart。config 元数据未变化，设备文件计数在动作前后
均为 1,969，NapCat/systemd/Docker 重启计数仍为 0。备份：
`/opt/hermes/backups/qq-cutover-20260830T153446Z/qr-refresh-20260830T162133Z`。

当时给用户的下一步只有一个：扫描权威 WebUI 的当前新 QR 一次并在手机确认，不再输入密码、
刷新或重启。该步骤已完成，结果见 8.9 节。

### 8.9 重新认证与最终上线

用户完成当前新 QR 的一次认证后，双层放行全部通过：

```text
WebUI CheckLoginStatus: code=0
isLogin=true
isOffline=false
loginError=false
WebUI GetQQLoginInfo: online=true, UIN=2952532060

OneBot WS_LINK=true
get_status: status/retcode 成功, online=true, good=true
get_login_info: status/retcode 成功, UIN=2952532060
```

同时确认：

- `/opt/napcat/config` 仍为目录 700、文件 600，非 root 不可读；
- `napcat-watch.service` 仍为 inactive/disabled；
- `cornna-cn:hermes.service` 仍为 inactive/disabled；
- 权威 NapCat active、systemd/Docker restart 均为 0。

`corlinman-prod:hermes.service` 于 `2026-08-30T16:23:17Z` enable/start，初始状态为
active、`NRestarts=0`、gateway 进程 1、持久 WS 1、`connect_failed=0`。

从 `16:23:17Z` 到 `16:29:05Z` 的六个分钟样本全部通过：

| 指标 | 结果 |
|---|---|
| Hermes | active/enabled，重启恒为 0，gateway 恒为 1 |
| SSH tunnel | active，重启恒为 0，持久 WS 恒为 1 |
| 权威 NapCat | active，systemd/Docker restart 恒为 0 |
| 账号 | 每次 `get_status` 均为 online/good |
| 旧实例 | cornna 旧 Hermes、watchdog 持续 inactive/disabled |
| 失败/副作用 | connect_failed=0、kicked=0、fatal=0、QR=0、send action=0 |

最终生产状态重新达到通过；没有发送 QQ 测试消息。

## 9. 回滚

最终切主的无秘密回滚顺序：

```bash
ssh corlinman-prod 'sudo systemctl disable --now hermes.service'
ssh cornna-cn 'sudo systemctl enable --now hermes.service'
ssh corlinman-prod 'sudo systemctl disable --now onebot-tunnel.service'
```

回滚后确认 `cornna-cn` 只有一个本地 Hermes→OneBot 连接，且 corlinman 的 3011
无监听。不要启动 corlinman staged NapCat；其未来 `ACCOUNT` 已修正为 2952532060，
但启动会与权威实例形成双登录风险。

以下命令仅用于把 corlinman staged NapCat 恢复到部署前状态，不是正常切主回滚：

以下命令不含秘密，把机器恢复到部署前“旧容器保留但停止、Hermes 停止”的状态。
它不会尝试用旧客户端登录，也不会删除新容器或新数据：

```bash
sudo systemctl stop hermes.service
sudo systemctl disable --now napcat.service
sudo docker rename corlinman-napcat corlinman-napcat-failed-20260830T123946Z
sudo docker rename corlinman-napcat-legacy-20260830T123501Z corlinman-napcat
sudo mv /etc/systemd/system/napcat.service \
  /opt/corlinman/backups/napcat-20260830T123501Z/napcat.service.rolled-back
sudo systemctl daemon-reload
```

回滚后应核对：`corlinman-napcat` 为 stopped、`hermes.service` 为 inactive、
3011/6099 无监听。不要执行 `docker rm -v`，不要删除
`/opt/corlinman/napcat/ntqq`、旧匿名 config volume 或备份目录。

如需撤销 OneBot token 同步，可在 NapCat 停止后从备份恢复旧 config：

```bash
sudo tar -xzf \
  /opt/corlinman/backups/napcat-20260830T123501Z/napcat-config.tar.gz \
  -C /opt/corlinman/napcat/config
sudo chown -R 1000:1000 /opt/corlinman/napcat/config
sudo find /opt/corlinman/napcat/config -type d -exec chmod 700 {} +
sudo find /opt/corlinman/napcat/config -type f -exec chmod 600 {} +
```

复审修复的回滚：

```bash
# tunnel unit：先保持 Hermes 停止，再恢复旧 unit；重启 tunnel 才会应用旧策略
ssh corlinman-prod 'sudo cp \
  /opt/corlinman/backups/napcat-20260830T123501Z/hermes-tunnel-20260830T153009Z/restart-hardening-20260830T155529Z/onebot-tunnel.service.before \
  /etc/systemd/system/onebot-tunnel.service && sudo systemctl daemon-reload'

# watchdog：仅在明确接受它会重启 QQ 的风险后恢复
ssh cornna-cn 'sudo systemctl enable --now napcat-watch.service'

# nginx 405 fallback（通常不应回滚）
ssh cornna-cn 'sudo cp \
  /opt/hermes/backups/qq-cutover-20260830T153446Z/nginx-405-fallback-20260830T161941Z/qq.cornna.xyz.before \
  /etc/nginx/sites-available/qq.cornna.xyz && sudo nginx -t && sudo systemctl reload nginx'
```

不建议回滚 config 权限。原权限元数据在备份中；如确需恢复，原统一状态是目录 755、文件
644、owner 0:0。

## 10. 待观测与剩余风险

- 权威 `cornna-cn:napcat.service` 当前仍是 systemd `Restart=always`；Docker 为
  `restart=no`，watchdog 已关闭。生命周期已收敛为单一 manager，但 unit 自身的无限重启仍应
  在人工认证维护窗口改成 on-failure + 合理退避，避免启动失败时反复登录。
- 权威环境仍存在 `NAPCAT_QUICK_PASSWORD`。v4.18.19 官方 entrypoint 未明确承诺该变量，
  本轮没有为清理它而重启已登录会话；后续应在有回滚和人工扫码窗口时移除。
- SSH tunnel 使用现有 root `napxfer` key。host key 固定且监听仅 loopback；后续可把该 key
  收紧为专用低权限账号和 `permitopen=127.0.0.1:3011`。
- 本地 account-state 加固尚未部署，账号被踢时远端 Hermes 仍可能只看到 link 层在线。

那个 **~2 小时被踢** 的规律是否在新部署上复现。样本仅 2 次，是线索不是定论。
若复现，说明是账号/协议层面而非环境；若不复现，则此前的删库重建（新设备指纹）可能是诱因。
