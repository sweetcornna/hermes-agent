# T4 — QQ 协议端迁至家宽住宅网络：迁移 runbook

**成文** 2026-08-31｜**状态**：待执行｜**执行方式**：人工逐步，不自动化
**前置阅读**：`docs/migration-corlinman/H1-napcat-hardened.md`（§4 拓扑、§8 加固、§10 遗留）

> 本文档是这次迁移的**唯一执行依据**。每一步都写明：做什么、预期输出是什么、
> 不是预期输出该怎么办。**未通过验证的步骤不得进入下一步。**
>
> 三条贯穿全文的硬约束，违反任何一条都会直接伤到线上账号：
>
> 1. **同一个 QQ 号在任何时刻只能有一个协议端在登录。** 后登踢先登，且会加计风控。
> 2. **设备身份目录 `ntqq/` 不复制、不删除、不重建。** 跨机复制实测无效（见 §2）。
> 3. **不做任何自动重登、自动密码登录、自动重启 QQ。** 登录尝试是稀缺资源。

---

## 0. 执行前必须理解的六件事

### 0.1 ⚠️ 现状已与 H1 不同：Docker 拓扑已被原生 AppImage 取代

H1 §4/§5 记录的是 2026-08-30 的状态：NapCat 跑在 Docker 里，容器内 3001 经端口映射到宿主 3011。
**2026-08-31 的只读实测表明权威机 `cornna-cn` 早已不是这个样子了。** 拿着 H1 去找容器，你会
一无所获（`docker ps` 上没有任何 napcat 容器），然后误判成"服务没在跑"。

| 项 | H1 记录（已过时） | 2026-08-31 实测（当前事实） |
|---|---|---|
| 运行形态 | Docker 容器 `mlikiowa/napcat-docker:v4.18.19` | **原生 AppImage**，无容器 |
| systemd unit | `napcat.service` | **`napcat-native.service`**（`napcat.service` 在 `cornna-cn` 上**不存在**） |
| 运行身份 | 容器内 `NAPCAT_UID=0` | `User=napcat-native` / `Group=napcat-native` |
| 工作目录 | 容器 `/app` | `WorkingDirectory=/opt/napcat-native/runtime` |
| 环境文件 | `/etc/napcat/env` | `/etc/napcat-native/env` |
| 进程树 | `docker run` → 容器 init | `dbus-run-session → AppRun → qq → qq` |
| OneBot 监听 | 容器 3001 → 宿主 `127.0.0.1:3011`（端口映射） | **直接** `127.0.0.1:3011`，无映射 |
| WebUI 监听 | 容器 6099 → 宿主 `127.0.0.1:6099` | **直接** `127.0.0.1:6099` |
| 设备身份 | `-v /opt/napcat/ntqq:/app/.config/QQ` | `BindPaths=/opt/napcat/ntqq:/opt/napcat-native/home/.config/QQ` |
| config | `-v /opt/napcat/config:/app/napcat/config` | `BindPaths=/opt/napcat-native/config-candidate:/opt/napcat-native/runtime/config` |
| unit 状态 | active/enabled | **`disabled` 但 active**（靠人工 start 起来的） |

**由此直接失效的一条 H1 结论**：H1 §6.1 和 00-PLAN D81 反复强调的"宿主 3011 只是 Docker 端口代理，
容器内 3001 可能没开、不能据此判定 OneBot 健康"这个坑，在原生形态下**不存在** ——
`ss -ltn` 看到 `127.0.0.1:3011` 就是 OneBot 自己在监听。

**仍然有效的 H1 结论**：目标架构（Hermes 当客户端、只连 loopback）、加固原则（单一生命周期管理器、
启动限流放 `[Unit]`、config 700/600、不用密码登录）、设备身份不可复制不可重建。
本 runbook 全部继承。哪条抄自哪里，见 §9。

### 0.2 这次迁移换的是「出口 IP 的性质」，不是「换个机房」

H1 §3 用 3035 条心跳测出：连续在线区间约 2 小时整（`2:02:02` 和 `1:56:39`），
两次都是 `[KickedOffLine] 你的账号当前登录已失效`，且快速登录票据同时失效，每次都要人工扫码。
两次空窗 9.7 h 和 11.7 h，**全在等人发现**。

判断：`cornna-cn` 是阿里云机房 IP，`2952532060` 是 2026-08-28 才启用的新号。
"约 2 小时固定会话寿命"是低信任账号的典型症状，在机房 IP 上不会自己改善。
本次把协议端下沉到家宽住宅 IP，是在**改变账号所处的信任环境**，不是在换服务器。

这条决定已由用户批准，**不在本 runbook 内重新论证**。本 runbook 只负责让它安全地发生。

### 0.3 一个账号、一次登录、一台机器

迁移的净效果是：**你会烧掉一次人工扫码，换来一个新设备指纹。** 这个新指纹从此就是这个账号的
设备身份，**永不再迁**（§2.3）。回滚也要再烧一次扫码（§7.1）—— 回滚不是免费的。

所以：**在开始之前，先确认你人在家里那台机器旁边，手机在手，能扫码。**
中途没人扫码 = QQ 长时间离线 = 又一次 9.7 小时空窗。

### 0.4 宿主机选型闸门（这一步不过，后面全白做）

家里那台机器必须同时满足：

| 条件 | 为什么 | 怎么验 |
|---|---|---|
| 7×24 常开，不休眠 | QQ 会话本来就只有约 2 小时，机器睡一次就等于一次掉线 | §4.1 的休眠检查 |
| 断电自恢复 | 家里跳闸比机房常见得多 | BIOS `Restore on AC Power Loss = Power On` |
| **Linux + systemd（硬性）** | 本 runbook 全部加固都是 systemd 单元。**没有 systemd 就没有可执行路径**，见下 | `systemctl --version` |
| 出口是住宅宽带 | 这是整个迁移的**唯一目的** | §0.2 |
| 你能在需要时够到它 | 掉线要扫码。CGNAT 后面的机器不是随时够得到的 | §0.5 |

> ### ⛔ macOS 不是本 runbook 支持的路径（2026-08-31 修订，原「附录 A」已删除）
>
> `mlikiowa/napcat-docker:v4.18.19` 确实有 arm64 变体，所以"镜像能跑"是真的 ——
> 但那只解决了架构问题，没解决 init 系统问题。**这不是"多一层风险"，是"没有可执行的步骤"**：
>
> 1. **§4.7 那把「每小时一次登录尝试」的人工闸在 launchd 上做不出来。**
>    launchd 没有 `StartLimitIntervalSec` / `StartLimitBurst` 的对应物。
>    那把闸是本次迁移的核心反风控设施，不是可选装饰。
> 2. **§4.4 / §5.2 的全部沙箱加固（`ProtectSystem`、`BindPaths`、`NoNewPrivileges`……）
>    在 launchd 上全部没有对应物。**
> 3. **Docker Desktop 的 VM 需要 GUI 登录会话才启动**，LaunchDaemon 在登录前就跑，
>    依赖不成立；改用 `colima` + LaunchAgent + 自动登录，等于把"7×24"降级成"每次重启要人登录桌面"。
> 4. **原生 AppImage 路径在 macOS 上根本不存在**（AppImage 是 Linux 格式），只剩 Docker 一条路，
>    而 Docker 路径在本 runbook 里本身就是回退路径（§4.5）。
>
> 本次修订**删除了原「附录 A：如果家里那台机器是 macOS」**。那份附录只有 systemd/launchd 概念
> 对照表和风险说明，**没有** LaunchDaemon plist、没有 NapCat 启动配置、没有 identity/config/权限
> 准备、没有 tunnel plist、没有 no-auto-restart 闸门、没有验收与回滚命令 ——
> 它读起来像一条可选路径，实际上按它执行会在第一步就卡住。
> **一份"看起来是路径但走不通的路径"，比明确说"这条路不支持"危险得多。**
>
> ⇒ **`systemctl --version` 不成功 = 换机器，没有别的分支。**
> 如果确实只有一台 Mac：在它上面装一个 Linux 虚拟机并把虚拟机做成开机自启（宿主 macOS 只当 hypervisor），
> 然后**在虚拟机里从头执行本 runbook**。那是一条独立的、本 runbook 未验证的路径，
> 要自己先验证「宿主重启后虚拟机自动回来」再用。

**结论：用一台 x86_64 Linux 小主机。** 架构与运行时选型见 §1.6。

### 0.5 ⚠️ 迁移之后，扫码控制台会跑到 CGNAT 后面

这是本次迁移**新引入的**运维风险，H1 里没有对应物，必须在动手前就有对策：

- 现在：QQ 掉线 → 你从任何地方 SSH 到 `cornna-cn` → `ssh -L 6099` → 扫码。
- 迁移后：QQ 掉线 → NapCat 的 WebUI 在家里那台机器的 `127.0.0.1:6099` 上 →
  家宽通常没有公网 IP 或处在 CGNAT 后面 → **你人不在家就够不到，只能等回家。**

考虑到 H1 §3 记录的空窗本来就是 9.7 h / 11.7 h，把"必须回家"加进这条链路会让空窗更长。

**对策**（§5.5 给配置）：在那条常驻反向隧道里**多带一个 WebUI 端口**，
把家里的 `127.0.0.1:6099` 反向映射到 `corlinman-prod:127.0.0.1:6100`，
之后从任何地方 `ssh -L 6099:127.0.0.1:6100 corlinman-prod` 就能扫码。
代价是：能 root `corlinman-prod` 的人也就能到家里的 WebUI。
考虑到那个人**本来就能**通过 3011 用这个 QQ 号发消息，边际风险有限，收益（能远程恢复账号）明确。
**本 runbook 默认打开它**；如果你不接受，就要接受"离家期间无法恢复 QQ"，二选一，别装作没这回事。

### 0.6 术语与端口约定

| 记号 | 含义 |
|---|---|
| `cornna-cn` | 现权威机（阿里云广州），跑 `napcat-native.service`。迁移后**退役但保留** |
| `corlinman-prod` | Hermes 主机，跑 `hermes.service` + `onebot-tunnel.service` |
| `home` | 家里那台新机器（本 runbook 里凡是 `home$` 提示符都指它） |
| `<PROD_HOST>` | `corlinman-prod` 的 SSH 可达地址（在 home 的 unit 里要写实际值） |
| `2952532060` | 目标 QQ UIN。全文出现的每一处都必须是这个数字 |

端口约定（**故意不对称，为的是 `ss` 输出能自证身份**）：

| 位置 | 端口 | 说明 |
|---|---|---|
| home OneBot v11 WS | `127.0.0.1:3001` | 新机监听 |
| home NapCat WebUI | `127.0.0.1:6099` | 新机监听 |
| prod OneBot 入口 | `127.0.0.1:3011` | 反向隧道的落点。**Hermes 只认这个，不改** |
| prod WebUI 入口 | `127.0.0.1:6100` | 反向隧道可选第二端口（§5.5） |
| cornna-cn（退役后） | 3011 / 6099 | 迁移完成后这两个端口必须**无监听** |

不要把 home 也设成 3011。这个项目已经在"哪个 3011"上栽过跟头（00-PLAN D81、H1 §6.1）：
两端不同号，`ss -ltn` 一眼就能看出你在看哪一台。

---

## 1. 架构与隧道方向

### 1.1 目标拓扑

```
家里小主机 home（住宅 IP，7×24）
  napcat-native.service（原生 AppImage，User=napcat-native）
    ├ OneBot v11 正向 WS  → 127.0.0.1:3001
    └ NapCat WebUI        → 127.0.0.1:6099
         │
         └ onebot-rtunnel.service（家里主动发起的 ssh -N -R）
              ├ -R 127.0.0.1:3011:127.0.0.1:3001   ← OneBot（必需）
              └ -R 127.0.0.1:6100:127.0.0.1:6099   ← WebUI（§5.5，默认开）
                   │
                   └ corlinman-prod
                        └ hermes.service 连 ws://127.0.0.1:3011   ← 配置一个字不用改
```

退役侧：`cornna-cn:napcat-native.service` 停止并 **mask**；
`corlinman-prod:onebot-tunnel.service` 停止并 **disable**（§3.4）。

### 1.2 为什么隧道必须由家里发起（`-R` 而不是 `-L`）

家宽的三个事实决定了方向：

1. **通常没有公网 IPv4**，或即使有也在 CGNAT 后面（运营商 NAT），`corlinman-prod` 无法主动连进来。
2. **即使有公网 IP，也是动态的**，重拨就变，任何写死对端地址的 `-L` 隧道都会在某个凌晨失效。
3. 主动往外连只需要一条出站 TCP:22，穿过任何家用路由器都不需要端口映射、不需要 DDNS、
   不需要在家里的路由器上开洞（开洞本身是新的攻击面）。

反过来，`corlinman-prod` 有固定公网地址且已经在跑 sshd，作为**被连的一端**是零成本的。

**副作用（有利）**：家里那台机器不需要任何入站端口，`ufw`/`firewalld` 可以默认拒绝一切入站。

### 1.3 为什么 Hermes 一个字都不用改 —— 以及一个必须核对的坑

因为反向隧道把落点做成了 `corlinman-prod:127.0.0.1:3011`，**和现在完全一样**。
Hermes 侧 `ONEBOT_WS_URL` 仍然是 `ws://127.0.0.1:3011`，`config.yaml` 的
`platforms.onebot.extra.ws_url` 也仍然是 `ws://127.0.0.1:3011`。

这不是巧合，是**故意的设计选择**：改端点必须两处一起改，而这个项目在这里翻过车。

- 00-PLAN 约 1959 行（D82）记录的实测：`.env` 的 `ONEBOT_WS_URL` 覆盖了 `config.yaml` 的
  `ws_url`，"只改 config.yaml 无效，hermes 仍连 3001"，真实路径是
  `/opt/hermes/data/.env`（不是 `/opt/hermes/.env`）。
- 而本地当前代码 `plugins/platforms/onebot/adapter.py:1568` 写的是
  `extra.get("ws_url") or os.getenv("ONEBOT_WS_URL", "")` —— **优先级正好相反**。
- 生产机 `/opt/hermes/repo` 停在 commit `8911e2e0`（H1 §7.4、§10；2026-08-31 复测仍然是它），
  和本地工作区不是同一份代码。

**结论：不要依赖优先级。** 这次不用改，正是最省事的路。将来真要改端点时，
`/opt/hermes/data/.env` 和 `/opt/hermes/data/config.yaml` **两处都要改并核对一致**，
改完必须重启 `hermes.service` 并从日志确认它实际连的是哪个 URL。

迁移前先把这个"不用改"确认成事实，而不是假设：

```bash
# 在 corlinman-prod 上（只读；只打印 key 与 URL，没有秘密）
ssh corlinman-prod 'sudo grep -n "^ONEBOT_WS_URL=" /opt/hermes/data/.env; \
  sudo grep -n -A2 "ws_url" /opt/hermes/data/config.yaml | head -20'
```

- **预期**：两处都出现 `ws://127.0.0.1:3011`。
- **如果 `.env` 里那行不存在**：说明现在完全靠 `config.yaml`。仍然不用改，但要在
  §6.3 放行时特别确认 Hermes 日志里打印的 URL 是 3011。
- **如果两处不一致**：**停下**。先把它们改成一致（备份见下）再继续迁移，
  否则你会在迁移后调试一个和迁移无关的老 bug。

```bash
# 需要改时的备份（项目惯例命名）
ssh corlinman-prod 'sudo cp -a /opt/hermes/data/.env \
  /opt/hermes/data/.env.bak-pre-T4-$(date -u +%Y%m%dT%H%M%SZ)'
ssh corlinman-prod 'sudo cp -a /opt/hermes/data/config.yaml \
  /opt/hermes/data/config.yaml.bak-pre-T4-$(date -u +%Y%m%dT%H%M%SZ)'
```

### 1.4 为什么用 `ssh -N -R` 而不是 `autossh`

任务书草案里写的是 `autossh -R`。**不建议**，理由：

| 关注点 | `autossh` | `ssh` + systemd |
|---|---|---|
| 谁负责重启 | autossh 自己 | systemd（本机已经有的那个） |
| 生命周期管理器数量 | **2 个**（systemd 管 autossh，autossh 管 ssh） | 1 个 |
| 探活方式 | 默认开两个 monitor 端口来回打流量（`-M`） | `ServerAliveInterval` + `ExitOnForwardFailure`，无额外监听 |
| 首次连接失败 | 默认 `AUTOSSH_GATETIME=30`，30 秒内退出会让 autossh 直接放弃 | systemd `Restart=always` 按 `RestartSec` 一直重试 |
| 失败可见性 | 包了一层，`systemctl status` 看到的是 autossh 的状态 | `systemctl status` 直接就是 ssh 的退出码 |

H1 §8.4 的教训正是"**第二个生命周期管理器**"惹的祸（`napcat-watch.py` 会自己
`systemctl restart napcat.service`，最后被有意 stop/disable）。这里没必要重蹈覆辙：
`ssh` 加上 `ServerAliveInterval=20`、`ServerAliveCountMax=3`、`ExitOnForwardFailure=yes`
之后，链路一断 ssh 就会**非零退出**，systemd 接管重启即可 —— autossh 能做的事它全做了。

如果你出于其他原因非用 autossh 不可：必须设 `-M 0`（关掉 monitor 端口）和
`AUTOSSH_GATETIME=0`（否则家宽刚上电、网络还没通时它会直接放弃）。

### 1.5 现有 `onebot-tunnel.service` 必须停 —— 这是本次最容易漏的一步

2026-08-31 实测：`corlinman-prod:onebot-tunnel.service` **active/enabled，SSH 进程正持有
`127.0.0.1:3011`**。它是**反方向**的（prod 主动 `-L` 连到 `cornna-cn:3011`）。

如果不停它：

- 新的 `-R 127.0.0.1:3011` 会因为端口被占而**转发失败**；
- 加了 `ExitOnForwardFailure=yes` 之后，ssh 会立刻退出，systemd 每 30 秒重试一次，
  隧道**永远起不来**，日志里反复出现
  `Warning: remote port forwarding failed for listen port 3011`；
- 而 Hermes 那边**看起来一切正常** —— 它连的 3011 还活着，只不过通向那台已经被你停掉 QQ 的
  `cornna-cn`。你会得到一个"链路在线、账号永远 offline"的假象，正好命中
  `plugins/platforms/onebot/README.md` 里那条"Transport online is not QQ-account online"。

**这个失败模式是安全的（fail-loud，不会双登录），但会浪费你半小时。** 处置见 §3.4。

### 1.6 运行时选型：原生 AppImage（推荐） vs Docker（回退）

这是一个开放决策。**本 runbook 选原生 AppImage**，理由：

1. **与已验证的加固一致。** `cornna-cn` 上那套 unit（`User=napcat-native`、
   `BindPaths` 挂设备身份、`StartLimitIntervalSec=3600`/`Burst=1`、preflight + flock 互斥）
   是**在这个账号上真实跑过**的。照抄比重新发明安全。
2. **少一层伪装。** H1 §4.1 记的"容器痕迹处理"：官方镜像 entrypoint 会尝试删除容器标识、
   伪造部分 cgroup/cpuset 视图。这次迁移的**全部目的**就是让这个账号看起来像个普通住宅用户，
   在住宅 IP 上再套一层"需要伪造宿主机视图"的容器，方向是拧的。原生跑就是一个普通 Linux QQ 装机。
3. **没有端口映射层。** 直接监听 loopback，`ss -ltn` 说什么就是什么，
   00-PLAN D81 那个"容器 loopback vs 容器 eth0"的坑整类消失。
4. **少一个守护进程。** 不需要 dockerd 也 7×24 健康。

代价与前提：

- 需要 **x86_64 Linux**。Linux QQ 的 arm64 构建存在，但 NapCat 的预打包 AppImage 是否有 arm64
  资产**必须自己去确认**，不要假设（下面给了命令）。
- 需要宿主上有 `dbus`（`dbus-run-session`）和 QQ AppImage 依赖的图形库（无需真正的 X server）。

**什么时候改用 Docker**：家里唯一的 7×24 机器是 ARM（NAS / SBC）且 NapCat 没有 arm64 AppImage，
或者你就是想要一个干净的可丢弃环境。这条路本 runbook 也给（§4.5），
`mlikiowa/napcat-docker:v4.18.19` 的双架构已确认。

#### 确认机器架构 —— 先做这一步，它决定你走哪条路

```bash
home$ uname -m
home$ getconf LONG_BIT
home$ cat /etc/os-release | grep -E '^(ID|VERSION_ID)='
home$ systemctl --version | head -1
```

- `x86_64` + systemd ⇒ **走原生路径（§4.4）**。
- `aarch64`/`arm64` ⇒ 先确认有没有 arm64 的 NapCat AppImage 资产；没有就**走 Docker 路径（§4.5）**。
- 没有 `systemctl` ⇒ 你不在 Linux 上（或不在 systemd 发行版上）。**这里没有分支** ——
  回 §0.4，换一台机器，或者在现有机器上跑一个开机自启的 Linux 虚拟机再从头执行本 runbook。

#### 走 Docker 路径时：确认拉到的是对的那个架构

```bash
home$ docker version --format 'client={{.Client.Arch}} server={{.Server.Arch}}'
# 预期：与 uname -m 对应（x86_64→amd64，aarch64→arm64）

home$ docker manifest inspect mlikiowa/napcat-docker:v4.18.19 \
  | jq -r '.manifests[] | "\(.platform.os)/\(.platform.architecture)\t\(.digest)"'
# 预期：至少出现 linux/amd64 和 linux/arm64 两行
```

- **如果 `docker manifest inspect` 报 "no such manifest"**：这个 tag 是单架构镜像，
  或者你的 Docker 太老。用 `docker buildx imagetools inspect mlikiowa/napcat-docker:v4.18.19` 再试。
- **如果只列出 amd64 而你在 arm64 上**：**停**。不要用 `--platform linux/amd64` 硬拉，
  QEMU 模拟跑 Electron + QQ 又慢又不稳，而且给了风控一个奇怪的指纹。回去换 x86_64 机器。

拉取并钉住：

```bash
home$ docker pull mlikiowa/napcat-docker:v4.18.19
home$ docker image inspect mlikiowa/napcat-docker:v4.18.19 \
  --format 'ARCH={{.Os}}/{{.Architecture}}
IMAGE_ID={{.Id}}
REPO_DIGEST={{index .RepoDigests 0}}'
```

**Docker 路径强制步骤：把本机实际 RepoDigest 落成 root-owned 单一来源。**
不要手抄进 unit，也不允许留下 `REPLACE_WITH...` 占位符：

```bash
home$ sudo bash -s <<'GATE'
set -uo pipefail
TAG=mlikiowa/napcat-docker:v4.18.19
OUT=/etc/napcat-native/docker-image.env
fail() { echo "DOCKER_DIGEST_GATE=BLOCK reason=$*" >&2; exit 1; }

if ! ref=$(docker image inspect "$TAG" --format '{{index .RepoDigests 0}}' 2>&1); then
  fail "image_inspect_failed: ${ref%%$'\n'*}"
fi
[[ "$ref" =~ ^(docker\.io/)?mlikiowa/napcat-docker@sha256:[0-9a-f]{64}$ ]] \
  || fail "unexpected_repo_digest_shape"
install -d -m 700 -o root -g root /etc/napcat-native || fail "cannot_prepare_etc_dir"
install -m 600 -o root -g root /dev/null "$OUT" || fail "cannot_create_digest_file"
printf 'NAPCAT_IMAGE=%s\n' "$ref" > "$OUT" || fail "cannot_write_digest_file"
grep -Fx "NAPCAT_IMAGE=$ref" "$OUT" >/dev/null 2>&1 || fail "digest_readback_mismatch"
echo "DOCKER_IMAGE_REF=$ref"
echo "DOCKER_DIGEST_GATE=PASS"
GATE
home$ echo "rc=$?"
```

- **唯一放行**：完整 digest（64 位十六进制）+ `DOCKER_DIGEST_GATE=PASS` + `rc=0`。
- 文件是 `root:root 600`；它不含凭据，但它决定启动哪一份二进制，不能让服务用户改写。

- **`ARCH` 必须等于本机架构。** 不等 ⇒ 停，你拉错了。
- **`REPO_DIGEST` 记下来**，unit 里用 `mlikiowa/napcat-docker@sha256:<这个值>` 引用，不要只写 tag。
- **`IMAGE_ID` 与 H1 §5 记的 `sha256:b132563d…` 不一致是正常的** ——
  本地 image ID 是 per-architecture 的 config digest，arm64 上必然不同。**不要因此以为镜像被换了。**
- **`REPO_DIGEST` 如果和 H1 记的 `sha256:1336a777…` 不一致**：说明这个 tag 被上游重新推送过。
  **停下**，把两个 digest 都记下来交给人判断，不要"反正是同一个 tag"就往下走。

---

## 2. 设备身份：不要复制

### 2.1 硬约束

> ### ⛔ 不要把 `cornna-cn:/opt/napcat/ntqq` 复制到新机
>
> 不管用 `rsync`、`tar`、`scp` 还是快照，都不行。

两条理由，都要记住：

1. **明令禁止。** H1 §4.1 把设备身份目录写成"**绝不能删、复制到并行实例或重建**——
   删掉会被视为新设备并增加验证风险"。`cornna-cn` 的 unit 注释也写着
   *"The single QQ device volume is bind-mounted in; it is never copied or duplicated."*
2. **实测无效。** 这个项目已经试过了：把 `login.enc` + `login.db` 从香港机原样搬到广州机，
   QQ 仍然 `loggedIn:false`。腾讯把凭据绑在设备/地域上，搬过去也得重新扫码 ——
   **你付出了"两台机器有同一份设备身份"的风险，什么也没换到。**

而且这次迁移的**目的**就是换环境。带着一个在阿里云机房上养出来的设备指纹去住宅 IP 上登录，
本身就是"同一设备突然换城市换运营商"这个最经典的风控信号。

### 2.2 可以复制的 / 绝不能复制的

跨机搬运这件事不是非黑即白，界线在这里：

| 路径 | 可否复制到新机 | 说明 |
|---|---|---|
| `/opt/napcat/ntqq/**` | ⛔ **绝对不可** | 设备身份。见 §2.1 |
| `/opt/napcat-native/config-candidate/onebot11_2952532060.json` | ✅ 可以 | 只是 OneBot 监听配置 + token。**复制它是为了让 Hermes 的 token 不用改**（§4.8） |
| `webui.json` | ❌ 不建议 | 含 WebUI 凭据。让新机自己生成一份，别扩散旧凭据 |
| `/opt/napcat-native/bin/*`（preflight / launcher 等脚本） | ✅ 推荐 | 已验证过的互斥逻辑，抄比重写安全（§4.3） |
| `napcat-native.service` / `napcat-native-health.service` | ✅ 推荐 | 加固参数照抄（§4.4、§4.6） |
| `/etc/napcat-native/env` | ⚠️ 只抄结构 | 逐字段核对后手写，不要整份搬（§4.2） |

判断口诀：**能重新生成的就重新生成，不能重新生成的（设备身份）本来就不该跨机。**

### 2.3 「永不再迁」

> ### 📌 一旦新机扫码成功，这台机器就是这个 QQ 号的**唯一**设备。
>
> 之后不管是换硬盘、重装系统、换机箱、还是"顺手把它 dockerize 一下"，
> 只要动到 `home:/opt/napcat/ntqq`，就等于**再烧一次扫码 + 再交一次新设备风险**。
>
> 具体禁止事项：
> - ⛔ 不要把 `home:/opt/napcat/ntqq` 复制去任何地方"做备份然后在别处启动"。
> - ⛔ 不要在同一台机器上开第二个 NapCat 实例指向同一个 `ntqq/`（flock 会挡，但别去试）。
> - ⛔ 不要 `rm -rf` 它来"清理一下重新登录"。H1 §10 已经怀疑过：
>   之前那次删库重建（新设备指纹）**可能就是 ~2 小时被踢的诱因**。
> - ✅ 允许：**原地**的文件级只读备份（`tar` 到同机另一块盘），仅用于灾难恢复，
>   **恢复也只能恢复到同一台机器**。

如果这台机器物理报废了：那就是重新扫码 + 新设备指纹，没有捷径。接受它，别想着"把身份救出来"。

### 2.4 新机首次启动前的用户、组、目录准备

> **顺序是硬性的：先建用户和组，再建目录。**
> `install -d -o napcat-native -g napcat-native` 在用户/组尚不存在时会直接失败
> （`install: invalid user`）。本节按可执行的顺序重排过。

#### 2.4.1 先建服务账号与私有组（两个：`napcat-native` 和 `obtun`）

`napcat-native` 跑 NapCat（本节）；`obtun` 跑反向隧道（§5.1.2 要用，一次建完，
免得到那一步才发现组不存在）。两个都用 `--user-group` **显式创建同名私有组** ——
不写这个参数时，部分发行版的 `useradd` 会把新用户塞进 `users` 这种共享组，
而 `/opt/napcat/ntqq` 是 700，组一错身份目录的权限模型就散了。

```bash
home$ sudo useradd --system --user-group --create-home \
      --home-dir /opt/napcat-native/home --shell /usr/sbin/nologin napcat-native
home$ sudo useradd --system --user-group --no-create-home \
      --shell /usr/sbin/nologin obtun
home$ id napcat-native
home$ id obtun
home$ getent group napcat-native
home$ getent group obtun
```

- **预期（逐条）**：
  - `id napcat-native` → `uid=NNN(napcat-native) gid=NNN(napcat-native) groups=NNN(napcat-native)`
    —— **`groups=` 里有且只有 `napcat-native` 自己**。
  - `id obtun` → 同形，`groups=` 里有且只有 `obtun`。
  - `getent group napcat-native` / `getent group obtun` → 各打印一行 `名字:x:NNN:`，
    **最后那个成员列表必须是空的**。
- ⛔ **`groups=` 里出现 `docker` / `sudo` / `wheel` / `adm` 中的任何一个 ⇒ 停下并移除。**
  `docker` 组尤其危险：它等价于 root（能挂载任意宿主目录进容器），
  而 §4.3 的 preflight **有意不给 `napcat-native` docker 权限**（见 §4.3.1 的理由）。
  移除：`sudo gpasswd -d napcat-native docker`，然后重跑 `id` 确认。
- **`useradd` 报 `user ... already exists`**：不要跳过检查。这台机器上以前有人建过同名账号，
  用 `id` 确认它的组集合干净、home 指向对、shell 是 nologin；有一项不对就先修，再往下。
- **`useradd: option '--user-group' is unknown`**（极老的 busybox/发行版）：先显式建组再建用户：
  `sudo groupadd --system napcat-native && sudo useradd --system -g napcat-native ...`

#### 2.4.2 建目录（用户和组已存在，这一步才不会失败）

```bash
home$ sudo install -d -m 700 -o root -g root /opt/napcat
home$ sudo install -d -m 700 -o napcat-native -g napcat-native /opt/napcat/backups
home$ sudo ls -ld /opt/napcat /opt/napcat/backups
```

- **预期**：`drwx------ root root /opt/napcat`、`drwx------ napcat-native napcat-native /opt/napcat/backups`。
- **注意 `/opt/napcat/ntqq` 不在这里创建** —— 它是设备身份目录，由 §2.4.3 的闸门决定能不能建。

#### 2.4.3 ⛔ 设备身份目录闸门（硬阻断，不得 `mv` / `rm` / 重建）

> ### 这是 H1 §4.1 的硬约束，没有例外分支
>
> 设备身份目录"**绝不能删、复制到并行实例或重建**"。
> 所以这一步只有**两种**合法结局：
>
> - **A. 目录不存在** ⇒ 这台机器此前从未跑过 NapCat ⇒ 创建一个**空目录**当挂载点，继续。
> - **B. 目录存在**（无论空不空、无论内容是哪来的）⇒ **硬阻断，本 runbook 到此为止。**
>
> **没有 C。** 上一版这里写了「`mv` 到 `.bak-pre-T4-…` 再重建空目录」和
> 「如果是复制品就删掉那个复制品」——**两条都是直接违反硬约束的操作，已删除。**
>
> 把真实设备身份移出工作路径 ⇒ NapCat 生成新身份 ⇒ 这就是一次**未经批准的新设备认证**，
> 正是 H1 §10 怀疑的「~2 小时被踢」的可能诱因。
> 删除"复制品"⇒ 销毁取证材料和回滚材料，而且**你无法在删之前确定它只是复制品**。

下面这段是**闸门**，不是巡检：它自己判断、自己阻断，不依赖你读输出。

```bash
home$ sudo bash -s <<'GATE'
set -uo pipefail
D=/opt/napcat/ntqq
fail() { echo "IDENTITY_GATE=BLOCK reason=$*" >&2; exit 1; }

# 1) 路径存在性：任何形式的存在都阻断（目录 / 文件 / 符号链接 / 挂载点）
if [ -e "$D" ] || [ -L "$D" ]; then
  echo "--- 只读取证（不改动任何东西）---" >&2
  ls -ld "$D" >&2 || true
  stat -c 'mode=%a owner=%U:%G mtime=%y' "$D" >&2 || true
  echo "entries=$(find "$D" -mindepth 1 2>/dev/null | wc -l)" >&2
  find "$D" -mindepth 1 -maxdepth 2 2>/dev/null | head -20 >&2
  mountpoint -q "$D" && echo "IS_MOUNTPOINT=true" >&2 || echo "IS_MOUNTPOINT=false" >&2
  fail "device identity path already exists at $D"
fi

# 2) 父目录必须已就绪（§2.4.2），否则说明步骤跳了
[ -d /opt/napcat ] || fail "/opt/napcat missing — run 2.4.2 first"

# 3) 用户/组必须存在（§2.4.1），否则 install -d 会失败在一个不明显的地方
id -u napcat-native >/dev/null 2>&1 || fail "user napcat-native missing — run 2.4.1 first"
getent group napcat-native >/dev/null 2>&1 || fail "group napcat-native missing — run 2.4.1 first"

# 4) 只有走到这里才允许创建空挂载点
install -d -m 700 -o napcat-native -g napcat-native "$D" || fail "install -d failed"
[ "$(find "$D" -mindepth 1 | wc -l)" -eq 0 ] || fail "newly created $D is not empty"
echo "IDENTITY_GATE=PASS empty_mountpoint_created=$D"
GATE
home$ echo "rc=$?"
```

- **预期（唯一的放行输出）**：`IDENTITY_GATE=PASS empty_mountpoint_created=/opt/napcat/ntqq`，`rc=0`。
- **`IDENTITY_GATE=BLOCK reason=device identity path already exists`，`rc=1`** ⇒
  **迁移在这里停止。** 处置只有一条：**隔离并报告**。
  - **隔离 = 把这台机器排出本次迁移**：不启动 NapCat、不扫码、不接隧道。
    **不是**指把目录挪走 —— 目录**原地不动**。
  - **报告 = 把闸门打印的只读取证（`ls -ld` / `stat` / `entries=` / 前 20 条路径 /
    `IS_MOUNTPOINT`）抄进执行记录**，交给人判断它是什么：
    | 可能来源 | 判断线索 | 处置 |
    |---|---|---|
    | 这台机器**以前真的跑过** NapCat 并登录过 | 目录内有 QQ 的完整目录树、mtime 跨多天 | 这是一份**真实设备身份**。绝不能删。要不要用它是一个**独立决策**（用它 = 复用旧指纹，不是本 runbook 的方案），需要人批准 |
    | 从 `cornna-cn` 或别处**复制**来的 | mtime 集中在一次拷贝、文件数接近 H1 记的 1,691/1,969 | **它本来就不该存在，但仍然不许你删。** 它现在是取证材料：说明有人做过一次跨机复制，那件事本身要先查清楚（§2.1 实测无效，且违反硬约束） |
    | 上一次失败尝试留下的残骸 | 目录存在但几乎是空的、只有一两个锁文件 | 仍然阻断。由人确认后**在执行记录里显式记录**再决定，不由 runbook 自动处置 |
  - ⛔ **在任何一种情况下，本 runbook 都不提供 `mv` / `rm -rf` / `install -d` 重建的命令。**
    需要动它时，那是一次**有人批准、单独记录**的操作，不是迁移的一个步骤。
- **`reason=/opt/napcat missing` 或 `user ... missing`** ⇒ 你跳了 §2.4.1 / §2.4.2，回去补。
- **`IS_MOUNTPOINT=true`** ⇒ 有东西已经把它 bind 挂上了（多半是一个还活着的 NapCat 实例）。
  **这是最危险的一种**：先按 §3.3.3 查这台机器上有没有 QQ 进程，再谈别的。

---

## 3. 单实例互斥（做错这一步会直接触发风控）

### 3.1 互斥模型：跨机用 mask，同机用 preflight + flock

同一个 QQ 号**绝不允许在两个 NapCat 实例并行登录**。00-PLAN D79 有实战记录：
`[KickedOffLine] [下线通知] 您的账号已在另一台终端登录` —— 后登踢先登，
而且那次连带把 NapCat 打到"恢复需扫码"。H1 §6.1 把它写成硬禁令。

问题是：**本地的锁看不见远程的实例。** 家里那台机器的 flock 挡不住 `cornna-cn`。
所以互斥要分两层：

| 层 | 机制 | 挡住什么 | 怎么验证 |
|---|---|---|---|
| **跨机** | `systemctl mask` | 退役那台被结构性地**无法启动**（包括手滑 `start`、包括重启后） | `systemctl is-enabled` 返回 `masked` |
| **同机** | `bin/preflight` + `bin/launcher` 里的 `flock` | 同一台机器上第二个实例、和另一种运行形态（Docker vs 原生）互相踩 | 手工起第二份，预期被拒 |

**不变式（可审计，一句话）**：
> 在任意时刻，退役侧 `cornna-cn:napcat-native.service` 必须 masked；home 只能选择
> `napcat-native.service`（原生）或 `napcat.service`（Docker）中的一个作为当前运行时，
> **选中项为 disabled-but-manually-started，未选项必须 absent 或 masked，绝不能两项都可启动。**

这个不变式可以用一条命令查（放进 §8 的日常巡检里）：

```bash
$ printf 'cornna-cn-native=%s\nhome-native=%s\nhome-docker=%s\n' \
    "$(ssh cornna-cn systemctl is-enabled napcat-native.service 2>&1)" \
    "$(ssh home systemctl is-enabled napcat-native.service 2>&1)" \
    "$(ssh home systemctl is-enabled napcat.service 2>&1)"
```

- **迁移后预期（原生）**：`cornna-cn-native=masked`、`home-native=disabled`、
  `home-docker=masked` 或 unit 不存在。
- **迁移后预期（Docker）**：`cornna-cn-native=masked`、`home-docker=disabled`、
  `home-native=masked` 或 unit 不存在。
- 选中项是 `disabled` 而不是 `enabled` —— **这是有意的**，开机自启取舍见 §4.7.1；
  未选项如果已经安装则显式 `sudo systemctl mask <未选unit>`，避免把另一运行时手滑拉起。
- **cornna 未 masked，或 home 两项都不是 absent/masked**：**立刻阻断启动**，先恢复上面对应分支；
  然后查 §8 日志确认有没有真的发生过双登录。
- **选中的 home unit 是 masked** 且你正在正常运行：说明回滚做了一半；只 unmask 选中项，
  确认它回到 `disabled`，不要同时 unmask 另一条路径。

### 3.2 为什么不用 `Conflicts=`

`cornna-cn` 的 unit 注释原文：
*"Deliberately NO `Conflicts=napcat.service`: this host has previously suffered a
`Conflicts=` + `Restart=` mutual-kill loop. Mutual exclusion is enforced instead by
`bin/preflight` (checks the Docker unit and the ports) and by the `flock` in `bin/launcher`."*

`Conflicts=` + `Restart=` 会互相踢：A 起来踢掉 B，B 的 `Restart=` 把 B 拉回来又踢掉 A，
A 的 `Restart=` 再把 A 拉回来 —— 每一轮都是一次 QQ 登录尝试。**这是踩过的坑，不要复现。**

**所以：新机的 unit 里同样不写 `Conflicts=`。** 互斥靠 mask（跨机）+ preflight/flock（同机）。

### 3.3 停旧机：完整顺序与检查点

⚠️ **从这一步开始，QQ 会离线，直到你在新机上扫码成功。** 确认你在新机旁边再开始。

**3.3.0 先备份旧机的 unit 和 config 元数据**（不复制设备身份，只留可回滚的证据）：

```bash
cornna-cn$ TS=$(date -u +%Y%m%dT%H%M%SZ); echo "TS=$TS"      # 记下这个时间戳
cornna-cn$ sudo install -d -m 700 /opt/napcat/backups/t4-decommission-$TS
cornna-cn$ sudo systemctl cat napcat-native.service \
    | sudo tee /opt/napcat/backups/t4-decommission-$TS/napcat-native.service.before >/dev/null
cornna-cn$ sudo systemctl show napcat-native.service \
    > /dev/null 2>&1; sudo systemctl show napcat-native.service \
    | sudo tee /opt/napcat/backups/t4-decommission-$TS/systemctl-show.before >/dev/null
cornna-cn$ sudo ls -laR /opt/napcat-native/config-candidate \
    | sudo tee /opt/napcat/backups/t4-decommission-$TS/config-listing.before >/dev/null
cornna-cn$ sudo find /opt/napcat/ntqq -type f -printf '%m %u:%g %s %p\n' \
    | sudo tee /opt/napcat/backups/t4-decommission-$TS/ntqq-inventory.before >/dev/null
cornna-cn$ sudo wc -l /opt/napcat/backups/t4-decommission-$TS/ntqq-inventory.before
```

- **预期**：最后一行给出设备目录的文件数（H1 记过 1,691 / 1,969 两个不同时间点的值，
  数字对不上是正常的，**它只是本次的基线，不是判据**）。
- **`find` 报 permission denied**：加 `sudo` 已经在了；如果仍失败，说明目录属主异常，
  记录下来但**不要 chown**，继续往下（这不是阻断项）。

**3.3.1 停止服务**

```bash
cornna-cn$ sudo systemctl stop napcat-native.service
cornna-cn$ systemctl is-active napcat-native.service
```

- **预期**：`inactive`。
- **如果是 `deactivating` 或卡住 >90 秒**：见 3.3.3 的进程残留分支。

**3.3.2 mask 掉它（这是跨机互斥的那把锁）**

```bash
cornna-cn$ sudo systemctl mask napcat-native.service
cornna-cn$ systemctl is-enabled napcat-native.service
cornna-cn$ ls -l /etc/systemd/system/napcat-native.service
```

- **预期**：`is-enabled` 输出 `masked`；`ls -l` 显示它是指向 `/dev/null` 的符号链接。
- **如果 `mask` 报 "Unit file ... is masked" 之外的错误**：说明 unit 文件在
  `/etc/systemd/system/` 而不是 `/usr/lib/systemd/system/`，`mask` 会**拒绝覆盖**它。
  这种情况下先把 unit 文件移走再 mask：
  ```bash
  cornna-cn$ sudo mv /etc/systemd/system/napcat-native.service \
      /opt/napcat/backups/t4-decommission-$TS/napcat-native.service.moved
  cornna-cn$ sudo systemctl daemon-reload && sudo systemctl mask napcat-native.service
  ```
- **验证 mask 真的有效**（这一步别跳，mask 是整个互斥的地基）：
  ```bash
  cornna-cn$ sudo systemctl start napcat-native.service; echo "rc=$?"
  ```
  **预期**：失败，`rc` 非 0，报 `Unit napcat-native.service is masked.`
  **如果它居然起来了**：**立刻 `stop`**，mask 没生效，回上一条排查。**在 mask 生效之前
  绝不允许新机登录。**

**3.3.3 确认进程树真的死透**（AppImage 的关键分支）

原生形态的进程树是 `dbus-run-session → AppRun → qq → qq`。如果 unit 的 `KillMode` 不是
`control-group`，systemd 只会杀掉主进程，**QQ 会作为孤儿继续持有登录态和端口**。

```bash
cornna-cn$ systemctl show -p KillMode,MainPID,Result,ExecMainStatus napcat-native.service
cornna-cn$ pgrep -a -u napcat-native || echo NO_NAPCAT_NATIVE_PROCS
cornna-cn$ pgrep -af 'dbus-run-session|AppRun|[/]qq' || echo NO_QQ_PROCS
cornna-cn$ ss -ltnp 2>/dev/null | grep -E '127\.0\.0\.1:(3011|6099)\b' || echo NO_LISTENERS
cornna-cn$ systemd-cgls /system.slice/napcat-native.service 2>/dev/null | tail -5
```

- **全部预期**：`KillMode=control-group`、`MainPID=0`、
  `NO_NAPCAT_NATIVE_PROCS`、`NO_QQ_PROCS`、`NO_LISTENERS`，cgroup 为空。
- **如果 `pgrep` 有输出（进程残留）**：按这个顺序处理，**不要直接 `kill -9` 整台机器上叫 qq 的东西**
  ```bash
  cornna-cn$ sudo systemctl kill -s SIGTERM napcat-native.service
  # 等 10 秒后重查 pgrep；仍有残留：
  cornna-cn$ sudo systemctl kill -s SIGKILL napcat-native.service
  # 仍有残留（说明进程已脱离 cgroup）：
  cornna-cn$ sudo pkill -u napcat-native
  # 最后手段：
  cornna-cn$ sudo pkill -9 -u napcat-native
  cornna-cn$ pgrep -a -u napcat-native || echo NO_NAPCAT_NATIVE_PROCS
  ```
  按 uid 杀是安全的：这个 uid 只跑 NapCat。
- **如果 `ss` 仍显示 3011/6099 有监听但 `pgrep` 没输出**：端口被别的进程占了。
  `sudo ss -ltnp | grep -E ':(3011|6099)'` 看是谁，**搞清楚再往下**，
  很可能是另一个你不知道的实例。

**3.3.4 确认没有别的东西会把它拉起来**

> **先分清两类 timer/unit，别把合法的判成异常：**
>
> | 类别 | 实例 | 允许状态 | 判据 |
> |---|---|---|---|
> | **恢复型（禁止）** | `napcat-watch.service`、`qq-watchdog.service`、`qq-watchdog.timer` | **必须 `inactive` 且 `masked`** | 它们会 `systemctl restart` NapCat ⇒ 第二个会消耗登录尝试的生命周期管理器（H1 §8.4） |
> | **只读探针（允许）** | `napcat-native-health.timer` / `.service` | **允许 `enabled` + `active`** | Description 原文 `Read-only NapCat native account health probe (no recovery)`；只写 `/run/napcat-native-health/state.json`，**不做任何恢复动作** |
>
> 2026-08-31 实测（`decisions.md` D-QQ-04）：三件恢复型 unit **均为 `masked`**，
> `napcat-native-health.timer` **enabled**。**这就是符合预期的现状。**
> 上一版这里把期望写成 `NO_TIMERS`，会把合法的 health timer 判成异常 —— 已修。

**(a) 恢复型三件套：逐个核对 `inactive` + `masked`（这是闸门，不是巡检）**

```bash
cornna-cn$ sudo bash -s <<'GATE'
set -uo pipefail
bad=0
for u in napcat-watch.service qq-watchdog.service qq-watchdog.timer; do
  # 一次查询拿到三个结构化属性；查询失败不能伪装成 ABSENT/unknown。
  if ! props=$(systemctl show "$u" -p LoadState -p ActiveState -p UnitFileState 2>&1); then
    printf 'WATCHDOG %-26s UNCHECKABLE query_failed=%s\n' "$u" "${props%%$'\n'*}"
    bad=1
    continue
  fi

  load= act= ena=
  while IFS='=' read -r key value; do
    case "$key" in
      LoadState)     load=$value ;;
      ActiveState)   act=$value ;;
      UnitFileState) ena=$value ;;
    esac
  done <<< "$props"

  if [ -z "$load" ] || [ -z "$act" ]; then
    printf 'WATCHDOG %-26s UNCHECKABLE missing_properties\n' "$u"
    bad=1
    continue
  fi
  if [ "$load" = "not-found" ]; then
    printf 'WATCHDOG %-26s ABSENT ok\n' "$u"
    continue
  fi
  case "$load" in
    loaded|masked) ;;
    *) printf 'WATCHDOG %-26s UNCHECKABLE load=%s\n' "$u" "$load"; bad=1; continue ;;
  esac

  printf 'WATCHDOG %-26s load=%s active=%s enabled=%s\n' "$u" "$load" "$act" "$ena"
  # 只接受明确 inactive/failed；unknown 是「查不出来」，必须阻断。
  case "$act" in inactive|failed) ;; *) echo "  ^ NOT CONFIRMED INACTIVE"; bad=1 ;; esac
  case "$ena" in masked|masked-runtime) ;; *) echo "  ^ NOT MASKED"; bad=1 ;; esac
done
[ "$bad" -eq 0 ] && echo "WATCHDOG_GATE=PASS" || { echo "WATCHDOG_GATE=BLOCK"; exit 1; }
GATE
cornna-cn$ echo "rc=$?"
```

- **预期**：三行都是 `load=masked active=inactive enabled=masked`（不同 systemd 版本也可能是
  `load=loaded active=inactive enabled=masked`），或 `ABSENT ok`；最后 `WATCHDOG_GATE=PASS`，`rc=0`。
- **`UNCHECKABLE` / `active=unknown`** ⇒ **阻断**。它们表示查询失败或状态无法确认，不能解释成“已停”。
- **`NOT MASKED`（例如 `enabled=disabled`）** ⇒ **必须补 mask**。
  `disabled` 只是"开机不自启"，**仍然可以被手动 `start`、可以被别的 unit 的 `Wants=`/`Requires=` 拉起来**。
  处置顺序**不能反**（先断运行，再断可启动性）：
  ```bash
  cornna-cn$ sudo systemctl disable --now <unit>
  cornna-cn$ sudo systemctl mask <unit>
  cornna-cn$ systemctl is-active <unit>; systemctl is-enabled <unit>
  ```
  **必须眼睛看到 `inactive` + `masked` 才算过**，然后重跑上面整个 GATE。
- **`NOT INACTIVE`（watchdog 正在跑）** ⇒ **这是最高优先级的异常**：
  它随时可能 `systemctl restart napcat-native.service`，即使你刚 mask 掉 NapCat，
  它也会在日志里制造混乱。立刻按上面的 `disable --now` → `mask` 处理，再回 §3.3.1 重新确认 NapCat 已停。
- **`WATCHDOG_GATE=BLOCK` 但你认为是误判** ⇒ 不要绕过。把 `systemctl cat <unit>` 的输出
  抄进执行记录再判断。

**(b) 只读 health timer：允许 enabled，但必须验证它确实没有恢复动作**

```bash
cornna-cn$ systemctl is-enabled napcat-native-health.timer; systemctl is-active napcat-native-health.timer
cornna-cn$ systemctl cat napcat-native-health.service | grep -E '^(Description|ExecStart|ExecStartPre|ExecStartPost|ExecStop)='
cornna-cn$ systemctl cat napcat-native-health.service \
    | grep -c -E 'systemctl (restart|start|reset-failed)|docker (restart|start)|pkill|kill -' \
    && echo "RECOVERY_VERBS_COUNT_ABOVE"
```

- **预期**：`enabled` / `active`（合法）；`Description=Read-only NapCat native account health probe (no recovery)`；
  **恢复动词计数为 `0`**。
- **恢复动词计数 ≥ 1** ⇒ 它不是只读探针，**按 (a) 的恢复型处理**（`disable --now` + `mask`）。
  名字里带 health 不代表它无害，**以 ExecStart 的实际内容为准**。
- **停机期间要不要停掉它**：可以停（它会一直报"账号不在线"刷日志），但**这是可选项，不是要求**。
  停了就记一笔，回滚时 `sudo systemctl enable --now napcat-native-health.timer` 起回来（§7.3.3 已写）。

**(c) 其余拉起路径：timer / cron / 容器**

```bash
cornna-cn$ systemctl list-timers --all --no-pager | grep -i -E 'napcat|qq' || echo NO_MATCHING_TIMERS
cornna-cn$ systemctl list-units --all --type=service --no-pager | grep -i -E 'napcat|qq'
cornna-cn$ sudo crontab -l 2>/dev/null | grep -i -E 'napcat|qq' || echo NO_ROOT_CRON
cornna-cn$ ls /etc/cron.d/ 2>/dev/null | grep -i -E 'napcat|qq' || echo NO_CRON_D
cornna-cn$ sudo docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null \
    | grep -i napcat || echo NO_NAPCAT_CONTAINERS
```

- **预期**：
  - timer 列表里**只允许**出现 `napcat-native-health.timer`（已由 (b) 验证）。
    出现别的 napcat/qq timer ⇒ 按 (a) 的恢复型处理。
    `NO_MATCHING_TIMERS` 也是合法结局（说明 health timer 也被停了）。
  - service 列表里只剩 `napcat-native.service`（masked）和 `napcat-native-health.service`。
  - `NO_ROOT_CRON`、`NO_CRON_D`。
  - `NO_NAPCAT_CONTAINERS`（2026-08-31 实测确实没有容器）。
- **如果居然查出一个 napcat 容器**：**停下**。存在容器 = 存在第二条并行登录路径。
  先只读取证（`docker inspect` 看挂了哪个目录、`RestartPolicy` 是什么），**抄进执行记录**，
  再 `docker update --restart=no <name> && docker stop <name>`。
  ⛔ **不要 `docker rm`**（§7.2：云上的东西一个都不删）。
- **`docker ps -a` 报权限错误而不是"没有容器"** ⇒ **这不是 `NO_NAPCAT_CONTAINERS`。**
  用 `sudo` 重跑；仍失败就把它当作**未知**处理并阻断，不要当成"没有容器"。
  （这与 §4.3.1 preflight 的失败关闭原则是同一条：查不了 ≠ 安全。）

**3.3.5 旧机的 WebUI 应当变成不可达**

```bash
$ curl -s -o /dev/null -w '%{http_code}\n' https://qq.cornna.xyz/webui/
```

- **预期**：`503`（H1 §8.7 已把 nginx 的错误页从误导性的 405 改成明确的 503）。
- **如果是 `200`**：**停下**。要么 QQ 还活着，要么 `qq.cornna.xyz` 指向的根本不是你刚停的那台
  （H1 §6.1 就吃过"两个实例指纹不同"的亏）。回 3.3.3 查监听。
- **如果是 `405`**：nginx 的 405 修复被回滚了。不阻断迁移，但记一笔。

### 3.4 prod 侧：停旧隧道，把 3011 让出来

```bash
corlinman-prod$ sudo systemctl stop hermes.service
corlinman-prod$ systemctl is-active hermes.service
corlinman-prod$ sudo systemctl disable --now onebot-tunnel.service
corlinman-prod$ systemctl is-active onebot-tunnel.service; systemctl is-enabled onebot-tunnel.service
corlinman-prod$ sudo ss -ltnp | grep -E '127\.0\.0\.1:3011\b' || echo PORT_3011_FREE
corlinman-prod$ pgrep -af 'ssh .*3011' || echo NO_TUNNEL_SSH
```

- **预期**：`hermes` `inactive`；`onebot-tunnel` `inactive` + `disabled`；
  `PORT_3011_FREE`；`NO_TUNNEL_SSH`。
- **为什么先停 Hermes**：账号此刻已经离线，让 Hermes 继续跑只会刷
  `connect_failed` 并在账号 `auth_required` 时反复触碰后端。H1 §8.5 的处置就是
  "发现掉线后立即 stop/disable Hermes"。这里照做。
- **如果 3011 仍被占**：`sudo ss -ltnp | grep 3011` 看 PID。
  常见原因是 ssh 进程没随 unit 退出：`sudo pkill -f 'ssh.*3011'`，再查一次。
  **3011 必须完全空出来，否则 §5 的反向隧道会一直起不来。**
- **不要 `systemctl mask onebot-tunnel.service`**：回滚（§7）要用它。`disable` 就够了。

### 3.5 放行闸：允许新机登录的前置条件

> ### ⚠️ 上一版这里是一张表格，**表格里的命令不可执行**
>
> 那张表把管道符写成了 Markdown 行内代码里的字面反斜杠（`\|\|`、`\|`）。
> 粘贴到 shell 里执行时，`\|` 是**字面字符**而不是管道运算符，`\|\|` 也不是 `||` ——
> **预期的 `DEAD` 和 `0` 根本不会可靠产生**。而且第 3 行只跑了 `pgrep -u napcat-native`，
> 一个"没有 `napcat-native` 用户进程、但仍持有 3011/6099"的残留可以让闸门**看起来通过**，
> 随后新机启动就是一次并行登录。
>
> **本节已改写成一段可直接执行的 fail-closed 脚本。**
> 它自己判断、自己以非零退出阻断，**不依赖你去读输出**。
> 铁律：**任何"查不了"都算 BLOCK，绝不映射成安全状态**（SSH 失败、命令不存在、
> 权限不足、sudo 要密码 —— 全部阻断）。

**这段脚本全绿之前，不要在新机上启动 NapCat，更不要扫码。**

在你的**操作机（laptop）**上落一个文件再跑（不要直接粘一长串到终端，
中途换行断开会让某个检查被静默跳过）：

```bash
laptop$ cat > /tmp/t4-gate35.sh <<'GATE'
#!/usr/bin/env bash
# T4 §3.5 放行闸 —— 允许新机登录的前置条件。
# 失败关闭：任何检查无法完成都视为 BLOCK。非零退出 = 不许启动新机 NapCat。
set -uo pipefail

SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes"
BLOCK=0
block() { printf 'BLOCK  %s\n' "$*"; BLOCK=1; }
ok()    { printf 'OK     %s\n' "$*"; }

# ---------------------------------------------------------------- 旧机 cornna-cn
# 闸 1/2/3 合并成一次远端执行：unit 状态 + 完整进程树 + 3011 + 6099 + cgroup + 容器
OUT=$($SSH cornna-cn bash -s <<'REMOTE' 2>&1)
set -uo pipefail
r=0
say() { printf '%s\n' "$*"; }

# 依赖的命令必须存在，否则「检查不了」= 阻断
for c in systemctl pgrep ss; do
  command -v "$c" >/dev/null 2>&1 || { say "MISSING_CMD=$c"; r=1; }
done
[ "$r" -eq 0 ] || { say "REMOTE_RC=1"; exit 1; }

# (1)+(2) unit 已明确停止且 masked。一次结构化查询；查询失败不能靠输出文本猜状态。
if ! props=$(systemctl show napcat-native.service \
      -p LoadState -p ActiveState -p UnitFileState 2>&1); then
  say "UNIT_STATE=UNCHECKABLE(query failed: ${props%%$'\n'*})"
  r=1
else
  load= act= ena=
  while IFS='=' read -r key value; do
    case "$key" in
      LoadState) load=$value ;;
      ActiveState) act=$value ;;
      UnitFileState) ena=$value ;;
    esac
  done <<< "$props"
  say "UNIT_LOAD=$load"
  say "UNIT_ACTIVE=$act"
  say "UNIT_ENABLED=$ena"
  case "$load" in loaded|masked) ;; *) r=1 ;; esac
  case "$act" in inactive|failed) ;; *) r=1 ;; esac
  case "$ena" in masked|masked-runtime) ;; *) r=1 ;; esac
fi

# pgrep 三态：0=有匹配 1=无匹配 >=2=查不了（查不了必须阻断，不能当成「没有」）
probe_pgrep() {  # $1=label, 其余=pgrep 参数
  local label="$1"; shift
  pgrep "$@" >/dev/null 2>&1; local rc=$?
  case "$rc" in
    0) say "$label=present"; pgrep "$@" 2>/dev/null | head -10; return 1 ;;
    1) say "$label=absent";  return 0 ;;
    *) say "$label=UNCHECKABLE(pgrep rc=$rc)"; return 1 ;;
  esac
}

# ss 必须先明确成功；失败/无权限/过滤器不支持都不能被空输出解释成「没有监听」。
probe_port_absent() {  # $1=label $2=port
  local label="$1" port="$2" out
  if ! out=$(ss -ltnH "( sport = :$port )" 2>&1); then
    say "$label=UNCHECKABLE(ss failed: ${out%%$'\n'*})"
    return 1
  fi
  if [ -n "${out//[[:space:]]/}" ]; then
    say "$label=present"
    printf '%s\n' "$out" | head -10
    return 1
  fi
  say "$label=absent"
  return 0
}

# (3a) 完整进程树：napcat-native 用户下无进程
probe_pgrep PROCS_NAPCAT_NATIVE -a -u napcat-native || r=1

# (3b) 完整进程树：全机范围内无 dbus-run-session / AppRun / qq（覆盖「换个用户跑」的残留）
probe_pgrep PROCS_QQ_TREE -af 'dbus-run-session|AppRun|[/]qq' || r=1

# (3c) 3011 无监听（任何 bind 地址都算占用）
probe_port_absent LISTEN_3011 3011 || r=1

# (3d) 6099 无监听（任何 bind 地址都算占用）
probe_port_absent LISTEN_6099 6099 || r=1

# (3e) cgroup 为空（systemd 只杀主进程时，孤儿会留在 cgroup 里继续持有登录态）
CG=/sys/fs/cgroup/system.slice/napcat-native.service/cgroup.procs
if [ -e "$CG" ]; then
  ncg=$(wc -l < "$CG" 2>/dev/null || echo -1)
  say "CGROUP_PROCS=$ncg"
  [ "$ncg" -eq 0 ] || r=1
else
  say "CGROUP_PROCS=absent"     # unit 停掉后 cgroup 目录消失，这是正常的
fi

# (3f) 无 napcat 容器在跑。runtime 存在但查不了 ⇒ 阻断，不当成「没有容器」
for rt in docker podman; do
  if ! command -v "$rt" >/dev/null 2>&1; then
    say "${rt^^}_NAPCAT_RUNNING=no-runtime"
    continue
  fi
  if ! dps=$(sudo -n "$rt" ps --format '{{.Names}}' 2>&1); then
    say "${rt^^}_NAPCAT_RUNNING=UNCHECKABLE"   # 没有免密 sudo / socket 无权限
    r=1
    continue
  fi
  matches=$(printf '%s\n' "$dps" | grep -i napcat); grep_rc=$?
  case "$grep_rc" in
    0) say "${rt^^}_NAPCAT_RUNNING=present"; printf '%s\n' "$matches"; r=1 ;;
    1) say "${rt^^}_NAPCAT_RUNNING=absent" ;;
    *) say "${rt^^}_NAPCAT_RUNNING=UNCHECKABLE(grep rc=$grep_rc)"; r=1 ;;
  esac
done

say "REMOTE_RC=$r"
exit "$r"
REMOTE
RC=$?
printf '%s\n' "$OUT"
if [ "$RC" -eq 255 ]; then
  block "cornna-cn: SSH 本身失败（rc=255）—— 查不了就是 BLOCK，不是通过"
elif [ "$RC" -ne 0 ]; then
  block "cornna-cn: 上面至少一项不满足（rc=$RC）"
else
  ok "cornna-cn: unit inactive+masked，进程树/3011/6099/cgroup/容器 全部干净"
fi

# ---------------------------------------------------------------- prod 3011 已空
OUT=$($SSH corlinman-prod bash -s <<'REMOTE' 2>&1)
set -uo pipefail
command -v ss >/dev/null 2>&1 || { echo "MISSING_CMD=ss"; exit 1; }
if ! sockets=$(ss -ltnH '( sport = :3011 )' 2>&1); then
  echo "PROD_LISTEN_3011=UNCHECKABLE(ss failed: ${sockets%%$'\n'*})"
  exit 1
fi
if [ -n "${sockets//[[:space:]]/}" ]; then
  echo "PROD_LISTEN_3011=present"
  printf '%s\n' "$sockets" | head -10
  exit 1
fi
echo "PROD_LISTEN_3011=absent"
pgrep -af 'ssh .*3011' >/dev/null 2>&1; rc=$?
case "$rc" in
  0) echo "PROD_TUNNEL_SSH=present"; pgrep -af 'ssh .*3011' | head -5; exit 1 ;;
  1) echo "PROD_TUNNEL_SSH=absent" ;;
  *) echo "PROD_TUNNEL_SSH=UNCHECKABLE(pgrep rc=$rc)"; exit 1 ;;
esac
REMOTE
RC=$?
printf '%s\n' "$OUT"
if [ "$RC" -eq 255 ]; then
  block "corlinman-prod: SSH 本身失败（rc=255）"
elif [ "$RC" -ne 0 ]; then
  block "corlinman-prod: 3011 未空出或旧隧道 ssh 仍在（rc=$RC）"
else
  ok "corlinman-prod: 3011 无监听、无旧隧道 ssh 进程"
fi

# ---------------------------------------------------------------- 新机身份目录为空
OUT=$($SSH home bash -s <<'REMOTE' 2>&1)
set -uo pipefail
D=/opt/napcat/ntqq
[ -d "$D" ] || { echo "HOME_NTQQ=missing"; exit 1; }   # §2.4.3 应已建好空挂载点
n=$(sudo -n find "$D" -mindepth 1 2>/dev/null | wc -l) || { echo "HOME_NTQQ=UNCHECKABLE"; exit 1; }
echo "HOME_NTQQ_ENTRIES=$n"
[ "$n" -eq 0 ] || { sudo -n find "$D" -mindepth 1 -maxdepth 2 | head -10; exit 1; }
# 新机上此刻不该有任何 NapCat 在跑
pgrep -af 'dbus-run-session|AppRun|[/]qq' >/dev/null 2>&1; rc=$?
case "$rc" in
  0) echo "HOME_QQ_PROCS=present"; pgrep -af 'dbus-run-session|AppRun|[/]qq' | head -10; exit 1 ;;
  1) echo "HOME_QQ_PROCS=absent" ;;
  *) echo "HOME_QQ_PROCS=UNCHECKABLE(pgrep rc=$rc)"; exit 1 ;;
esac
# Docker 回退路径也必须纳入跨运行时互斥；runtime 存在但查不了同样阻断。
for rt in docker podman; do
  command -v "$rt" >/dev/null 2>&1 || continue
  if ! dps=$(sudo -n "$rt" ps --format '{{.Names}}' 2>&1); then
    echo "HOME_${rt^^}_NAPCAT_RUNNING=UNCHECKABLE"
    exit 1
  fi
  matches=$(printf '%s\n' "$dps" | grep -i napcat); grep_rc=$?
  case "$grep_rc" in
    0) echo "HOME_${rt^^}_NAPCAT_RUNNING=present"; printf '%s\n' "$matches"; exit 1 ;;
    1) echo "HOME_${rt^^}_NAPCAT_RUNNING=absent" ;;
    *) echo "HOME_${rt^^}_NAPCAT_RUNNING=UNCHECKABLE(grep rc=$grep_rc)"; exit 1 ;;
  esac
done
REMOTE
RC=$?
printf '%s\n' "$OUT"
if [ "$RC" -eq 255 ]; then
  block "home: SSH 本身失败（rc=255）"
elif [ "$RC" -ne 0 ]; then
  block "home: 设备目录非空 / 不存在 / 查不了，或已有 QQ 进程/napcat 容器（rc=$RC）"
else
  ok "home: 设备目录存在且为空，无 QQ 进程、无运行中的 napcat 容器"
fi

# ---------------------------------------------------------------- 结论
if [ "$BLOCK" -ne 0 ]; then
  echo "GATE35=BLOCK — 不许在新机上启动 NapCat，不许扫码"
  exit 1
fi
echo "GATE35=PASS"
GATE
laptop$ bash /tmp/t4-gate35.sh; echo "rc=$?"
```

**唯一的放行结果**：最后一行 `GATE35=PASS`，`rc=0`。

**任何其他结果都是阻断。** 常见分支：

| 输出 | 含义 | 处置 |
|---|---|---|
| `MISSING_CMD=ss` / `MISSING_CMD=pgrep` | 远端缺工具，**检查不了** | 装上再跑。**不要**因为"应该没问题"就跳过 |
| `SSH 本身失败（rc=255）` | 网络/密钥/主机名问题 | 修 SSH。**查不了 ≠ 安全** |
| `UNIT_ENABLED=disabled` | 停了但没 mask | 回 §3.3.2 补 `mask` 并验证 `start` 会失败 |
| `UNIT_STATE=UNCHECKABLE` / unit 属性缺失 | `systemctl show` 查询失败或结果不可判定 | **阻断**；修 systemd/D-Bus/权限后重跑，不能按输出猜“已停” |
| `PROCS_QQ_TREE=present` 而 `PROCS_NAPCAT_NATIVE=absent` | **正是上一版会漏掉的那种残留**：QQ 以别的身份/脱离 cgroup 活着 | 回 §3.3.3 的 `systemctl kill` → `pkill -u` 阶梯 |
| `LISTEN_3011=present` / `LISTEN_6099=present` 而进程检查通过 | 端口被**你不知道的东西**占着 | 见下面「查端口占用者」那条命令。搞清楚是谁再往下 |
| `LISTEN_*=UNCHECKABLE` / `PROD_LISTEN_3011=UNCHECKABLE` | `ss` 失败、无权限或过滤器不支持 | **阻断**。修好 `ss` 后重跑；空输出不能再被解释成“无监听” |
| `CGROUP_PROCS` > 0 | 孤儿进程还在 unit 的 cgroup 里 | 同上，`systemctl kill -s SIGKILL` |
| `DOCKER_NAPCAT_RUNNING=UNCHECKABLE` / `PODMAN_...=UNCHECKABLE` | runtime 存在但查询失败/没权限/grep 异常 | **阻断**。先 `sudo -v` 预热或修 runtime，重跑。存在容器 = 第二条并行登录路径，不能“看不见就当没有” |
| `HOME_NTQQ=missing` | §2.4.3 的闸门没走完 | 回 §2.4.3。**注意那里非空是硬阻断，不是让你去清空它** |
| `HOME_NTQQ_ENTRIES` > 0 | 新机身份目录不空 | 回 §2.4.3 —— **硬阻断，不得 `mv`/`rm`/重建** |
| `HOME_DOCKER_NAPCAT_RUNNING=present` / `HOME_PODMAN_...=present` | home 已有另一运行时实例 | **阻断**；先停并取证。Docker 路径的停止态同名容器再由 §4.5.3/§4.5.4 处理 |
| `HOME_*_NAPCAT_RUNNING=UNCHECKABLE` | runtime 存在但权限/daemon/查询失败 | **阻断**；不能当成“没有容器” |

**查端口占用者**（上表 `LISTEN_*` 那一行用的就是这条 —— 放在这里而不是表格里，
是因为**表格单元格里的管道符必须写成 `\|` 才能正常渲染，而那个反斜杠会被一起复制走**，
正是上一版 §3.5 出错的根源）：

```bash
cornna-cn$ sudo ss -ltnp | grep -E ':(3011|6099)'
```

- **`pgrep` 模式误报怎么办**：闸门用的模式（`dbus-run-session`、`AppRun`、`[/]qq`）
  可能命中别的进程。**误报也必须查清再放行，不许放宽模式** ——
  闸门的误报成本是几分钟，漏报成本是一次并行登录 + 加计风控。
- **把整段输出抄进你的执行记录**（不只是最后那行 PASS）。
  这是事后判断"有没有发生过双登录"的唯一证据。

> **📌 一条通用写作纪律（这次就是栽在它上面）**：
> **任何要被复制执行的命令，都必须放在 fenced code block 里，不能放在 Markdown 表格单元格里。**
> 表格里的 `|` 必须转义成 `\|`，而复制出去的 `\|` 在 shell 里是**字面字符**，不是管道。
> 表格只用来放"判据"和"结论"，命令一律放代码块。

---

## 4. 新机的 systemd / 运行时配置

### 4.1 主机准备（先做，做完再碰 NapCat）

```bash
home$ sudo timedatectl set-timezone Asia/Shanghai
home$ timedatectl | grep -E 'Time zone|synchronized'
home$ sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
home$ systemctl status sleep.target 2>&1 | head -3
home$ sudo apt-get install -y dbus-x11 libnss3 libatk-bridge2.0-0 libgtk-3-0 libasound2 xdg-utils jq
home$ command -v dbus-run-session && echo DBUS_OK
```

- **时区必须是 `Asia/Shanghai`**：00-PLAN D79 就吃过"容器时钟 vs 宿主时间"的亏，日志时间对不上
  会让 §8 的会话时长统计全部作废。`synchronized: yes` 也要确认。
- **`sleep.target` 必须 masked**：`status` 里出现 `Loaded: masked` 即可。
  机器睡一次 = QQ 掉一次线 = 一次人工扫码。
- **包名按发行版调整**。RHEL 系是 `dbus-x11 nss atk gtk3 alsa-lib`。
  **如果 `dbus-run-session` 找不到**：装 `dbus-daemon`/`dbus-x11`，没有它 QQ 起不来。
- 顺手确认 BIOS 的 `Restore on AC Power Loss = Power On`（这一条只能进 BIOS 看，没有命令）。

防火墙：家里那台机器**不需要任何入站端口**。

> ### ⚠️ 先备份状态、先放行 SSH，再 `enable` —— 顺序反了会把自己锁在门外
>
> 上一版这里是三条命令一行连写，**没有保存任何回滚材料**，也**没有先确认 SSH 会被放行**。
> 如果你是**通过 SSH** 在操作这台机器（多数情况如此），
> `ufw default deny incoming` + `ufw enable` 会**立刻切断你当前的管理连接**，
> 而这台机器在你家里、可能还没接显示器。这违反硬约束 11（破坏性步骤前必须备份）。

**第 1 步：先备份现有防火墙状态（这是回滚材料）**

```bash
home$ TS=$(date -u +%Y%m%dT%H%M%SZ); echo "TS=$TS"
home$ sudo install -d -m 700 /opt/napcat/backups/t4-fw-$TS
home$ sudo ufw status numbered > /tmp/ufw.before 2>&1; sudo cp /tmp/ufw.before /opt/napcat/backups/t4-fw-$TS/ufw-status-numbered.before
home$ sudo ufw status verbose >  /tmp/ufw.v      2>&1; sudo cp /tmp/ufw.v      /opt/napcat/backups/t4-fw-$TS/ufw-status-verbose.before
home$ sudo cp -a /etc/ufw /opt/napcat/backups/t4-fw-$TS/etc-ufw.before 2>/dev/null || echo "(no /etc/ufw — ufw 可能未安装)"
home$ sudo iptables-save > /tmp/ipt.before 2>/dev/null; sudo cp /tmp/ipt.before /opt/napcat/backups/t4-fw-$TS/iptables-save.before 2>/dev/null || true
home$ sudo ls -l /opt/napcat/backups/t4-fw-$TS/
home$ rm -f /tmp/ufw.before /tmp/ufw.v /tmp/ipt.before
```

- **预期**：备份目录里至少有 `ufw-status-numbered.before` 和 `ufw-status-verbose.before`。
- **`ufw` 未安装**：`sudo apt-get install -y ufw`，然后**重做这一步**（备份要在 enable 之前）。

**第 2 步：确认你怎么连着这台机器，并先放行它**

```bash
home$ echo "SSH_CLIENT=${SSH_CLIENT:-<not-over-ssh>}"
home$ ip -o -4 addr show scope global | awk '{print $2, $4}'
```

- **`SSH_CLIENT=<not-over-ssh>`**（你在本地键盘前）⇒ 可以直接跳到第 3 步。
- **有 `SSH_CLIENT`**（你在远程）⇒ **必须先放行你的来源网段**，否则 `enable` 会踢掉你自己：
  ```bash
  home$ sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp   # 换成你实际的 LAN 段
  home$ sudo ufw show added | grep 22
  ```
  **不要 `ufw allow 22`**（那是对全世界开放）。网段从 `SSH_CLIENT` 的第一个字段推出来。

**第 3 步：设默认策略并启用**

```bash
home$ sudo ufw default deny incoming
home$ sudo ufw default allow outgoing
home$ sudo ufw --force enable
home$ sudo ufw status verbose
```

- **预期**：`Status: active`；`Default: deny (incoming), allow (outgoing)`；
  规则列表为空，或只有你在第 2 步加的那条 LAN SSH。
- **`--force` 是必需的**：不加时 `ufw enable` 在非交互场景会卡在
  `Command may disrupt existing ssh connections. Proceed (y|n)?` 上。
  **你已经在第 2 步处理了 SSH，所以这里跳过提示是安全的** —— 顺序反了才危险。

**回滚（如果把自己锁出去了，或者需要撤销）**：

```bash
home$ sudo ufw disable                       # 立刻恢复到"无防火墙"，先拿回连接
home$ sudo ufw status verbose                # 应为 Status: inactive
# 需要恢复原有规则时（用第 1 步的备份，TS 换成你记下的那个）：
home$ sudo cat /opt/napcat/backups/t4-fw-<TS>/ufw-status-numbered.before
home$ sudo cp -a /opt/napcat/backups/t4-fw-<TS>/etc-ufw.before/. /etc/ufw/
home$ sudo systemctl restart ufw 2>/dev/null || true
```

- **人已经被锁在外面了怎么办**：这就是为什么第 2 步不能跳。
  只能接显示器/键盘，或用 IPMI/串口。**家里那台机器多半没有带外管理** ——
  所以这一节的顺序是硬性的，不是建议。

### 4.2 `/etc/napcat-native/env`

```bash
home$ sudo install -d -m 700 -o root -g root /etc/napcat-native
home$ sudo install -m 600 -o root -g root /dev/null /etc/napcat-native/env
home$ sudo tee /etc/napcat-native/env >/dev/null <<'ENV'
# NapCat 原生实例 — 家宽住宅节点
# 只使用官方 ACCOUNT 快速登录契约。失败后转人工扫码，不做任何自动重试。
ACCOUNT=2952532060
TZ=Asia/Shanghai
# 关闭 NapCat 的 bypass.*（对抗腾讯检测注入的那组开关）。
# 依据 decisions.md D-QQ-05：权威机上已显式设置，本次迁移保持关闭 ——
# 迁移的思路是「让这个号看起来像普通住宅用户」，不走对抗检测那条路。
NAPCAT_DISABLE_BYPASS=1
# 明确不设置 NAPCAT_QUICK_PASSWORD —— 见下方说明，这是有意的空缺。
ENV
home$ sudo chmod 600 /etc/napcat-native/env && sudo chown root:root /etc/napcat-native/env
home$ sudo ls -l /etc/napcat-native/env
home$ sudo grep -c 'QUICK_PASSWORD' /etc/napcat-native/env
home$ sudo grep -c '^NAPCAT_QUICK_PASSWORD=' /etc/napcat-native/env
home$ sudo awk -F= '/^[A-Za-z_]+=/{print $1}' /etc/napcat-native/env
```

- **预期**：`-rw------- 1 root root`；`grep -c 'QUICK_PASSWORD'` 输出 **`1`**（那行注释）；
  **`grep -c '^NAPCAT_QUICK_PASSWORD='` 必须是 `0`**；
  最后那条 `awk` 只打印**键名**（`ACCOUNT` / `TZ` / `NAPCAT_DISABLE_BYPASS`），**不打印任何值** ——
  这个文件将来可能被人加进秘密，养成"只核对键名"的习惯。

> **⚠️ 存疑（如实标注，不要当成已验证的事实）**：
> `decisions.md` D-QQ-05 实测 `cornna-cn:/etc/napcat-native/env`（活单元实际读的那份）
> 的键**只有** `HOME`、`NAPCAT_DISABLE_BYPASS`、`TMPDIR`、`TZ`、`XDG_CACHE_HOME`、
> `XDG_CONFIG_HOME`、`XDG_DATA_HOME` —— **里面并没有 `ACCOUNT`**。
> 而 H1 §4.1 那条「只使用官方 `ACCOUNT` 快速登录契约」描述的是 **Docker 镜像 entrypoint** 的行为。
> ⇒ **原生 AppImage 路径下 `ACCOUNT` 是否真的被读取、能否真的触发快速登录，本项目没有实测数据。**
> 本 runbook 仍然写上它（无害：多一个用不到的环境变量没有副作用），
> 但**不要把「配了 `ACCOUNT` 所以会自动快速登录」当成前提**去做任何判断 ——
> §4.7.1 的开机 enable 取舍就是按「快速登录不一定可用」来推理的。
> 要验证只能看首次启动 journal 里有没有快速登录相关记录，不能靠假设。

> ### ⛔ 不要配置 `NAPCAT_QUICK_PASSWORD`
>
> H1 §4.1 明令：只用官方 `ACCOUNT` 快速登录契约，失败后转人工扫码。三个理由：
> 1. 当前镜像 entrypoint **只明确支持 `ACCOUNT`**，`NAPCAT_QUICK_PASSWORD` 是未受支持的变量。
> 2. 反复的密码登录会让风控加码 —— 而这个号本来就是低信任新号。
> 3. **被踢之后密码登录会阻塞在短信验证上**（H1 §8.6 实测：密码登录只会撞上
>    `loginError=duplicate_login_guard`，什么也解决不了）。
>
> #### H1 §10 那条"环境里仍有 `NAPCAT_QUICK_PASSWORD`"的遗留 —— 必须区分**两份**文件
>
> H1 §10 写的是「权威环境仍存在 `NAPCAT_QUICK_PASSWORD`……后续应在有回滚和人工扫码窗口时移除」。
> **那句话写于 Docker 时代，指的不是活单元读的那份文件。**
> `decisions.md` D-QQ-05 的 2026-08-31 只读实测把这件事查清楚了：
>
> | 文件 | 谁在读 | `NAPCAT_QUICK_PASSWORD` | 结论 |
> |---|---|---|---|
> | **`/etc/napcat-native/env`** | **`napcat-native.service` 的 `EnvironmentFile=`（活单元实际读的就是这份）** | **没有** | 干净。直接读运行中进程的 `/proc/<pid>/environ` 也证实无该变量 |
> | `/etc/napcat/napcat.env` | **没有单元在读**（Docker 时代残留，600 root:root，mtime 2026-08-29） | 有 1 行非空 | **惰性残留，不影响当前行为** |
>
> ⇒ **「砍掉密码登录」这件事在权威机上其实已经做完了**（D-QQ-05：近 6 小时 journal 的登录方式
> 词频是 `二维码`×15、`扫码`×6、`快速登录`×2，**无任何密码回退/密码登录痕迹**）。
> H1 §10 那条遗留的**真实剩余部分**只是一次**卫生清理**：删掉或清空那个陈旧文件，
> 免得将来有人误以为它还在生效。**收益是防止未来踩坑，不是当下降风控。**
>
> **对本 runbook 的意义**：这次是全新机器，`/etc/napcat/napcat.env` 压根不会被创建，
> 所以这条遗留在新机上**自动不存在**。
> ⚠️ 但**退役的 `cornna-cn` 上那份残留仍然在**（§7.2 明确不删任何东西，包括它）。
> 回滚时旧机会被重新启用 —— 那时它依然是惰性残留，不影响行为，**但也别去"顺手清理"**：
> 清理它属于独立的维护动作，不该混进迁移或回滚的关键路径。

### 4.3 preflight 与 launcher：同机互斥的行为契约

**行为契约（五条，缺一不可）**：

1. 端口 3001 或 6099 已被监听 ⇒ **拒绝启动**（有别的实例在跑）。
2. 存在名字含 `napcat` 的 **running** 容器 ⇒ **拒绝启动**（另一种运行形态在跑）。
3. 本机存在 QQ 进程树（`dbus-run-session` / `AppRun` / `qq`，**不限用户**）⇒ **拒绝启动**。
4. 拿不到 `/run/napcat-native.lock` 的排他 flock ⇒ **拒绝启动**。
5. **以上任一检查"无法完成"也必须拒绝启动** —— 命令不存在、权限不足、
   `ss`/`docker` 异常退出，**一律当作"有并行实例"处理**。

> ### ⚠️ 第 5 条是本次修订的重点：上一版是**失败开放**的
>
> 上一版 preflight 里写的是 `docker ps ... 2>/dev/null | grep -qi napcat`。
> 在 `User=napcat-native` 下运行时，**Docker 存在但无权访问 socket** ⇒ `docker ps` 失败 ⇒
> 错误被 `2>/dev/null` 丢掉 ⇒ 条件分支为假 ⇒ 直接跳过 ⇒ **最后照样打印 `preflight: OK`**。
> `ss` 不存在或失败时同样如此。
>
> **"查不了"被映射成了"安全"** —— 而这正是这把闸唯一要挡的东西。
> 已全部改成失败关闭。

**preflight 只做判断，不做处置。** 绝不"清理一下再启动" —— 自动处置就是又一个生命周期管理器。

#### 4.3.1 为什么拆成两个脚本，以及为什么**不**把 `napcat-native` 加进 `docker` 组

有一部分检查（容器、全机进程树）需要 root，而服务用户 `napcat-native` 是低权限账号。
有两条路：

| 方案 | 代价 |
|---|---|
| 把 `napcat-native` 加进 `docker` 组 | ⛔ **不采纳。** `docker` 组等价于 root（能把宿主任意目录挂进容器再读写）。为了一条只读检查，把跑 QQ 的那个账号提权成 root 等价，是本 runbook 里性价比最差的一笔交易 |
| **拆成一个 root-owned 只读 helper** | ✅ **采纳。** systemd 的 `ExecStartPre=+/path` 前缀表示"**以完整权限（root）运行，忽略 `User=`**"。helper 只读、只判断、不处置，且**失败关闭** |

⇒ `napcat-native.service` 有**两条** `ExecStartPre`（§4.4 已写进 unit）：

```
ExecStartPre=+/usr/local/sbin/napcat-preflight-root     # root，查容器 + 全机进程树
ExecStartPre=/opt/napcat-native/bin/preflight           # napcat-native，查端口 + 自己的进程 + 锁
```

**先跑 root 那条**：容器/并行实例是"更外层"的问题，先排除它再谈端口。

#### 4.3.2 root-owned 只读 helper

```bash
home$ sudo tee /usr/local/sbin/napcat-preflight-root >/dev/null <<'SH'
#!/usr/bin/env bash
# napcat-preflight-root — 需要 root 的只读互斥检查。只判断，不处置。
# 由 napcat-native.service 的 `ExecStartPre=+` 以 root 运行（'+' 前缀绕过 User=）。
# 失败关闭：任何检查无法完成都以非零退出。见 runbook §4.3.1。
set -uo pipefail
fail() { echo "preflight-root: REFUSE: $*" >&2; exit 1; }

QQ_TREE='dbus-run-session|AppRun|[/]qq'

# (0) 依赖命令必须存在。缺了就是「查不了」，不是「没问题」。
for c in pgrep; do
  command -v "$c" >/dev/null 2>&1 || fail "required command not found: $c"
done

# (1) 容器运行时。存在但查不了 ⇒ 阻断，绝不当成「没有容器」。
for rt in docker podman; do
  command -v "$rt" >/dev/null 2>&1 || continue
  if ! out=$("$rt" ps --format '{{.Names}}' 2>&1); then
    fail "$rt is installed but '$rt ps' failed (fail-closed): $(printf '%s' "$out" | head -1)"
  fi
  matches=$(printf '%s\n' "$out" | grep -i napcat); grep_rc=$?
  case "$grep_rc" in
    0) printf '%s\n' "$matches" >&2
       fail "a running $rt container matches 'napcat' — two runtimes would double-login" ;;
    1) : ;;
    *) fail "grep exited rc=$grep_rc while checking $rt output (fail-closed)" ;;
  esac
done

# (2) 全机范围的 QQ 进程树（覆盖「以别的用户跑」的并行实例）
#     必须区分 pgrep 的三种退出码，不能「非 0 就当没有」：
#       0 = 有匹配（阻断） / 1 = 无匹配（正常） / >=2 = 语法或致命错误（查不了 ⇒ 阻断）
#     busybox 的 pgrep 没有 -a，会以 rc=2 退出 —— 旧写法会把它当成「没有 QQ 进程」放行。
pgrep -af "$QQ_TREE" >/dev/null 2>&1; rc=$?
case "$rc" in
  0) pgrep -af "$QQ_TREE" >&2
     fail "a QQ process tree is already running on this host" ;;
  1) : ;;
  *) fail "pgrep exited rc=$rc — cannot verify process tree (fail-closed; busybox pgrep lacks -a?)" ;;
esac

echo "preflight-root: OK"
SH
home$ sudo chown root:root /usr/local/sbin/napcat-preflight-root
home$ sudo chmod 755 /usr/local/sbin/napcat-preflight-root
home$ sudo bash -n /usr/local/sbin/napcat-preflight-root && echo ROOT_HELPER_SYNTAX_OK
home$ sudo /usr/local/sbin/napcat-preflight-root; echo "rc=$?"
```

- **预期**：`ROOT_HELPER_SYNTAX_OK`；`preflight-root: OK`；`rc=0`。
- **属主必须是 `root:root` 且 `755`（非组/其他可写）**：它以 root 运行，
  可写就等于给了 `napcat-native` 一条提权路径。核对：`sudo ls -l /usr/local/sbin/napcat-preflight-root`。

#### 4.3.3 非特权 preflight（端口 / 自身进程 / 锁）

端口用环境变量传，**不要用 `sed` 改脚本**（上一版让人"逐行读一遍并修改硬编码端口"却没给命令，
结果是没人真的改）：home 是 **3001/6099**，`cornna-cn` 是 3011/6099。

```bash
home$ sudo install -d -m 755 -o root -g root /opt/napcat-native/bin
home$ sudo tee /opt/napcat-native/bin/preflight >/dev/null <<'SH'
#!/usr/bin/env bash
# preflight — 同机单实例互斥检查。只判断，不处置。非零退出即拒绝启动。
# 以 User=napcat-native 运行。需要 root 的检查在 /usr/local/sbin/napcat-preflight-root
# （由 ExecStartPre=+ 以 root 跑）。见 runbook §4.3.1。
# 失败关闭：命令不存在 / 权限不足 / 异常退出，一律拒绝启动。
set -uo pipefail
fail() { echo "preflight: REFUSE: $*" >&2; exit 1; }

# home=3001/6099；cornna-cn=3011/6099。用环境变量覆盖，不要改脚本。
PORTS="${NAPCAT_PREFLIGHT_PORTS:-3001 6099}"
LOCK="${NAPCAT_LOCK:-/run/napcat-native.lock}"

# (0) 依赖命令
for c in ss pgrep flock; do
  command -v "$c" >/dev/null 2>&1 || fail "required command not found: $c"
done

# (1) 端口。ss 失败 = 查不了 = 阻断（不是「没人监听」）。
#     `-H` 去掉表头后直接判断输出是否为空，避免 tail/grep 自身失败被误读成“零监听”。
#     sport 过滤器覆盖 0.0.0.0 / [::] / 127.0.0.1 等所有 bind 地址。
for p in $PORTS; do
  if ! out=$(ss -ltnH "( sport = :$p )" 2>&1); then
    fail "'ss' failed while checking port $p (fail-closed): $(printf '%s' "$out" | head -1)"
  fi
  if [ -n "${out//[[:space:]]/}" ]; then
    printf '%s\n' "$out" >&2
    fail "port $p already has a listener — another NapCat instance is running"
  fi
done

# (2) 本用户下的 QQ 进程（全机范围那条在 root helper 里）
#     同样区分 0 / 1 / >=2 三种退出码，见 root helper 里的注释。
pgrep -u "$(id -un)" -f '[/]qq' >/dev/null 2>&1; rc=$?
case "$rc" in
  0) fail "a QQ process is already running as $(id -un)" ;;
  1) : ;;
  *) fail "pgrep exited rc=$rc — cannot verify own processes (fail-closed)" ;;
esac

# (3) 锁必须能拿到。拿不到 = 有实例持有 = 阻断；打不开 = 查不了 = 也阻断。
#     这里只做「能否拿到」的试探并立刻释放，真正长期持锁的是 launcher。
if ! flock -n "$LOCK" -c true 2>/dev/null; then
  if [ -e "$LOCK" ] && [ ! -w "$LOCK" ]; then
    fail "cannot open lock $LOCK (permission) — fail-closed"
  fi
  fail "another instance holds $LOCK"
fi

echo "preflight: OK"
SH
home$ sudo chmod 755 /opt/napcat-native/bin/preflight
home$ sudo bash -n /opt/napcat-native/bin/preflight && echo PREFLIGHT_SYNTAX_OK
home$ sudo -u napcat-native /opt/napcat-native/bin/preflight; echo "rc=$?"
```

- **预期**：`PREFLIGHT_SYNTAX_OK`；`preflight: OK`；`rc=0`。
- **必须用 `sudo -u napcat-native` 跑一次**（而不是 root）：这才是它在 unit 里的真实身份。
  用 root 跑通不代表服务用户跑得通 —— 尤其 `/run/napcat-native.lock` 的写权限。
- **`REFUSE: cannot open lock ... (permission)`**：`/run` 下的锁文件属主不对。
  `sudo rm -f /run/napcat-native.lock` 后重跑（**这是锁文件，不是设备身份，删它是安全的**）。
- **`rc=1` 且提示 port 3001**：有东西在监听。`sudo ss -ltnp | grep 3001` 查是谁。
  **不要为了让 preflight 过就去 kill 它**，先搞清楚它是什么。

> **关于"从 `cornna-cn` 整份复制 preflight/launcher"**：上一版把它列为首选路径。
> **本次修订取消了这个首选。** 理由：那份脚本写死了 `cornna-cn` 的端口和 Docker 时代的逻辑，
> 而"复制后逐行读一遍并修改"是一个**没有验证点的步骤**——没人能证明改对了。
> 上面这两个脚本是完整的、端口参数化的、有行为测试的实现，**直接用它们**。
> 如果你想拿旧脚本做交叉参考，只读地看一眼即可：
> `ssh cornna-cn 'sudo cat /opt/napcat-native/bin/preflight'`，**不要覆盖上面的文件**。

#### 4.3.4 launcher（flock 在这里）

launcher（`-n` 是非阻塞，抢不到就立刻退出）：

```bash
home$ sudo tee /opt/napcat-native/bin/launcher >/dev/null <<'SH'
#!/usr/bin/env bash
# launcher — 持有排他 flock，然后在 dbus session 里拉起 NapCat AppImage。
set -uo pipefail
LOCK="${NAPCAT_LOCK:-/run/napcat-native.lock}"
exec 9>"$LOCK" || { echo "launcher: cannot open $LOCK" >&2; exit 1; }
if ! flock -n 9; then
  echo "launcher: REFUSE: another instance holds $LOCK" >&2
  exit 1
fi
exec dbus-run-session -- "${NAPCAT_APPIMAGE:?NAPCAT_APPIMAGE is required}" --no-sandbox
SH
home$ sudo chmod 755 /opt/napcat-native/bin/launcher
home$ sudo bash -n /opt/napcat-native/bin/launcher && echo LAUNCHER_SYNTAX_OK
```

- flock 挂在 fd 9 上，进程活着锁就在，进程死了内核自动释放 —— **不需要清理陈旧锁文件**，
  这正是用 flock 而不是 pidfile 的原因。
- **验证互斥真的生效**（在服务已经跑起来之后再做，见 §6.6 收尾）：
  ```
  home$ sudo -u napcat-native NAPCAT_APPIMAGE=/bin/true /opt/napcat-native/bin/launcher; echo "rc=$?"
  ```
  **预期**：`launcher: REFUSE: another instance holds ...`，`rc=1`。
  **如果 rc=0**：flock 没生效（多半是 `/run/napcat-native.lock` 权限不对，
  服务用户写不进去导致每次开的是不同文件）。修到它能拒绝为止再往下。

#### 4.3.5 行为测试：证明它**真的会拒绝**（这一步不许跳）

只验证"正常情况下返回 OK"是不够的 —— 一个 `exit 0` 的空脚本也能通过那种验证。
**必须证明每一条契约在被触发时都返回非零。** 在 NapCat 尚未启动时做（§6.0 第 3 步之前）。

**测试 A —— 端口被占用时必须拒绝**

```bash
home$ python3 -c 'import socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(("127.0.0.1",3001)); s.listen(1); print("FAKE_LISTENER_UP",flush=True); time.sleep(60)' &
home$ sleep 2; sudo -u napcat-native /opt/napcat-native/bin/preflight; echo "TEST_A_rc=$?"
home$ kill %1
```

- **预期**：`preflight: REFUSE: port 3001 already has a listener ...`，**`TEST_A_rc=1`**。
- **`TEST_A_rc=0`** ⇒ 端口检查没生效。多半是 `ss` 的 `sport` 过滤器语法在这个版本上不支持：
  手工跑 `ss -ltn "( sport = :3001 )"` 看有没有输出；没有就说明过滤器没工作，**修到它能报为止**。

**测试 B —— 端口 6099 同样要拒绝**（别只测一个端口就以为循环是对的）

```bash
home$ python3 -c 'import socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(("127.0.0.1",6099)); s.listen(1); print("FAKE_LISTENER_UP",flush=True); time.sleep(60)' &
home$ sleep 2; sudo -u napcat-native /opt/napcat-native/bin/preflight; echo "TEST_B_rc=$?"
home$ kill %1
```

- **预期**：`REFUSE: port 6099 ...`，**`TEST_B_rc=1`**。

**测试 C —— QQ 进程存在时必须拒绝**

```bash
home$ sudo install -d -m 755 /tmp/t4test && sudo ln -sf /bin/sleep /tmp/t4test/qq
home$ sudo -u napcat-native /tmp/t4test/qq 60 &
home$ sleep 1; sudo /usr/local/sbin/napcat-preflight-root; echo "TEST_C_root_rc=$?"
home$ sudo -u napcat-native /opt/napcat-native/bin/preflight; echo "TEST_C_rc=$?"
home$ sudo pkill -f '/tmp/t4test/qq'; sudo rm -rf /tmp/t4test
```

- **预期**：两个都 `REFUSE`，`TEST_C_root_rc=1` 且 `TEST_C_rc=1`。
- **只有 root helper 报而 preflight 不报** ⇒ 也算通过（分工如此：全机进程树归 root helper）。
  但 **两个都是 0 是失败**，必须修。

**测试 D —— 容器运行时"查不了"时必须失败关闭**（不需要真的建容器）

```bash
home$ sudo install -d -m 755 /tmp/t4fake
home$ printf '#!/bin/sh\nexit 13\n' | sudo tee /tmp/t4fake/docker >/dev/null
home$ sudo chmod 755 /tmp/t4fake/docker
home$ sudo env PATH=/tmp/t4fake:$PATH /usr/local/sbin/napcat-preflight-root; echo "TEST_D_rc=$?"
home$ printf '#!/bin/sh\necho napcat-anything\n' | sudo tee /tmp/t4fake/docker >/dev/null
home$ sudo env PATH=/tmp/t4fake:$PATH /usr/local/sbin/napcat-preflight-root; echo "TEST_E_rc=$?"
home$ sudo rm -rf /tmp/t4fake
```

- **预期 D**：`REFUSE: docker is installed but 'docker ps' failed (fail-closed)`，**`TEST_D_rc=1`**。
  **这一条就是上一版的致命缺陷**：那时它会打印 `preflight: OK`。
- **预期 E**：`REFUSE: a running docker container matches 'napcat'`，**`TEST_E_rc=1`**。
- **`TEST_D_rc=0`** ⇒ 你写进去的还是旧版（带 `2>/dev/null` 的那版）。回 §4.3.2 重写。

**把这五个 rc 抄进执行记录。** 它们是"互斥闸真的会拦"的唯一证据 ——
`preflight: OK` 本身证明不了任何事。

### 4.4 `napcat-native.service`（原生路径 —— 推荐）

```bash
home$ sudo tee /etc/systemd/system/napcat-native.service >/dev/null <<'UNIT'
[Unit]
Description=NapCat (native AppImage, QQ OneBot v11 backend)
Documentation=file:///opt/hermes/docs/orchestration/tasks/T4-home-migration-runbook.md
After=network-online.target
Wants=network-online.target

# 每小时只允许一次启动尝试。失败后必须由人显式:
#   systemctl reset-failed napcat-native.service && systemctl start napcat-native.service
# 这是反风控设施，不是可调优参数。见 runbook §4.7。
# 必须放在 [Unit] 段 —— 放 [Service] 会被 systemd 静默忽略（H1 §4.1）。
StartLimitIntervalSec=3600
StartLimitBurst=1

# 故意不写 Conflicts= : 本项目曾发生 Conflicts= + Restart= 互杀循环。
# 互斥由 bin/preflight（端口/容器/进程）和 bin/launcher 里的 flock 保证。见 runbook §3.2。

[Service]
Type=simple
User=napcat-native
Group=napcat-native
WorkingDirectory=/opt/napcat-native/runtime
EnvironmentFile=/etc/napcat-native/env
Environment=NAPCAT_APPIMAGE=/opt/napcat-native/runtime/NapCat.AppImage
Environment=HOME=/opt/napcat-native/home
Environment=XDG_CONFIG_HOME=/opt/napcat-native/home/.config
# home 是 3001/6099（cornna-cn 是 3011/6099）。端口用环境变量传，不改脚本。见 §4.3.3。
Environment=NAPCAT_PREFLIGHT_PORTS=3001 6099

# 两条 preflight，都失败关闭（§4.3）：
#   '+' 前缀 = 以完整权限(root)运行，忽略 User= 。用它做容器/全机进程树检查，
#   这样就不必把 napcat-native 加进 docker 组（docker 组等价 root，见 §4.3.1）。
ExecStartPre=+/usr/local/sbin/napcat-preflight-root
ExecStartPre=/opt/napcat-native/bin/preflight
ExecStart=/opt/napcat-native/bin/launcher

# Restart=on-failure + RestartSec=45s + [Unit] 里的 StartLimitBurst=1 三者合起来
# 等价于「失败即停手等人」：第 2 次启动必然撞启动限流并进入 failed，必须人工
# reset-failed 才能再试。这是 H1 §10 建议的「on-failure + 合理退避」的更严版本。
# ⛔ 不要改成 Restart=always（无退避的反复登录尝试），也不要为了「更安全」改成
#    Restart=no —— 那会丢掉 45s 内一次性抖动的自愈，却换不到任何额外的人工闸
#    （人工闸已经由 StartLimitBurst=1 提供）。语义详解见 §4.7。
Restart=on-failure
RestartSec=45s
TimeoutStopSec=90
KillMode=control-group
KillSignal=SIGTERM

# 唯一的设备身份卷，bind 挂进来；它从不被复制或复刻。
BindPaths=/opt/napcat/ntqq:/opt/napcat-native/home/.config/QQ
BindPaths=/opt/napcat-native/config-candidate:/opt/napcat-native/runtime/config

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/napcat/ntqq /opt/napcat-native/config-candidate /opt/napcat-native/home /run
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
# 注意：这里故意没有 MemoryDenyWriteExecute / CapabilityBoundingSet= 空。见 runbook §4.6。

# [Install] 段保留，是为了「将来想 enable 时不用改 unit」。
# ⚠️ 但本 runbook **默认不执行 `systemctl enable`** —— 开机自启的取舍见 §4.7.1。
[Install]
WantedBy=multi-user.target
UNIT
home$ sudo systemd-analyze verify /etc/systemd/system/napcat-native.service && echo UNIT_VERIFY_OK
home$ sudo systemctl daemon-reload
home$ systemctl show -p StartLimitIntervalUSec,StartLimitBurst,Restart,RestartUSec,KillMode \
      napcat-native.service
home$ systemctl show -p ExecStartPre napcat-native.service | tr ';' '\n' | grep -E 'path=|flags='
```

- **预期**：`UNIT_VERIFY_OK`；`show` 输出
  `StartLimitIntervalUSec=1h`、`StartLimitBurst=1`、`Restart=on-failure`、
  `RestartUSec=45s`、`KillMode=control-group`。
- **`ExecStartPre` 必须是两条**，且第一条带 root 权限标志：
  `path=/usr/local/sbin/napcat-preflight-root` 那条的 `flags=` 里要出现
  **`privileged`**（这就是 `+` 前缀生效的证据）；
  `path=/opt/napcat-native/bin/preflight` 那条的 `flags=` 不含 `privileged`。
  **看不到 `privileged` ⇒ `+` 没写上或被 systemd 忽略** ⇒ root helper 会以 `napcat-native`
  身份跑，`docker ps` 失败 ⇒ 按 §4.3 的失败关闭它会**拒绝启动**（安全，但你会一直起不来）。
  回去确认 unit 里那行确实是 `ExecStartPre=+/usr/local/sbin/...`。
- **⚠️ 如果 `StartLimitIntervalUSec` 显示 `10s`（默认值）而不是 `1h`**：
  你把它写进 `[Service]` 段了。systemd **不报错、静默忽略**（H1 §4.1 专门记了这个坑）。
  移到 `[Unit]` 段重来。**这一条必须眼睛看到 `1h` 才算过。**
- **如果 `systemd-analyze verify` 抱怨 `BindPaths` 两行只生效一行**：
  某些 systemd 版本对重复的 `BindPaths=` 是累加的，某些不是。改成一行分号分隔：
  `BindPaths=/opt/napcat/ntqq:/opt/... /opt/napcat-native/config-candidate:/opt/...`
  然后用 `systemctl show -p BindPaths napcat-native.service` **确认两条都在**。
  挂载错了 = 设备身份没挂上 = NapCat 会当新设备重建，正是 §2 严禁的事。
- **AppImage 路径**：`NAPCAT_APPIMAGE` 指向你实际放的文件。
  从 `cornna-cn` 复制 AppImage 本体是可以的（它是程序，不是身份）：
  ```
  home$ sudo install -d -m 755 -o napcat-native -g napcat-native /opt/napcat-native/runtime
  home$ ssh cornna-cn 'sudo cat /opt/napcat-native/runtime/NapCat.AppImage' \
      | sudo tee /opt/napcat-native/runtime/NapCat.AppImage >/dev/null
  home$ sudo chmod 755 /opt/napcat-native/runtime/NapCat.AppImage
  home$ sudo chown napcat-native:napcat-native /opt/napcat-native/runtime/NapCat.AppImage
  home$ ssh cornna-cn 'sudo sha256sum /opt/napcat-native/runtime/NapCat.AppImage'
  home$ sudo sha256sum /opt/napcat-native/runtime/NapCat.AppImage
  ```
  **两个 sha256 必须完全相同**，不同就重传。文件名如果不同，先
  `ssh cornna-cn 'sudo ls -l /opt/napcat-native/runtime/'` 看实际叫什么。
- **架构不匹配时**（新机是 arm64、AppImage 是 x86_64）：`file NapCat.AppImage` 会显示
  `x86-64`。**不要用 qemu 兜**，改走 §4.5 的 Docker 路径。

### 4.5 `napcat.service`（Docker 回退路径 —— 只在 §1.6 判定需要时用）

> ### ⚠️ 上一版这条路径有两个会让人白折腾半天的缺陷，已修
>
> 1. **挂的是一个从未被创建、也从未被填充的 config 目录。**
>    原生路径把 OneBot 配置写进 `/opt/napcat-native/config-candidate`（§4.8），
>    而 Docker unit 挂的是 `-v /opt/napcat/config:/app/napcat/config` ——
>    runbook 里**没有任何一步创建或填充 `/opt/napcat/config`**。
>    ⇒ 容器会拿到一个空目录（Docker 会自动建），**没有 OneBot token/host/port**
>    ⇒ 容器内 3001 根本不启动 ⇒ Hermes 握手失败 ⇒ 你会去查隧道，
>    而问题根本不在隧道。**本次修订统一了配置源。**
> 2. **`ExecStartPre=-/usr/bin/docker rm -f napcat` 会无备份删除同名容器。**
>    它把"清理"做成了每次启动的隐藏副作用，且在删之前**没有保存
>    inspect / 配置 / 状态证据**，违反硬约束 11（破坏性步骤前必须备份）。
>    **已删除该行与 `ExecStopPost` 里的同类行**，改为"发现同名容器就阻断，交人处理"。

#### 4.5.1 统一配置源：Docker 和原生用**同一个**目录

**结论：两条路径都用 `/opt/napcat-native/config-candidate`。**
Docker 把它挂到容器里的 `/app/napcat/config`。

理由：§4.8 那一整套（拷贝旧配置、改端口、核对 token 指纹、权限收口 700/600）
是**已经写好并有验证步骤**的，让 Docker 复用它，比再写一套 `/opt/napcat/config`
的创建/填充/校验流程要少一半出错面。**一个配置源，一套验证。**

**唯一的差异是 `host` 字段**（这个差异是真实存在的，不能靠"统一"消掉）：

| 路径 | `host` | 原因 |
|---|---|---|
| 原生 AppImage | `127.0.0.1` | 没有容器边界，绑 loopback 才不会暴露到 LAN |
| **Docker** | **`0.0.0.0`** | 00-PLAN D81 的坑：Docker 端口映射转发到容器 **eth0**，不是容器 loopback。绑 loopback 的话宿主敲 3001 永远 `did not receive a valid HTTP response`。宿主侧的暴露面由 `-p 127.0.0.1:3001:3001` 兜住 |

⇒ **走 Docker 路径时，§4.8 那段 patch 脚本里的 `s["host"]` 要写 `"0.0.0.0"`**
（脚本里那行已有注释标出）。先确认再起容器：

```bash
home$ sudo jq -r '.network.websocketServers[] | "enable=\(.enable) host=\(.host) port=\(.port) token_len=\((.token//"")|length)"' \
      /opt/napcat-native/config-candidate/onebot11_2952532060.json
home$ sudo ls -ld /opt/napcat-native/config-candidate
home$ sudo ls -l  /opt/napcat-native/config-candidate
```

- **预期（Docker 路径）**：`enable=true host=0.0.0.0 port=3001 token_len=<非0>`；
  目录 `700 napcat-native:napcat-native`，文件 `600`。
- **`host=127.0.0.1`** ⇒ 你用的是原生路径的值。回 §4.8 改成 `0.0.0.0` 再起容器，
  否则会得到"容器在跑但宿主连不上 3001"这个最难查的组合。
- **`token_len=0`** ⇒ 配置没带 token。回 §4.8。**别起容器**。
- **容器内以 root 运行**（H1 §4.1 记 `NAPCAT_UID=0`），所以 700/`napcat-native` 属主的目录
  容器读得到（root 绕过权限位）。**如果你的 Docker 开了 userns-remap**，
  容器内的 root 会被映射成非特权 uid ⇒ 读不到 700 的目录。
  用 `sudo docker info --format '{{.SecurityOptions}}' | grep -c userns` 确认；
  为 `1` 时**停下**，这条路径需要单独设计属主，不在本 runbook 覆盖范围内。

#### 4.5.2 digest 守卫与 unit

先装一个 root-owned 只读守卫。它不接受 tag，只接受 §1.6 写入的精确 digest；文件缺失、格式不对、
镜像本地不可解析都失败关闭：

```bash
home$ sudo tee /usr/local/sbin/napcat-docker-image-guard >/dev/null <<'SH'
#!/usr/bin/env bash
set -uo pipefail
F=/etc/napcat-native/docker-image.env
fail() { echo "docker-image-guard: REFUSE: $*" >&2; exit 1; }

[ -r "$F" ] || fail "$F missing or unreadable — run §1.6 DOCKER_DIGEST_GATE"
exec 3< "$F" || fail "cannot open $F"
IFS= read -r line <&3 || fail "$F is empty or unreadable"
if IFS= read -r extra <&3; then
  exec 3<&-
  fail "$F must contain exactly one line"
fi
exec 3<&-
[[ "$line" == NAPCAT_IMAGE=* ]] || fail "NAPCAT_IMAGE key missing"
ref=${line#NAPCAT_IMAGE=}
[[ "$ref" =~ ^(docker\.io/)?mlikiowa/napcat-docker@sha256:[0-9a-f]{64}$ ]] \
  || fail "image reference is not an exact approved repo digest"
command -v docker >/dev/null 2>&1 || fail "docker not found"
docker image inspect "$ref" >/dev/null 2>&1 \
  || fail "exact digest is not present/readable locally"
echo "docker-image-guard: OK ref=$ref"
SH
home$ sudo chown root:root /usr/local/sbin/napcat-docker-image-guard
home$ sudo chmod 755 /usr/local/sbin/napcat-docker-image-guard
home$ sudo bash -n /usr/local/sbin/napcat-docker-image-guard && echo IMAGE_GUARD_SYNTAX_OK
home$ sudo /usr/local/sbin/napcat-docker-image-guard; echo "rc=$?"
```

- **预期**：`IMAGE_GUARD_SYNTAX_OK`、`docker-image-guard: OK ref=...@sha256:<64 hex>`、`rc=0`。
- 这一步在每次启动前重跑；因此删掉/篡改 digest 文件不会退化成 tag 启动，只会拒绝启动。

unit 同时复用 §4.3 的端口 preflight 和同一把 flock；Docker/native 两条路径不能在同机并行：

```bash
home$ sudo tee /etc/systemd/system/napcat.service >/dev/null <<'UNIT'
[Unit]
Description=NapCat (docker, QQ OneBot v11 backend)
After=network-online.target docker.service
Requires=docker.service
Wants=network-online.target
StartLimitIntervalSec=3600
StartLimitBurst=1
# 同样故意不写 Conflicts= 。见 runbook §3.2。

[Service]
Type=simple
User=napcat-native
Group=napcat-native
EnvironmentFile=/etc/napcat-native/env
EnvironmentFile=/etc/napcat-native/docker-image.env
Environment=NAPCAT_PREFLIGHT_PORTS=3001 6099

# 四道启动闸：全机容器/QQ 树、精确 digest、同名容器证据、端口+锁。
# unit 默认身份是 napcat-native；只有带 '+' 的 root helper / docker lifecycle 以完整权限运行。
ExecStartPre=+/usr/local/sbin/napcat-preflight-root
ExecStartPre=+/usr/local/sbin/napcat-docker-image-guard
# 同名容器检查：发现就阻断，绝不自动删。见 §4.5.3。
ExecStartPre=+/usr/local/sbin/napcat-docker-guard
ExecStartPre=/opt/napcat-native/bin/preflight

# ⛔ 这里**故意没有** `ExecStartPre=-/usr/bin/docker rm -f napcat`。
#    无备份删容器 = 破坏证据，违反硬约束 11。同名容器由上面的 guard 阻断，交人处理。
# flock 与 native launcher 使用同一个锁；docker run 前台存活多久，锁就持有多久。
ExecStart=+/usr/bin/flock --no-fork --nonblock /run/napcat-native.lock \
  /usr/bin/docker run --name napcat --restart=no \
  --env-file /etc/napcat-native/env \
  -v /opt/napcat/ntqq:/app/.config/QQ \
  -v /opt/napcat-native/config-candidate:/app/napcat/config \
  -p 127.0.0.1:3001:3001 \
  -p 127.0.0.1:6099:6099 \
  ${NAPCAT_IMAGE}
ExecStop=+/usr/bin/docker stop -t 30 napcat

# ⛔ 同样**故意没有** `ExecStopPost=-/usr/bin/docker rm -f napcat`。
#    停下来的容器是取证材料（logs / inspect / 退出码），保留它。
#    清理用 §4.5.4 的 napcat-docker-archive（先归档再删，且只在人明确执行时）。

Restart=on-failure
RestartSec=45s
TimeoutStopSec=90

# [Install] 保留但默认不 enable，理由同原生路径，见 §4.7.1。
[Install]
WantedBy=multi-user.target
UNIT
home$ sudo systemd-analyze verify /etc/systemd/system/napcat.service && echo UNIT_VERIFY_OK
home$ sudo systemctl daemon-reload
home$ systemctl show -p StartLimitIntervalUSec,StartLimitBurst,Restart napcat.service
home$ sudo bash -s <<'GATE'
set -uo pipefail
fail() { echo "DOCKER_UNIT_GATE=BLOCK reason=$*"; exit 1; }
if ! unit=$(systemctl cat napcat.service 2>&1); then
  fail "systemctl_cat_failed: ${unit%%$'\n'*}"
fi
# 注释里的反面示例保留；只扫描生效指令。
if ! active=$(printf '%s\n' "$unit" | sed '/^[[:space:]]*#/d;/^[[:space:]]*$/d'); then
  fail "cannot_filter_active_directives"
fi
case "$active" in *'docker rm'*) fail "active_docker_rm_present" ;; esac
case "$active" in *'REPLACE_WITH'*) fail "digest_placeholder_present" ;; esac
for required in \
  'User=napcat-native' \
  'EnvironmentFile=/etc/napcat-native/docker-image.env' \
  'ExecStartPre=+/usr/local/sbin/napcat-docker-image-guard' \
  'ExecStartPre=/opt/napcat-native/bin/preflight' \
  'ExecStart=+/usr/bin/flock --no-fork --nonblock /run/napcat-native.lock' \
  'ExecStop=+/usr/bin/docker stop' \
  '${NAPCAT_IMAGE}'; do
  case "$active" in *"$required"*) : ;; *) fail "missing:$required" ;; esac
done
echo "DOCKER_UNIT_GATE=PASS"
GATE
home$ echo "rc=$?"
```

- **预期**：`UNIT_VERIFY_OK`；`StartLimitIntervalUSec=1h`；`DOCKER_UNIT_GATE=PASS`、`rc=0`。
  这同时证明：没有生效的自动 `docker rm`、没有 digest 占位符、digest 守卫/端口 preflight/flock 都接入。

#### 4.5.3 同名容器守卫（阻断，不删）

```bash
home$ sudo tee /usr/local/sbin/napcat-docker-guard >/dev/null <<'SH'
#!/usr/bin/env bash
# napcat-docker-guard — 发现同名容器就阻断启动。只判断 + 只读取证，绝不删除。
# 违反硬约束 11 的旧做法是 `docker rm -f`：无备份删容器 = 破坏证据。
set -uo pipefail
NAME="${NAPCAT_CONTAINER:-napcat}"
fail() { echo "docker-guard: REFUSE: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker not found (this unit needs it)"

# 查不了就阻断（同 §4.3 的失败关闭原则）
if ! ids=$(docker ps -aq --filter "name=^/${NAME}$" 2>&1); then
  fail "'docker ps -aq' failed (fail-closed): $(printf '%s' "$ids" | head -1)"
fi

if [ -n "${ids//[[:space:]]/}" ]; then
  echo "docker-guard: existing container named '$NAME':" >&2
  docker ps -a --filter "name=^/${NAME}$" \
    --format '  id={{.ID}} status={{.Status}} image={{.Image}} created={{.CreatedAt}}' >&2 || true
  fail "a container named '$NAME' already exists — archive it first: napcat-docker-archive"
fi

echo "docker-guard: OK (no container named '$NAME')"
SH
home$ sudo chown root:root /usr/local/sbin/napcat-docker-guard
home$ sudo chmod 755 /usr/local/sbin/napcat-docker-guard
home$ sudo bash -n /usr/local/sbin/napcat-docker-guard && echo DOCKER_GUARD_SYNTAX_OK
home$ sudo /usr/local/sbin/napcat-docker-guard; echo "rc=$?"
```

- **预期（首次，尚无容器）**：`docker-guard: OK (no container named 'napcat')`，`rc=0`。
- **`REFUSE: a container named 'napcat' already exists`** ⇒ **这是有意的阻断，不是故障。**
  它可能是上一次运行留下的**停止态**容器（里面有 logs 和退出码），
  也可能是**正在跑的第二个实例**（那就是并行登录风险）。
  先看 guard 打印的 `status=`：
  - `status=Up ...` ⇒ **有实例在跑**。⛔ 不要 `docker rm`。
    先 `sudo docker update --restart=no napcat && sudo docker stop napcat`，
    然后回 §3.5 重跑放行闸。
  - `status=Exited ...` ⇒ 停止态残骸。用 §4.5.4 归档后再删。

#### 4.5.4 归档后再删（唯一允许删容器的路径）

```bash
home$ sudo tee /usr/local/sbin/napcat-docker-archive >/dev/null <<'SH'
#!/usr/bin/env bash
# napcat-docker-archive — 先把容器的取证材料落盘，再删除容器。
# 这是本 runbook 里**唯一**允许删除 napcat 容器的路径，且必须由人显式执行。
# 拒绝删除正在运行的容器（不做 `rm -f`）。任何归档项失败都禁止进入 docker rm。
set -uo pipefail
NAME="${NAPCAT_CONTAINER:-napcat}"
fail() { echo "archive: ABORT: $*" >&2; exit 1; }

if ! TS=$(date -u +%Y%m%dT%H%M%SZ); then
  fail "cannot obtain UTC timestamp"
fi
DIR="/opt/napcat/backups/docker-${NAME}-${TS}"

if ! docker inspect "$NAME" >/dev/null 2>&1; then
  fail "no readable container named '$NAME'"
fi
if ! running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>&1); then
  fail "cannot determine whether container is running: ${running%%$'\n'*}"
fi
[ "$running" = "false" ] || fail "container is running — stop it first (never rm -f)"

umask 077
mkdir "$DIR" || fail "cannot create fresh archive directory $DIR"
chmod 700 "$DIR" || fail "cannot protect $DIR"

# 四项都是强制证据。命令非零时保留 partial 目录供排查，但绝不删除容器。
docker inspect "$NAME" > "$DIR/inspect.json" 2> "$DIR/inspect.err" \
  || fail "inspect capture failed (partial archive kept at $DIR)"
docker logs --tail 2000 "$NAME" > "$DIR/logs.txt" 2> "$DIR/logs.err" \
  || fail "logs capture failed (partial archive kept at $DIR)"
docker ps -a --filter "name=^/${NAME}$" --no-trunc > "$DIR/ps.txt" 2> "$DIR/ps.err" \
  || fail "ps capture failed (partial archive kept at $DIR)"
ls -laR /opt/napcat-native/config-candidate > "$DIR/config-listing.txt" 2> "$DIR/config-listing.err" \
  || fail "config listing failed (partial archive kept at $DIR)"

[ -s "$DIR/inspect.json" ] || fail "inspect.json is empty"
[ -s "$DIR/ps.txt" ] || fail "ps.txt is empty"
[ -s "$DIR/config-listing.txt" ] || fail "config-listing.txt is empty"
[ -e "$DIR/logs.txt" ] || fail "logs.txt missing"
(
  cd "$DIR" || exit 1
  sha256sum inspect.json logs.txt ps.txt config-listing.txt > SHA256SUMS
) || fail "cannot write SHA256SUMS"
printf 'container=%s\narchived_at=%s\nstatus=complete\n' "$NAME" "$TS" > "$DIR/ARCHIVE_COMPLETE" \
  || fail "cannot write completion marker"
sync || fail "sync failed; archive durability unconfirmed"
echo "archive: saved complete evidence to $DIR"

# 只有 ARCHIVE_COMPLETE 已落盘并 sync 成功后才可能走到这里。
docker rm "$NAME" >/dev/null || fail "docker rm failed"    # 注意：没有 -f
echo "archive: removed container '$NAME' (evidence kept in $DIR)"
SH
home$ sudo chown root:root /usr/local/sbin/napcat-docker-archive
home$ sudo chmod 700 /usr/local/sbin/napcat-docker-archive
home$ sudo bash -n /usr/local/sbin/napcat-docker-archive && echo DOCKER_ARCHIVE_SYNTAX_OK
```

- **`chmod 700`（不是 755）**：它是**破坏性**脚本，只给 root。
  guard 是只读的，755 无妨；这个不是。
- ⛔ **脚本里没有 `-f`，且拒绝删除运行中的容器** —— 这两点是有意的，别"优化"掉。
- **`inspect.json` / `logs.txt` / `ps.txt` / `config-listing.txt` 任一采集命令失败，或校验清单、
  `ARCHIVE_COMPLETE`、`sync` 任一步失败 ⇒ 脚本非零退出，`docker rm` 根本不会被调用。**
  失败时留下的目录是明确的 partial 取证，不得手工补一个 `ARCHIVE_COMPLETE` 绕过。
- **`inspect.json` 里会不会有秘密？** 会：`--env-file` 的内容会出现在 `.Config.Env` 里。
  所以归档目录是 **700**，且 ⛔ **不要 `cat` 它、不要把它贴进工单**。
  需要看时用 `sudo jq '.[0].State' $DIR/inspect.json` 这类**只取所需字段**的读法。

#### 4.5.5 Docker 路径的其余两条检查

1. **Docker 自己的重启策略必须是 `no`。** systemd 和 Docker 双重无限重启 = 双重登录尝试。
   ```
   home$ sudo docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}} running={{.State.Running}}' napcat
   ```
   **预期**：`restart=no`。不是就 `sudo docker update --restart=no napcat`。
2. **容器确实读到了配置，且 OneBot 监听 `0.0.0.0:3001`。**
   ```
   home$ sudo docker exec napcat sh -c 'ls -l /app/napcat/config' | head
   home$ sudo docker exec napcat sh -c 'ss -ltn 2>/dev/null || netstat -ltn' | grep 3001
   ```
   **预期**：能列出 `onebot11_2952532060.json`（**这就是 §4.5.1 那个统一配置源真的挂进去了的证据**）；
   监听是 `0.0.0.0:3001`。
   - **`ls` 是空的** ⇒ 挂载路径写错，或你还在用旧版 unit 里的 `/opt/napcat/config`。
     回 §4.5.2 核对 `-v` 那一行。**这就是上一版会卡住的地方。**
   - **看到 `127.0.0.1:3001`** ⇒ 回 §4.8 把 `host` 改成 `0.0.0.0`（§4.5.1 的表）。

### 4.6 加固项落点表 —— 哪些能放 QQ 单元、哪些会把它打死

`cornna-cn` 上 `napcat-native-health.service` 用了一套很紧的沙箱参数。
**不要整份抄到 QQ 单元上。** QQ 是个 Electron 应用，有些参数会直接让它起不来：

| 指令 | 健康探针单元 | QQ 单元 | 原因 |
|---|---|---|---|
| `NoNewPrivileges=yes` | ✅ | ✅ | 安全，无副作用 |
| `ProtectSystem=strict` | ✅ | ✅ **但必须配 `ReadWritePaths`** | 少一条路径 QQ 就写不了设备目录，会当成新设备 |
| `ProtectHome=yes` | ✅ | ✅ | HOME 已显式指到 `/opt/napcat-native/home` |
| `PrivateTmp=yes` | ✅ | ✅ | Electron 用 `/tmp` 做 IPC socket，隔离即可，禁止不行 |
| `CapabilityBoundingSet=`（空） | ✅ | ⚠️ **不建议** | Chromium sandbox 需要 `CAP_SYS_ADMIN` 建 user namespace。已经用 `--no-sandbox` 绕开，但清空 cap 集合在部分内核上仍会触发莫名其妙的启动失败 |
| `MemoryDenyWriteExecute=yes` | ✅ | ⛔ **绝对不能** | V8 的 JIT 必须 W+X。加上它 QQ **必然启动失败**，而且报错很难看懂 |
| `PrivateNetwork=yes` | ⚠️ | ⛔ | QQ 要联网 |
| `RestrictNamespaces=yes` | ✅ | ⛔ | 同 `CapabilityBoundingSet`，Chromium 会用 namespace |

**如果 QQ 起不来而日志难以理解**：按这个顺序逐条注释掉再试 ——
`MemoryDenyWriteExecute` → `RestrictNamespaces` → `CapabilityBoundingSet` →
`ProtectSystem`（降到 `full`）。**每次只改一条**，改完
`sudo systemctl daemon-reload && sudo systemctl reset-failed napcat-native.service`
再启动（不 `reset-failed` 的话第二次启动就会被 §4.7 的启动限流挡住）。

### 4.7 `StartLimitIntervalSec=3600` / `StartLimitBurst=1` 的真实语义

这是从 `cornna-cn` 照抄的，unit 注释原文：
*"Exactly one start attempt per hour. A deliberate human retry after a failure must first run:
`systemctl reset-failed napcat-native.service`"*

**它的实际效果比字面更强，必须理解清楚：**

`Burst=1` 意味着**一小时窗口里只允许一次启动**。于是：

- 服务启动失败 → `Restart=on-failure` 想重启 → 那会是这一小时里的**第 2 次**启动 →
  **直接命中启动限流** → unit 进入 `failed`，状态 `start-limit-hit`，**不再自动重试**。
- 也就是说：**实际上没有自动恢复。** `Restart=on-failure` + `RestartSec=45s` 只是把
  "第一次失败后 45 秒"这个窗口留着，真正的语义是"**失败即停手，等人**"。
- **人工重试也受同一把闸约束。** 你在同一小时里直接 `systemctl start` 会看到：
  `Failed to start napcat-native.service: Unit napcat-native.service start request repeated too quickly.`

**这正是我们要的。** 每一次 NapCat 启动都可能是一次 QQ 登录尝试；对一个 2026-08-28 才启用、
已经被踢过两次的新号来说，**限制登录尝试频率比提高可用性重要**。

> **⚠️ 引用 H1 §10 时必须换算成当前的 unit 名，否则会指向一个不存在的东西**
>
> H1 §10 的原话是「权威 `cornna-cn:napcat.service` 当前仍是 systemd `Restart=always`……
> 应在人工认证维护窗口改成 on-failure + 合理退避」。
> **`napcat.service` 这个 unit 在 `cornna-cn` 上已经不存在了**（§0.1 的对照表、
> `decisions.md` D-QQ-04 实测）。当前跑的是 **`napcat-native.service`**。
>
> | | H1 §10 的说法（Docker 时代） | 2026-08-31 实测（当前事实） |
> |---|---|---|
> | unit | `napcat.service` | **`napcat-native.service`**（`napcat.service` 不存在） |
> | `Restart=` | `always`（H1 §10 说"应改成 on-failure"） | **需要在执行时用 `systemctl show -p Restart napcat-native.service` 当场确认**，不要照抄 H1 的 `always` |
> | `EnvironmentFile=` | `/etc/napcat/env`（Docker 时代） | **`/etc/napcat-native/env`**；`/etc/napcat/napcat.env` 是**没有单元在读**的残留（见 §4.2） |
>
> **执行时当场核对，不要引用 H1 的值**：
> ```bash
> cornna-cn$ systemctl show -p Restart,RestartUSec,StartLimitIntervalUSec,StartLimitBurst \
>       napcat-native.service
> cornna-cn$ systemctl show -p EnvironmentFiles napcat-native.service
> cornna-cn$ systemctl cat napcat.service 2>&1 | head -1   # 预期：Unit napcat.service could not be found.
> ```
> 最后那条**预期是报错**：它确认 `napcat.service` 确实不存在，
> 从而确认你没在对着一个幽灵 unit 做决定。

新机在这一点上做得比 H1 §10 的建议**更彻底**：不只是 `on-failure` + 退避，
而是靠 `StartLimitBurst=1` 把它变成**人工闸**（失败即停手等人）。

**人工重试的唯一正确姿势**：

```bash
home$ systemctl is-failed napcat-native.service
home$ sudo journalctl -u napcat-native.service -n 50 --no-pager     # 先看为什么失败
home$ sudo systemctl reset-failed napcat-native.service
home$ sudo systemctl start napcat-native.service
home$ systemctl is-active napcat-native.service
```

- **先看日志再 reset。** 不看日志就 reset 然后重试 = 你在盲目消耗登录尝试。
- **一小时内不要重试第三次。** 如果两次都失败，问题不在"再试一次"能解决的范围内。
- **`reset-failed` 不会重启 QQ、不会碰设备目录**，它只清 systemd 的失败计数，安全。

#### 4.7.1 开机 `enable`：本 runbook 的取舍与推荐

这是一个**独立于 `Restart=` 的决策**，上一版把它一笔带过（附录 B 里直接写"`enable`（断电后能自启）"），
本次修订把它显式摊开。

**先厘清一件事**：`Restart=on-failure` 和「开机 enable」是两回事。

| | 触发时机 | 有没有人在场 | 受 `StartLimitBurst=1` 约束 |
|---|---|---|---|
| `Restart=on-failure` | 服务**刚被人启动后**崩了 | **有**（是人刚敲的 `start`） | 受。第 2 次必然撞限流 ⇒ `failed` ⇒ 等人 |
| 开机 `enable` | 机器**开机时**（跳闸、断电恢复、内核升级重启） | **通常没有**（凌晨三点） | 受，但那一次额度是被**无人值守地**花掉的 |

⇒ **`Restart=` 的自动重启永远发生在"人刚动过手"的上下文里**，这就是它与硬约束 5 针对的
watchdog 模式（`napcat-watch.py`：检测异常 → 无人值守 `systemctl restart`）的本质区别。
**开机 enable 是这两者之间的灰色地带**，所以要单独判。

**两边的账**：

| | `enable`（开机自启） | `disable`（**本 runbook 推荐**） |
|---|---|---|
| 断电恢复后 | NapCat 自己起来，**若快速登录票据仍有效**则 QQ 无人值守回来 | QQ 保持离线，等人 `start` + 扫码 |
| 票据已失效时 | 起来 → `ACCOUNT` 快速登录失败 → 转等扫码 → **这一小时的启动额度已被花掉** | 额度留着，人到场时立刻可用 |
| 谁在场 | 通常没人 | 人到场才动 |
| 与 §3.5 放行闸的关系 | **绕过它**：开机自启不检查旧机是否仍 masked | 每次启动都经过人和闸门 |

**推荐：不 enable（保持 `disabled`）。** 三条理由，按分量排序：

1. **它偷走的是"人的那次尝试"。** §4.7 已经说明：`StartLimitBurst=1` 下，
   **人工 `systemctl start` 和自动启动共用同一个额度**。凌晨三点跳闸自动起一次失败之后，
   早上你到场想手动重试，会撞上
   `start request repeated too quickly` —— 你还得先 `reset-failed`。
   也就是说：**开机自启在你最需要那次尝试的时刻把它花掉了**，还多消耗了一次可能的登录尝试。
2. **它买到的东西大概率不存在。** 本 runbook §7.1 已经论证过同一件事的另一面：
   服务被停之后重新启动**不会**自动登录回来，`ACCOUNT` 快速登录契约会失败然后转人工扫码
   （依据：H1 §3 实测两次 `KickedOffLine` 时快速登录票据同时失效，两次 `quick_login_failed`）。
   如果这个结论对"断电重启"同样成立，那么 enable 的全部收益就是零，成本却照付。
   > **⚠️ 存疑（如实标注）**：H1 §3 的实测是**被踢**场景。
   > **"干净关机 → 开机后快速登录票据是否仍然有效"这一点本项目没有实测数据**，
   > 本 runbook 不假装知道。所以推荐 `disabled` 的理由主要靠第 1 条和第 3 条，
   > 第 2 条只是"收益存疑"，不是"收益为零"。**证伪它的方法见下面的复议条件。**
3. **它绕过 §3.5 的放行闸。** 开机自启是这套设计里**唯一**一条能在"没有人确认
   `cornna-cn` 仍然 masked"的情况下把 NapCat 拉起来的路径。跨机互斥当前靠 mask 兜底，
   够用；但把唯一的无人值守启动路径留着，等于给未来某次"临时 unmask 旧机排查问题"
   留了一个并行登录的坑。

**"那断电之后 QQ 岂不是一直不回来？"** —— 对，会一直离线到人处理。
但**这个成本的真正大头不是"自启与否"，而是"多久发现"**：H1 §3 记录的两次空窗
9.7 h / 11.7 h **全在等人发现**。压缩它的是 T3 的告警（把发现延迟从小时级压到分钟级），
不是开机自启 —— 因为**开机自启大概率也只能把你带到"等扫码"那一步**（理由 2）。

**执行**（明确不 enable，并留下可审计的证据）：

```bash
home$ systemctl is-enabled napcat-native.service
home$ systemctl show -p WantedBy,UnitFileState napcat-native.service
# Docker 回退路径把上面两条中的 unit 改为 napcat.service。
```

- **预期**：选中的 unit（native 或 Docker）是 `disabled`；`UnitFileState=disabled`；
  未选 unit 不存在，或已按 §3.1 mask。
- **如果选中 unit 已经是 `enabled`**（你或上一版 runbook 执行过）：
  `sudo systemctl disable <napcat-native.service|napcat.service>`，
  再确认 `is-enabled` 返回 `disabled`。**`disable` 不会停止正在跑的服务**，QQ 不会掉线。
- ⚠️ **`disable` ≠ `mask`。** home 的**选中运行时**只能 `disabled`；
  退役机和 home 的**未选运行时**才是 absent/masked（§3.1）。搞混会让在用路径起不来，
  或让两条本地运行时都可启动。

**复议条件（什么情况下改成 `enable`）**：当且仅当出现**实测证据**表明
「干净关机/重启后，NapCat 能靠 `ACCOUNT` 快速登录契约在无人值守下恢复在线」——
例如某次计划内重启后 journal 里出现登录成功且**没有**扫码记录。
届时把证据写进 `docs/orchestration/decisions.md`，再执行 `systemctl enable`。
**在有那份证据之前，不要因为"感觉更可用"就打开它。**

### 4.8 OneBot 配置：端口、host、以及不回显的 token 传递

目标：**Hermes 的 `ONEBOT_ACCESS_TOKEN` 一个字都不用改**，所以新机必须用**同一个 token**。
最省事也最不容易出错的办法：**把旧机那份 OneBot 配置整份搬过来，只改端口。**
（这是配置，不是设备身份 —— 见 §2.2 的界线。）

```bash
home$ sudo install -d -m 700 -o napcat-native -g napcat-native /opt/napcat-native/config-candidate
home$ umask 077
home$ ssh cornna-cn 'sudo cat /opt/napcat-native/config-candidate/onebot11_2952532060.json' \
    | sudo tee /opt/napcat-native/config-candidate/onebot11_2952532060.json >/dev/null
home$ sudo chown napcat-native:napcat-native /opt/napcat-native/config-candidate/onebot11_2952532060.json
home$ sudo chmod 600 /opt/napcat-native/config-candidate/onebot11_2952532060.json
# 只看结构，不看任何秘密值（递归剥掉 token/secret/password/key 类字段）：
home$ sudo python3 - <<'PY'
import json, pathlib
P = pathlib.Path("/opt/napcat-native/config-candidate/onebot11_2952532060.json")
SECRET_HINTS = ("token", "secret", "password", "passwd", "key", "cookie")

def strip(obj):
    """递归把任何疑似秘密的字段值换成 <redacted:len=N>，只保留结构和长度。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in k.lower() for h in SECRET_HINTS) and isinstance(v, str):
                out[k] = "<redacted:len=%d>" % len(v)
            else:
                out[k] = strip(v)
        return out
    if isinstance(obj, list):
        return [strip(x) for x in obj]
    return obj

print(json.dumps(strip(json.loads(P.read_text(encoding="utf-8"))),
                 ensure_ascii=False, indent=2))
PY
```

> **为什么不用上一版那条 `jq`**：上一版是
> `jq '{network: (.network | map_values(if type=="array" then map(del(.token)) else . end))}'` ——
> 它**只处理数组**，`network` 下任何**非数组**的值（对象形式的配置节）会被**原样打印**，
> 里面的 token 就漏出来了。上面这段 Python 是**递归**的，且对所有秘密类键名生效，
> 不依赖配置的具体形状。

- **预期**：能看到 `websocketServers` 数组，里面有 `enable` / `host` / `port` 字段，
  `token` 显示成 `<redacted:len=N>`（**长度不是秘密，它还能帮你判断 token 是不是空的**）。
- **如果 `ssh cornna-cn 'sudo cat ...'` 报文件不存在**：先
  `ssh cornna-cn 'sudo ls -l /opt/napcat-native/config-candidate/'` 看实际文件名
  （可能叫 `onebot11.json` 或带别的后缀）。
- **如果 `jq` 报 parse error**：文件传坏了（多半是 `sudo` 的密码提示混进了管道）。
  用 `ssh -t` 先做一次 sudo 预热，或配免密 sudo 后重传。**不要手工修 JSON。**

改端口和 host（**原生路径绑 loopback**）：

```bash
home$ sudo python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/opt/napcat-native/config-candidate/onebot11_2952532060.json")
cfg = json.loads(p.read_text(encoding="utf-8"))
servers = (cfg.get("network") or {}).get("websocketServers") or []
if not servers:
    raise SystemExit("REFUSE: no websocketServers entry found")
changed = []
for s in servers:
    if s.get("enable"):
        s["host"] = "127.0.0.1"      # 原生路径：只绑 loopback。Docker 路径改成 0.0.0.0
        s["port"] = 3001
        changed.append(s.get("name") or "<unnamed>")
if not changed:
    raise SystemExit("REFUSE: no enabled websocketServers entry")
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print("PATCHED=" + ",".join(changed))
PY
home$ sudo jq -r '.network.websocketServers[] | "enable=\(.enable) host=\(.host) port=\(.port) token_len=\((.token//"")|length)"' \
      /opt/napcat-native/config-candidate/onebot11_2952532060.json
```

- **预期**：`PATCHED=...`，然后 `enable=true host=127.0.0.1 port=3001 token_len=<非0>`。
- **`token_len=0`**：token 没传过来。回上一步重传。**没有 token 的话 Hermes 会被
  握手拒绝，而且症状是"连不上"，很容易被误判成隧道问题。**
- **`REFUSE: no enabled websocketServers entry`**：旧机那份配置里没有启用的 WS server。
  去 `ssh cornna-cn 'sudo jq ".network.websocketServers[].enable" ...'` 核对。

**核对 token 与 Hermes 一致（不回显值）**：

```bash
home$ sudo jq -r '[.network.websocketServers[]|select(.enable)][0].token' \
      /opt/napcat-native/config-candidate/onebot11_2952532060.json \
    | tr -d '\n' | sha256sum | cut -c1-12
corlinman-prod$ sudo awk -F= '/^ONEBOT_ACCESS_TOKEN=/{print substr($0,index($0,"=")+1)}' \
      /opt/hermes/data/.env | python3 -c 'import sys;print(sys.stdin.read().strip().strip(chr(34)+chr(39)),end="")' \
    | sha256sum | cut -c1-12
```

（`.env` 里的值可能带引号；上面那个 `python3` 只做 strip，不打印任何内容。）

- **预期**：两边 12 位十六进制**完全相同**。
- 这是**指纹，不是值**。但它仍然是敏感派生物：**只在你自己的终端里比对，
  不要粘进工单 / 聊天 / 本文档**。
- **不相同**：说明 Hermes 用的 token 和 NapCat 配的不是一个。
  **不要改 Hermes 的 `.env`**（那会牵出 §1.3 的两处同步问题），
  而是把 NapCat 的 token 改成 Hermes 那个值 —— 用下面这段，全程不回显：
  ```
  home$ umask 077
  home$ ssh corlinman-prod 'sudo awk -F= "/^ONEBOT_ACCESS_TOKEN=/{print substr(\$0,index(\$0,\"=\")+1)}" /opt/hermes/data/.env' \
      | tr -d '\r\n' | sudo tee /root/.t4-token >/dev/null
  home$ if sudo test -s /root/.t4-token; then echo TOKEN_FETCHED=true; \
        else echo TOKEN_FETCHED=false; false; fi
  home$ sudo python3 -c 'import json,pathlib,sys;\
p=pathlib.Path("/opt/napcat-native/config-candidate/onebot11_2952532060.json");\
t=pathlib.Path("/root/.t4-token").read_text().strip();\
c=json.loads(p.read_text());\
[s.__setitem__("token",t) for s in c["network"]["websocketServers"] if s.get("enable")];\
p.write_text(json.dumps(c,ensure_ascii=False,indent=2));print("TOKEN_SYNCED=true")'
  home$ sudo shred -u /root/.t4-token && echo TEMP_TOKEN_WIPED=true
  ```

**权限收口**（H1 §8.1 的修复，照抄）：

```bash
home$ sudo find /opt/napcat-native/config-candidate -type d -exec chmod 700 {} +
home$ sudo find /opt/napcat-native/config-candidate -type f -exec chmod 600 {} +
home$ sudo chown -R napcat-native:napcat-native /opt/napcat-native/config-candidate
home$ sudo -u nobody test -r /opt/napcat-native/config-candidate/onebot11_2952532060.json \
    && echo NOBODY_CAN_READ=true || echo NOBODY_CAN_READ=false
```

- **预期**：`NOBODY_CAN_READ=false`。
- **`true`**：权限没收住，重跑上面三条。H1 §8.1 记录过权威机上
  `webui.json` 曾经 644、非 root 用户可读 —— 别在新机上重演。

### 4.9 WebUI：只绑 loopback，绝不上公网

```bash
home$ sudo ss -ltnp | grep -E ':(3001|6099)\b'
```

- **预期**（服务起来之后）：两行都是 `127.0.0.1:3001` / `127.0.0.1:6099`，
  **绝不能出现 `0.0.0.0:` 或 `*:`**。
- **出现 `0.0.0.0:6099`**：WebUI 暴露在家庭 LAN 上了。WebUI token 能控制这个 QQ 号
  （改配置、重启 QQ）。立刻改回 loopback。
- **不要给 WebUI 套 nginx + `auth_basic`。** H1 §5.1 踩过：二维码刷新走 WebSocket，
  浏览器不会给 WS 升级请求自动附带 Basic 凭据，结果是反复弹认证框。

访问方式（三选一，都不需要在家里开入站端口）：

1. 家里那台机器有显示器：直接开 `http://127.0.0.1:6099/webui/`。
2. 你在同一个 LAN：`ssh -L 6099:127.0.0.1:6099 home`，然后本机开 `http://127.0.0.1:6099/webui/`。
3. **你不在家**：走 §5.5 的 WebUI 反向端口，
   `ssh -L 6099:127.0.0.1:6100 corlinman-prod`，同样开 `http://127.0.0.1:6099/webui/`。

#### WebUI 凭据：只验证"存在且非空"，**不打印**

> ### ⛔ 上一版这里让你把 token 打到终端 —— 已删除
>
> 上一版写的是 `sudo jq -r '.token // .webuiToken // empty' .../webui.json`，
> 以及"空输出就 `sudo jq . webui.json` 看全貌"这条兜底。
> **两条都会把凭据值输出到终端**，直接违反硬约束 10（token / 密码 / cookie 的值
> **不得出现在文档、命令行或输出里**）。
>
> 文档里那句"⛔ 不要粘进聊天"**不算数**：**"打印到终端"本身就是被禁止的"输出"**。
> shell scrollback、终端录制（`script`/`asciinema`）、远程会话日志、
> 屏幕共享、以及任何采集 stdout 的运维工具都会留下它。
> 拿到 WebUI token 的人可以**控制这个 QQ 号**：改配置、重启 QQ、发起登录。

下面这段**只判断字段是否存在且非空，不输出任何值**：

```bash
home$ sudo python3 - <<'PY'
import json, pathlib, sys
p = pathlib.Path("/opt/napcat-native/config-candidate/webui.json")
if not p.exists():
    print("WEBUI_JSON_PRESENT=false"); sys.exit(1)
print("WEBUI_JSON_PRESENT=true")
print("WEBUI_JSON_MODE=%o" % (p.stat().st_mode & 0o777))
try:
    cfg = json.loads(p.read_text(encoding="utf-8"))
except Exception as exc:
    print("WEBUI_JSON_PARSE_OK=false reason=" + type(exc).__name__); sys.exit(1)
print("WEBUI_JSON_PARSE_OK=true")
# 只看键名和"非空"，绝不打印值
keys = [k for k in cfg if "token" in k.lower() or "secret" in k.lower() or "key" in k.lower()]
print("WEBUI_CRED_KEYS=" + (",".join(sorted(keys)) or "<none>"))
present = any(isinstance(cfg.get(k), str) and cfg.get(k).strip() for k in keys)
print("WEBUI_TOKEN_PRESENT=%s" % str(present).lower())
sys.exit(0 if present else 1)
PY
home$ echo "rc=$?"
```

- **预期**：
  ```text
  WEBUI_JSON_PRESENT=true
  WEBUI_JSON_MODE=600
  WEBUI_JSON_PARSE_OK=true
  WEBUI_CRED_KEYS=token
  WEBUI_TOKEN_PRESENT=true
  rc=0
  ```
  （`WEBUI_CRED_KEYS` 打印的是**键名**，不是值 —— 键名不是秘密，它只告诉你该去哪个字段找。）
- **`WEBUI_JSON_PRESENT=false`**：NapCat 还没首次启动过，这个文件是它自己生成的。
  先做 §6.1 启动，再回来跑这段。
- **`WEBUI_TOKEN_PRESENT=false`** 或 `WEBUI_CRED_KEYS=<none>`：字段名和预期不同。
  **不要**去 `jq .` 打印整个文件。改用上面这段脚本的 `WEBUI_CRED_KEYS` 输出定位键名；
  仍然找不到就看**键的结构**（同样不打印值）：
  ```bash
  home$ sudo python3 -c 'import json;print(sorted(json.load(open("/opt/napcat-native/config-candidate/webui.json")).keys()))'
  ```
- **`WEBUI_JSON_MODE` 不是 `600`**：`sudo chmod 600 .../webui.json`。
  H1 §8.1 记录过权威机上 `webui.json` 曾经 644、非 root 可读 —— 别在新机上重演。

**那怎么拿到 token 去登录 WebUI？** —— **不经过终端**：

1. **首选：从本机浏览器直接读文件。** 按 §4.9 的三种访问方式打开 WebUI 页面，
   需要 token 时用编辑器（`sudoedit`）打开 `webui.json`，**从编辑器里选中复制**，
   粘贴进浏览器。编辑器缓冲区不进 shell scrollback。
2. **把它存进你的密码管理器**（1Password / Bitwarden / `pass` 等），
   之后从密码管理器填充。这台机器的 WebUI token 应该有一条自己的条目。
3. ⛔ **不要**用 `cat` / `jq` / `echo` / `grep` 把它打到终端。
   ⛔ **不要**把它写进本 runbook、执行记录、工单或聊天（H1 §6 明令）。
   ⛔ **不要**从 journal 里 grep 它：
   `journalctl -u napcat-native.service | grep -i webui` 可能把首启动打印的
   **带 token 的 WebUI URL** 整行打出来。要看 WebUI 是否就绪，用 §4.9 开头的 `ss -ltnp` 查 6099 监听即可。

---

## 5. 隧道的失败策略

家宽和机房最大的区别不是带宽，是**链路会周期性地消失**：运营商重拨、路由器重启、
跳闸、光猫过热重启、凌晨的线路维护。所以这条隧道的设计目标不是"别断"，而是
**"断了之后不用人管也能回来，而且回得来"**。

H1 §8.2 已经为反方向的隧道踩过一次坑并修正了参数；这里照抄它的结论，再针对家宽加三条。

### 5.1 `corlinman-prod` 侧准备

#### 5.1.1 专用低权限账号（同时兑现 H1 §10 的一条遗留）

H1 §10 记着："SSH tunnel 使用现有 root `napxfer` key……后续可把该 key 收紧为专用低权限账号和
`permitopen=127.0.0.1:3011`"。方向反过来之后，家里那台机器要**主动连进 prod**，
这是一条**新增的入站凭据**，正好一次做对。

```bash
corlinman-prod$ sudo useradd --system --create-home --home-dir /home/onebot-tunnel \
      --shell /usr/sbin/nologin onebot-tunnel
corlinman-prod$ sudo install -d -m 700 -o onebot-tunnel -g onebot-tunnel /home/onebot-tunnel/.ssh
corlinman-prod$ id onebot-tunnel
corlinman-prod$ ssh -V
```

- **预期**：`id` 只显示它自己的组（**不能**出现 `sudo` / `wheel` / `docker` / `adm`）。
- **`ssh -V` 记下 OpenSSH 版本**：下一步的 `permitlisten` 需要 **OpenSSH ≥ 7.8**。
- **`useradd` 报 already exists**：确认它不是别的用途在用的账号，再决定复用还是换名字。
- **`--shell /usr/sbin/nologin` 会不会让隧道失败？** 不会。`ssh -N` 根本不请求会话通道，
  sshd 不会去执行这个用户的 shell。这一点在 §5.6 的演练里会被实际验证。

#### 5.1.2 `authorized_keys`：把这把 key 锁死成"只能开这一个反向端口"

先在**家里那台机器**上生成密钥（私钥永远不离开家里那台机器）。

**前置**：`obtun` 用户和同名私有组已在 **§2.4.1** 建好。先验证，再建目录 ——
用户/组不存在时 `install -d -o obtun -g obtun` 会直接失败：

```bash
home$ id obtun && getent group obtun || { echo "MISSING_OBTUN — 回 §2.4.1"; }
home$ sudo install -d -m 700 -o obtun -g obtun /etc/onebot-rtunnel
home$ sudo -u obtun ssh-keygen -t ed25519 -N '' -C 'onebot-rtunnel@home' \
      -f /etc/onebot-rtunnel/id_ed25519
home$ sudo cat /etc/onebot-rtunnel/id_ed25519.pub
```

- **预期**：`id obtun` 打印 `uid=NNN(obtun) gid=NNN(obtun) groups=NNN(obtun)`；
  `getent group obtun` 打印一行且成员列表为空；
  `install -d` 无输出；`ssh-keygen` 生成两个文件；最后打印一行 `ssh-ed25519 AAAA...` 公钥。
- **`MISSING_OBTUN`** ⇒ 回 §2.4.1 建用户和组，**不要**在这里临时 `useradd` ——
  §2.4.1 有 `--user-group` 和组集合检查，临时建容易漏掉私有组。

把上面那行公钥贴到 prod，**前面必须带这一串限定**：

```bash
corlinman-prod$ sudo -u onebot-tunnel tee -a /home/onebot-tunnel/.ssh/authorized_keys >/dev/null <<'AK'
restrict,port-forwarding,permitopen="127.0.0.1:1",permitlisten="127.0.0.1:3011",permitlisten="127.0.0.1:6100" ssh-ed25519 AAAA...把上面那行公钥原样贴这里... onebot-rtunnel@home
AK
corlinman-prod$ sudo chmod 600 /home/onebot-tunnel/.ssh/authorized_keys
corlinman-prod$ sudo chown onebot-tunnel:onebot-tunnel /home/onebot-tunnel/.ssh/authorized_keys
corlinman-prod$ sudo awk '{print $1}' /home/onebot-tunnel/.ssh/authorized_keys
```

每一段的作用（**这就是"这把 key 能干什么"的完整答案**）：

| 选项 | 作用 |
|---|---|
| `restrict` | 关掉**一切**：no-pty、no-agent-forwarding、no-X11、no-port-forwarding、no-user-rc |
| `port-forwarding` | 在 `restrict` 之上**只**把转发能力加回来 |
| `permitopen="127.0.0.1:1"` | 正向（`-L`）转发只允许到一个没人监听的端口 ⇒ **等于禁用正向转发**。OpenSSH 没有"只允许反向"的开关，这是标准写法 |
| `permitlisten="127.0.0.1:3011"` | 反向（`-R`）**只**允许监听这一个端口 |
| `permitlisten="127.0.0.1:6100"` | §5.5 的 WebUI 端口。**不用 §5.5 就删掉这一段** |

- **预期**：最后那条 `awk` 打印出的第一列就是上面那串 `restrict,...`，说明限定没被写漏。
- **`permitlisten` 在 OpenSSH < 7.8 上会导致这把 key 直接失效**（sshd 报
  `Unknown key option`，整行被忽略）。如果 §5.1.1 查出版本低于 7.8：
  去掉两个 `permitlisten`，只保留 `restrict,port-forwarding,permitopen="127.0.0.1:1"`，
  并在执行记录里写明"这台 prod 的 OpenSSH 太老，反向端口未被限定"。
- **验证限定真的生效**（在 §5.2 隧道跑起来之后做）。

  > ⚠️ 上一版这条验证命令**会挂住，而且可能给出反向的结论**：
  > 它没加 `-o ExitOnForwardFailure=yes` —— 而本 runbook §5.3 自己已经写明
  > **没有这个选项时，`-R` 转发失败 ssh 也会保持连接不退出**。
  > 于是命令会一直挂在前台，你按 Ctrl-C 之后很可能把
  > 「ssh 还活着」误读成「限制没生效」。它也没有预检 9999 是否已被占用。

  **先挑一个确认没人用的临时端口**：

  ```bash
  home$ PORT=9999
  home$ ssh corlinman-prod "ss -ltnH '( sport = :$PORT )' | grep -c ." 
  ```

  - **预期**：`0`（端口空闲）。**不是 0** ⇒ 换一个端口（`PORT=9998`…）重试，
    否则你会把"端口被占"误判成"`permitlisten` 生效"。

  **再跑验证（三个关键选项一个都不能少）**：

  ```bash
  home$ sudo -u obtun timeout 20 ssh -F /dev/null -i /etc/onebot-rtunnel/id_ed25519 \
        -o IdentitiesOnly=yes -o BatchMode=yes \
        -o UserKnownHostsFile=/etc/onebot-rtunnel/known_hosts -o StrictHostKeyChecking=yes \
        -o ExitOnForwardFailure=yes -o ConnectTimeout=10 \
        -N -R 127.0.0.1:$PORT:127.0.0.1:22 onebot-tunnel@<PROD_HOST>
  home$ echo "rc=$?"
  home$ ssh corlinman-prod "ss -ltnH '( sport = :$PORT )' | grep -c ."
  ```

  - **预期（这就是"限制生效"的样子）**：
    输出 `Warning: remote port forwarding failed for listen port 9999`，
    **`rc` 非 0 且不是 124**，最后那条端口计数是 **`0`**。
  - **三个选项各自的作用**：
    | 选项 | 没有它会怎样 |
    |---|---|
    | `-o ExitOnForwardFailure=yes` | 转发被拒后 ssh **不退出**，命令挂住，你会以为是"卡了" |
    | `-o ConnectTimeout=10` | 网络黑洞时挂到内核超时（可达 2 分钟） |
    | `timeout 20` | 兜底：无论如何 20 秒后收回终端 |
  - **`rc=124`** ⇒ `timeout` 强杀，说明 ssh 既没成功也没失败退出 ——
    **`ExitOnForwardFailure` 没生效**（多半是拼写错误或 ssh 太老）。修好再判。
  - **`rc=0` 或最后那条端口计数是 `1`** ⇒ **`permitlisten` 没生效，9999 真的转发成功了。**
    回去检查 `authorized_keys` 那一行有没有被换行断开（`awk '{print $1}'` 那条应打印出完整的
    `restrict,port-forwarding,permitopen=...,permitlisten=...` 串），
    以及 §5.1.1 查到的 OpenSSH 版本是否 ≥ 7.8。
  - **两个判据要一起看**：只看 `rc` 不够（ssh 可能因别的原因退出），
    只看端口计数也不够（可能是别的东西占着）。**`rc` 非 0 **且** 端口计数为 0** 才算通过。

#### 5.1.3 sshd：`ClientAlive*`（**这一条是家宽场景的关键，漏了会很难查**）

家宽掉线时，家里那台机器的 ssh 进程是"人间蒸发"式消失的 —— 它来不及发 FIN。
prod 上的 sshd 不知道对端没了，**会继续持有 `127.0.0.1:3011` 这个监听 socket**。
等家里网络恢复、systemd 重新拉起隧道时，新的 `-R` 会撞上这个"僵尸监听"：

```
Warning: remote port forwarding failed for listen port 3011
```

加了 `ExitOnForwardFailure=yes` 之后 ssh 会立刻退出，systemd 30 秒后重试，**再次失败** ——
于是隧道进入一个可能持续几十分钟到几小时的**死循环**，直到 sshd 的默认 TCP keepalive
终于收掉那个旧会话。而这段时间里 Hermes 那边看到的是"3011 连不上"，
症状和"家里断电了"完全一样，**极难区分**。

治本的办法是让 sshd 主动收尸：

```bash
corlinman-prod$ sudo cp -a /etc/ssh/sshd_config \
      /etc/ssh/sshd_config.bak-pre-T4-$(date -u +%Y%m%dT%H%M%SZ)
corlinman-prod$ sudo tee /etc/ssh/sshd_config.d/60-onebot-rtunnel.conf >/dev/null <<'SSHD'
# T4: 家宽反向隧道。对端可能"人间蒸发"，sshd 必须主动探活并回收陈旧的 -R 监听，
# 否则重连会一直撞 "remote port forwarding failed for listen port 3011"。
ClientAliveInterval 30
ClientAliveCountMax 3
# 反向端口只绑 loopback（默认值，写出来是为了防止将来有人手滑改成 yes）
GatewayPorts no
SSHD
corlinman-prod$ sudo sshd -t && echo SSHD_CONFIG_OK
corlinman-prod$ sudo systemctl reload sshd
corlinman-prod$ sudo sshd -T | grep -E '^(clientaliveinterval|clientalivecountmax|gatewayports)'
```

- **预期**：`SSHD_CONFIG_OK`；最后一条打印
  `clientaliveinterval 30` / `clientalivecountmax 3` / `gatewayports no`。
- **如果这台 prod 的 sshd 不支持 `/etc/ssh/sshd_config.d/`**（老版本没有 `Include`）：
  `sudo sshd -T | grep -c include` 为 0 时，把那三行直接追加到 `sshd_config` 末尾。
  **改之前那份 `.bak-pre-T4-*` 备份必须已经存在。**
- **⚠️ `reload` 不会踢掉你当前这个 SSH 会话**（`restart` 也不会踢已建立的连接，
  但 `reload` 更稳）。**先 `sshd -t` 通过再 reload**，配置写错 + restart = 把自己锁在门外。
- **90 秒后旧会话才会被回收**（30×3）。这就是家宽掉线后隧道恢复的下限延迟，可以接受。

### 5.2 家里那台机器：`onebot-rtunnel.service`

```bash
home$ export PROD_HOST='<填 corlinman-prod 的真实 SSH 地址>'
home$ bash -s <<'GATE'
set -uo pipefail
case "$PROD_HOST" in ''|*' '*|*'<'*|*'>'*) echo "HOSTKEY_GATE=BLOCK reason=invalid_prod_host"; exit 1 ;; esac
TMP=$(mktemp); ERR=${TMP}.err; trap 'rm -f "$TMP" "$ERR"' EXIT
if ! ssh-keyscan -t ed25519,rsa "$PROD_HOST" > "$TMP" 2> "$ERR"; then
  echo "HOSTKEY_GATE=BLOCK reason=ssh_keyscan_failed"; head -3 "$ERR"; exit 1
fi
[ -s "$TMP" ] || { echo "HOSTKEY_GATE=BLOCK reason=empty_keyscan"; exit 1; }
sudo install -m 644 -o obtun -g obtun "$TMP" /etc/onebot-rtunnel/known_hosts \
  || { echo "HOSTKEY_GATE=BLOCK reason=install_failed"; exit 1; }
echo "HOSTKEY_GATE=PASS"
GATE
home$ sudo ssh-keygen -lf /etc/onebot-rtunnel/known_hosts
```

- **必须先看到 `HOSTKEY_GATE=PASS`**；keyscan 失败或空输出都阻断，不能生成空 known_hosts 后继续。
- **把这个指纹和你笔记本上已知的对照**：
  `ssh-keygen -lf ~/.ssh/known_hosts | grep <PROD_HOST>`（或
  `ssh-keygen -F <PROD_HOST> -l`）。
- **⚠️ 两个指纹不一致就停下。** 那意味着中间人，或者你 keyscan 错了主机。
  在指纹核对通过之前不要启动隧道。**不要**用 `StrictHostKeyChecking=no` 绕过去 ——
  那等于把这条承载 QQ 全部流量的隧道交给任何能劫持路由的人。

#### 5.2.1 Hermes 探针守门：查询**真正宿主**的只读状态通道

> ### ⛔ 不能让 home 探针检查 home 本机的 `hermes.service`
>
> Hermes 跑在 `corlinman-prod`。home 本机没有这个 unit 是正常的，绝不等于 Hermes 已停。
> 所以探针需要一条独立的、只能查询 `hermes.service` 是否 active 的 SSH key：
> **查询成功且明确 rc=3（inactive/failed）才允许建 OneBot WS；active(rc=0)、其他 rc、SSH 失败、
> 超时、命令缺失全部拒绝。** 这条 key 不允许 shell、端口转发或其他命令。

先在 prod 建只读查询账号。它需要 `/bin/sh`，因为 sshd 执行 forced command 时会经用户 shell `-c`；
安全边界由密码锁定 + `authorized_keys` 的 `restrict,command=...` 提供：

```bash
corlinman-prod$ sudo useradd --system --user-group --create-home \
      --home-dir /home/hermes-probe-guard --shell /bin/sh hermes-probe-guard
corlinman-prod$ sudo passwd -l hermes-probe-guard
corlinman-prod$ sudo install -d -m 700 -o hermes-probe-guard -g hermes-probe-guard \
      /home/hermes-probe-guard/.ssh
corlinman-prod$ id hermes-probe-guard
corlinman-prod$ command -v systemctl
```

- **预期**：账号只有同名私有组，密码为 locked；`command -v systemctl` 输出 `/usr/bin/systemctl`。
- **如果实际路径不是 `/usr/bin/systemctl`**：下面 forced command 使用实际绝对路径，不能依赖 PATH。

在 home 生成**独立** key（不能复用隧道 key；两把 key 的权限完全不同）：

```bash
home$ sudo install -d -m 700 -o root -g root /etc/hermes-probe-guard
home$ sudo ssh-keygen -t ed25519 -N '' -C 'hermes-probe-guard@home' \
      -f /etc/hermes-probe-guard/id_ed25519
home$ sudo chmod 600 /etc/hermes-probe-guard/id_ed25519
home$ sudo cp /etc/onebot-rtunnel/known_hosts /etc/hermes-probe-guard/known_hosts
home$ sudo chmod 644 /etc/hermes-probe-guard/known_hosts
home$ sudo env PROD_HOST="$PROD_HOST" sh -c 'printf "%s\n" "$PROD_HOST" > /etc/hermes-probe-guard/prod-host'
home$ sudo chmod 644 /etc/hermes-probe-guard/prod-host
home$ sudo grep -Fx "$PROD_HOST" /etc/hermes-probe-guard/prod-host
home$ sudo cat /etc/hermes-probe-guard/id_ed25519.pub
```

把最后一行公钥贴到 prod；整行必须只有这个 forced command：

```bash
corlinman-prod$ sudo -u hermes-probe-guard tee \
      /home/hermes-probe-guard/.ssh/authorized_keys >/dev/null <<'AK'
restrict,command="/usr/bin/systemctl is-active --quiet hermes.service" ssh-ed25519 AAAA...把上面那行公钥原样贴这里... hermes-probe-guard@home
AK
corlinman-prod$ sudo chmod 600 /home/hermes-probe-guard/.ssh/authorized_keys
corlinman-prod$ sudo chown hermes-probe-guard:hermes-probe-guard \
      /home/hermes-probe-guard/.ssh/authorized_keys
corlinman-prod$ sudo sed -E 's/ ssh-ed25519 .*/ <public-key-redacted>/' \
      /home/hermes-probe-guard/.ssh/authorized_keys
```

- **预期**：输出完整的 `restrict,command="/usr/bin/systemctl is-active --quiet hermes.service"`
  后跟 `<public-key-redacted>`；没有 `port-forwarding`、`permitopen`、`permitlisten`。
- forced command 忽略客户端提交的任何命令，只执行那条只读 `systemctl is-active --quiet`。

在迁移前（此时 Hermes 仍 active）验证查询通道。**这里 rc=0 才是预期**，因为它证明查到的确实是
prod 上正在运行的 Hermes；不是在证明“可以开探针”：

```bash
home$ sudo ssh -F /dev/null -i /etc/hermes-probe-guard/id_ed25519 \
      -o IdentitiesOnly=yes -o BatchMode=yes \
      -o UserKnownHostsFile=/etc/hermes-probe-guard/known_hosts \
      -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
      hermes-probe-guard@"$PROD_HOST"
home$ echo "HERMES_GUARD_CHANNEL_ACTIVE_rc=$?"
```

- **预期（迁移前 Hermes active）**：无状态文本输出，`HERMES_GUARD_CHANNEL_ACTIVE_rc=0`。
- **rc=3**：查询通道可用，但 Hermes 此刻明确 inactive/failed；先确认这是否符合当前步骤。
- **其他 rc（尤其 255）**：状态不可确认，**R2 守门未就绪，迁移不得开始**。
- §3.4 停 Hermes 后，同一 forced command 会返回 rc=3；§6.3 的探针只在这一种返回码下放行。


#### 5.2.2 `onebot-rtunnel.service`

```bash
home$ sudo chown -R obtun:obtun /etc/onebot-rtunnel
home$ sudo chmod 700 /etc/onebot-rtunnel
home$ sudo chmod 600 /etc/onebot-rtunnel/id_ed25519
home$ sudo chmod 644 /etc/onebot-rtunnel/known_hosts
home$ sudo tee /etc/systemd/system/onebot-rtunnel.service >/dev/null <<'UNIT'
[Unit]
Description=Reverse SSH tunnel: corlinman-prod 127.0.0.1:3011 -> local NapCat OneBot 3001
Documentation=file:///opt/hermes/docs/orchestration/tasks/T4-home-migration-runbook.md
After=network-online.target
Wants=network-online.target

# 禁用启动限流：家宽会长时间断网。若保留限流，连续失败会耗尽启动额度后
# 永久停在 failed，必须人工 reset —— 这正是 H1 §8.2 修掉的那个缺陷。
# 隧道不消耗任何 QQ 登录尝试，所以这里的取舍与 napcat-native.service 正好相反。
StartLimitIntervalSec=0
StartLimitBurst=0

[Service]
Type=simple
User=obtun
Group=obtun

# Restart=always 而不是 on-failure：ssh 在某些"对端优雅关闭"路径下以 0 退出，
# on-failure 会让隧道静静地停在 inactive(dead) 且没有任何告警。见 runbook §5.3。
Restart=always
RestartSec=30

ExecStart=/usr/bin/ssh -N -T \
  -F /dev/null \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o IdentityFile=/etc/onebot-rtunnel/id_ed25519 \
  -o UserKnownHostsFile=/etc/onebot-rtunnel/known_hosts \
  -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=10 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=20 \
  -o ServerAliveCountMax=3 \
  -o TCPKeepAlive=yes \
  -o Compression=no \
  -R 127.0.0.1:3011:127.0.0.1:3001 \
  -R 127.0.0.1:6100:127.0.0.1:6099 \
  onebot-tunnel@PROD_HOST_PLACEHOLDER

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
UNIT
home$ test -n "${PROD_HOST:-}" && echo PROD_HOST_SET=true   # 复用上面 HOSTKEY_GATE 已验证的值
home$ sudo env PROD_HOST="$PROD_HOST" python3 - <<'PY'
import os, pathlib
p = pathlib.Path("/etc/systemd/system/onebot-rtunnel.service")
host = os.environ.get("PROD_HOST", "")
if not host or any(c.isspace() for c in host) or any(c in host for c in "<>"):
    raise SystemExit("RTUNNEL_UNIT_GATE=BLOCK reason=invalid_prod_host")
text = p.read_text(encoding="utf-8")
if text.count("PROD_HOST_PLACEHOLDER") != 1:
    raise SystemExit("RTUNNEL_UNIT_GATE=BLOCK reason=placeholder_count_not_one")
tmp = p.with_name(p.name + ".tmp-T4")
tmp.write_text(text.replace("PROD_HOST_PLACEHOLDER", host), encoding="utf-8")
os.chmod(tmp, p.stat().st_mode & 0o777)
os.replace(tmp, p)
print("PROD_HOST_REPLACED=true")
PY
home$ sudo bash -s <<'GATE'
set -uo pipefail
F=/etc/systemd/system/onebot-rtunnel.service
if grep -q 'PROD_HOST_PLACEHOLDER' "$F"; then
  echo "RTUNNEL_UNIT_GATE=BLOCK reason=placeholder_present"; exit 1
else
  rc=$?
  [ "$rc" -eq 1 ] || { echo "RTUNNEL_UNIT_GATE=BLOCK reason=grep_failed rc=$rc"; exit 1; }
fi
echo "RTUNNEL_UNIT_GATE=PASS"
GATE
home$ sudo systemd-analyze verify /etc/systemd/system/onebot-rtunnel.service && echo UNIT_VERIFY_OK
home$ sudo systemctl daemon-reload
home$ systemctl show -p Restart,RestartUSec,StartLimitIntervalUSec,StartLimitBurst onebot-rtunnel.service
```

- **预期**：`PROD_HOST_REPLACED=true`、`RTUNNEL_UNIT_GATE=PASS`、`UNIT_VERIFY_OK`；
  任何 placeholder 查询错误也阻断，不再用 `grep ... || echo PLACEHOLDER_REPLACED` 把“查不了”伪装成已替换。
  `Restart=always`、`RestartUSec=30s`、`StartLimitIntervalUSec=0`。
- **⚠️ `StartLimitIntervalUSec` 必须是 `0`**。如果显示 `10s`，同样是被写进 `[Service]` 段
  然后被静默忽略了（H1 §4.1 那个坑，这里再犯一次）。移到 `[Unit]` 段。
- **`MemoryDenyWriteExecute=yes` 在这里是安全的**（ssh 不是 Electron），
  但**绝不能**照抄到 `napcat-native.service` 上 —— 见 §4.6。
- **不带 `-R ...:6100:...` 的话**（不用 §5.5），把那一行删掉，同时删掉 §5.1.2 里对应的
  `permitlisten="127.0.0.1:6100"`。

启动并验证：

```bash
home$ sudo systemctl enable --now onebot-rtunnel.service
home$ systemctl is-active onebot-rtunnel.service
home$ systemctl show -p NRestarts onebot-rtunnel.service
corlinman-prod$ sudo ss -ltnp | grep -E '127\.0\.0\.1:(3011|6100)\b'
```

- **预期**：`active`；`NRestarts=0`；prod 上两行监听，**都是 `127.0.0.1:`**，
  进程是 `sshd: onebot-tunnel`。
- **⚠️ 出现 `0.0.0.0:3011` 或 `*:3011`**：`GatewayPorts` 被改成了 `yes`/`clientspecified`，
  **QQ 的 OneBot 接口正暴露在公网上**。立刻 `sudo systemctl stop onebot-rtunnel`（在 home 上），
  回 §5.1.3 把 `GatewayPorts no` 加回去。
- **`journalctl` 出现 `remote port forwarding failed for listen port 3011`**：
  1. 先确认 §3.4 的旧 `onebot-tunnel.service` 真的停了：
     `ssh corlinman-prod 'systemctl is-active onebot-tunnel.service'` → 必须 `inactive`；
  2. 再确认不是僵尸监听：`ssh corlinman-prod 'sudo ss -ltnp | grep 3011'`，
     如果 owner 是一个老的 `sshd:` 会话，等 90 秒（§5.1.3 的 `ClientAlive` 会收掉它）后自愈；
  3. 都不是的话，`permitlisten` 写错了 —— 回 §5.1.2 的验证步骤。
- **`journalctl` 出现 `Permission denied (publickey)`**：`authorized_keys` 那行贴断了，
  或者 `/home/onebot-tunnel/.ssh` 权限不对（必须 700，`authorized_keys` 必须 600、属主正确）。
  在 prod 上 `sudo journalctl -u ssh -n 30` 看 sshd 侧的原因，比客户端报错清楚得多。

### 5.3 参数逐条理由（对照 H1 §8.2）

| 参数 | 值 | 来源 | 为什么 |
|---|---|---|---|
| `RestartSec` | **30s** | **照抄 H1 §8.2** | 原值 5s。5 秒重试对一条要跨公网建 TCP + SSH 握手的隧道太急，断网时每分钟 12 次无谓握手 |
| `StartLimitIntervalSec` | **0** | **照抄 H1 §8.2** | 原值 600s + Burst 8。持续断网会在 10 分钟内耗尽 8 次额度，之后**永久停在 failed**，必须人工 `systemctl reset-failed` 才能恢复 —— 家宽的断网经常超过 10 分钟，这个组合等于"断网超过 10 分钟就要人去手动救"。设 0 即禁用限流 |
| `ConnectTimeout` | **10** | **照抄 H1 §8.2** | 原值：无。没有它，网络黑洞时 ssh 会挂在 TCP 连接上直到内核默认超时（可达 2 分钟以上），`Restart` 完全用不上 |
| `Restart` | **always** | **本次修改（对 H1 加强）** | H1 用 `on-failure`。ssh 在"对端优雅关闭连接"时可能以 **0** 退出，`on-failure` 下 unit 会安静地停在 `inactive (dead)`，**没有失败状态、没有日志、没有告警** —— 你只有在 QQ 半天没反应时才会发现。隧道不消耗任何 QQ 登录尝试，可用性优先。理由见 §5.3.1 |
| `ServerAliveInterval` | **20** | **本次新增** | 20s×3 = 60 秒内发现链路死亡并让 ssh 非零退出。没有它，一条"半开"的 TCP 可以维持很久，Hermes 会以为 3011 通着 |
| `ServerAliveCountMax` | **3** | **本次新增** | 同上。别调太小：家宽偶发 20 秒级抖动很常见，`1` 会导致无谓重连 |
| `ExitOnForwardFailure` | **yes** | **本次新增** | **最重要的一条**。没有它，`-R` 转发失败时 ssh 会**保持连接不退出**，systemd 显示 `active (running)`，而 prod 上的 3011 根本没有监听 —— "隧道显示正常但完全不通"，这是最难查的一类故障 |
| `-F /dev/null` | — | **本次新增** | 忽略用户级和系统级 `ssh_config`。防止有人某天在 `/etc/ssh/ssh_config` 里加一条 `ProxyCommand` 或 `StrictHostKeyChecking no` 就悄悄改变了这条隧道的行为 |
| `-T` | — | 本次新增 | 明确不要 pty。配合 `restrict` 的 no-pty，双保险 |
| `Compression=no` | — | 本次新增 | OneBot 是小 JSON 帧，压缩只增加 CPU 和延迟 |

#### 5.3.1 `Restart=always` 是有意偏离 H1 —— 为什么这里是对的

H1 §8.4 的教训是"**不要有第二个会消耗登录尝试的生命周期管理器**"。
判断一个自动重启是否危险，标准只有一条：**它会不会产生 QQ 登录尝试。**

| 对象 | 自动重启会不会产生登录尝试 | 本 runbook 的策略 |
|---|---|---|
| `napcat-native.service` | **会**（每次启动都可能触发一次登录/快速登录） | §4.7：`StartLimitBurst=1` ⇒ **实际上是人工闸** |
| `onebot-rtunnel.service` | **不会**（只是一条 TCP + SSH 公钥认证） | `Restart=always` + 30s 退避 |

两者策略相反不是不一致，而是**同一条原则的正确应用**。
把这条写进 unit 注释里（上面已写），免得后来人"统一一下风格"把它们改成一样的。

### 5.4 家宽特有的三种断法，以及各自的自愈路径

| 断法 | 现象 | 自愈路径 | 需要人吗 |
|---|---|---|---|
| **运营商重拨 / 换 IP** | ssh 连接静默死亡，家里公网 IP 变了 | `ServerAliveInterval` 60 秒内察觉 → ssh 非零退出 → 30 秒后重连（**出方向不关心自己的 IP 变没变**） → prod 侧旧监听由 `ClientAlive` 在 90 秒内回收 | **不需要**。最坏约 2 分钟 |
| **路由器重启 / 断网 10 分钟以上** | ssh 反复 `ConnectTimeout` | `StartLimitIntervalSec=0` ⇒ 每 30 秒重试一次，**无限重试，永不锁死** | **不需要** |
| **家里断电** | 机器关机 | 来电后 BIOS 自启（§4.1）→ systemd 拉起**隧道**（`onebot-rtunnel` 是 `enabled`）→ **NapCat 不会自己起来**（它是 `disabled`，见 §4.7.1）⇒ 需要人 `start` + 扫码 | **需要人到场**（这是 §0.5 那条风险的实际场次） |

**注意最后一行**：断电重启之后，隧道会自己回来，但 QQ 不会。你会得到
"链路在线、账号 auth_required"这个组合 —— 正是 `plugins/platforms/onebot/README.md` 里
"Transport online is not QQ-account online"描述的状态，也正是 T3 的 `A1_AUTH_REQUIRED` 告警要覆盖的。

**验证 IP 变化不会打断隧道**（可选但推荐，在正式迁移前做一次）：

```bash
home$ systemctl show -p NRestarts onebot-rtunnel.service          # 记下当前值 N0
# 然后手动重启一次家里的路由器 / 拔掉 WAN 口 90 秒再插回
home$ journalctl -u onebot-rtunnel.service -n 40 --no-pager
home$ systemctl is-active onebot-rtunnel.service
home$ systemctl show -p NRestarts onebot-rtunnel.service          # 应为 N0 + 1 或更多
corlinman-prod$ sudo ss -ltn | grep -c '127.0.0.1:3011'
```

- **预期**：`active`；`NRestarts` 增加；prod 上 3011 计数回到 `1`。
- **恢复时间超过 5 分钟**：几乎一定是 §5.1.3 的僵尸监听。
  `ssh corlinman-prod 'sudo ss -ltnp | grep 3011'` 确认 owner，
  必要时在 prod 上 `sudo pkill -u onebot-tunnel` 强制收掉旧会话（**这只杀隧道，不碰 QQ**）。
- **这次演练要在正式迁移之前做**（那时 QQ 还在 `cornna-cn` 上），
  这样即使弄断了也不影响线上账号。

### 5.5 可选的第二、第三个反向端口

| 端口 | 用途 | 本 runbook 默认 | 风险 |
|---|---|---|---|
| `-R 127.0.0.1:3011:127.0.0.1:3001` | OneBot，Hermes 用 | **必需** | — |
| `-R 127.0.0.1:6100:127.0.0.1:6099` | NapCat WebUI，**离家时扫码用** | **开** | 能 root prod 的人也能到家里的 WebUI（可改配置、可重启 QQ） |
| `-R 127.0.0.1:2222:127.0.0.1:22` | 家里机器的 SSH，远程维护用 | **关** | prod root → 家里 root shell，爆炸半径大得多 |

**为什么 6100 默认开**：见 §0.5。不开的话，QQ 一掉线你人不在家就只能等回家，
而 H1 §3 记录的历史空窗本来就是 9.7 h / 11.7 h。
边际风险有限：能 root prod 的人**本来就**能通过 3011 用这个 QQ 号发消息。

**为什么 2222 默认关**：OneBot 和 WebUI 的爆炸半径都限于"这个 QQ 号"，
而一个 root shell 的爆炸半径是"家里那台机器上的一切"，量级不同。
如果你确实需要它（例如那台机器在别的城市），**要显式决定并记录**，
同时在 prod 的 `authorized_keys` 里加 `permitlisten="127.0.0.1:2222"`。

离家扫码的完整路径：

```bash
laptop$ ssh -N -L 6099:127.0.0.1:6100 corlinman-prod
# 另开一个终端 / 浏览器打开：
laptop$ open http://127.0.0.1:6099/webui/
```

- **预期**：看到 NapCat WebUI 登录页。
- **打不开 / 连接被拒**：先确认 prod 上 6100 有监听
  （`ssh corlinman-prod 'ss -ltn | grep 6100'`），没有的话是家里的隧道没带那个 `-R`，
  或者 `permitlisten` 里漏了 6100。
- ⛔ **不要**把这个 6100 反代到公网域名上。H1 §5.1 的 `auth_basic` 坑（WebSocket 不带 Basic 凭据）
  和"WebUI token 等于 QQ 控制权"这两条叠在一起，公网入口没有安全的做法。

### 5.6 无损演练（照 H1 §8.3 的做法，不碰生产 endpoint）

H1 §8.3 用 `/run/systemd/system` 下的临时 unit + 一个没人用的端口验证过
"持续失败超过旧门槛后不会锁死，endpoint 恢复时能自行恢复"。这里同样做一遍，
验证的是**新的 `Restart=always` + `StartLimitIntervalSec=0`** 组合。

```bash
home$ sudo tee /run/systemd/system/t4-tunnel-drill.service >/dev/null <<'UNIT'
[Unit]
Description=T4 drill (temporary, /run only)
StartLimitIntervalSec=0
[Service]
Type=simple
Restart=always
RestartSec=5
ExecStart=/usr/bin/ssh -N -T -F /dev/null -o BatchMode=yes -o ConnectTimeout=2 \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -p 39997 -R 127.0.0.1:39998:127.0.0.1:39999 nobody@127.0.0.1
UNIT
home$ sudo systemctl daemon-reload && sudo systemctl start t4-tunnel-drill.service
# 让它失败一分钟以上，然后：
home$ systemctl show -p NRestarts,ActiveState,Result t4-tunnel-drill.service
```

- **预期**：`NRestarts` 是一个持续增长的两位数，`ActiveState=activating`（auto-restart），
  **`Result` 不是 `start-limit-hit`**。这就证明了持续失败不会把 unit 锁死。
- **如果出现 `start-limit-hit`**：`StartLimitIntervalSec=0` 没生效（又是段位置问题）。
  修好再回 §5.2。

清理（**别忘了，`/run` 下的 unit 重启会消失，但当次会话里它还在跑**）：

```bash
home$ sudo systemctl stop t4-tunnel-drill.service
home$ sudo rm -f /run/systemd/system/t4-tunnel-drill.service
home$ sudo systemctl daemon-reload
home$ systemctl is-active t4-tunnel-drill.service 2>&1 | head -1
home$ ss -ltn | grep -c 39998
```

- **预期**：`inactive` 或 `Unit ... could not be found`；`39998` 计数为 `0`。

---

## 6. 验收

### 6.0 执行顺序（前面各节的装配次序）

前面各节是按"主题"组织的，实际动手请按这个顺序：

| # | 动作 | 节 | 这一步做完 QQ 的状态 |
|---|---|---|---|
| 1 | prod：核对 `ws_url` 两处一致 | §1.3 | 在线（旧机） |
| 2 | home：主机准备、装包、建用户、建目录 | §4.1 §2.4 | 在线（旧机） |
| 3 | home：按 §1.6 安装所选运行时（AppImage 或精确 digest Docker）、写对应 unit 与互斥脚本 | §4.3 §4.4/§4.5 | 在线（旧机） |
| 4 | prod + home：建隧道账号/key/sshd 配置，并建 Hermes 探针受限状态查询通道 | §5.1（含 §5.2.1）§5.2 | 在线（旧机） |
| 5 | home：隧道断网演练 | §5.4 §5.6 | 在线（旧机） |
| 6a | **T3 采集器上线**（实现 + 部署 + `T3_DEPLOY_GATE=PASS`） | §8.1.1 | 在线（旧机） |
| 6b | **收 baseline 到 `BASELINE_GATE=PASS`**（≥7 天 / ≥10 段非删失） | §8.1.2 | 在线（旧机） |
| 6c | 备份账本 + 写 `t4_migration_begin` 事件；部署 `t4-egress-log` timer | §8.2.1 §8.3.1 | 在线（旧机） |
| 7 | **旧机停机 + mask；prod 停 Hermes 停旧隧道** | §3.3 §3.4 | **离线** |
| 7b | **`GATE35=PASS`**（放行闸，fail-closed 脚本） | §3.5 | 离线 |
| 7c | **切 `epoch=home_broadband`**（必须在此刻：无未闭合在线区间） | §8.2.2 | 离线 |
| 8 | home：拷 OneBot 配置、改端口、核对 token | §4.8 | 离线 |
| 9 | home：只启动 §1.6 选定的 NapCat unit（native 或 Docker） | §6.1 | 离线（等扫码） |
| 10 | **扫码一次** | §6.2 | 在线（新机） |
| 11 | home 本地探针（**必须在放行 Hermes 之前**） | §6.3 | 在线 |
| 12 | home：启动反向隧道 | §5.2 | 在线 |
| 13 | prod 经隧道探针（**必须在放行 Hermes 之前**） | §6.4 | 在线 |
| 14 | prod：启动 Hermes | §6.5 | 在线 |
| 15 | 铁律闸复验 + 稳定性观察（只读）+ 互斥复验 | §6.6 | 在线 |
| 16 | 写 `t4_migration_done` 事件 + **验证 epoch 真的切了** | §8.2.3 | 在线 |

**第 7 步之后 QQ 就离线了。** 从第 7 步到第 10 步之间的时间就是这次迁移的停机窗口，
正常应该在 15 分钟以内。**别在第 7 步之前才发现 AppImage 没传、包没装。**

> ### 📌 顺序上的一条铁律：**探针（11、13）必须在放行 Hermes（14）之前**
>
> `onebot_probe.py` 会建立一条到 NapCat 的 WebSocket。Hermes 一旦 active，
> 它自己就持有一条持久 WS —— 此时再开探针就是**第二条连接**（§6.6.0）。
> 所以第 11、13 步排在第 14 步之前**不是巧合，是约束**。
>
> **这条约束已经写成代码里的断言**：home 经 §5.2.1 的受限 SSH 查询真实宿主
> `corlinman-prod`，prod 查本机；只有明确 inactive(rc=3) 才放行。active 或无法确认都直接退出 3
> 并拒绝连接（§6.3 的 `refuse_if_hermes_active()`）。
> 也就是说，**即使你把顺序做反了，探针也会拒绝执行**，而不是悄悄多开一条连接。
> 第 15 步（§6.6）之后的所有观测都改成只读 runtime state，不再建立任何新连接。

### 6.1 启动 NapCat（新机首次；原生 / Docker 二选一）

⛔ **只执行你在 §1.6 选定的那一组。不得先试 native、失败后不清状态又顺手启动 Docker。**

**原生路径（推荐）**：

```bash
home$ sudo /opt/napcat-native/bin/preflight && echo PREFLIGHT_PASSED
home$ sudo systemctl start napcat-native.service
home$ systemctl is-active napcat-native.service
home$ systemctl show -p NRestarts,MainPID napcat-native.service
home$ sudo ss -ltnp | grep -E '127\.0\.0\.1:(3001|6099)\b'
```

- **预期**：`PREFLIGHT_PASSED`；`active`；`NRestarts=0`；
  `127.0.0.1:6099` **一定**有监听（WebUI），
  `127.0.0.1:3001` 此刻**可能还没有**（OneBot 要等登录成功才开）。
- **`preflight` 拒绝**：回 §4.3 按它报的那一条查。**不要为了让它过去而 kill 东西。**
- **`is-active` 是 `failed`**：`sudo journalctl -u napcat-native.service -n 80 --no-pager`。
  常见三种：
  1. `MemoryDenyWriteExecute` 之类的沙箱把 Electron 打死了 → §4.6 逐条放松；
  2. `BindPaths` 挂载失败（目录不存在/权限不对）→ §2.4 重建目录；
  3. `dbus-run-session: command not found` → §4.1 装包。
  **修完必须 `systemctl reset-failed` 再 `start`**，否则会撞 §4.7 的启动限流。
- **一小时内失败两次就停手**，先把日志看懂再动。这是 §4.7 那把闸存在的意义。

**Docker 回退路径（仅 §1.6 判定必须用 Docker 时）**：

```bash
home$ sudo /usr/local/sbin/napcat-docker-image-guard
home$ sudo /usr/local/sbin/napcat-docker-guard
home$ sudo -u napcat-native /opt/napcat-native/bin/preflight && echo PREFLIGHT_PASSED
home$ sudo systemctl start napcat.service
home$ systemctl is-active napcat.service
home$ systemctl show -p NRestarts,MainPID napcat.service
home$ sudo docker inspect -f 'running={{.State.Running}} restart={{.HostConfig.RestartPolicy.Name}} image={{.Image}}' napcat
home$ sudo ss -ltnp | grep -E '127\.0\.0\.1:(3001|6099)\b'
```

- **预期**：两个 guard 与 preflight 全部 OK；`napcat.service` active、`NRestarts=0`；
  容器 `running=true restart=no`；6099 有监听，3001 仍可能要等扫码。
- **`docker-image-guard` 拒绝** ⇒ digest 文件/本地镜像不可信；回 §1.6，不能改成 tag 绕过。
- **`docker-guard` 拒绝 existing container** ⇒ 按 §4.5.3 判 running/exited；停止态也必须先走
  §4.5.4 完整归档，不能 `docker rm -f`。
- **preflight 报端口或锁占用** ⇒ 本机还有 native/Docker 实例路径。查清并保持单实例；不要 kill 后直接重试。
- **`is-active=failed`** ⇒ 先看 `journalctl -u napcat.service`；同样受每小时一次启动闸约束，
  修好后才 `reset-failed`。不要转去启动 `napcat-native.service`。

后续 §6.2–§6.6 对两条路径共用：宿主端口都是 3001/6099、探针 URL 不变；
凡检查 unit/journal 的位置，Docker 路径用 `napcat.service`，原生路径用 `napcat-native.service`。

### 6.2 扫码（**只扫一次**）

按 §4.9 的三种方式之一打开 `http://127.0.0.1:6099/webui/`，用 WebUI 凭据登录 ——
凭据**从密码管理器或 `sudoedit` 编辑器缓冲区取**，⛔ **不要用 `cat`/`jq`/`grep` 打到终端**
（理由与做法见 §4.9「WebUI 凭据：只验证存在且非空，不打印」）。

- 确认页面上待登录的账号是 **`2952532060`**。
- **只扫描当前这一个二维码一次。** 手机上确认。
- ⛔ **不要**输入密码（H1 §8.6：只会撞 `duplicate_login_guard`，什么也解决不了，
  而且被踢后密码登录会阻塞在短信验证上）。
- ⛔ **不要**用重启容器/重启服务来"刷新二维码"。二维码会自己刷新；
  真的过期了就用 WebUI 自带的 `RefreshQRcode`（H1 §8.8 确认过它不会删设备身份）。
- ⛔ **不要**把二维码截图发到任何聊天里。

扫完之后：

```bash
home$ sudo ss -ltnp | grep '127\.0\.0\.1:3001'
```

> ### ⚠️ kicked 检查必须分两步：先证明日志可读，再解释计数
>
> 上一版这里是
> `sudo journalctl ... | grep -c -i -E 'kicked|KickedOffLine' || true`。
> **`journalctl` 失败、unit 名写错、日志不可读时，`grep` 收到空输入照样输出 `0`，
> 而 `|| true` 再把失败状态吞掉** ⇒ 你看到 `0`，把它解释成"没有被踢"，
> **在没有任何真实日志证据的情况下继续迁移**。
>
> 「读不到日志」和「读到了日志且没有 kicked」是**两件完全不同的事**，必须分开。

```bash
# 原生路径用 napcat-native.service；Docker 路径把下一行改成 NAPCAT_UNIT=napcat.service。
home$ sudo env NAPCAT_UNIT=napcat-native.service bash -s <<'CHK'
set -uo pipefail
UNIT=${NAPCAT_UNIT:-}
case "$UNIT" in
  napcat-native.service|napcat.service) ;;
  *) echo "KICKED_CHECK=BLOCK reason=invalid_unit unit=$UNIT"; exit 1 ;;
esac
LOG=$(mktemp); trap 'rm -f "$LOG"' EXIT

# (1) 选定 unit 必须存在且 active —— 名字/运行时选错时 journalctl 也可能返回空或旧日志。
if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
  echo "KICKED_CHECK=BLOCK reason=unit_not_found unit=$UNIT"; exit 1
fi
act=$(systemctl is-active "$UNIT" 2>&1); act_rc=$?
if [ "$act_rc" -ne 0 ] || [ "$act" != "active" ]; then
  echo "KICKED_CHECK=BLOCK reason=unit_not_active unit=$UNIT state=$act rc=$act_rc"; exit 1
fi

# (2) 采集：journalctl 自身必须成功。
# `-q -o json` 很关键：无记录时输出 0 条 JSON，而不是把 `-- No entries --` 提示算成一行日志。
if ! journalctl -q -o json -u "$UNIT" --since '-10min' --no-pager > "$LOG" 2>"$LOG.err"; then
  echo "KICKED_CHECK=BLOCK reason=journalctl_failed"; head -3 "$LOG.err"; rm -f "$LOG.err"; exit 1
fi
rm -f "$LOG.err"

# (3) 必须至少有一条真实 journal 记录；提示文本不再可能喂大这个计数。
LINES=$(wc -l < "$LOG")
echo "JOURNAL_LINES=$LINES"
if [ "$LINES" -eq 0 ]; then
  echo "KICKED_CHECK=BLOCK reason=empty_log_window (读不到日志 ≠ 没有 kicked)"; exit 1
fi

# (4) 只有走到这里，计数才有意义
K=$(grep -c -i -E 'kicked|KickedOffLine' "$LOG"); GREP_RC=$?
if [ "$GREP_RC" -ge 2 ]; then
  echo "KICKED_CHECK=BLOCK reason=grep_error rc=$GREP_RC"; exit 1
fi
echo "KICKED_COUNT=$K"
[ "$K" -eq 0 ] || { echo "KICKED_CHECK=BLOCK reason=kicked_seen"; grep -i -E 'kicked|KickedOffLine' "$LOG" | tail -5; exit 1; }
echo "KICKED_CHECK=PASS"
CHK
home$ echo "rc=$?"
```

- **预期**：`127.0.0.1:3001` 出现监听；`JOURNAL_LINES=<非 0>`；`KICKED_COUNT=0`；
  `KICKED_CHECK=PASS`；`rc=0`。
- **`reason=invalid_unit` / `unit_not_found` / `unit_not_active`**：运行时分支选错、unit 名写错，
  或你在错误机器上；先确认 §6.1 实际启动的是 native 还是 Docker，不能拿另一条 unit 的旧日志放行。
- **`reason=journalctl_failed`**：多半是没有 `sudo` / 不在 `systemd-journal` 组。
  **这不是"没有 kicked"**，修好权限重跑。
- **`reason=empty_log_window`**：10 分钟窗口里一条真实 journal 记录都没有 —— 服务刚起来还没写日志，
  或 journal 存储被关掉（`Storage=none`）。`journalctl` 的 `-- No entries --` 提示已由 `-q -o json`
  排除，绝不会再把提示文本当成“有日志”。等 1 分钟重试；仍为空就用
  `sudo journalctl -u napcat-native.service -n 50 --no-pager` 确认到底有没有日志系统可用。
  **在能读到日志之前，不要把"没看到 kicked"当成证据。**
- **3001 一直不开（等了 3 分钟以上）**：登录没落地。**不要重启**，先看日志分类：
  ```
  home$ UNIT=napcat-native.service   # Docker 路径改为 napcat.service
  home$ sudo journalctl -u "$UNIT" --since '-10min' --no-pager \
      | grep -i -c -E 'qrcode|二维码'
  ```
  还在刷二维码 ⇒ 手机端授权没成功，**再扫一次当前的新码**（H1 §6.1 就是卡在这个状态）。
- **日志里出现 `KickedOffLine`**：**立刻停下**。这意味着**别处还有一个实例在登录同一个号** ——
  回 §3.3 把旧机的状态从头查一遍（尤其 3.3.3 的进程残留和 3.3.4 的容器）。
  **在查清楚之前不要再扫码**，每一次重扫都在加计风控。

### 6.3 只读探针（home 本地）

探针的硬要求：**短生命周期、只读、只调 `get_status` 和 `get_login_info`、
token 从进程内读取、不出现在命令行或输出里、不发任何消息。**

```bash
home$ sudo python3 -m venv /opt/napcat-native/probe-venv
home$ sudo /opt/napcat-native/probe-venv/bin/pip -q install 'websockets>=12'
home$ sudo install -d -m 700 /opt/napcat-native/probe
home$ sudo tee /opt/napcat-native/probe/onebot_probe.py >/dev/null <<'PY'
#!/usr/bin/env python3
"""短生命周期 OneBot 只读探针。

只做三件事：握手、get_status、get_login_info。
token 由本进程从 --token-from 指定的文件里读出，绝不进入 argv，也绝不打印。
用法:
  onebot_probe.py <ws-url> --token-from napcat:<path>|env:<path>
      --hermes-guard local|ssh:<真实Hermes宿主>|ssh-file:<path> [--expect-uin <uin>]
退出码: 0 = 全部通过; 1 = 未通过; 2 = 用法/配置错误;
        3 = 被拒绝执行（Hermes active 或状态无法确认）
"""
import asyncio, json, os, pathlib, subprocess, sys

ACTIONS = ("get_status", "get_login_info")   # 白名单，写死。不要往里加东西。


def refuse_if_hermes_active(target: str) -> None:
    """铁律（§6.6.0）：只在真实 Hermes 宿主明确返回 inactive(rc=3) 时允许建 WS。

    target="local" 用于 corlinman-prod 本机；home 使用 `ssh-file:/etc/hermes-probe-guard/prod-host`
    （也支持显式 `ssh:<host>`），经 §5.2.1 forced-command key 查询真宿主。任何无法确认都失败关闭。
    """
    if os.environ.get("T4_PROBE_ALLOW_WITH_HERMES") == "1":
        print("PROBE_GUARD=bypassed(explicit_break_glass)", file=sys.stderr)
        return

    if target.startswith("ssh-file:"):
        path = target[len("ssh-file:"):]
        try:
            host = pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print("PROBE_REFUSED=hermes_state_unconfirmed host_file=%s type=%s" %
                  (path, type(exc).__name__), file=sys.stderr)
            raise SystemExit(3)
        if not host or any(c.isspace() for c in host) or any(c in host for c in "<>"):
            print("PROBE_REFUSED=invalid_hermes_guard_host_file", file=sys.stderr)
            raise SystemExit(3)
        target = "ssh:" + host

    if target == "local":
        label = "local(corlinman-prod)"
        cmd = ["systemctl", "is-active", "--quiet", "hermes.service"]
    elif target.startswith("ssh:") and target[4:]:
        host = target[4:]
        label = "ssh:" + host
        cmd = [
            "ssh", "-F", "/dev/null",
            "-i", "/etc/hermes-probe-guard/id_ed25519",
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "UserKnownHostsFile=/etc/hermes-probe-guard/known_hosts",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
            "hermes-probe-guard@" + host,
        ]
    else:
        print("PROBE_REFUSED=invalid_hermes_guard_target", file=sys.stderr)
        raise SystemExit(2)

    try:
        cp = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("PROBE_REFUSED=hermes_state_unconfirmed host=%s type=%s" %
              (label, type(exc).__name__), file=sys.stderr)
        raise SystemExit(3)

    rc = cp.returncode
    if rc == 0:
        print("PROBE_REFUSED=hermes_active host=" + label, file=sys.stderr)
        print("Hermes 正持有一条到同一个 NapCat 的持久 WS。再开一条短连接会与它"
              "竞争同一个账号，且你读到的是这条额外连接的状态，不是 Hermes 实际"
              "使用的那条。改用 §6.6.1 的 runtime state 只读采样。", file=sys.stderr)
        raise SystemExit(3)
    if rc == 3:
        print("PROBE_GUARD=ok(hermes_inactive host=%s)" % label, file=sys.stderr)
        return

    # systemctl 的 unknown/error（通常 rc=4）、D-Bus/权限错误，以及 SSH rc=255 都在这里阻断。
    print("PROBE_REFUSED=hermes_state_unconfirmed host=%s rc=%d" % (label, rc),
          file=sys.stderr)
    raise SystemExit(3)


def read_token(spec: str) -> str:
    kind, _, path = spec.partition(":")
    if not path:
        raise SystemExit(2)
    raw = pathlib.Path(path).read_text(encoding="utf-8")
    if kind == "napcat":
        cfg = json.loads(raw)
        network = cfg.get("network") if isinstance(cfg, dict) else None
        servers = network.get("websocketServers") if isinstance(network, dict) else None
        if not isinstance(servers, list):
            raise SystemExit(2)
        enabled = [s for s in servers if isinstance(s, dict) and s.get("enable") is True]
        if not enabled:
            raise SystemExit(2)
        token = enabled[0].get("token")
        if not isinstance(token, str) or not token.strip():
            raise SystemExit(2)
        return token.strip()
    if kind == "env":
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("ONEBOT_ACCESS_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if not token:
                    raise SystemExit(2)
                return token
        raise SystemExit(2)
    raise SystemExit(2)


async def probe(url: str, token: str) -> dict:
    import websockets
    hdr = {"Authorization": "Bearer " + token} if token else {}
    out = {"WS_LINK": False, "GET_STATUS_OK": False, "ACCOUNT_ONLINE": None,
           "ACCOUNT_GOOD": None, "GET_LOGIN_INFO_OK": False, "UIN": None}
    try:
        conn = websockets.connect(url, additional_headers=hdr, open_timeout=10,
                                  close_timeout=5, ping_interval=None)
    except TypeError:                       # websockets < 14 用的是 extra_headers
        conn = websockets.connect(url, extra_headers=hdr, open_timeout=10,
                                  close_timeout=5, ping_interval=None)
    async with conn as ws:
        out["WS_LINK"] = True
        loop = asyncio.get_running_loop()
        for i, action in enumerate(ACTIONS):
            echo = "t4probe%d" % i
            await ws.send(json.dumps({"action": action, "params": {}, "echo": echo}))
            deadline = loop.time() + 10
            while True:
                remain = deadline - loop.time()
                if remain <= 0:
                    break
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remain))
                if msg.get("echo") != echo:
                    continue                 # 跳过事件推送帧
                ok = msg.get("status") in ("ok", "async") and msg.get("retcode") == 0
                data = msg.get("data") or {}
                if action == "get_status":
                    out["GET_STATUS_OK"] = ok
                    out["ACCOUNT_ONLINE"] = data.get("online")
                    out["ACCOUNT_GOOD"] = data.get("good")
                else:
                    out["GET_LOGIN_INFO_OK"] = ok
                    out["UIN"] = data.get("user_id")
                break
    return out


def main() -> int:
    args = sys.argv[1:]
    usage = ("usage: onebot_probe.py <ws-url> --token-from napcat:<p>|env:<p> "
             "--hermes-guard local|ssh:<host>|ssh-file:<path> [--expect-uin <uin>]")
    if not args:
        print(usage, file=sys.stderr)
        return 2
    url, rest = args[0], args[1:]
    opts = {}
    i = 0
    while i < len(rest):
        if rest[i] not in ("--token-from", "--hermes-guard", "--expect-uin") or i + 1 >= len(rest):
            print(usage, file=sys.stderr)
            return 2
        if rest[i] in opts:
            print(usage, file=sys.stderr)
            return 2
        opts[rest[i]] = rest[i + 1]
        i += 2
    if "--token-from" not in opts or "--hermes-guard" not in opts:
        print(usage, file=sys.stderr)
        return 2
    spec = opts["--token-from"]
    expect = opts.get("--expect-uin")
    refuse_if_hermes_active(opts["--hermes-guard"])  # 真宿主闸；放在读 token/建连之前。
    token = read_token(spec)
    try:
        out = asyncio.run(probe(url, token))
    except Exception as exc:                # 只打印异常类型，不打印可能含 token 的 repr
        print("PROBE_ERROR=" + type(exc).__name__)
        return 1
    for k in ("WS_LINK", "GET_STATUS_OK", "ACCOUNT_ONLINE", "ACCOUNT_GOOD",
              "GET_LOGIN_INFO_OK", "UIN"):
        print("%s=%s" % (k, out[k]))
    passed = (out["WS_LINK"] and out["GET_STATUS_OK"] and out["GET_LOGIN_INFO_OK"]
              and out["ACCOUNT_ONLINE"] is True and out["ACCOUNT_GOOD"] is True
              and (expect is None or str(out["UIN"]) == str(expect)))
    print("PROBE_PASS=%s" % passed)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
PY
home$ sudo chmod 700 /opt/napcat-native/probe/onebot_probe.py
home$ sudo grep -c -E 'send_msg|send_private_msg|send_group_msg|set_|delete_msg|Restart' \
      /opt/napcat-native/probe/onebot_probe.py
home$ sudo grep -n -E 'subprocess\.(run|Popen|call|check_)' \
      /opt/napcat-native/probe/onebot_probe.py
home$ sudo grep -c -E 'systemctl (restart|start|stop|reset-failed)|docker |pkill|os\.system' \
      /opt/napcat-native/probe/onebot_probe.py
```

- **第一条 `grep -c` 必须是 `0`**。这是"这个探针不会说话"的可审计证据。
  （`ACTIONS` 这张白名单只有两项，也可以直接看。）
- **第二条**：应当**只有一处** `subprocess.run`，参数来自 `refuse_if_hermes_active()` 构造的两种白名单：
  prod 本机的 `systemctl is-active --quiet hermes.service`，或 home 经 §5.2.1 forced-command key 的 `ssh`。
  两者都只读；SSH key 不能执行其他命令。**出现第二处 `subprocess` 调用就要停下来看它在干什么。**
- **第三条 `grep -c` 必须是 `0`**：探针里不允许出现任何**恢复动词**
  （`systemctl restart/start/stop/reset-failed`、`docker`、`pkill`、`os.system`）。
  这三条合起来是"探针只读、不说话、不恢复"的完整可审计证据。

**R2 必测负例：模拟 `systemctl` 返回非 0、非 3（rc=4/unknown），探针必须在建连前拒绝。**
这个测试只 import 脚本并替换 `subprocess.run`，不会读取 token、不会 import `websockets`、不会建 WS：

```bash
home$ sudo /opt/napcat-native/probe-venv/bin/python - <<'PY'
import importlib.util, os
p = "/opt/napcat-native/probe/onebot_probe.py"
spec = importlib.util.spec_from_file_location("t4probe", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
os.environ.pop("T4_PROBE_ALLOW_WITH_HERMES", None)
class Result:
    returncode = 4
m.subprocess.run = lambda *a, **kw: Result()
try:
    m.refuse_if_hermes_active("local")
except SystemExit as exc:
    print("TEST_GUARD_RC4_EXIT=%s" % exc.code)
    raise SystemExit(0 if exc.code == 3 else 1)
print("TEST_GUARD_RC4_UNEXPECTEDLY_ALLOWED=true")
raise SystemExit(1)
PY
home$ echo "TEST_GUARD_RC4_runner_rc=$?"
```

- **预期**：`PROBE_REFUSED=hermes_state_unconfirmed ... rc=4`、`TEST_GUARD_RC4_EXIT=3`、
  `TEST_GUARD_RC4_runner_rc=0`。
- **出现 `...UNEXPECTEDLY_ALLOWED=true` 或 runner rc 非 0** ⇒ R2 仍是失败开放，**不许运行真实探针**。

跑它（home 必须显式指定真实 Hermes 宿主，不能写 `local`）：

```bash
home$ sudo /opt/napcat-native/probe-venv/bin/python \
      /opt/napcat-native/probe/onebot_probe.py \
      ws://127.0.0.1:3001 \
      --token-from napcat:/opt/napcat-native/config-candidate/onebot11_2952532060.json \
      --hermes-guard ssh-file:/etc/hermes-probe-guard/prod-host \
      --expect-uin 2952532060
home$ echo "rc=$?"
```

**预期输出**（第一行的 host 是你填入的真实 prod 地址，其余逐字）：

```text
PROBE_GUARD=ok(hermes_inactive host=ssh:<PROD_HOST>)
WS_LINK=True
GET_STATUS_OK=True
ACCOUNT_ONLINE=True
ACCOUNT_GOOD=True
GET_LOGIN_INFO_OK=True
UIN=2952532060
PROBE_PASS=True
rc=0
```

异常分支：

| 输出 | 含义 | 处置 |
|---|---|---|
| `PROBE_REFUSED=hermes_active` | 真宿主上的 Hermes 仍 active | **不建连**；确认你是否跳过了 §3.4。不要用 break-glass 绕过 |
| `PROBE_REFUSED=hermes_state_unconfirmed ... rc=4/255` | systemd unknown/错误，或受限 SSH 查询失败 | **不建连**；修 §5.2.1 查询通道。查不了 ≠ Hermes 已停 |
| `rc=2` 且未建连 | `--hermes-guard`/token 来源缺失、JSON 结构错误、无 enabled WS 或 token 空 | 修配置；缺字段/空 token 不再回退到任意 server 或无认证连接 |
| `PROBE_ERROR=ConnectionRefusedError` | 3001 没监听 | 回 §6.2：登录还没落地，或原生实例绑错了 host |
| `PROBE_ERROR=InvalidStatus` / `InvalidStatusCode` | 握手被拒 = **token 不对** | 回 §4.8 重新核对 token 指纹。**注意**：`ONEBOT_ACCESS_TOKEN` 和 WebUI token 是两个独立的秘密，弄混会得到永久 401（`plugins/platforms/onebot/README.md`「Two different secrets」） |
| `PROBE_ERROR=TimeoutError` | 连上了但没人应答 | NapCat 起着但 OneBot 模块没就绪。等 60 秒重试一次；仍然如此看 journal |
| `ACCOUNT_ONLINE=False` `ACCOUNT_GOOD=True` | 链路通、**账号离线** | 这正是 H1 §8.5 记录的被踢形态。回 §6.2 检查是不是双登录 |
| `ACCOUNT_ONLINE=None` | 后端没报这个字段 | **不要当成离线**。看 `GET_LOGIN_INFO_OK` 和 `UIN`；两者正常就再等一个心跳周期重试 |
| `UIN=` 别的数字 | **登录成了别的号** | **立刻停手**。`/etc/napcat-native/env` 的 `ACCOUNT` 写错了，或者你扫码时用的是别的微信/QQ。这种情况下**不要**继续接 Hermes |
| `PROBE_ERROR=ModuleNotFoundError` | venv 里没装 `websockets` | 重跑上面的 `pip install` |

### 6.4 只读探针（prod，经隧道）

**同一个脚本，换 URL 和 token 来源。** 这一步验证的是隧道，不是账号。

```bash
corlinman-prod$ sudo install -d -m 700 /opt/hermes/probe
home$ sudo cat /opt/napcat-native/probe/onebot_probe.py \
    | ssh corlinman-prod 'sudo tee /opt/hermes/probe/onebot_probe.py >/dev/null'
corlinman-prod$ sudo chmod 700 /opt/hermes/probe/onebot_probe.py
corlinman-prod$ sudo /opt/hermes/venv/bin/python /opt/hermes/probe/onebot_probe.py \
      ws://127.0.0.1:3011 \
      --token-from env:/opt/hermes/data/.env \
      --hermes-guard local \
      --expect-uin 2952532060
corlinman-prod$ echo "rc=$?"
```

- **预期**：第一行是 `PROBE_GUARD=ok(hermes_inactive host=local(corlinman-prod))`，
  后续七行与 §6.3 相同，最后 `rc=0`。
- **`/opt/hermes/venv/bin/python` 不存在**：先找 Hermes 实际用的解释器
  `systemctl show -p ExecStart hermes.service | tr ' ' '\n' | grep python`。
  用它就行（Hermes 依赖里必然有 `websockets`）。
- **home 通过而 prod 不通过**：**问题一定在隧道，不在账号。** 按这个顺序查：
  1. `sudo ss -ltnp | grep 3011` —— 没有监听 ⇒ 回 §5.2 的异常分支；
  2. 有监听但 `ConnectionRefusedError` ⇒ ssh 还活着但转发没建立
     （这就是没加 `ExitOnForwardFailure` 时会出现的"假在线"）；
     `ssh home 'systemctl restart onebot-rtunnel.service'`，30 秒后重试；
  3. `InvalidStatus` ⇒ prod `.env` 里的 token 和 NapCat 里的不是一个，回 §4.8。
- **prod 通过而 home 不通过**：不可能。真出现说明你的两条命令指向了不同的后端 ——
  多半是旧的 `onebot-tunnel.service` 又起来了，3011 通向 `cornna-cn`。
  `ssh corlinman-prod 'systemctl is-active onebot-tunnel.service'` 必须是 `inactive`。

⛔ **到这里为止，不要发送任何测试消息。** 上面两个探针已经证明了链路和账号；
发一条"测试"消息证明不了额外的东西，却会在一个刚被踢过两次的低信任号上多一次出站行为。
H1 全程的验收也都是"没有发送任何 QQ 测试消息"。

### 6.5 放行 Hermes

**只有 §6.3 和 §6.4 都 `PROBE_PASS=True` 之后**才执行这一步。

```bash
corlinman-prod$ sudo systemctl start hermes.service
corlinman-prod$ sudo bash -s <<'GATE'
set -uo pipefail
bad=0
block() { echo "HERMES_RELEASE_GATE=BLOCK reason=$*"; bad=1; }

act=$(systemctl is-active hermes.service 2>&1); act_rc=$?
[ "$act_rc" -eq 0 ] && [ "$act" = "active" ] || block "unit_not_active state=$act rc=$act_rc"
if ! nrestarts=$(systemctl show -p NRestarts --value hermes.service 2>&1); then
  block "nrestarts_uncheckable"
elif ! [[ "$nrestarts" =~ ^[0-9]+$ ]] || [ "$nrestarts" -ne 0 ]; then
  block "nrestarts=$nrestarts"
fi

LOG=$(mktemp); ERR=${LOG}.err; trap 'rm -f "$LOG" "$ERR"' EXIT
if ! journalctl -q -o cat -u hermes.service --since '-3min' --no-pager > "$LOG" 2> "$ERR"; then
  block "journalctl_failed"; head -3 "$ERR"
else
  lines=$(wc -l < "$LOG")
  [ "$lines" -gt 0 ] || block "empty_log_window"
  grep -Eq 'OneBot adapter initialised|adapter connected' "$LOG"; rc=$?
  case "$rc" in 0) : ;; 1) block "adapter_ready_log_missing" ;; *) block "adapter_log_grep_error rc=$rc" ;; esac
  grep -Fq 'ws://127.0.0.1:3011' "$LOG"; rc=$?
  case "$rc" in 0) : ;; 1) block "expected_ws_url_missing" ;; *) block "url_grep_error rc=$rc" ;; esac
  grep -Eq 'bot account is 2952532060|QQ account is online' "$LOG"; rc=$?
  case "$rc" in 0) : ;; 1) block "account_ready_log_missing" ;; *) block "account_grep_error rc=$rc" ;; esac
  cf=$(grep -c 'connect_failed' "$LOG"); rc=$?
  case "$rc" in 0|1) : ;; *) block "connect_failed_grep_error rc=$rc" ;; esac
  [ "$cf" -eq 0 ] || block "connect_failed_count=$cf"
fi

gw=$(pgrep -c -f 'hermes.*gateway' 2>&1); rc=$?
case "$rc" in
  0) [[ "$gw" =~ ^[0-9]+$ ]] || block "gateway_count_non_numeric" ;;
  1) gw=0 ;;
  *) block "gateway_pgrep_error rc=$rc" ;;
esac
[ "$gw" = "1" ] || block "gateway_count=$gw"

printf 'HERMES_STATE=%s NRESTARTS=%s CONNECT_FAILED=%s GATEWAY_COUNT=%s\n' \
  "$act" "${nrestarts:-UNCHECKABLE}" "${cf:-UNCHECKABLE}" "$gw"
if [ "$bad" -ne 0 ]; then exit 1; fi
echo "HERMES_RELEASE_GATE=PASS"
GATE
corlinman-prod$ echo "rc=$?"
```

- **预期**：`HERMES_STATE=active NRESTARTS=0 CONNECT_FAILED=0 GATEWAY_COUNT=1`、
  `HERMES_RELEASE_GATE=PASS`、`rc=0`；日志闸已确认 adapter、3011 URL 与账号在线证据真实存在。
- journal 查询失败/空窗口、grep 错误/缺证据、`connect_failed>0`、gateway 数不是 1 都非零阻断；
  不再允许 `journalctl` 失败后由 `grep -c` 伪造一个 0。
- **journal 里的 URL 不是 3011**：`.env` / `config.yaml` 有一处被改过。回 §1.3。
- **`bot account is` 是别的号**：立刻 `systemctl stop hermes.service`，回 §6.3 的 `UIN` 分支。
- **`connect_failed` 在增长**：隧道在抖。`ssh home 'systemctl show -p NRestarts onebot-rtunnel.service'`
  看重启次数是不是也在涨；是的话回 §5.4。
- **gateway 进程数是 2**：有第二个 Hermes 在跑（H1 §4 记录 `cornna-cn` 上也有一份，
  已 inactive/disabled）。核对：`ssh cornna-cn 'systemctl is-active hermes.service'` 必须 `inactive`。

### 6.6 稳定性观察与互斥复验（迁移当天收尾）

#### 6.6.0 铁律：Hermes 活着的时候，不许再开第二条 OneBot WS

> ### 这条必须是**可测试的断言**，不是口头约定
>
> 上一版 §6.6 在 Hermes 已经 `active` 之后，每分钟跑一次
> `onebot_probe.py` × 6 —— 在 Hermes 那条持久 WS 之外**再建六条短生命周期 WS**。
> 虽然动作是只读的，仍然违反"避免第二条连接"的设计边界：
>
> 1. **与账号竞争**：后端连接槽位 / 事件顺序上多一个参与者。
> 2. **读的不是你想读的东西**：你测到的是**这条额外连接**的状态，
>    **不是 Hermes 实际使用的那条**的状态。观测行为改变了被观测对象。
>
> **T3 审计的 F-01 和 T4 审计的 F16 是各自独立犯的同一个错。**
> 两份设计互不相干却踩了同一个坑，说明"大家都知道这条规矩"是靠不住的 ——
> 所以本次修订把它做成**探针自己的结构性拒绝**，而不是又写一句叮嘱。

**断言已经落在代码里**（§6.3 的 `onebot_probe.py` 里那个 `refuse_if_hermes_active()`）：
home 查真实宿主 `corlinman-prod`、prod 查本机；只接受明确 inactive(rc=3)。
Hermes active 或状态无法确认时，探针都**直接退出 3 并拒绝连接**。

**验证这条闸真的在生效**（在 §6.5 放行 Hermes 之后立刻做，这是本节的第一步）：

```bash
corlinman-prod$ systemctl is-active hermes.service
corlinman-prod$ sudo /opt/hermes/venv/bin/python /opt/hermes/probe/onebot_probe.py \
      ws://127.0.0.1:3011 --token-from env:/opt/hermes/data/.env \
      --hermes-guard local --expect-uin 2952532060
corlinman-prod$ echo "rc=$?"
```

- **预期**（**注意：这里"探针失败"才是通过**）：
  ```text
  active
  PROBE_REFUSED=hermes_active host=local(corlinman-prod)
  rc=3
  ```
- **`rc=0` 且打印了 `PROBE_PASS=True`** ⇒ **闸没生效**：你装的是没有
  `refuse_if_hermes_active()` 的旧版脚本，或者 `main()` 里漏了那一行调用。
  **回 §6.3 重写脚本**，并把刚才那次多余的连接记进执行记录。
- **`PROBE_REFUSED=hermes_state_unconfirmed`** ⇒ `systemctl` 查询失败/超时/返回 unknown；
  这同样必须 `rc=3` 拒绝，不能解释成 Hermes 已停。先修查询，再复验铁律闸。
- 例外通道：`T4_PROBE_ALLOW_WITH_HERMES=1` **只允许**在"已确认 Hermes 死亡、
  需要取证"时使用（对应 T3-design §3.3 探针触发条件：`gateway_state` 陈旧
  **且** writer 进程已不存在）。**迁移验收流程里没有任何一步该用它。**

#### 6.6.1 六个分钟级样本 —— 只读 runtime state 与进程级指标

照 H1 §7.3 / §8.9 的做法取**六个分钟级样本**，但**全部改成不建立任何新连接**：
读的是 Hermes 自己写的状态文件、systemd 的进程级指标、以及监听端口计数。

```bash
corlinman-prod$ sudo bash -s <<'GATE'
set -uo pipefail
overall=0
for i in 1 2 3 4 5 6; do
  sample_bad=0

  if ! ts=$(date -u +%H:%M:%SZ); then
    ts=TIME_UNCHECKABLE; sample_bad=1
  fi
  hermes=$(systemctl is-active hermes.service 2>&1); hrc=$?
  [ "$hrc" -eq 0 ] && [ "$hermes" = "active" ] || sample_bad=1

  if ! nrestarts=$(systemctl show -p NRestarts --value hermes.service 2>&1); then
    nrestarts="UNCHECKABLE(${nrestarts%%$'\n'*})"; sample_bad=1
  elif ! [[ "$nrestarts" =~ ^[0-9]+$ ]] || [ "$nrestarts" -ne 0 ]; then
    sample_bad=1
  fi

  if ! sockets=$(ss -ltnH '( sport = :3011 )' 2>&1); then
    tunnelports="UNCHECKABLE(${sockets%%$'\n'*})"; sample_bad=1
  else
    tunnelports=0
    while IFS= read -r line; do
      [ -n "$line" ] && tunnelports=$((tunnelports + 1))
    done <<< "$sockets"
    [ "$tunnelports" -eq 1 ] || sample_bad=1
  fi

  gw=$(pgrep -c -f 'hermes.*gateway' 2>&1); grc=$?
  case "$grc" in
    0) [[ "$gw" =~ ^[0-9]+$ ]] || { gw="UNCHECKABLE(non_numeric)"; sample_bad=1; } ;;
    1) gw=0 ;;
    *) gw="UNCHECKABLE(pgrep_rc=$grc)"; sample_bad=1 ;;
  esac
  [ "$gw" = "1" ] || sample_bad=1

  printf '%s hermes=%s nrestarts=%s tunnelports=%s gw=%s ' \
    "$ts" "$hermes" "$nrestarts" "$tunnelports" "$gw"
  python3 - <<'PY'
import json, pathlib
# 只读 Hermes 自己写的状态文件 —— 这正是 T3 哨兵用的同一个数据源（T3-design §3.2），
# 读它不产生任何到 NapCat 的连接。
p = pathlib.Path("/opt/hermes/data/gateway_state.json")
try:
    st = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(st, dict):
        raise TypeError("root_not_object")
    platforms = st.get("platforms")
    if not isinstance(platforms, dict):
        raise KeyError("platforms")
    ob = platforms.get("onebot")
    if not isinstance(ob, dict):
        raise KeyError("platforms.onebot")
except Exception as exc:
    print("state=UNREADABLE(%s)" % type(exc).__name__)
    raise SystemExit(1)
state = ob.get("state")
online = ob.get("account_online")
print("state=%s online=%s err=%s updated=%s" % (
    state, online, (ob.get("error_message") or "")[:40] or None, st.get("updated_at")))
if state != "connected" or online is False:
    raise SystemExit(1)
PY
  py_rc=$?
  [ "$py_rc" -eq 0 ] || sample_bad=1

  if [ "$sample_bad" -ne 0 ]; then
    echo "SAMPLE_${i}=BLOCK"
    overall=1
  else
    echo "SAMPLE_${i}=PASS"
  fi
  if [ "$i" -lt 6 ]; then sleep 60; fi
done

if [ "$overall" -ne 0 ]; then
  echo "STABILITY_GATE=BLOCK"
  exit 1
fi
echo "STABILITY_GATE=PASS"
GATE
corlinman-prod$ echo "rc=$?"
```

- **预期**：六组指标全部
  `hermes=active nrestarts=0 tunnelports=1 gw=1 state=connected online=True err=None ...`，
  每组后有 `SAMPLE_N=PASS`，最后 `STABILITY_GATE=PASS`、`rc=0`。
- **任一采样命令失败、返回不可解析值、runtime state 不可读/缺结构、Hermes 非 active、重启数非 0、
  3011 监听数非 1、gateway 进程数非 1、`state!=connected` 或 `online=False`** ⇒ 对应样本 BLOCK，
  六次仍会采完以保留时间序列，但最终 `STABILITY_GATE=BLOCK`、非零退出，不能被后续正常样本覆盖。
- **`gw` 不是 1** ⇒ 有第二个 Hermes gateway 进程 = 两条持久 WS（H1 §4 就是这么发现问题的）。
  核对 `ssh cornna-cn 'systemctl is-active hermes.service'` 必须 `inactive`。
- **`tunnelports` 不是 1** ⇒ 隧道断了或有重复监听，回 §5.2 / §5.4。
- **`nrestarts` 在涨** ⇒ Hermes 在崩溃重启，先查它，别急着下"迁移无效"的结论。
- **`state=UNREADABLE`** ⇒ 文件不存在或权限不对。这**不代表** QQ 离线，
  只代表你这条观测路径坏了 —— 修它，别把它解释成账号状态。

> ### ⚠️ `updated_at` 陈旧**不等于** Hermes 死了（这条容易误判）
>
> 生产 `/opt/hermes/repo` 停在 `8911e2e0`（T1 未部署），是**旧版 adapter**。
> T3-design §1.2 / §11.3 实测：**文件冻结 15 小时而 `hermes.service` 全程 active**。
> 所以在旧 adapter 上，`updated_at` 只在**变化时**有意义，**陈旧不是证据**。
> 判断 Hermes 死活请看 `hermes=` 和 `gw=` 这两个**进程级**指标。
> 等 T1 部署后（T3-design §3.2.1 的 modern 格式），`updated_at` 才成为可用判据。

- **`online=None`** ⇒ **不要当成离线**（T3-design §11.2：`account_online is None` 是 *unknown*，
  adapter 对 `None` 的处理是直接 return）。看 `state=connected` 和进程级指标；
  真要确认账号，等 T3 哨兵的判定，或**在下一次 Hermes 停机窗口**用 §6.3 的探针。
- **如果六个样本里出现异常，而你确实需要 `get_status` 级别的证据**：
  **不要**绕过 §6.6.0 的闸。正确顺序是
  `systemctl stop hermes.service` → 跑探针 → 处理 → `start hermes.service`。
  停 Hermes 不消耗 QQ 登录尝试（H1 §8.5 的标准处置就是先停 Hermes），
  而多开一条 WS 会。

#### 6.6.2 互斥复验（不变式 §3.1 + flock §4.3）

这两条都不建立任何到 NapCat 的连接，Hermes active 时做是安全的：

```bash
$ printf 'cornna-cn-native=%s home-native=%s home-docker=%s\n' \
    "$(ssh cornna-cn systemctl is-enabled napcat-native.service 2>&1)" \
    "$(ssh home systemctl is-enabled napcat-native.service 2>&1)" \
    "$(ssh home systemctl is-enabled napcat.service 2>&1)"
# 原生路径执行：
home$ sudo -u napcat-native NAPCAT_APPIMAGE=/bin/true /opt/napcat-native/bin/launcher; echo "NATIVE_FLOCK_rc=$?"
# Docker 路径改执行下面两行（不要同时跑两种）：
home$ sudo -u napcat-native test -w /run/napcat-native.lock; echo "DOCKER_LOCK_WRITABLE_rc=$?"
home$ sudo -u napcat-native flock -n /run/napcat-native.lock -c true; echo "DOCKER_FLOCK_rc=$?"
```

- **预期（原生）**：`cornna-cn-native=masked home-native=disabled`，home-docker absent/masked；
  launcher 被 flock 拒绝，`NATIVE_FLOCK_rc=1`。
- **预期（Docker）**：`cornna-cn-native=masked home-docker=disabled`，home-native absent/masked；
  锁对服务用户可打开（`DOCKER_LOCK_WRITABLE_rc=0`），但已被 Docker unit 持有（`DOCKER_FLOCK_rc=1`）。
- **选中 unit 是 `enabled`**：本 runbook默认不开机自启（§4.7.1）；`disable` 不会停掉正在跑的服务。
- **选中 unit 是 `masked`**：回滚做了一半；只 unmask 选中项回 `disabled`。
- **未选 unit 不是 absent/masked，或 flock rc=0**：互斥不成立，`STABILITY_GATE` 后续结果不得放行。

#### 6.6.3 确认副作用为零

```bash
corlinman-prod$ sudo bash -s <<'CHK'
set -uo pipefail
LOG=$(mktemp); trap 'rm -f "$LOG"' EXIT
# 与 §6.2 同一条纪律：先证明日志读得到，再解释计数。不要用 `|| true`。
if ! journalctl -u hermes.service --since '-30min' --no-pager > "$LOG" 2>/dev/null; then
  echo "SIDEEFFECT_CHECK=BLOCK reason=journalctl_failed"; exit 1
fi
L=$(wc -l < "$LOG"); echo "JOURNAL_LINES=$L"
[ "$L" -gt 0 ] || { echo "SIDEEFFECT_CHECK=BLOCK reason=empty_log_window"; exit 1; }
N=$(grep -c -E 'send_msg|send_private_msg|send_group_msg' "$LOG"); RC=$?
[ "$RC" -ge 2 ] && { echo "SIDEEFFECT_CHECK=BLOCK reason=grep_error"; exit 1; }
echo "SEND_MSG_COUNT=$N"
[ "$N" -eq 0 ] && echo "SIDEEFFECT_CHECK=PASS" || { echo "SIDEEFFECT_CHECK=FAIL"; exit 1; }
CHK
corlinman-prod$ echo "rc=$?"
```

- **预期**：`JOURNAL_LINES=<非 0>`、`SEND_MSG_COUNT=0`、`SIDEEFFECT_CHECK=PASS`、`rc=0`。
  整个迁移过程**没有发出任何一条 QQ 消息**。
- **`reason=empty_log_window`**：读不到日志 ≠ 没发消息。修好再判（同 §6.2 的理由）。

---

## 7. 回滚

### 7.1 ⚠️ 回滚不是免费的：它也要重新扫码

> **回滚 = 再烧一次人工扫码 + 让 `cornna-cn` 的设备身份重新上线。**

理由和迁移时一模一样：`cornna-cn` 上那份设备身份的**登录票据已经失效了**。
H1 §3 实测：每次 `KickedOffLine` 时快速登录票据同时失效（两次 `quick_login_failed`）。
你在 §3.3 停掉它的那一刻，那个会话就结束了；重新启动它**不会**自动登录回来，
`ACCOUNT` 快速登录契约会失败，然后转人工扫码。

所以回滚的真实代价是：

| 代价 | 说明 |
|---|---|
| **一次人工扫码** | 必须有人在能访问 `cornna-cn` WebUI 的地方，拿着手机 |
| **一段停机** | 从停新机到旧机扫码成功，通常 10–20 分钟 |
| **两个设备指纹都被这个号用过** | 短时间内在两个地域的两台设备上登录，本身就是风控信号 |
| **观测数据断档** | §8 的会话账本会多一段 `end_censored` 区间，样本量本来就紧张 |

**因此：不要把回滚当成"随时可以试试"的后路。** 只有在新机被证明不可用时才回滚，
而"新机 2 小时被踢一次"**不是**回滚理由 —— 那正是 §8 要测的东西，旧机也是这个数。

### 7.2 云上的东西全部保留，一个都不删

回滚能成立的前提是 `cornna-cn` 侧原封不动。**以下路径在任何情况下都不许删**：

| 路径 / 对象 | 状态 |
|---|---|
| `cornna-cn:/opt/napcat/ntqq` | **保留**。这是旧设备身份，删了就永远回不去了 |
| `cornna-cn:/opt/napcat-native/config-candidate` | 保留 |
| `cornna-cn:/opt/napcat-native/runtime`（AppImage） | 保留 |
| `cornna-cn:/etc/systemd/system/napcat-native.service` | 保留（masked，符号链接指向 `/dev/null`，unit 文件本体在备份里或原位） |
| `cornna-cn:/opt/napcat/backups/t4-decommission-<TS>` | 保留 |
| `corlinman-prod:/etc/systemd/system/onebot-tunnel.service` | 保留（disabled，**没有 mask**，就是为了能回滚） |
| `corlinman-prod:/opt/hermes/data/*.bak-pre-T4-*` | 保留 |

⛔ 不要执行 `docker rm -v`、不要 `rm -rf /opt/napcat*`、不要清理"看起来没用的备份目录"。
H1 §9 的同一条纪律。

### 7.3 回滚步骤

⚠️ **回滚同样受 §3 的互斥约束。顺序不能反：先停新的，再放旧的。**

**7.3.1 停 Hermes、停新机 NapCat（按实际运行时二选一）**

先停 Hermes（两条路径共用）：

```bash
corlinman-prod$ sudo systemctl stop hermes.service
corlinman-prod$ systemctl is-active hermes.service
```

**原生路径**：

```bash
home$ sudo systemctl disable --now napcat-native.service
home$ systemctl is-active napcat-native.service
home$ sudo systemctl mask napcat-native.service
home$ systemctl is-enabled napcat-native.service
home$ pgrep -a -u napcat-native || echo NO_NAPCAT_PROCS
home$ sudo ss -ltn | grep -E '127\.0\.0\.1:(3001|6099)\b' || echo NO_LISTENERS
```

**Docker 路径**（停止但保留容器证据，绝不 `rm`）：

```bash
home$ sudo systemctl disable --now napcat.service
home$ systemctl is-active napcat.service
home$ sudo systemctl mask napcat.service
home$ systemctl is-enabled napcat.service
home$ sudo docker inspect -f 'running={{.State.Running}} restart={{.HostConfig.RestartPolicy.Name}}' napcat
home$ pgrep -af 'dbus-run-session|AppRun|[/]qq' || echo NO_QQ_PROCS
home$ sudo ss -ltn | grep -E '127\.0\.0\.1:(3001|6099)\b' || echo NO_LISTENERS
```

- **预期**：选中 unit `inactive` + `masked`、无 QQ 进程、无监听；Docker 分支额外是
  `running=false restart=no`，容器原地保留。
- **原生进程残留**：走 §3.3.3 那套 `systemctl kill` → `pkill -u napcat-native` 阶梯。
- **Docker 仍 running**：`sudo docker update --restart=no napcat && sudo docker stop napcat`，
  再查；不得 `docker rm`，回滚取证要保留。
- **⚠️ 互斥不变式此刻必须是** cornna old native + home 选中 runtime 都 masked；
  home 未选 runtime 仍须 absent/masked。**绝不允许任何两条路径同时可启动。**

**7.3.2 停家里的反向隧道，把 prod 的 3011 让出来**

```bash
home$ sudo systemctl disable --now onebot-rtunnel.service
home$ systemctl is-active onebot-rtunnel.service
corlinman-prod$ sudo ss -ltnp | grep -E '127\.0\.0\.1:(3011|6100)\b' || echo PORTS_FREE
```

- **预期**：`inactive`；`PORTS_FREE`。
- **3011 还在**：僵尸监听（§5.1.3）。等 90 秒让 `ClientAlive` 收掉，
  或 `corlinman-prod$ sudo pkill -u onebot-tunnel`。**3011 必须空出来**，
  否则旧的 `onebot-tunnel.service` 起不来。

**7.3.3 解除旧机 mask 并启动**

```bash
cornna-cn$ sudo systemctl unmask napcat-native.service
cornna-cn$ systemctl is-enabled napcat-native.service
cornna-cn$ sudo systemctl reset-failed napcat-native.service
cornna-cn$ sudo systemctl start napcat-native.service
cornna-cn$ systemctl is-active napcat-native.service
cornna-cn$ sudo ss -ltnp | grep -E '127\.0\.0\.1:(3011|6099)\b'
```

- **预期**：`unmask` 后 `is-enabled` 变回 `disabled`（它本来就是 disabled 但 active，
  见 §0.1）；`is-active` 为 `active`；6099 有监听，**3011 大概率还没有**（等扫码）。
- **`unmask` 之后 unit 不存在**：说明 §3.3.2 走了"移走 unit 文件再 mask"那条分支。
  从备份恢复：
  ```
  cornna-cn$ sudo cp /opt/napcat/backups/t4-decommission-<TS>/napcat-native.service.moved \
      /etc/systemd/system/napcat-native.service
  cornna-cn$ sudo systemctl daemon-reload
  ```
- **`start` 报 start-limit-hit**：`sudo systemctl reset-failed` 之后再 `start`（§4.7）。
- **如果 §3.3.4 里把 `napcat-native-health.service` 也停了**：
  `sudo systemctl enable --now napcat-native-health.timer` 恢复它。

**7.3.4 扫码（这就是"回滚不免费"的那一步）**

```bash
laptop$ ssh -N -L 6099:127.0.0.1:6099 cornna-cn
```

打开 `http://127.0.0.1:6099/webui/`，确认账号是 `2952532060`，**扫一次码**。
规则和 §6.2 完全相同：不输密码、不重启刷新、不截图外发。

**7.3.5 恢复正向隧道并放行 Hermes**

```bash
corlinman-prod$ sudo systemctl enable --now onebot-tunnel.service
corlinman-prod$ systemctl is-active onebot-tunnel.service
corlinman-prod$ sudo ss -ltn | grep -c '127.0.0.1:3011'
corlinman-prod$ sudo /opt/hermes/venv/bin/python /opt/hermes/probe/onebot_probe.py \
      ws://127.0.0.1:3011 --token-from env:/opt/hermes/data/.env \
      --hermes-guard local --expect-uin 2952532060
corlinman-prod$ sudo systemctl start hermes.service
```

- **顺序不能变**：探针 `PROBE_PASS=True` 之后才 `start hermes`（同 §6.5）。
- **探针 `InvalidStatus`**：如果 §4.8 里改过 NapCat 的 token，
  旧机那份配置的 token 可能和 prod `.env` 不一致了。
  用 §4.8 的指纹比对法核对 `cornna-cn` 那份 `onebot11_2952532060.json`。

### 7.4 回滚后的验收

```bash
$ printf 'cornna-cn-native=%s home-native=%s home-docker=%s\n' \
    "$(ssh cornna-cn systemctl is-enabled napcat-native.service 2>&1)" \
    "$(ssh home systemctl is-enabled napcat-native.service 2>&1)" \
    "$(ssh home systemctl is-enabled napcat.service 2>&1)"
corlinman-prod$ systemctl is-active hermes.service onebot-tunnel.service
corlinman-prod$ pgrep -c -f 'hermes.*gateway'
home$ sudo ss -ltn | grep -cE '127\.0\.0\.1:(3001|6099)'
cornna-cn$ systemctl is-active hermes.service
```

- **预期**：`cornna-cn-native=disabled`；home 实际使用过的 runtime 为 `masked`，另一项 absent/masked；
  prod 上 hermes 和 onebot-tunnel 都 `active`；gateway 进程 `1`；
  home 上监听计数 `0`；`cornna-cn:hermes.service` 仍 `inactive`（H1 §7.2 已 disable，别放回来）。

### 7.5 部分回滚（只回隧道，不回 QQ）—— 通常用不到

如果新机的 QQ 好好的，只是隧道方案有问题，**不要整体回滚**（那要白烧一次扫码）。
可以只把隧道换掉：在 prod 上临时用 `-L` 连回家里是行不通的（家宽无公网 IP），
所以实际的降级选项只有一个 —— **把 Hermes 挪到家里那台机器上跑**，
让它直接连 `ws://127.0.0.1:3001`。这是一次独立的变更，不在本 runbook 范围内，
但要知道这条路存在，别因为"隧道不好使"就去动 QQ。

---

## 8. 迁移后的观测

### 8.1 ⚠️ 采集器必须在迁移**之前**就跑起来

**这一节不设计新的采集机制。** 采集器已经由 T3 设计完成：
`docs/orchestration/tasks/T3-design.md` 的 `ops/qq_sentinel`
（systemd oneshot + timer，住在 `corlinman-prod`，每 60 秒读一次
`/opt/hermes/data/gateway_state.json`，**不新开任何到 NapCat 的连接**）。
它是"一个观测器，两个消费者"：Telegram 告警 + `sessions.jsonl` 会话账本。

**本 runbook 对它的唯一要求是时序：**

> ### 📌 `ops/qq_sentinel` 必须在 §6.0 第 7 步（停旧机）**之前**上线并开始写账本。
>
> 迁移之后你**永远无法**补采迁移前的数据。错过这个窗口，这次迁移就变成不可证伪的动作 ——
> 而 H1 §10 已经明确写了："样本仅 2 次，是线索不是定论"。

依赖链（三个任务的先后关系，别搞反）：

```
T1（account-state 加固部署到 /opt/hermes/repo）
   └→ gateway_state.json 里才会出现 auth_required
        └→ T3（ops/qq_sentinel 上线，开始写 sessions.jsonl）
             └→ 采一段迁移前 baseline
                  └→ T4（本 runbook：迁移）
                       └→ 采两周迁移后数据
                            └→ 对比中位会话时长
```

- **T1 没部署的后果**：T3-design §6.4 / §4.1 已经处理了 —— 采集器会判为
  `SOURCE_INCOMPATIBLE` 并发一条**降级告警**，而不是假装在工作。
  但那意味着**账本里的"被踢"永远不会出现**，baseline 是空的。

> ### ⛔ 2026-08-31 现状：`ops/qq_sentinel` **还不存在**
>
> 这不是"可能没部署"，是**确定还没实现**：
>
> - `docs/orchestration/tasks/T3-design.md` 开头写着「**设计完成，待 GPT 轨道实现**」；
> - 工作区里 `ops/` 目录本身就不存在（`ops/qq_sentinel/__init__.py`、`observe.py` 均无）；
> - `docs/orchestration/results/T3.md` 也不存在。
>
> ⇒ **按上一版 runbook 执行，会在 `python -m ops.qq_sentinel selftest` 这一步直接失败。**
> 上一版只给了四条使用命令，**没有实现、安装、systemd unit 部署和配置步骤** ——
> 操作者很可能把它当成"环境问题"跳过去，然后在**没有告警、没有账本**的情况下完成迁移。
>
> **本 runbook 不实现 T3**（那是 T3 的交付物，清单见 `T3-design.md` §12.1）。
> 本节改为一道**硬前置闸**：T3 没有真正跑起来并攒够 baseline，**迁移不许开始**。

#### 8.1.1 硬前置：T3 必须已实现 + 已部署 + selftest 通过

**在 §6.0 第 7 步（停旧机）之前**跑这段。任一项不过就**停止迁移**。

```bash
corlinman-prod$ sudo bash -s <<'GATE'
set -uo pipefail
bad=0
chk() { if [ "$2" -eq 0 ]; then printf 'OK    %s\n' "$1"; else printf 'BLOCK %s\n' "$1"; bad=1; fi; }

# (1) 代码真的在生产 repo 里
[ -f /opt/hermes/repo/ops/qq_sentinel/__main__.py ]; chk "ops/qq_sentinel 代码已部署" $?

# (2) unit 与 timer 已安装
systemctl cat hermes-qq-sentinel.service >/dev/null 2>&1; chk "hermes-qq-sentinel.service 已安装" $?
systemctl cat hermes-qq-sentinel.timer   >/dev/null 2>&1; chk "hermes-qq-sentinel.timer 已安装"   $?

# (3) timer 处于 active + enabled
[ "$(systemctl is-active  hermes-qq-sentinel.timer 2>&1)" = "active"  ]; chk "timer active"  $?
[ "$(systemctl is-enabled hermes-qq-sentinel.timer 2>&1)" = "enabled" ]; chk "timer enabled" $?

# (4) selftest 必须通过（它自己会校验数据源兼容性、A0 是否已清、状态文件是否新鲜）
(cd /opt/hermes/repo && sudo -u hermes /opt/hermes/venv/bin/python -m ops.qq_sentinel selftest) \
  >/tmp/t3-selftest.out 2>&1; rc=$?
chk "selftest 通过 (rc=$rc)" "$rc"
[ "$rc" -eq 0 ] || { echo "--- selftest 输出 ---"; tail -20 /tmp/t3-selftest.out; }
rm -f /tmp/t3-selftest.out

# (5) 账本文件存在
[ -f /var/lib/hermes-qq-sentinel/sessions.jsonl ]; chk "sessions.jsonl 存在" $?

[ "$bad" -eq 0 ] && echo "T3_DEPLOY_GATE=PASS" || { echo "T3_DEPLOY_GATE=BLOCK"; exit 1; }
GATE
corlinman-prod$ echo "rc=$?"
```

- **预期**：六行 `OK`，`T3_DEPLOY_GATE=PASS`，`rc=0`。
- **任何 `BLOCK`** ⇒ **停下，先把 T3 做完**。这不是可以"以后补"的东西：
  **迁移之后你永远无法补采迁移前的数据。**
- **`selftest` 报数据源不兼容 / `A0_SOURCE_INCOMPATIBLE`** ⇒ T1 还没部署到
  `/opt/hermes/repo`（实测该 repo 仍停在 `8911e2e0`）。
  按 T3-design §11.8 的部署顺序，**T1 → T3-A → 攒 baseline → 才轮到本 runbook**。
- **路径与解释器**：上面用的是 `/opt/hermes/venv/bin/python`。
  实际用哪个以 `systemctl show -p ExecStart hermes-qq-sentinel.service` 为准。

#### 8.1.2 硬前置：baseline 必须真的攒够（`wc -l ≥ 1` **不是**闸门）

> ### ⚠️ 上一版这里的放行条件是"账本行数 ≥ 1"，那不是闸门
>
> **一条旧的、空洞的、或仅仅是手写 marker 的 JSON 就能满足 `wc -l ≥ 1`。**
> 而 T3-design §10.6 定义的**真正**迁移前置是三条：
>
> | 条件 | 阈值 | 出处 |
> |---|---|---|
> | 哨兵连续运行 | **≥ 7 天** | `T3-design.md` §10.6.1（`MIN_BASELINE_DAYS=7`） |
> | `epoch=datacenter` 的**非删失且证据完整**区间数 | **≥ 10** | 同上（`MIN_SESSIONS_FOR_COMPARE=10`，口径见 T3 §10.2.2） |
> | `A0_SOURCE_INCOMPATIBLE` | **已清除**（即 T1 已部署） | `T3-design.md` §6.4 |
> | `A4_WRITER_UNVERIFIABLE` | **已清除**（沙箱硬条件全过） | `T3-design.md` §10.6.1 / §8.3 |
>
> 「非删失且证据完整」= `start_censored == false` **且** `end_censored == false`
> **且** `account_evidence != "unknown"`（T3 rev-1 的 §10.2.2 口径：
> 含 `account_evidence="unknown"` 的区间不得进中位数）。
> 被 Hermes 重启/崩溃打断的区间**不算数** —— 把一段"Hermes 崩了所以看起来只有 20 分钟"
> 的区间算进中位数，等于**用运维事故去证明家宽迁移失败**（T3-design §10.2）。

```bash
corlinman-prod$ sudo python3 - <<'PY'
import json, pathlib, subprocess, sys, datetime as dt, time

LEDGER = pathlib.Path("/var/lib/hermes-qq-sentinel/sessions.jsonl")
TIMER = "hermes-qq-sentinel.timer"
MIN_DAYS, MIN_N = 7, 10                       # T3-design §10.6 / §12.2

def parse_ts(s):
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))

def timer_continuous_days():
    """只用当前 timer 这次 active 代际计算；旧账本记录不能贡献连续运行时长。"""
    try:
        cp = subprocess.run(
            ["systemctl", "show", TIMER, "-p", "ActiveState",
             "-p", "ActiveEnterTimestampMonotonic"],
            text=True, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("BASELINE_GATE=BLOCK reason=timer_query_uncheckable type=%s" % type(exc).__name__)
        raise SystemExit(1)
    if cp.returncode != 0:
        print("BASELINE_GATE=BLOCK reason=timer_query_failed rc=%d" % cp.returncode)
        raise SystemExit(1)
    props = {}
    for line in cp.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    if props.get("ActiveState") != "active":
        print("BASELINE_GATE=BLOCK reason=timer_not_active state=%s" % props.get("ActiveState"))
        raise SystemExit(1)
    try:
        active_us = int(props["ActiveEnterTimestampMonotonic"])
        now_us = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        print("BASELINE_GATE=BLOCK reason=timer_timestamp_uncheckable type=%s" % type(exc).__name__)
        raise SystemExit(1)
    if active_us <= 0 or active_us > now_us:
        print("BASELINE_GATE=BLOCK reason=timer_timestamp_invalid")
        raise SystemExit(1)
    return (now_us - active_us) / 1_000_000 / 86400

continuous_days = timer_continuous_days()

if not LEDGER.exists():
    print("BASELINE_GATE=BLOCK reason=ledger_missing"); sys.exit(1)

rows, bad_lines = [], []
for i, line in enumerate(LEDGER.read_text(encoding="utf-8").splitlines(), 1):
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        bad_lines.append((i, "not-json")); continue
    if not isinstance(obj, dict):
        bad_lines.append((i, "not-object")); continue
    # 会影响放行判定的字段必须全部显式存在；缺失绝不能由 dict.get 的 None 伪装成安全值。
    # 手写 marker 行没有这些字段 —— 这里把它们抓出来，不让它们混进统计。
    required = ("schema", "duration_s", "epoch", "start_censored", "end_censored", "account_evidence")
    missing = [k for k in required if k not in obj]
    if missing:
        bad_lines.append((i, "missing:" + ",".join(missing))); continue
    if not isinstance(obj["start_censored"], bool) or not isinstance(obj["end_censored"], bool):
        bad_lines.append((i, "invalid:censored_flags_must_be_bool")); continue
    if not isinstance(obj["account_evidence"], str) or not obj["account_evidence"].strip():
        bad_lines.append((i, "invalid:account_evidence")); continue
    rows.append(obj)

if bad_lines:
    print("BASELINE_GATE=BLOCK reason=non_session_lines_in_ledger")
    for i, why in bad_lines[:10]:
        print("  line %d: %s" % (i, why))
    print("  ⇒ sessions.jsonl 里混进了非 session 记录（多半是手写的 marker）。")
    print("     T3 的账本是 append-only 的 session 流，marker 不属于这里 —— 见 §8.2。")
    sys.exit(1)

if not rows:
    print("BASELINE_GATE=BLOCK reason=empty_ledger"); sys.exit(1)

dc = [r for r in rows if r["epoch"] == "datacenter"]
# 「非删失且证据完整」= 两端字段都明确为 false，且 account_evidence 不是 unknown。
# 必填/类型校验已在上面完成，所以不存在 missing -> None -> `not None` 放行。
good = [r for r in dc
        if r["start_censored"] is False and r["end_censored"] is False
        and r["account_evidence"] != "unknown"]
# 账本跨度仅作诊断；它不再贡献「连续运行 ≥7 天」的放行判定。
starts = [parse_ts(r["started_at"]) for r in rows if "started_at" in r and r["started_at"]]
now = dt.datetime.now(dt.timezone.utc)
span_days = (now - min(starts)).total_seconds() / 86400 if starts else 0.0

print("LEDGER_LINES=%d" % len(rows))
print("EPOCH_DATACENTER_TOTAL=%d" % len(dc))
print("EPOCH_DATACENTER_NONCENSORED=%d (need >=%d)" % (len(good), MIN_N))
print("TIMER_CONTINUOUS_ACTIVE_DAYS=%.2f (need >=%d)" % (continuous_days, MIN_DAYS))
print("LEDGER_SPAN_DAYS_DIAGNOSTIC=%.2f (not a gate)" % span_days)
print("EPOCHS_SEEN=%s" % sorted({r["epoch"] for r in rows}))

fail = []
if len(good) < MIN_N:          fail.append("noncensored_datacenter_sessions<%d" % MIN_N)
if continuous_days < MIN_DAYS: fail.append("timer_continuous_days<%d" % MIN_DAYS)
if fail:
    print("BASELINE_GATE=BLOCK reason=" + ",".join(fail)); sys.exit(1)
print("BASELINE_GATE=PASS")
PY
corlinman-prod$ echo "rc=$?"
```

- **预期**：`EPOCH_DATACENTER_NONCENSORED` ≥ 10、`TIMER_CONTINUOUS_ACTIVE_DAYS` ≥ 7、
  `BASELINE_GATE=PASS`、`rc=0`。`LEDGER_SPAN_DAYS_DIAGNOSTIC` 只用于排查，**无论多大都不能放行**。
- **`reason=non_session_lines_in_ledger`** ⇒ 有人往 `sessions.jsonl` 里手写了 marker。
  **这正是上一版 §8.2 教你做的事，已经改掉了**（见 §8.2）。
  处置：把那些行**从账本里移出去**（先 `cp -a` 备份整个文件），恢复成纯 session 流。
- **`reason=noncensored_datacenter_sessions<10`** ⇒ 样本不够。
  **不要**通过放宽阈值来"解决"它 —— H1 §3 用 **n=2** 得出"~2 小时固定会话寿命"，
  T3-design §10.5 专门为此写了"任一侧 `n<10` 必须打印『样本不足，不下结论』"的防呆。
  真正能加快攒样本的是 **T3 的告警**（把"被踢→有人去扫码"的延迟从小时级压到分钟级），
  不是改阈值。
- **`reason=timer_continuous_days<7`** ⇒ 当前这次 timer 连续 active 的时间还不够；旧账本记录不补时长。
- **`reason=timer_query_*` / `timer_not_active` / `timer_timestamp_*`** ⇒ 连续性无法确认，**阻断**；
  不得回退到 `LEDGER_SPAN_DAYS_DIAGNOSTIC`。
- **`EPOCHS_SEEN` 里出现了 `home_broadband`** ⇒ epoch 已经被改过了（§8.2 第 2 步）。
  迁移前不该出现；出现说明有人提前改了，回去改回 `datacenter` 并核对哪些区间被打错了标。

> **连续性判据的边界（如实记录，但不再失败开放）**：
> 本闸现在检查 `hermes-qq-sentinel.timer` 当前这次 `ActiveState=active` 的
> `ActiveEnterTimestampMonotonic`，用同一启动代际的 monotonic clock 计算连续 active 天数。
> timer 一旦 stop、重启或主机重启，该时间都会重新起算；**旧 `sessions.jsonl` 记录完全不能贡献时长**。
> 这比账本跨度强：它确实证明 timer 本身在当前代际没有中断。
> 它仍不能单独证明“七天里的每一拍都成功执行”——这部分由 §8.1.1 的当前 selftest、
> `sessions.jsonl` 结构检查与 ≥10 段证据共同约束。若 T3 将来在 selftest/report 暴露逐拍连续性字段，
> 可以在独立对账后替换本判据；在此之前，**查询失败一律 BLOCK，绝不退回账本跨度近似**。

### 8.2 迁移在账本上必须留下的边界

> ### ⚠️ 上一版这一节有三个各自独立的错误，全部已修
>
> 1. **往 `sessions.jsonl` 里手写 marker 行。** T3 的账本是**一行一段在线区间**的
>    append-only session 流（`T3-design.md` §10.2），**没有 `type=marker` 这种记录类型**。
>    手写进去的行会被 `ledger`/`report` 当成普通 session 处理，或直接让解析失败 ——
>    §8.1.2 的闸门现在会把这种行**抓出来并阻断**。
> 2. **用 `sed -i` 全文件替换 placeholder。** `sed -i` 会**重写整个 JSONL**，
>    违反 T3 的 append-only 约束（`T3-design.md` §11.9：「只追加不重写」）。
> 3. **备份写在动作之后。** 代码块先 `tee -a` 再 `sed -i`，`cp -a ...bak` 只出现在
>    下面的说明文字里 —— 按代码块顺序执行等于**改完才备份**，违反硬约束 11。
>
> **正确的做法是：账本里的迁移边界由 `epoch` 字段承载，不是由 marker 行承载。**
> 这是 T3 已经设计好的机制（`T3-design.md` §10.4），T4 直接用它，不发明新东西。

**分工**：

| 需求 | 载体 | 为什么 |
|---|---|---|
| **统计上区分迁移前后** | T3 的 **`epoch`** 字段（`datacenter` → `home_broadband`） | T3 的正式契约，`report --compare` 直接消费它 |
| **人类可读的迁移事件时间线** | **另一个文件** `/opt/hermes/backups/t4-migration/events.jsonl` | 它不是 session，就不该待在 session 流里 |

#### 8.2.1 迁移开始：先备份，再记事件（§6.0 第 7 步**之前**）

**备份在最前面** —— 这是硬约束 11 的顺序要求：

```bash
corlinman-prod$ TS=$(date -u +%Y%m%dT%H%M%SZ); echo "TS=$TS"
corlinman-prod$ sudo cp -a /var/lib/hermes-qq-sentinel/sessions.jsonl \
      /var/lib/hermes-qq-sentinel/sessions.jsonl.bak-pre-T4-$TS
corlinman-prod$ sudo ls -l /var/lib/hermes-qq-sentinel/sessions.jsonl.bak-pre-T4-$TS
corlinman-prod$ sudo cmp /var/lib/hermes-qq-sentinel/sessions.jsonl \
      /var/lib/hermes-qq-sentinel/sessions.jsonl.bak-pre-T4-$TS && echo BACKUP_IDENTICAL
```

- **预期**：备份文件存在、大小非 0、`BACKUP_IDENTICAL`。
- **注意**：本 runbook **不修改** `sessions.jsonl`。这份备份是"万一别人动了它"的兜底，
  也是迁移前账本状态的快照证据。

**再记事件**（写到**另一个文件**，一次追加一条完整 JSON，**不用 placeholder + `sed`**）：

```bash
corlinman-prod$ sudo install -d -m 700 /opt/hermes/backups/t4-migration
corlinman-prod$ sudo python3 - "$TS" <<'PY'
import json, pathlib, sys, datetime as dt
ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
rec = {
    "event": "t4_migration_begin",
    "ts": ts,
    "backup_ts": sys.argv[1],
    "epoch_before": "datacenter",
    "epoch_after": "home_broadband",
    "note": "停旧机(cornna-cn)前。epoch 尚未切换，切换在 8.2.2。",
}
p = pathlib.Path("/opt/hermes/backups/t4-migration/events.jsonl")
with p.open("a", encoding="utf-8") as f:          # append-only，一次写一条完整 JSON
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("WROTE " + json.dumps(rec, ensure_ascii=False))
PY
corlinman-prod$ sudo tail -1 /opt/hermes/backups/t4-migration/events.jsonl
corlinman-prod$ sudo python3 -c 'import json,sys;[json.loads(l) for l in open("/opt/hermes/backups/t4-migration/events.jsonl")];print("EVENTS_JSONL_VALID=true")'
```

- **预期**：`WROTE {...}` 且 `ts` 是真实 UTC 时间（不是 `REPLACE_UTC`）；
  `EVENTS_JSONL_VALID=true`。
- **时间戳在写入时就算好了**，所以**没有 placeholder，也就不需要 `sed -i`** ——
  这是修掉上一版第 2、3 两个错误的关键。

#### 8.2.2 切换 `epoch`（在停机窗口内，QQ 已离线时做）

> **为什么在停机窗口内切**：
>
> **T3 rev-1 已经把跨 epoch 的行为定义清楚了**（`T3-design.md` §10.4.1，
> 「epoch 切换是一个显式事务，区间**永不跨 epoch**」）：哨兵每一拍开头比对环境变量，
> 发现变了就把当前区间以 `end_reason="epoch_change"` / `end_censored=true` 关掉，
> 再以 `start_censored=true` 开一段新的。
> ⇒ **即使你在有区间开着的时候切，数据也不会被污染** —— 这一点由 T3 保证，不用 T4 操心。
>
> **但仍然应该在停机窗口内切**，理由变成了一条更简单的：
> 在停机窗口里 QQ 已离线 ⇒ **没有 `ONLINE` 区间开着** ⇒ 切换**不会白白烧掉一段区间**。
> 在有区间开着时切，会多产生一段 `end_reason=epoch_change` 的删失区间 ——
> 不致命（删失区间本来就不进中位数），但样本量本来就紧张，能不浪费就不浪费。
>
> **执行位置：§3.4 之后（Hermes 与旧 NapCat 都已停），§6.1 启动新机之前。**
>
> **另外照 T3 §10.4.1 的建议**：迁移当天可以同时设一个覆盖扫码窗口的 `mute_until`（T3 §5.4），
> 免得扫码过程中的 A1/A2 告警把人吵一遍。**账本照记，静音只影响发送。**
> 具体命令以 T3 实现的 CLI 为准。

```bash
corlinman-prod$ sudo install -d -m 755 /etc/systemd/system/hermes-qq-sentinel.service.d
corlinman-prod$ sudo tee /etc/systemd/system/hermes-qq-sentinel.service.d/20-t4-epoch.conf >/dev/null <<'CONF'
# T4 家宽迁移：切换会话账本的分组键。见 T3-design.md §10.4 与 runbook §8.2.2。
# 切换时机必须是"没有未闭合在线区间"的时刻（停机窗口内），否则跨界区间归属未定义。
[Service]
Environment=HERMES_QQ_SENTINEL_EPOCH=home_broadband
CONF
corlinman-prod$ sudo systemctl daemon-reload
corlinman-prod$ systemctl show -p Environment hermes-qq-sentinel.service | tr ' ' '\n' | grep EPOCH
```

- **预期**：打印 `HERMES_QQ_SENTINEL_EPOCH=home_broadband`。
- **打印的还是 `datacenter`，或什么都没打印** ⇒ drop-in 没生效。最可能的原因是
  T3 的实现用 `EnvironmentFile=` 提供这个变量，且该文件在 drop-in **之后**被读取。
  **以 `T3-design.md` / `ops/qq_sentinel/README.md` 记录的机制为准**，
  改那里的值，然后**仍然用 `systemctl show -p Environment` 确认**。
  > **⚠️ 存疑**：T3 尚未实现（§8.1），所以"epoch 到底从哪里读"在执行时才能确定。
  > 上面的 drop-in 是 systemd 通用做法，**但它不是 T3 契约的一部分**。
  > 唯一可靠的验收是下面 §8.2.3 那条「新区间的 `epoch` 确实变成了 `home_broadband`」。
- **timer 触发的是 oneshot service**，所以新的环境变量在**下一拍**（≤60 秒）才生效，
  不需要也不应该去 `restart` 那个 oneshot service 本身。

#### 8.2.3 迁移完成：记事件并**验证 epoch 真的切了**（§6.0 第 14 步之后）

```bash
corlinman-prod$ sudo python3 - <<'PY'
import json, pathlib, datetime as dt
ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
rec = {"event": "t4_migration_done", "ts": ts,
       "epoch_now": "home_broadband", "backend_host": "home",
       "note": "Hermes 已放行并验收通过（§6.5/§6.6）。"}
p = pathlib.Path("/opt/hermes/backups/t4-migration/events.jsonl")
with p.open("a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("WROTE " + json.dumps(rec, ensure_ascii=False))
PY
corlinman-prod$ sudo cat /opt/hermes/backups/t4-migration/events.jsonl
```

- **预期**：文件里正好两行，`t4_migration_begin` 和 `t4_migration_done`，
  两个 `ts` 之间就是这次迁移的停机窗口。**迁移窗口到这里才算可靠闭合。**

**然后必须验证 epoch 真的落到了新区间上**（这一步不做，前面全是猜）。
新机登录成功后**至少等一段区间闭合**（即至少经历一次掉线/重启，或直接看下面的宽松判据）：

```bash
corlinman-prod$ sudo python3 - <<'PY'
import json, pathlib, sys
rows = []
for line in pathlib.Path("/var/lib/hermes-qq-sentinel/sessions.jsonl").read_text().splitlines():
    line = line.strip()
    if line:
        rows.append(json.loads(line))
tail = rows[-5:]
print("LAST5_EPOCHS=%s" % [r.get("epoch") for r in tail])
print("EPOCH_COUNTS=%s" % {e: sum(1 for r in rows if r.get("epoch") == e)
                           for e in sorted({r.get("epoch") for r in rows})})
PY
corlinman-prod$ sudo cat /var/lib/hermes-qq-sentinel/state.json 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print("OPEN_INTERVAL_EPOCH=%s" % d.get("epoch"))' 2>/dev/null || echo "(state.json 结构以 T3 实现为准)"
```

- **预期**：`state.json` 里那段**未闭合区间**的 `epoch` 是 `home_broadband`；
  一旦它闭合，`EPOCH_COUNTS` 里会出现 `home_broadband: 1`。
- **新区间的 epoch 还是 `datacenter`** ⇒ §8.2.2 的切换没生效。
  **立刻回 §8.2.2 修**，并在执行记录里写明"从 `<时间>` 到修好为止的区间被打错了标"——
  这些区间在 §8.4 分析时要手工排除。
- **`EPOCH_COUNTS` 里 `home_broadband` 一直是 0，但 `state.json` 显示对了** ⇒
  正常，只是还没有区间闭合（新机一直在线是**好事**）。等它自然产生。

**统计口径**：`t4_migration_begin` 到 `t4_migration_done` 之间那段停机
本来就会被 T3 标成 `end_censored=true`（Hermes 被 stop ⇒ `planned_stop`/`observer_down`，
见 `T3-design.md` §10.2），而**删失区间默认就被排除在中位数之外**。
所以**不需要手工排除**，T3 的 schema 已经处理了这件事 —— 这正是"用 T3 的契约而不是自己发明"的收益。

### 8.3 会话记录的字段：**完全采用 T3 的 schema，本 runbook 不新增字段**

> ### ⚠️ 上一版在这里自己发明了一套 schema，与 T3 不兼容
>
> 上一版要求账本"再带三个字段"：`env`（`cloud-aliyun-gz`/`home-residential`）、
> `backend_host`、`egress_ip_changed`，并在 §8.4 执行 `report --group-by env`。
> **T3 的契约完全不是这样**（`T3-design.md` §10.2 / §10.4 / §10.5）：
>
> | T4 上一版 | **T3 的契约（权威）** |
> |---|---|
> | `env=cloud-aliyun-gz` / `home-residential` | **`epoch=datacenter` / `home_broadband`** |
> | `report --group-by env` | **`report --compare`** / `report --epoch <名>` |
> | marker 行只有 `type`/`event`/环境字段/`ts` | session 记录必须有 **`schema`** / **`duration_s`** / **`epoch`** |
>
> ⇒ `report --group-by env` 很可能因**未知参数直接失败**；
> `env` 这个字段根本不存在于 T3 的记录里，分组会是空的。
>
> **本次修订的决定：完全采用 T3 的契约，T4 不引入任何新字段。**
> 理由：这是**最小改动**，而且方向正确 —— T3 是账本的所有者，
> 让消费者（T4）去改生产者（T3）的 schema，需要先改 T3 设计、实现和测试，
> 而 T3 **连实现都还没有**（§8.1）。为一个还没写的组件增加需求，只会让它更晚交付。

**三个字段的逐条处置：**

| 上一版字段 | 处置 | 替代 |
|---|---|---|
| `env` | **删除** | T3 的 **`epoch`**，语义完全等价（§8.2.2 切换它） |
| `backend_host` | **删除** | 冗余：`epoch` 已经区分了两台机器。真需要溯源时查 §8.2 的 `events.jsonl`（里面记了 `backend_host`） |
| `egress_ip_changed` | **移出账本** | 独立的本地日志 + 事后人工关联，见 §8.3.1。**它不在 T3 schema 里，不要往里塞** |

#### 8.3.1 出口 IP 变化：独立日志（**不进 `sessions.jsonl`**）

**为什么仍然需要它**：家宽会**重拨换 IP**。H1 §4.1 明说"频繁换 IP 本身是风控信号"。
如果被踢总是紧跟在重拨之后，那根因是**重拨**，不是"住宅 IP 没用" ——
修法是关掉路由器的定时重拨，比换机器便宜得多。**没有这份日志就无法排除这个混杂因素。**

**为什么不进账本**：`egress_ip_changed` 不在 T3 的 schema 里（§8.3 的表）。
往 append-only 的 session 流里塞一个 T3 不认识的字段，会被 §8.1.2 的闸门当成非法行拦下。

> ### ⚠️ 上一版这个脚本会把"根本没采集到"报告成成功
>
> `[ -z "$IP" ] && exit 0` —— 断网、DNS 挂了、`ipinfo.io` 限流、`curl` 没装，
> **全部静默 `exit 0`**。于是日志里"没有新行"同时意味着
> 「**IP 没变**」和「**根本没采到**」两件事，而它们对结论的影响正好相反：
> 前者支持"住宅 IP 稳定"，后者说明你**什么都不知道**。
> 已改为：采集失败**写一条显式的 `unknown` 记录**，并以**非零退出**让 systemd 记下失败。

```bash
home$ sudo tee /usr/local/sbin/t4-egress-log >/dev/null <<'SH'
#!/bin/sh
# t4-egress-log — 记录出口 IP 的变化。只写变化，不写心跳。纯本地文件，不上报、不告警。
# 失败可诊断：采集不到时写一条 unknown 记录并非零退出，绝不把"没采到"伪装成"没变化"。
# ⛔ 这个脚本除了写本地文件之外什么都不做：不通知、不重启、不告警。
#    告警是 T3 的事（它有 T3-design §8 那一整套权限边界）。
set -u
F=/var/log/t4-egress-ip
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
last_val() { tail -1 "$F" 2>/dev/null | awk '{print $2}'; }

if ! command -v curl >/dev/null 2>&1; then
  printf '%s unknown reason=curl_missing\n' "$(now)" >> "$F"
  echo "t4-egress-log: curl not found" >&2
  exit 2
fi

IP=$(curl -s --max-time 8 https://ipinfo.io/ip 2>/dev/null)
RC=$?
# 只接受形如 a.b.c.d 的响应；限流页/HTML/空响应都算失败
if [ "$RC" -ne 0 ] || ! printf '%s' "$IP" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
  LAST=$(last_val)
  # 连续 unknown 不重复刷屏，但第一条一定要写下来
  if [ "$LAST" != "unknown" ]; then
    printf '%s unknown reason=fetch_failed curl_rc=%s\n' "$(now)" "$RC" >> "$F"
  fi
  echo "t4-egress-log: fetch failed (curl_rc=$RC)" >&2
  exit 1
fi

LAST=$(last_val)
[ "$IP" = "$LAST" ] && exit 0        # 没变化，不写噪声
printf '%s %s\n' "$(now)" "$IP" >> "$F"
exit 0
SH
home$ sudo chown root:root /usr/local/sbin/t4-egress-log
home$ sudo chmod 755 /usr/local/sbin/t4-egress-log
home$ sudo sh -n /usr/local/sbin/t4-egress-log && echo EGRESS_SYNTAX_OK
home$ sudo install -m 644 -o root -g root /dev/null /var/log/t4-egress-ip
home$ sudo /usr/local/sbin/t4-egress-log; echo "rc=$?"
home$ sudo cat /var/log/t4-egress-ip
```

- **预期**：`EGRESS_SYNTAX_OK`；`rc=0`；文件里出现一行 `<UTC时间> <IP>`。
- **`rc=1` 且写了 `unknown reason=fetch_failed`** ⇒ 采集失败（断网 / DNS / 限流）。
  **这是正确行为**：它把"不知道"如实记下来了。查网络后重跑。
- **`rc=2` `reason=curl_missing`** ⇒ `sudo apt-get install -y curl`。

**service + timer（上一版只说了一句"用 systemd timer 每 5 分钟跑一次即可"，没给任何东西）**：

```bash
home$ sudo tee /etc/systemd/system/t4-egress-log.service >/dev/null <<'UNIT'
[Unit]
Description=T4 egress IP change logger (local file only, no network side effects)
Documentation=file:///opt/hermes/docs/orchestration/tasks/T4-home-migration-runbook.md
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/t4-egress-log
# 采集失败时让 systemd 记下 failed，这样 `systemctl list-units --failed` 能看见。
# 不设 Restart= ：它是 oneshot，由 timer 驱动，重试等下一拍即可。
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/log
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=
UNIT
home$ sudo tee /etc/systemd/system/t4-egress-log.timer >/dev/null <<'UNIT'
[Unit]
Description=Run T4 egress IP logger every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
UNIT
home$ sudo systemd-analyze verify /etc/systemd/system/t4-egress-log.service && echo EGRESS_UNIT_OK
home$ sudo systemctl daemon-reload
home$ sudo systemctl enable --now t4-egress-log.timer
home$ systemctl is-active t4-egress-log.timer; systemctl is-enabled t4-egress-log.timer
home$ systemctl list-timers t4-egress-log.timer --no-pager
home$ sudo systemctl start t4-egress-log.service; systemctl is-failed t4-egress-log.service
```

- **预期**：`EGRESS_UNIT_OK`；timer `active` + `enabled`；`list-timers` 显示下次触发时间；
  `is-failed` 返回 `active`（即"不是 failed"）。
- **`is-failed` 返回 `failed`** ⇒ 手工跑 `/usr/local/sbin/t4-egress-log` 看 rc 和 stderr，
  按上面的 `rc=1`/`rc=2` 分支处理。
- **这个 unit 的沙箱可以很紧**（`MemoryDenyWriteExecute=yes`、`CapabilityBoundingSet=` 空），
  因为它是 shell + curl，**不是 Electron** —— §4.6 那张表的禁忌只针对 QQ 单元。
- **回滚 / 卸载**：
  ```bash
  home$ sudo systemctl disable --now t4-egress-log.timer
  home$ sudo rm -f /etc/systemd/system/t4-egress-log.{service,timer}
  home$ sudo systemctl daemon-reload
  # 日志文件 /var/log/t4-egress-ip 保留（它是观测数据，不删）
  ```

**怎么和账本关联**（人工，在 §8.4 分析阶段做，**不写进 `sessions.jsonl`**）：

```bash
corlinman-prod$ sudo python3 -m ops.qq_sentinel report --epoch home_broadband
home$ sudo cat /var/log/t4-egress-ip
```

把两边的时间戳并排看：**每一段 `home_broadband` 区间的结束时刻，是否总是紧跟在
一条新的 IP 记录之后**（比如 5 分钟以内）。

- **总是紧跟** ⇒ 根因是**重拨换 IP**，不是"住宅 IP 这条路走不通"。
  修法：关掉路由器的定时重拨（或改成凌晨固定时间），**不要因此回滚**。
- **毫无相关** ⇒ 重拨不是混杂因素，§8.4 的结论可以直接读。
- **日志里 `unknown` 占比很高** ⇒ **这段数据不能用来排除重拨因素**。
  先修采集（多半是 `ipinfo.io` 限流，换一个只返回纯 IP 的端点），
  **不要**把"没采到"当成"没换过 IP"。

### 8.4 两周后怎么读这份数据

**用 T3 的正式接口**（`T3-design.md` §10.5）：

```bash
corlinman-prod$ sudo /opt/hermes/venv/bin/python -m ops.qq_sentinel report --compare
# 需要单看一侧 / 交叉检查时间窗时：
corlinman-prod$ sudo /opt/hermes/venv/bin/python -m ops.qq_sentinel report \
      --epoch datacenter [--since ISO] [--until ISO]
corlinman-prod$ sudo /opt/hermes/venv/bin/python -m ops.qq_sentinel report \
      --epoch home_broadband
```

> **⚠️ 不要用 `report --group-by env`。** 上一版写的是这条 —— 它**不是 T3 的接口**，
> 而且 `env` 这个字段根本不存在于 T3 的记录里（§8.3）。
> 真跑它的结果多半是**未知参数直接报错**，或者更糟：分组为空却看起来"跑成功了"。

`--compare` 会并排打印两个 epoch 的六项（`T3-design.md` §10.5 的正式清单）：
`exposure_h` / `failures` / `censored`（按 `end_reason` 分列）/ `n_complete` /
`median / p25 / p75 / min / max` / `longest_ongoing_h`，以及中位数差值。

> **`n < 10` 时 `median / p25 / p75 / min / max` 五项全部**打印「样本不足，不下结论」而不是数字
> （T3 rev-2 把 `min/max` 也纳入了防呆）。`exposure_h` / `failures` / `censored` 照常输出数字 ——
> 它们是事实，不是结论。

> ### ⚠️ 迁移**后**的判定规则**不是**"再收满 ≥7 天 / ≥10 段"
>
> 那是 §8.1.2 的**迁移前** pilot 闸门口径，**不能照搬到迁移后**。
> `T3-design.md` §10.6.2 把这个错误单独拎出来讲了，它是**自毁式**的：
>
> > 如果家宽迁移确实管用、账号连续在线超过 7 天，那么这 7 天里**一段完成区间都不会产生**。
> > 于是「迁移成功」这个结果，恰恰无法通过验收。
>
> 后果：为了凑够 10 段而等待 = **主动等待失败发生**；凑出来的 10 段还天然全是失败事件周围的区间，
> 有选择偏差；最坏的情况是诱使人**去人为制造掉线来"补样本"**。
>
> ⇒ **迁移后走 T3 §10.6.2 的右删失判定**：
> `POST_MIGRATION_MIN_DAYS=7`（最短观察期）、`POST_MIGRATION_MAX_WAIT_DAYS=30`（到点必须出结论）。

**判读规则（写死，避免事后自己骗自己）。观察期满 7 天后，`report --compare` 会按
T3 §10.6.2 的表「先命中先返回」直接打印结论行，下面是你要能看懂的四种结论：**

| # | 条件 | 结论 | 下一步 |
|---|---|---|---|
| 1 | **两侧 `n_complete ≥ 10`** | 并排中位数 + 差值。**这是最强的结论** | 按下面的"中位数怎么读"那张表判 |
| 2 | `failures == 0` 且暴露时间 `T` 足够 | **「显著改善」**（零事件的 95% 单侧上界，rule of three） | 直接采信。**这是迁移成功时最可能命中的一行** |
| 3 | `failures > 0` 但 `n_complete < 10`，且已到 30 天 | 「证据不足以做中位数比较」+ 暴露/失败/删失三元组 | **由人判断**，不要继续等 |
| 4 | 其余（未满 7 天，或未到 30 天且样本仍在积累） | 「观察中」+ 当前三元组 + 距各判据还差多少 | 继续等 |

**第 2 行怎么算**（能手算复核，这是 T3 选它而不选 Kaplan–Meier 的理由）：
基线掉线率 `λ₀ = failures_datacenter / exposure_h_datacenter`（次/小时）。
**这两个数都取自 T3 rev-2 的同一个 `rate_rows` 总体**（只含 `account_evidence == "online"`
且非 `verification_degraded` 的区间，`T3-design.md` §10.5）—— 分子是分母的严格子集。
手算复核时**不要**用别处看到的"所有区间时长之和"当分母，那会把观测者根本无法验证的时段
算成"无失败的暴露时间"，让结论偏向好看。
迁移后零失败、暴露 `T` 小时 ⇒ 95% 单侧上界 `λ_upper = 3/T`；
**`3/T < λ₀/2` ⇒ 「显著改善（掉线率至少降低一半）」**，否则打印还需要多少小时（`T_needed = 6/λ₀`）。

> 举个数：基线若是"平均每 2 小时掉一次"（`λ₀ = 0.5`），
> 则 `T_needed = 6/0.5 = 12` 小时 —— **家宽下连续在线 12 小时零掉线，就足以在 95% 置信下
> 排除"掉线率没变"**。这远比"等 10 次掉线"现实。

**命中第 1 行时，中位数怎么读：**

| 条件 | 结论 | 下一步 |
|---|---|---|
| **任一侧 `n_complete < 10`** | **不下结论** | `T3-design.md` §10.5 有硬性防呆：`n<10` 时**必须**打印`结论：样本不足（datacenter n=X, home_broadband n=Y），不下结论`而不是打印差值。**看到差值就以为有结论，正是 H1 §3 用 n=2 得出"~2 小时固定会话寿命"的那个错误**（`report --epoch` 单侧 `n<10` 同样拒绝下结论） |
| `home_broadband` 中位 ≥ 8 小时 | 迁移**有效**，环境是主因 | 记录进 H1 §10，把"~2 小时"那条从"待观测"改成"已解释" |
| `home_broadband` 中位仍在 1.5–2.5 小时 | 迁移**无效**，是账号/协议层面 | **不要回滚**（回滚也不会更好，且要再烧一次扫码，见 §7.1）。转而查：账号年龄、协议版本、是否有并行登录残留 |
| `home_broadband` 中位介于 3–8 小时 | 改善但未解决 | 继续观察到 30 天上限，再按第 3 行由人判断 |

**无论命中哪一行，都要一起看的两组旁证：**

| 条件 | 结论 | 下一步 |
|---|---|---|
| 被踢时刻**总是**紧跟一条新的出口 IP 记录（§8.3.1 的日志，5 分钟以内） | 根因是**重拨换 IP**，不是机房 vs 住宅 | 去路由器上关掉定时重拨；这是比换机器便宜得多的修法 |
| 出口 IP 日志里 `unknown` 占比高 | **无法排除重拨这个混杂因素** | 先修 §8.3.1 的采集，别把"没采到"读成"没换过 IP" |
| 任一侧**删失区间占比 > 30%**（`censored` / 全部区间数） | **数据不可信** | Hermes 自己在频繁重启/崩溃，先修那个。删失区间本来就被排除在中位数之外（`T3-design.md` §10.2），占比过高说明剩下的样本没有代表性 |
| 任一侧 `n_complete` 远小于「非删失区间数」 | **观测质量不可信**，且**这一条不会被上一行捕捉到** | T3 rev-2 收窄了 `censored` 的定义（只数 `start_censored` / `end_censored`），而 `account_evidence != "online"` 与 `verification_degraded` 的区间是**另一层**排除。两者差得多 ⇒ 哨兵大量时间处于"说不清账号状态"，先查 §8.1.2 的 `A4_WRITER_UNVERIFIABLE` 与 `A0_SOURCE_INCOMPATIBLE` 是否真的清干净了 |

> **报告必须打印暴露时间、失败数、删失数三元组**（T3 §10.5），
> 而不是只报一个"完成区间的中位数"。**看到只有中位数的报告，说明 T3 的实现没做完，
> 别拿它下结论。**

**一个必须诚实面对的采样问题**：每次被踢都需要**人工扫码**才能开始下一段会话。
所以样本量不由时间决定，而由"**你多快发现并去扫码**"决定 ——
如果每次都要 9.7 小时才发现，两周最多攒到十几段。

**这正是 T3 的告警器不只是"顺手做的"的原因**：它把发现延迟从小时级压到分钟级，
直接决定了两周内能不能攒够 `n ≥ 10`。**没有 T3，§8 这一节在两周内不会有结论。**

### 8.5 迁移后头 48 小时的额外巡检

除了自动采集，头两天每天手工看一眼（**只读，不改任何东西**）：

```bash
$ ssh corlinman-prod 'systemctl is-active hermes.service; \
    systemctl show -p NRestarts hermes.service; \
    ss -ltn | grep -c "127.0.0.1:3011"'
$ ssh home 'systemctl is-active napcat-native.service napcat.service onebot-rtunnel.service; \
    systemctl show -p NRestarts napcat-native.service napcat.service onebot-rtunnel.service'
$ ssh home 'sudo tail -3 /var/log/t4-egress-ip'
$ printf 'cornna-cn-native=%s home-native=%s home-docker=%s\n' \
    "$(ssh cornna-cn systemctl is-enabled napcat-native.service 2>&1)" \
    "$(ssh home systemctl is-enabled napcat-native.service 2>&1)" \
    "$(ssh home systemctl is-enabled napcat.service 2>&1)"
```

- **预期**：hermes `active` / `NRestarts=0` / 3011 计数 `1`；
  home 的**选中 NapCat unit**和 `onebot-rtunnel` active，未选 NapCat unit absent/masked；
  选中 NapCat unit 的 `NRestarts` **必须恒为 0**（它是人工闸，自己涨了说明有人在手动重启）；
  互斥不变式是 cornna old native masked、home 选中项 disabled、未选项 absent/masked（§3.1/§4.7.1）。
- **`onebot-rtunnel` 的 `NRestarts` 在涨**：正常，家宽会断。
  但一天涨超过 20 次就该查线路了（`journalctl -u onebot-rtunnel -n 100`）。
- **选中的 NapCat unit `NRestarts` 不为 0**：**去看是谁重启的。**
  H1 §8.4 的 watchdog 幽灵不该在新机上重生；不要通过切换到另一运行时掩盖。

---

## 9. 本 runbook 与 H1 的关系

`docs/migration-corlinman/H1-napcat-hardened.md` 是这套 QQ 后端的既有权威文档。
本 runbook **不取代它**，只在它之上做一次环境迁移。这一节逐条交代继承关系，
免得后来人拿两份文档对不上时不知道该信哪个。

### 9.1 照抄 H1 已验证的（不做改动，只换机器）

| 本文位置 | 抄自 | 原文结论 |
|---|---|---|
| §2 设备身份不复制 | H1 §4.1 | 设备身份目录"**绝不能删、复制到并行实例或重建**——删掉会被视为新设备并增加验证风险" |
| §3 单实例互斥 | H1 §6.1 | "禁止让同一个 QQ 账号在两个 NapCat 实例并行登录" |
| §4.2 不用 `NAPCAT_QUICK_PASSWORD` | H1 §4.1 | 只用官方 `ACCOUNT` 快速登录契约，失败后转人工扫码 |
| §4.4 `StartLimitIntervalSec` 放 `[Unit]` | H1 §4.1 | "放 `[Service]` 会被 systemd 静默忽略" |
| §4.5 Docker `restart=no` | H1 §4.1 §6 | systemd + Docker 双重无限重启必须避免 |
| §4.8 config 700 / 文件 600 | H1 §8.1 | 权威机曾经 755/644，非 root 可读 `webui.json`，已修 |
| §4.9 不给 WebUI 套 `auth_basic` | H1 §5.1 | 二维码刷新走 WebSocket，浏览器不给 WS 附带 Basic 凭据，会反复弹框 |
| §5.3 `RestartSec=30` | H1 §8.2 | 原值 5s 太急 |
| §5.3 `StartLimitIntervalSec=0` | H1 §8.2 | 原值 600s/8 次会在持续断网时耗尽额度后**永久 failed**，必须人工 reset |
| §5.3 SSH `ConnectTimeout` | H1 §8.2 | 原本没有，网络黑洞时 `Restart` 用不上 |
| §5.6 无损演练用 `/run/systemd/system` | H1 §8.3 | 临时 test unit + 未使用端口，不碰生产 endpoint |
| §6.2 §6.4 不发测试消息 | H1 全篇 | 每一节的验收都写着"没有发送任何 QQ 测试消息" |
| §6.3 探针只调两个动作、token 进程内读 | H1 §6.2 §7.1 | "短生命周期探针……token 从进程内读取，不放到命令行或输出中；只调用 `get_status` 和 `get_login_info`" |
| §6.5 探针通过后才放行 Hermes | H1 §6.2 步骤 5、§8.9 | 双层放行 |
| §6.6 六个分钟级样本 | H1 §7.3 §8.9 | 切主后取六个只读快照，全部一致才算通过 |
| §7.2 云上东西全保留 | H1 §9 | "不要执行 `docker rm -v`，不要删除……或备份目录" |
| §3.3.4 watchdog 不许复活 | H1 §8.4 | `napcat-watch.py` 是"第二个会消耗登录尝试的生命周期管理器"，已被有意 stop/disable |

### 9.2 本次新增（H1 里没有对应物）

| 本文位置 | 新增内容 | 为什么需要 |
|---|---|---|
| §0.4 | 宿主机选型闸门（7×24 / 不休眠 / 断电自恢复 / Linux+systemd） | H1 全程在云主机上，这些条件默认成立。家里的机器不成立 |
| §0.5 §5.5 | **WebUI 反向端口 6100** | 迁移把扫码控制台放到了 CGNAT 后面。这是本次迁移**新引入**的风险，必须配对策 |
| §1.2 | 隧道方向反转（`-R` 而非 `-L`） | 家宽无公网 IP / CGNAT |
| §1.4 | 否决 `autossh`，用 `ssh` + systemd | H1 的隧道用的就是裸 ssh + systemd；这里把理由写清楚，防止有人"升级"成 autossh |
| §1.5 | 旧 `onebot-tunnel.service` 必须停（3011 争用） | H1 时代只有一条隧道，不存在争用 |
| §3.1 | **掩码互斥**（`systemctl mask` 作为跨机互斥的锁） | H1 靠"人记得别启动"。跨机 + 家宽断连场景下，需要一个结构性的、可 `is-enabled` 查询的锁 |
| §5.1.2 | `restrict,port-forwarding,permitopen,permitlisten` | **同时兑现 H1 §10 的遗留项**："后续可把该 key 收紧为专用低权限账号和 `permitopen=127.0.0.1:3011`" |
| §5.2.1 | home→prod 的 Hermes 状态 forced-command key | home 探针必须检查真实 Hermes 宿主；只允许查询 `systemctl is-active --quiet hermes.service`，无法确认即拒绝建 WS |
| §5.1.3 | prod sshd `ClientAliveInterval/CountMax` | 家宽"人间蒸发"式掉线会在 prod 上留下僵尸监听，导致重连一直撞 `remote port forwarding failed`。机房环境很少遇到 |
| §5.3 | `ExitOnForwardFailure` / `ServerAlive*` / `-F /dev/null` | H1 的隧道参数表里没有这三项 |
| §5.4 | 家宽三种断法的自愈路径表 + 拔网线演练 | 全新场景 |
| §4.6 | **加固项落点表**（哪些会打死 Electron） | H1 §8.3 那套沙箱参数是给只读探针用的。直接抄到 QQ 单元上，`MemoryDenyWriteExecute=yes` 会让它**必然启动失败** |
| §4.7 | 把 `StartLimitBurst=1` 的语义讲穿：**实际上是人工闸** | H1 §10 只说"应改成 on-failure + 合理退避"。这里说明为什么更彻底才是对的 |
| §8.1 §8.2 | **T3 采集器的部署闸 + baseline 闸 + `epoch` 切换时机** | H1 §10 只说"要观测"，没说观测什么、也没说怎么保证观测在迁移前就位。字段与报告接口**全部沿用 T3 的契约**（`epoch` / `report --compare`），T4 不新增 schema |
| §8.3.1 | 出口 IP 变化日志（**独立文件，不进账本**）+ service/timer | 家宽会重拨换 IP，H1 §4.1 说"频繁换 IP 本身是风控信号"。不排除这个混杂因素就无法解释 §8.4 的结果。它不在 T3 schema 里，所以做成独立日志 + 事后人工关联 |

### 9.3 有意偏离 H1（并说明为什么这里是对的）

| 项 | H1 | 本文 | 理由 |
|---|---|---|---|
| 隧道 `Restart=` | `on-failure`（H1 §8.2 保持不变） | **`always`** | ssh 在"对端优雅关闭"路径下可能以 0 退出，`on-failure` 会让隧道静静停在 `inactive (dead)`，**无失败态、无日志、无告警**。隧道**不消耗任何 QQ 登录尝试**，所以 H1 §8.4"第二个生命周期管理器"的顾虑在这里不适用。详见 §5.3.1 |
| NapCat `Restart=` 策略 | H1 §4.1 用 `on-failure` + 45s；§10 记权威机当时仍是 `Restart=always`（**但 §10 说的是已不存在的 `napcat.service`**，见 §4.7 的换算表） | `on-failure` + 45s + **`StartLimitBurst=1`**，且**不 enable 开机自启**（§4.7.1） | 更严。等价于"失败即停手等人"。对一个已被踢两次的低信任新号，**限制登录尝试频率 > 可用性**。注意：这里保留 `on-failure` 而**不是**改成 `Restart=no` —— 人工闸由 `StartLimitBurst=1` 提供，`no` 只会丢掉 45s 内一次性抖动的自愈 |
| 运行形态 | Docker | **原生 AppImage** | 权威机 2026-08-31 实测早已是原生（§0.1）。且容器 entrypoint 的"痕迹处理"（伪造 cgroup/cpuset）与本次"让账号看起来像普通住宅用户"的目标方向相反 |
| `mlikiowa/napcat-docker` digest | H1 §5 记 `sha256:1336a777…` / image ID `sha256:b132563d…` | 只作为**参考值**，以本机 `RepoDigests` 为准 | 本地 image ID 是 per-arch 的，arm64 上必然不同。§1.6 明确写了"不一致是正常的，别误以为镜像被换了" |

### 9.4 H1 里已经过时的（不要再照着做）

- **H1 §4 / §5 的 Docker 拓扑表**：`cornna-cn` 上没有 napcat 容器，
  `napcat.service` 这个 unit 也不存在。用 §0.1 的对照表。
- **H1 §10 里所有出现 `napcat.service` 的地方**（"权威 `cornna-cn:napcat.service` 当前仍是
  systemd `Restart=always`"）：那个 unit **不存在**。当前 unit 是 **`napcat-native.service`**，
  它的 `Restart=` 实际值必须**当场 `systemctl show` 确认**，不要照抄 H1 的 `always`。
  换算表和核对命令见 **§4.7**。
- **H1 §10 里"环境仍存在 `NAPCAT_QUICK_PASSWORD`"**：指的是**没有单元在读**的残留
  `/etc/napcat/napcat.env`，**不是**活单元读的 `/etc/napcat-native/env`（后者实测干净）。
  这条遗留的真实剩余部分只是卫生清理，不是当下的风控杠杆。详见 **§4.2** 的两份文件对照表
  和 `decisions.md` D-QQ-05。
- **H1 §6.1 / 00-PLAN D81 的"宿主 3011 只是 Docker 端口代理"**：
  原生形态下 3011 就是 OneBot 自己在监听。**但这条在 §4.5 的 Docker 回退路径上仍然有效**，
  所以不是废弃，是"仅适用于 Docker 路径"。
- **H1 §4 的"OneBot 在 `127.0.0.1:3011` → 容器 `3001`"**：迁移后
  home 监听 3001、prod 落点 3011，中间是 SSH 隧道而不是 Docker 端口映射。

### 9.5 「出口 IP 保持不变」那条为什么**不**构成冲突

H1 §4.1 有一行加固记录：

> **出口 IP 保持不变** | 仍走阿里云广州。**不建议**改走境外代理节点：对国内 QQ 号更可疑，
> 且频繁换 IP 本身是风控信号

有人会拿这条来说"T4 违反了 H1"。**不成立**，理由三条：

**① 那条否决的是"境外代理节点"，不是"换一个国内住宅 IP"。**
原文的理由写得很清楚："对国内 QQ 号更可疑"。一个国内个人 QQ 号的流量从境外 VPS / 代理出去，
是**典型的异常**；而同一个号从国内某个城市的家庭宽带出去，是**典型的正常**。
两者在风控模型里的方向**正好相反**。本次迁移的落点是国内住宅宽带，
不是境外节点，不在那条否决的射程内。

**② "频繁换 IP 是风控信号"说的是"频繁"，不是"换一次"。**
本次迁移是**一次性的、有计划的**环境变更，之后长期稳定在同一条家宽线路上
（同城、同 ASN、同运营商）。这与"来回切代理"是两回事。
本 runbook §2.3 那条"永不再迁"正是在把这次变更钉成**唯一一次**。

**③ 这条记录的上下文是 corlinman staged 实例，不是一个通用禁令。**
H1 §4.1 的标题就是"**corlinman staged 实例（已停用）的加固内容**"。
它记的是"在当时那个决策点上，我们选择不动出口 IP"，
而不是"这个账号的出口 IP 永远不许变"。H1 §10 自己还留着一条待观测：

> 那个 **~2 小时被踢** 的规律是否在新部署上复现。样本仅 2 次，是线索不是定论。
> 若复现，说明是账号/协议层面而非环境；若不复现，则此前的删库重建（新设备指纹）可能是诱因。

**本次迁移恰恰是在做 H1 §10 要求的那个实验** —— 换一个环境，看那条规律复不复现。
§8 的观测设计就是这个实验的读数方法。

**唯一需要留意的**是原文里"频繁换 IP"那半句：家宽会**重拨换 IP**。
所以 §8.3.1 专门加了一份出口 IP 变化日志（独立文件，不进账本）—— 如果被踢总是紧跟在重拨之后，
那说明 H1 那半句在家宽环境里是对的，修法是**关掉路由器的定时重拨**，
而不是"住宅 IP 这条路走不通"。**这是本 runbook 对 H1 那条记录的正面回应，不是绕过它。**

---

## 附录 A：（已删除）macOS 路径

**2026-08-31 修订删除。** 原附录 A 给的是 systemd↔launchd 概念对照表，**不是可执行的迁移路径** ——
没有 LaunchDaemon plist、没有 NapCat 启动配置、没有 identity/config/权限准备、没有 tunnel plist、
没有 no-auto-restart 闸门、没有验收与回滚命令。而 §0.4 当时又把 macOS 写成"唯一 7×24 机器时可以走"，
两者合起来会把人骗进一条走不通的路。

**现在的口径**：macOS **不是**受支持的宿主路径，理由见 §0.4 的 ⛔ 框（四条，核心是
「§4.7 的人工登录闸在 launchd 上做不出来」）。要么换 x86_64 Linux 小主机，
要么在 Mac 上跑一个开机自启的 Linux 虚拟机，然后在虚拟机里从头执行本 runbook。

---

## 附录 B：一页纸执行清单

打印这一页，做一项划一项。详细命令和异常分支回正文对应节。

**迁移前（QQ 仍在线，可以从容做）**

- [ ] `ws_url` 两处一致（§1.3）
- [ ] 确认机器架构，决定原生 / Docker 路径（§1.6）
- [ ] 主机准备：时区、禁休眠、装包、防火墙、BIOS 断电自启（§4.1）
- [ ] 建 `napcat-native` / `obtun` 用户，建目录，**确认 `ntqq/` 是空的**（§2.4）
- [ ] 按 §1.6 选运行时：native 传 AppImage 并核对 sha256；Docker 写入精确 RepoDigest 且 `DOCKER_DIGEST_GATE=PASS`
- [ ] 写 `/etc/napcat-native/env`，确认**没有** `NAPCAT_QUICK_PASSWORD`（§4.2）
- [ ] preflight 就位；native launcher 或 Docker unit 的同锁 flock 就位，语法/行为检查通过（§4.3/§4.5）
- [ ] 写选中的 NapCat unit：native §4.4 或 Docker §4.5；确认 `StartLimitIntervalUSec=1h`，未选 unit absent/masked
- [ ] prod 建 `onebot-tunnel` 用户 + `restrict,permitlisten` key（§5.1）
- [ ] home→prod 的 Hermes 探针守门 key 已按 §5.2.1 限死 forced command；Hermes active 时查询 rc=0（证明查的是真宿主）
- [ ] prod sshd `ClientAliveInterval 30` / `CountMax 3`，`sshd -t` 通过（§5.1.3）
- [ ] home 写 `onebot-rtunnel.service`，确认 `StartLimitIntervalUSec=0`（§5.2）
- [ ] 隧道断网演练 + `/run` 临时 unit 演练（§5.4 §5.6）
- [ ] **`ops/qq_sentinel` 上线并确认在写 `sessions.jsonl`**（§8.1）
- [ ] **T3 部署闸 `T3_DEPLOY_GATE=PASS`**（代码/unit/timer/selftest 全绿）（§8.1.1）
- [ ] **baseline 闸 `BASELINE_GATE=PASS`**（`epoch=datacenter` 非删失 ≥10、timer 当前代际连续 active ≥7 天；账本跨度不放行）（§8.1.2）
- [ ] 备份 `sessions.jsonl`（`cmp` 确认一致），再写 `t4_migration_begin` 事件到
      `/opt/hermes/backups/t4-migration/events.jsonl`（**不写进账本**）（§8.2.1）
- [ ] 部署 `t4-egress-log` service + timer，手工跑一次 rc=0（§8.3.1）

**停机窗口（目标 15 分钟内）**

- [ ] prod: `stop hermes.service`（§3.4）
- [ ] cornna-cn: 备份 unit/config 元数据（§3.3.0）
- [ ] cornna-cn: `stop` + **`mask`** + 验证 `start` 会失败（§3.3.1 §3.3.2）
- [ ] cornna-cn: 进程树死透、3011/6099 无监听（§3.3.3）
- [ ] cornna-cn: watchdog 三件套 `WATCHDOG_GATE=PASS`（各自 `inactive`+`masked`）；
      health timer 允许 enabled 但 ExecStart 无恢复动词；无 cron / 无容器（§3.3.4）
- [ ] prod: `disable --now onebot-tunnel.service`，**3011 空出来**（§3.4）
- [ ] **`bash /tmp/t4-gate35.sh` ⇒ `GATE35=PASS`，抄下全部输出**（§3.5）
- [ ] prod: 切换 `epoch=home_broadband`（**必须在此刻，QQ 已离线、无未闭合区间**）（§8.2.2）
- [ ] home: 拷 `onebot11_2952532060.json`，改 host/port，核对 token 指纹（§4.8）
- [ ] home: 只 `start` 选中的 `napcat-native.service` 或 `napcat.service`，6099 有监听（§6.1）
- [ ] **扫码一次**，3001 出现监听；用对应 `NAPCAT_UNIT` 跑日志闸，`KICKED_CHECK=PASS`（§6.2）

**放行**

- [ ] home 本地探针先显示 `PROBE_GUARD=ok(...host=ssh:<PROD_HOST>)`，再 `PROBE_PASS=True` / `UIN=2952532060`（§6.3）
- [ ] home: `enable --now onebot-rtunnel.service`，prod 上 3011/6100 监听且都是 `127.0.0.1:`（§5.2）
- [ ] prod 经隧道探针 `PROBE_PASS=True`（§6.4）
- [ ] prod: `start hermes.service`，日志 `ws://127.0.0.1:3011` + `bot account is 2952532060`，`connect_failed=0`，gateway 进程 =1（§6.5）
- [ ] **铁律闸复验**：Hermes active 后跑探针，**期望 `PROBE_REFUSED=hermes_active` / `rc=3`**（§6.6.0）
- [ ] 六个分钟级样本全绿（**只读 runtime state，不建新连接**）（§6.6.1）
- [ ] 互斥不变式：cornna old native masked；home 选中 unit disabled、未选 unit absent/masked；同一 flock 拒绝第二实例（§6.6.2）
- [ ] `SIDEEFFECT_CHECK=PASS`（`send_msg` 计数 0，且日志确实读到了）（§6.6.3）
- [ ] home: 确认选中的 NapCat unit 是 **`disabled`**、未选项 absent/masked（**不 enable**，§4.7.1）
- [ ] 写 `t4_migration_done` 事件到 `events.jsonl`（**不写进账本**）（§8.2.3）
- [ ] **验证 epoch 真的切了**：新区间 / `state.json` 的 `epoch` 是 `home_broadband`（§8.2.3）

**之后**

- [ ] 头 48 小时每天巡检一次（§8.5）
- [ ] 观察满 **7 天**（`POST_MIGRATION_MIN_DAYS`）后跑
      `python -m ops.qq_sentinel report --compare`
      ⚠️ **迁移后不要求"再攒 10 段"** —— 那是 §8.1.2 的迁移**前**口径，
      照搬到迁移后是自毁式的（迁移成功 = 不掉线 = 攒不出区间）。见 §8.4 的 ⚠️ 框
- [ ] 按 §8.4 的四行结论表判读；**零失败时看 rule-of-three 那一行**，
      不要为了凑样本去等掉线
- [ ] 最迟 **30 天**（`POST_MIGRATION_MAX_WAIT_DAYS`）必须出结论，不再继续等
- [ ] 对照 `/var/log/t4-egress-ip`，排除"重拨换 IP"这个混杂因素（§8.3.1）
- [ ] 结论回写进 `docs/migration-corlinman/H1-napcat-hardened.md` §10
