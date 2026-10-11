# T4 修订结果 — 逐条处置

**修订对象**：`docs/orchestration/tasks/T4-home-migration-runbook.md`（原 2300 行 / 127 KB）
**修订依据**：`docs/orchestration/tasks/T4-rev.md`（修订单，权威）＋ `docs/orchestration/audits/T4.md`（17 findings）
**权威文档**：`docs/migration-corlinman/H1-napcat-hardened.md`、`docs/orchestration/decisions.md`、
`docs/orchestration/tasks/T3-design.md`（**T3 契约为准，T4 适配**）

**修订前备份**：`docs/orchestration/tasks/T4-home-migration-runbook.md.bak-pre-rev-20260831T102507Z`
（`cp -a`，UTC 时间戳，见文末验证输出）

**未触碰**：生产（无 SSH）、`docs/orchestration/tasks/T3-design.md`（另一 agent 并行修改中）、
`docs/migration-corlinman/H1-napcat-hardened.md`（权威运维文档，只读引用）。

---

## 处置总表

| # | 严重度 | 处置 | 改动位置 |
|---|---|---|---|
| F1 | 中（主线程下调） | 部分采纳：保留 `Restart=on-failure`，只补「开机 enable」取舍 | §4.7.1（新）、§3.1、§6.6、附录 B |
| F2 | 致命 | 全采纳：非空即硬阻断 | §2.4 |
| F3 | 致命 | 全采纳：五条闸重写为 fail-closed 脚本 | §3.5 |
| F4 | 致命 | 全采纳：preflight 失败关闭 + root helper | §4.3 |
| F5 | 致命 | 全采纳：只查存在性，删打印 | §4.9 |
| F6 | 高 | 全采纳：baseline 闸门改为 T3 真实前置 | §8.1 |
| F7 | 高 | 全采纳：完全采用 T3 `epoch` + `report --compare` 契约 | §8.2 §8.3 §8.4 §9.2 |
| F8 | 高 | 全采纳：三件套逐个核对 + health timer 白名单 | §3.3.4 |
| F9 | 高 | 全采纳：统一 config 源，删自动 `docker rm -f` | §4.5 |
| F10 | 高 | 全采纳：区分 `napcat.service` / `napcat-native.service` 与两份 env | §4.2 §4.7 §9.3 §9.4 |
| F11 | 高 | 全采纳：分离采集与匹配，删 `\|\| true` | §6.2 |
| F12 | 中 | 全采纳：先建用户组再建目录 | §2.4 §5.1.2 |
| F13 | 中 | 全采纳：补 service/timer，采集失败写 `unknown` | §8.3 |
| F14 | 中 | 全采纳：备份前置；UFW 状态先存 | §4.1 §8.2 |
| F15 | 中 | 全采纳：补 `ExitOnForwardFailure` + 端口预检 + `timeout` | §5.1.2 |
| F16 | 中 | 全采纳：探针移到 Hermes 启动前，写成可测试断言 | §6.3 §6.4 §6.6（新 §6.6.0） |
| F17 | 低 | 全采纳：删除 macOS 作为受支持路径 | §0.4、附录 A（已删空） |

---

## 逐条明细

### F17 macOS 附录不是可执行路径 —— 已删除该路径

**处置：采纳「明确删除 macOS 作为受支持路径」这个选项**，不补齐。

**为什么选删而不是补齐**：附录 A 缺的不是几条命令，而是整条路径的核心 ——
§4.7 的「每小时至多一次登录尝试」人工闸依赖 `StartLimitIntervalSec` / `StartLimitBurst`，
**launchd 没有对应物**。那把闸是本次迁移的核心反风控设施（对一个 2026-08-28 才启用、
已被踢两次的新号，限制登录尝试频率 > 可用性）。在 launchd 上"近似"它意味着完全手动启动，
那就不再是同一个方案。同理 §4.4 / §5.2 的全部 systemd 沙箱在 launchd 上无对应物。
补一份未经测试的 plist 只会制造第二份需要审计的假设。

**改动位置**：
- §0.4：`Linux + systemd` 行标为**硬性**；原「优先用 Linux，只有没别的机器时才走 macOS」
  那段替换为 ⛔ 框，列出四条不可执行的具体理由，并给出唯一替代（Mac 上跑开机自启 Linux VM，
  **标注为本 runbook 未验证的独立路径**）。
- §1.6「确认机器架构」：`没有 systemctl ⇒ ... 或走附录 A` 改为「**这里没有分支**」。
- 附录 A：整节删除，留一段墓碑说明为什么删（避免后来人以为是漏写）。

### F2 设备身份目录：非空必须硬阻断（致命）

**处置：全采纳。删除了 `mv` / `rm` / 重建三条路径，改成硬阻断。**

上一版 §2.4 在 `/opt/napcat/ntqq` 非空时给了两条处置：`mv` 到 `.bak-pre-T4-…` 再 `install -d`
重建空目录；以及「如果是从别处复制来的就删掉那个复制品」。**两条都直接违反 H1 §4.1
「设备身份目录绝不能删、复制到并行实例或重建」**，且第二条还会销毁取证与回滚材料 ——
更根本的问题是：**你无法在删之前确定它"只是"复制品**。

**改动位置**：§2.4 整节重写为三个小节（§2.4.1 用户组 / §2.4.2 目录 / §2.4.3 身份目录闸门）。

- §2.4.3 现在是一段 `sudo bash -s` 闸门脚本，只有两种结局：
  **路径不存在** ⇒ 创建空挂载点、`IDENTITY_GATE=PASS`；
  **路径以任何形式存在**（目录/文件/符号链接/挂载点，无论空不空）⇒ `IDENTITY_GATE=BLOCK`、
  非零退出、**runbook 到此为止**。文中明写「**没有 C**」。
- 阻断分支只做**只读取证**（`ls -ld` / `stat` / `entries=` / 前 20 条路径 / `IS_MOUNTPOINT`）
  并要求抄进执行记录。
- 「隔离」在文中被显式定义为**把这台机器排出本次迁移**（不启动、不扫码、不接隧道），
  **不是**把目录挪走 —— 因为审计建议里的「隔离」很容易被读成 `mv`，这里堵死这个歧义。
- 给了一张「可能来源 → 判断线索 → 处置」表（真实身份 / 跨机复制品 / 失败残骸），
  三种来源的处置**都是阻断**，区别只在于人要去查什么。
- 明写：**本 runbook 不提供 `mv` / `rm -rf` / 重建的命令**；要动它是一次有人批准、单独记录的操作。

### F12 用户/组创建顺序不可执行（中）

**处置：全采纳。**

原 §2.4 先 `install -d -o napcat-native -g napcat-native`，之后才 `useradd` ——
用户/组不存在时 `install` 直接失败（`install: invalid user`）。
`obtun` 的创建则埋在 §5.1.2 的一个括号说明里，且没有创建私有组。

**改动位置**：
- 新 §2.4.1：**先**建 `napcat-native` 和 `obtun` 两个账号，都用 `--user-group` 显式创建同名私有组
  （不写这个参数时部分发行版会把新用户塞进 `users` 共享组，而身份目录是 700，组一错权限模型就散）；
  给出 `id` / `getent group` 的**逐条预期输出**（`groups=` 里有且只有自己、组成员列表为空）。
- 加了一条硬检查：`groups=` 里出现 `docker`/`sudo`/`wheel`/`adm` 任一 ⇒ 停下并移除，
  并说明 `docker` 组等价 root（与 F4 的 root helper 决策呼应）。
- 给了 `--user-group` 不被支持时的 `groupadd` 回退。
- §5.1.2 改为**先验证** `id obtun && getent group obtun` 再 `install -d`，
  并明写「不要在这里临时 `useradd`」（临时建容易漏私有组）。

### F3 §3.5 五条放行闸不可执行且漏检（致命）

**处置：全采纳。整节从表格改写为 fail-closed 脚本。**

原表格把管道符写成 Markdown 行内代码里的**字面反斜杠**（`\|\|`、`\|`），
粘贴执行时 shell 把 `\|` 当字面字符，**预期的 `DEAD` / `0` 不会可靠产生**；
第 3 行还只跑 `pgrep -u napcat-native`，一个「无 `napcat-native` 用户进程但仍持有 3011/6099」
的残留可以让闸门**看起来通过**。

**改动位置**：§3.5 整节。现在是一个落盘再执行的脚本 `/tmp/t4-gate35.sh`
（要求落盘而不是直接粘贴，避免换行断开导致某项检查被静默跳过）。

- **真管道，在 fenced code block 里**，不再有任何 `\|`。
- **最终闸门同时验证**：unit `inactive` + `masked`、`napcat-native` 用户进程、
  **全机 QQ 进程树**（`dbus-run-session|AppRun|[/]qq`，覆盖「换个用户跑」）、
  **3011**、**6099**、**cgroup.procs**、**容器**（docker + podman）。
- **失败关闭**：SSH 本身失败（rc=255）、命令不存在（`MISSING_CMD=`）、
  `sudo -n` 拿不到权限（`DOCKER_NAPCAT_RUNNING=UNCHECKABLE`）—— 全部 BLOCK。
  文中明写「**查不了 ≠ 安全**」。
- **硬阻断而非人眼**：每段用 `if ...; then ... exit 1; fi`，最后统一 `GATE35=PASS` / `GATE35=BLOCK`
  并以非零退出。
- 异常分支表里专门列了一行 `PROCS_QQ_TREE=present` 而 `PROCS_NAPCAT_NATIVE=absent` ——
  **正是上一版会漏掉的那种残留**。
- 补了一条纪律：`pgrep` 模式误报**也必须查清再放行，不许放宽模式**
  （误报成本几分钟，漏报成本是一次并行登录 + 加计风控）。

### F4 preflight 失败开放（致命）

**处置：全采纳。拆成 root helper + 非特权 preflight，全部失败关闭，并补行为测试。**

原 preflight 在 `User=napcat-native` 下跑 `docker ps ... 2>/dev/null | grep -qi napcat`：
Docker 存在但无权访问 socket ⇒ `docker ps` 失败 ⇒ 错误被 `2>/dev/null` 吞 ⇒
条件为假 ⇒ 跳过 ⇒ **仍打印 `preflight: OK`**。`ss` 缺失/失败同样失败开放。
**「查不了」被映射成了「安全」，而那正是这把闸唯一要挡的东西。**

**改动位置**：§4.3 整节重写为 §4.3.1–§4.3.5。

- **§4.3.1 明确不把 `napcat-native` 加进 `docker` 组**，并写明理由（`docker` 组等价 root：
  能把宿主任意目录挂进容器再读写；为一条只读检查把跑 QQ 的账号提权成 root 等价是最差交易）。
  改用 systemd 的 **`ExecStartPre=+/usr/local/sbin/napcat-preflight-root`** ——
  `+` 前缀表示以完整权限运行、忽略 `User=`，这就是审计要的「root-owned 只读 helper」。
- **§4.3.2 root helper**：容器（docker **和 podman**）+ 全机 QQ 进程树。
  「存在但查不了」一律 `REFUSE`。要求属主 `root:root` / `755`（可写就等于给了提权路径）。
- **§4.3.3 非特权 preflight**：端口 + 自身进程 + 锁。
  **端口参数化为 `NAPCAT_PREFLIGHT_PORTS`（unit 里 `Environment=` 传 `3001 6099`）**，
  取消了原来「整份复制 `cornna-cn` 的脚本，然后逐行读一遍改硬编码端口」这条
  **没有验证点**的路径（没人能证明改对了）；旧脚本降级为只读交叉参考。
  要求用 `sudo -u napcat-native` 跑一次，理由是 root 跑通不代表服务用户跑得通。
- **§4.3.5 行为测试（五个 rc，要求抄进执行记录）**：
  A 端口 3001 被占 ⇒ rc=1；B 端口 6099 被占 ⇒ rc=1；C QQ 进程存在 ⇒ rc=1；
  D **docker 存在但 `docker ps` 失败 ⇒ rc=1**（这一条就是上一版的致命缺陷）；
  E docker 报出 napcat 容器 ⇒ rc=1。
  文中明写「`preflight: OK` 本身证明不了任何事，一个 `exit 0` 的空脚本也能通过那种验证」。
- **§4.4 unit** 相应加了两条 `ExecStartPre`，并给出验证命令
  `systemctl show -p ExecStartPre | tr ';' '\n' | grep -E 'path=|flags='`，
  要求 root helper 那条的 `flags=` 里出现 **`privileged`**（`+` 生效的证据）。

**额外发现（本次修订自查出的、审计没提的一个同类 fail-open）**：
`pgrep` 的退出码是 **0=有匹配 / 1=无匹配 / ≥2=语法或致命错误**。
原写法 `if pgrep -af ... >/dev/null 2>&1; then` 把 rc≥2（例如 **busybox 的 pgrep 没有 `-a`**）
当成「没有 QQ 进程」放行 —— 与 F4 是同一类缺陷。
已在 root helper、preflight、§3.5 闸门的**全部四处** `pgrep` 调用上改成三态 `case`，
rc≥2 一律阻断（`UNCHECKABLE`）。这一条有本地实测输出，见文末「验证输出」。

### F8 watchdog 核验（高）

**处置：全采纳。**

上一版 §3.3.4 把 `systemctl list-timers ... || echo NO_TIMERS` 的期望写成 `NO_TIMERS`，
**会把 `decisions.md` D-QQ-04 实测确认为合法的 `napcat-native-health.timer`（enabled，
只读探针，Description 原文 `... (no recovery)`）判成异常**；同时完全没有核对
`qq-watchdog.service` / `qq-watchdog.timer` 是否仍为 masked；对 active 的 watchdog
也只 `disable --now` 而不 `mask`（`disabled` 仍可被手动启动或被依赖拉起）。

**改动位置**：§3.3.4 重写为 (a)/(b)/(c) 三块，开头加一张「恢复型（禁止） vs 只读探针（允许）」对照表。

- **(a) 恢复型三件套**（`napcat-watch.service`、`qq-watchdog.service`、`qq-watchdog.timer`）：
  一段 GATE 脚本**逐个**核对 `inactive` + `masked`，并允许 `ABSENT`（本来就没装过）这个合法结局；
  任何一项不满足 ⇒ `WATCHDOG_GATE=BLOCK` + 非零退出。
  处置顺序写死为 **`disable --now` → `mask` → 验证 `is-enabled` 返回 `masked`**，并注明顺序不能反。
- **(b) health timer 白名单**：允许 `enabled` + `active`，但**必须验证其 ExecStart 无恢复动作** ——
  给了 `grep -c -E 'systemctl (restart|start|reset-failed)|docker (restart|start)|pkill|kill -'`，
  期望计数 `0`；≥1 就按恢复型处理。明写「名字里带 health 不代表它无害，以 ExecStart 实际内容为准」。
- **(c) 其余拉起路径**：timer 列表里**只允许**出现 `napcat-native-health.timer`；
  cron / 容器照旧，但把 `docker ps -a` 的权限失败从「没有容器」改成**未知 ⇒ 阻断**
  （与 §4.3 的失败关闭同一条原则）；发现容器时先只读取证再停，且 ⛔ **不要 `docker rm`**（§7.2）。

### F1 systemd `Restart=`（主线程下调为「中」）

**处置：按修订单口径。保留 `Restart=on-failure` / `RestartSec=45s` / `StartLimitBurst=1`，
未改成 `Restart=no`。只补「开机 enable」的取舍讨论。**

审计员把硬约束 5 读成禁止 systemd 的 `Restart=`；但那条约束针对的是 watchdog 模式
（`napcat-watch.py`：检测异常 → 无人值守 `systemctl restart`），而 **H1 §10 恰恰建议**
把 `Restart=always` 改成「on-failure + 合理退避」，**H1 §4.1 的加固表里本来就是
`Restart=on-failure` / `RestartSec=45s`**。`StartLimitIntervalSec=3600` + `StartLimitBurst=1`
⇒ 每小时至多一次自动重启，之后进入 `failed` 且必须人工 `reset-failed` —— **这本身就是人工闸**。

**改动位置**：
- §4.4 unit：`Restart=on-failure` / `RestartSec=45s` **保持不变**，
  但补了注释说明为什么**既不改成 `always` 也不改成 `no`**
  （改成 `no` 会丢掉 45s 内一次性抖动的自愈，却换不到任何额外人工闸 —— 人工闸已由
  `StartLimitBurst=1` 提供）。
- **新增 §4.7.1「开机 `enable`：本 runbook 的取舍与推荐」**（这是 F1 保留下来的有效部分）：
  - 先厘清 `Restart=` 与开机 `enable` 是两件事，给了一张对照表：
    **`Restart=` 的自动重启永远发生在「人刚敲过 `start`」的上下文里**，
    而开机 `enable` 是**无人值守**的 —— 这正是它与 watchdog 模式的灰色地带。
  - **推荐：不 enable（保持 `disabled`）**，三条理由按分量排序：
    1. **它偷走的是「人的那次尝试」**：`StartLimitBurst=1` 下人工 `start` 与自动启动
       共用同一个额度，凌晨跳闸自动起一次失败后，早上你到场会撞
       `start request repeated too quickly`。
    2. 它买到的东西**大概率不存在**（§7.1 已论证停机后重启不会自动登录回来）。
       —— **此条已如实标注为存疑**：H1 §3 的实测是**被踢**场景，
       「干净关机后快速登录票据是否仍有效」本项目**没有实测数据**，
       所以推荐主要靠第 1、3 条，第 2 条只是「收益存疑」不是「收益为零」。
    3. 它是**唯一**能绕过 §3.5 放行闸把 NapCat 拉起来的路径。
  - 回应了「那断电后 QQ 岂不是一直不回来」：成本大头是**多久发现**（H1 §3 两次空窗
    9.7 h / 11.7 h 全在等人发现），压缩它的是 T3 告警而不是开机自启。
  - 给了执行与验证命令，并强调 **`disable` ≠ `mask`**（在用的机器只能 `disabled`）。
  - 给了**复议条件**：当且仅当出现实测证据表明干净重启后能无人值守恢复在线，
    写进 `decisions.md` 后再 enable。
- 连带把全文 4 处 `home=enabled` 的期望改为 `home=disabled`
  （§3.1 互斥不变式、§6.6、§8.5 巡检、附录 B 清单），
  并在 §3.1 说明 `disabled` 完全满足不变式（不变式管的是"能不能被启动"）；
  §5.4「家里断电」那行也改为「NapCat 不会自己起来，需要人到场」。
- 附录 B 的 `[ ] home: enable napcat-native.service（断电后能自启）`
  改为 `[ ] 确认是 disabled（不 enable）`。

### F5 WebUI token 被打印到终端（致命，硬约束 10）

**处置：全采纳。删除全部打印命令，改成只检查"存在且非空"。**

上一版 §4.9 的
`sudo jq -r '.token // .webuiToken // empty' .../webui.json` 会把 token 值打到终端；
兜底建议 `sudo jq . webui.json` 还会打印整个文件。
文档里那句「⛔ 不要粘进聊天」**不算数** —— **「打印到终端」本身就是硬约束 10 禁止的「输出」**
（shell scrollback、`script`/`asciinema` 录制、远程会话日志、屏幕共享、采集 stdout 的运维工具
都会留下它）。拿到 WebUI token 的人可以**控制这个 QQ 号**（改配置、重启 QQ、发起登录）。

**改动位置**：§4.9 的「WebUI token 的取法」整块替换为
「WebUI 凭据：只验证"存在且非空"，**不打印**」。

- 新脚本只输出 `WEBUI_JSON_PRESENT` / `WEBUI_JSON_MODE` / `WEBUI_JSON_PARSE_OK` /
  `WEBUI_CRED_KEYS` / **`WEBUI_TOKEN_PRESENT=true|false`**，
  **绝不输出任何值**。`WEBUI_CRED_KEYS` 打印的是**键名**（键名不是秘密，
  它只告诉你该去哪个字段找），这样"字段名不同"的情况也不需要打印文件内容就能定位。
- 删掉了 `sudo jq . webui.json` 这条兜底；替代方案是只打印 `sorted(keys)`。
- 顺带堵掉一个上一版没意识到的同类泄漏：原文建议
  `journalctl -u napcat-native.service | grep -i webui` 找 WebUI 地址 ——
  **首启动打印的 WebUI URL 里带 token**。已改为用 `ss -ltnp` 查 6099 监听判断就绪。
- 补了「那怎么拿到 token 去登录」的**受控取法**：`sudoedit` 编辑器缓冲区复制，
  或从密码管理器填充；⛔ 不用 `cat`/`jq`/`echo`/`grep` 打到终端。
- §6.2 扫码那句「用 §4.9 取到的 WebUI token 登录」同步改写，指向新的受控取法。
- 顺带保留了 `WEBUI_JSON_MODE` 检查（期望 `600`），对应 H1 §8.1 记录的
  「权威机上 `webui.json` 曾经 644、非 root 可读」。

**本地实测**（四个分支 + 泄漏计数，见文末「验证输出」）：
正常 ⇒ `WEBUI_TOKEN_PRESENT=true` rc=0；空白 token ⇒ `false` rc=1；
坏 JSON ⇒ `WEBUI_JSON_PARSE_OK=false` rc=1；文件不存在 ⇒ `WEBUI_JSON_PRESENT=false` rc=1。
**`SECRET_LEAK_COUNT=0`** —— 四个分支的全部输出里都没有出现 token 值。

### F11 kicked 检查用 `|| true` 把失败伪装成零次（高）

**处置：全采纳。分离"采集"与"匹配"两个状态。**

上一版：`sudo journalctl ... | grep -c -i -E 'kicked|KickedOffLine' || true`。
`journalctl` 失败 / unit 名写错 / 日志不可读时，`grep` 收到空输入照样输出 `0`，
`|| true` 再把失败状态吞掉 ⇒ 操作者看到 `0`，解释成「没有 kicked」，
**在没有任何真实日志证据的情况下继续迁移**。

**改动位置**：§6.2「扫完之后」整块。现在是一段四步 CHK 脚本：

1. **unit 必须存在**（`systemctl cat`）—— 名字写错时 `journalctl` 也会"成功"返回空；
2. **采集必须成功**（`journalctl` 自身退出码，`||true` 已删除）；
3. **日志必须真的有内容**（`JOURNAL_LINES` > 0）—— 0 行不能解释成"一切正常"；
4. **只有走到这里计数才有意义**，且区分 `grep` 的 rc≥2（错误）与 rc=1（无匹配）。

失败时打印 `KICKED_CHECK=BLOCK reason=unit_not_found|journalctl_failed|empty_log_window|grep_error|kicked_seen`
并非零退出。文中明写「**读不到日志 ≠ 没有 kicked**」。

同一条纪律也应用到了 §6.6.3 的 `send_msg` 副作用检查（原来也是裸管道 `grep -c`）。

**本地实测**：`grep -c` 无匹配 ⇒ rc=1（不阻断，正确）；有匹配 ⇒ rc=0 且触发 `kicked_seen`；
文件读不到 ⇒ rc=2 ⇒ 阻断。见文末「验证输出」。

### F16 Hermes active 时重复建立额外 OneBot WS（中，但按铁律处理）

**处置：全采纳，并按修订单的额外强调**写成**可测试断言**，不停在口头约定。

上一版 §6.6 在 Hermes 已 `active` 之后每分钟跑一次 `onebot_probe.py` × 6，
即在 Hermes 的持久 WS 之外**再建六条短生命周期 WS**。两个后果：
(a) 与账号竞争连接槽位/事件顺序；
(b) **读到的是这条额外连接的状态，不是 Hermes 实际使用的那条** —— 观测行为改变了被观测对象。

**改动位置**：

- **§6.3 探针脚本本体**：新增 `refuse_if_hermes_active()`，在 `main()` 里于读 token **之前**调用。
  `hermes.service` active ⇒ 打印 `PROBE_REFUSED=hermes_active` 并 **`SystemExit(3)`**。
  这就是修订单要求的「**可测试断言**」——**结构性拒绝**，不是叮嘱。
  例外通道 `T4_PROBE_ALLOW_WITH_HERMES=1` 仅限「已确认 Hermes 死亡、需要取证」
  （对齐 T3-design §3.3 的探针触发条件：`gateway_state` 陈旧 **且** writer 进程不存在），
  并明写「迁移验收流程里没有任何一步该用它」。
  退出码表补 `3 = 被拒绝执行（Hermes 活着）`。
- **§6.6.0（新）**：把铁律与其验证步骤写成验收的第一步 ——
  在 §6.5 放行 Hermes 之后**立刻**跑一次探针，**期望 `rc=3` / `PROBE_REFUSED=hermes_active`**。
  文中强调「**这里"探针失败"才是通过**」；若 `rc=0` 说明装的是旧版脚本，属于缺陷。
- **§6.6.1（改写）**：六个分钟级样本全部改为**不建立任何新连接**：
  `systemctl is-active` / `NRestarts` / 3011 监听计数 / gateway 进程数 +
  **只读 `/opt/hermes/data/gateway_state.json`**（正是 T3 哨兵用的同一个数据源，
  T3-design §3.2，读它零连接）。
- **§6.0 执行顺序**新增一条「📌 顺序上的一条铁律」：探针（11、13）必须在放行 Hermes（14）之前，
  并指出这条约束**已经写成代码里的断言**，顺序做反时探针会拒绝执行而不是悄悄多开连接。
- 需要 `get_status` 级证据时的正确做法写死为：
  `stop hermes.service` → 跑探针 → 处理 → `start hermes.service`
  （停 Hermes 不消耗 QQ 登录尝试，H1 §8.5 的标准处置就是先停 Hermes；多开一条 WS 会）。
- 诚实标注了旧 adapter 的陷阱：生产 repo 停在 `8911e2e0`，
  **`updated_at` 陈旧不等于 Hermes 死了**（T3-design §1.2/§11.3 实测文件冻结 15 小时而服务全程 active），
  判死活要看进程级指标；`online=None` 也**不得**当成离线（T3-design §11.2）。

**本地实测**（fake `systemctl` 注入）：
hermes active ⇒ `PROBE_REFUSED=hermes_active` rc=3；
hermes inactive ⇒ `PROBE_GUARD=ok(hermes_not_active)` 放行；
`T4_PROBE_ALLOW_WITH_HERMES=1` ⇒ `PROBE_GUARD=bypassed` 放行。见文末「验证输出」。

**额外发现（审计没提，本次修订实测查出）**：上一版 §6.6 采样循环里的
`[ "$i" -lt 6 ] && read -r -t 60 _ </dev/null` **根本不会等待** ——
从 `/dev/null` 读立刻 EOF 返回（实测 `read_rc=1`、`elapsed=0s`）。
所谓「六个**分钟级**样本」实际会在**毫秒内**跑完六次，完全达不到"跨越几分钟观察稳定性"的目的。
已改为 `sleep 60`。

### F9 Docker 回退路径挂错 config 目录 + 无备份删容器（高）

**处置：全采纳。统一配置源；删掉两处自动 `docker rm -f`，改为 guard 阻断 + 归档后再删。**

两个缺陷：
(a) native 把 OneBot 配置写进 `/opt/napcat-native/config-candidate`（§4.8），
Docker unit 却挂 `-v /opt/napcat/config:/app/napcat/config`，而 runbook
**从未创建或填充 `/opt/napcat/config`** ⇒ 容器拿到 Docker 自动创建的空目录 ⇒
没有 OneBot token/host/port ⇒ 容器内 3001 不启动 ⇒ Hermes 握手失败 ⇒
**操作者会去查隧道，而问题根本不在隧道**。
(b) `ExecStartPre=-/usr/bin/docker rm -f napcat` 与 `ExecStopPost=-/usr/bin/docker rm -f napcat`
在每次启动/停止时无备份删除同名容器，**删之前没有保存 inspect/logs/状态证据**，
违反硬约束 11，且把清理做成了隐藏副作用。

**改动位置**：§4.5 重写为 §4.5.1–§4.5.5。

- **§4.5.1 统一配置源**：两条路径都用 `/opt/napcat-native/config-candidate`，
  Docker 挂到 `/app/napcat/config`。理由写明：§4.8 那一整套（拷配置、改端口、
  核对 token 指纹、权限 700/600）**已经有验证步骤**，复用它比再写一套创建/填充/校验流程
  少一半出错面 —— **一个配置源，一套验证**。
  唯一保留的差异是 `host`（native `127.0.0.1` / Docker `0.0.0.0`，00-PLAN D81 的坑），
  给了差异表和起容器前的确认命令。
  额外补了一个上一版没考虑的阻断条件：**Docker 开了 userns-remap 时容器 root 读不到 700 目录**，
  给了检测命令并要求停下（不在本 runbook 覆盖范围）。
- **§4.5.2 unit**：两处 `docker rm -f` **已删除**，并在原位留下 ⛔ 注释说明为什么故意没有；
  `ExecStartPre` 改为 `+/usr/local/sbin/napcat-preflight-root` + `+/usr/local/sbin/napcat-docker-guard`；
  `-v` 改为统一配置源。加了可审计证据命令：
  **`systemctl cat napcat.service | grep -c 'docker rm'` 必须是 `0`**。
- **§4.5.3 `napcat-docker-guard`**：发现同名容器（running 或 exited）⇒ 只读取证 + **阻断**，
  绝不删除。`docker ps -aq` 失败也阻断（失败关闭）。
  分支处置：`status=Up` ⇒ 有实例在跑，`update --restart=no` + `stop`，回 §3.5 重跑放行闸；
  `status=Exited` ⇒ 用 §4.5.4 归档后再删。
- **§4.5.4 `napcat-docker-archive`**：**唯一**允许删容器的路径，必须由人显式执行。
  先把 `inspect.json` / `logs.txt` / `ps.txt` / config listing 落盘到
  `/opt/napcat/backups/docker-<name>-<UTC>`，**再** `docker rm`（**没有 `-f`**，
  且**拒绝删除运行中的容器**）。脚本 `chmod 700`（破坏性脚本只给 root，guard 是只读的所以 755）。
  提醒了 `inspect.json` 的 `.Config.Env` 里会有 `--env-file` 的秘密 ⇒ 目录 700，
  ⛔ 不要 `cat`，要看只取所需字段。
- **§4.5.5**：保留原有两条检查，并把「容器确实读到了配置」做成显式验证
  （`docker exec napcat ls -l /app/napcat/config` 要能列出 `onebot11_2952532060.json`）——
  **这正是上一版会卡住的地方**。

**本地实测**（fake `docker` 注入，见文末「验证输出」）：
无同名容器 ⇒ `docker-guard: OK` rc=0；有同名容器 ⇒ `REFUSE ... already exists` rc=1；
`docker ps -aq` 失败 ⇒ `REFUSE ... (fail-closed)` rc=1。

**顺带修掉一个本次修订自己引入的缺陷**：替换 §4.3 时留下了一份**重复的旧 launcher 代码块**
（硬编码 `LOCK=/run/napcat-native.lock`，且引用了已改号的「§6.2 收尾」）。已删除，
现在全文 `bin/launcher` 只有一处定义（§4.3.4）。

### F10 §10 仍引用不存在的 `napcat.service`（高）

**处置：全采纳。**

T4 多处转述 H1 §10，而 H1 §10 说的是 **Docker 时代**的 `napcat.service` ——
`decisions.md` D-QQ-04 实测该 unit 在 `cornna-cn` 上**不存在**，当前是 `napcat-native.service`。
§0.1 自己写清楚了这一点，§4.7 / §9.3 却又照抄 H1 的说法，**把维护者引向一个幽灵 unit**。
`NAPCAT_QUICK_PASSWORD` 同理：H1 §10 说"环境仍存在"，但没区分**活单元实际读的**
`/etc/napcat-native/env` 与 **Docker 时代残留的** `/etc/napcat/napcat.env`。

**改动位置**：

- **§4.7**：新增一个 ⚠️ 框和换算表（H1 §10 说法 vs 2026-08-31 实测），
  并给出**当场核对命令**：`systemctl show -p Restart,... napcat-native.service`、
  `systemctl show -p EnvironmentFiles napcat-native.service`、
  以及 `systemctl cat napcat.service 2>&1 | head -1`（**预期是报错**
  `Unit napcat.service could not be found.` —— 用报错来确认你没在对着幽灵 unit 做决定）。
  明写「不要照抄 H1 的 `always`，实际值要当场 `systemctl show` 确认」。
- **§4.2**：新增两份 env 文件的对照表（依据 D-QQ-05 实测）：
  `/etc/napcat-native/env` = 活单元读的、**没有** `NAPCAT_QUICK_PASSWORD`（`/proc/<pid>/environ` 亦证实）；
  `/etc/napcat/napcat.env` = **没有单元在读**的 Docker 时代残留、有 1 行非空、**惰性残留不影响行为**。
  并说明 H1 §10 那条遗留的真实剩余部分只是**卫生清理**，不是当下的风控杠杆
  （D-QQ-05：近 6 小时 journal 无任何密码登录痕迹）。
  对本 runbook 的意义：新机不会有那份残留；退役机上那份**保留不删**（§7.2），
  回滚时也**不要顺手清理**（属独立维护动作，不该混进关键路径）。
- **§9.3** 表格中 NapCat `Restart=` 那行补注「§10 说的是已不存在的 `napcat.service`」，
  并补上「保留 `on-failure` 而不是改成 `Restart=no`」的理由。
- **§9.4「H1 里已经过时的」** 新增两条：H1 §10 的 `napcat.service` 引用、
  H1 §10 的 `NAPCAT_QUICK_PASSWORD` 说法，各自指向 §4.7 / §4.2 的换算表。

**顺带（依据 D-QQ-05，非审计条目）**：
- §4.2 的 env 模板补上 `NAPCAT_DISABLE_BYPASS=1`（权威机已显式设置；
  保持关闭 = 不走对抗腾讯检测那条路，与"让这个号看起来像普通住宅用户"的迁移思路一致）。
  上一版模板漏了它。
- 新增一条 **⚠️ 存疑**：D-QQ-05 实测活单元 env 的键里**并没有 `ACCOUNT`**，
  而 H1 §4.1 的「只使用官方 `ACCOUNT` 快速登录契约」描述的是 **Docker entrypoint** 的行为。
  ⇒ **原生路径下 `ACCOUNT` 是否真被读取、能否触发快速登录，本项目没有实测数据。**
  仍然写上它（无害），但明确禁止把「配了 ACCOUNT 所以会自动快速登录」当作前提 ——
  §4.7.1 的 enable 取舍正是按"快速登录不一定可用"推理的。
- env 核对增加 `awk` 只打印**键名**的命令（该文件将来可能被加进秘密）。

### F14 破坏性动作前没有备份（中，硬约束 11）

**处置：全采纳。**（marker 那一半随 F7 一起处理，见下。）

**UFW 部分**：上一版是
`sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw enable` 一行连写，
**没有保存任何回滚材料**，也**没有先确认 SSH 会被放行**。
通过 SSH 操作新机时（多数情况），这一行会**立刻切断管理连接**，
而那台机器在你家里、可能没接显示器、几乎肯定没有带外管理。

**改动位置**：§4.1 防火墙部分重写为三步 + 回滚：

1. **先备份**：`ufw status numbered` / `status verbose` / `cp -a /etc/ufw` / `iptables-save`
   落盘到 `/opt/napcat/backups/t4-fw-<UTC>`（项目惯例的 UTC 时间戳命名）。
2. **先确认连接方式并放行**：用 `SSH_CLIENT` 判断是否远程；远程时**必须先**
   `ufw allow from <LAN段> to any port 22 proto tcp`，并明写**不要 `ufw allow 22`**。
3. **最后才** `ufw default ...` + `ufw --force enable`，并解释 `--force` 为何在此处是安全的
   （因为第 2 步已处理 SSH；顺序反了才危险）。
4. **补了完整回滚段**：`ufw disable` 先拿回连接，再从备份恢复 `/etc/ufw`。
   并诚实写明"人已经被锁在外面了"没有软件解法，所以这一节的顺序是硬性的。

### F15 `permitlisten` 验证遗漏 `ExitOnForwardFailure`（中）

**处置：全采纳。**

上一版验证命令没加 `-o ExitOnForwardFailure=yes` —— 而 T4 §5.3 自己已写明
**没有该选项时 `-R` 转发失败 ssh 会保持连接不退出**。
⇒ 命令挂在前台，操作者 Ctrl-C 后很可能把「ssh 还活着」误读成「限制没生效」（**结论正好反了**）。
也没有预检 9999 是否已被占用。

**改动位置**：§5.1.2 的验证子项。

- **加了端口预检**：先 `ssh corlinman-prod "ss -ltnH '( sport = :$PORT )' | grep -c ."` 期望 `0`，
  并说明不预检会把"端口被占"误判成"`permitlisten` 生效"。
- **补齐三个选项**：`-o ExitOnForwardFailure=yes`、`-o ConnectTimeout=10`、外层 `timeout 20`，
  并给了一张「没有它会怎样」的作用表。顺带补上 `UserKnownHostsFile` / `StrictHostKeyChecking`
  （原命令用 `-F /dev/null` 却没指定 known_hosts）。
- **两个判据必须一起看**：**`rc` 非 0 且不是 124** **且** 远端端口计数为 `0` 才算通过。
  只看 `rc` 不够（ssh 可能因别的原因退出），只看端口计数也不够（可能别的东西占着）。
- **`rc=124`** 单列为一个分支 ⇒ `timeout` 强杀 ⇒ `ExitOnForwardFailure` 没生效，修好再判。

### F6 T3 采集器不存在，baseline 闸门远低于设计要求（高）

**处置：全采纳。**

**先复核了审计的事实主张，成立**（本次修订实际检查工作区）：
`ops/` 目录**根本不存在**（`ls ops/` → `No such file or directory`），
`docs/orchestration/results/T3.md` 不存在，`T3-design.md` 开头写着「设计完成，**待 GPT 轨道实现**」。
⇒ 按上一版执行会在 `python -m ops.qq_sentinel selftest` 处直接失败；
操作者若把它当"环境问题"跳过，就会在**没有告警、没有账本**的情况下完成这次不可逆的迁移。

**改动位置**：§8.1 拆为 §8.1.1（部署闸）+ §8.1.2（baseline 闸）。

- 开头加 ⛔ 框，把「`ops/qq_sentinel` 现在还不存在」写成**确定事实**并给出三条证据，
  并明确 **本 runbook 不实现 T3**（那是 T3 的交付物，清单在 `T3-design.md` §12.1），
  本节只负责**硬前置闸**。
- **§8.1.1 部署闸**（脚本，非零退出阻断）逐项核对：
  生产 repo 里有 `ops/qq_sentinel/__main__.py`、`hermes-qq-sentinel.service`/`.timer` 已安装、
  timer `active`+`enabled`、**`selftest` rc=0**（它自己会校验数据源兼容、A0 是否已清、状态文件新鲜）、
  `sessions.jsonl` 存在 ⇒ `T3_DEPLOY_GATE=PASS`。
  `selftest` 失败时打印其输出尾部；`A0_SOURCE_INCOMPATIBLE` 单列一个分支，
  指向 T3-design §11.8 的部署顺序（**T1 → T3-A → 攒 baseline → 才轮到 T4**）。
- **§8.1.2 baseline 闸**：**`wc -l ≥ 1` 已彻底删除**，替换为 T3-design §10.6 的三条真实前置：
  哨兵连续运行 **≥7 天**、`epoch=datacenter` 的**非删失**区间 **≥10**、`A0` 已清除。
  文中解释了「非删失」= `start_censored==false && end_censored==false`，
  并引用 T3-design §10.2 的理由（把"Hermes 崩了所以只有 20 分钟"的区间算进中位数，
  等于**用运维事故去证明家宽迁移失败**）。
- **明确禁止 marker 或旧历史满足闸门**：闸门脚本会逐行校验每条记录必须带
  `schema`/`duration_s`/`epoch`/`start_censored`，**缺字段的行（典型就是手写 marker）
  直接 `BLOCK` 并打印行号**。这同时兑现了 F7 里"不要手写 ledger 不支持的 marker 行"。
- 加了防呆提示：不要靠放宽阈值来"解决"样本不足；真正能加快攒样本的是 **T3 的告警**
  （把"被踢→有人去扫码"的延迟从小时级压到分钟级）。
- **如实标注了一处契约缺口（存疑）**：「哨兵**连续**运行 ≥7 天」目前只能用**账本跨度**近似，
  **证明不了中间没断过**；T3-design 没有定义"哨兵自身运行时长/断点"的可查字段。
  已建议 T3 在 `selftest`/`report` 里直接输出，并要求在执行记录里注明这条是"弱验证"。

**本地实测**（六个分支，见文末「验证输出」）。最关键的一条对照：
**只有 1 行旧记录时，上一版 `wc -l ≥1` 会放行；新闸门给出
`BASELINE_GATE=BLOCK reason=noncensored_datacenter_sessions<10`。**

### F7 账本字段 / marker / 报告 CLI 与 T3 不兼容（高）

**处置：全采纳，且按修订单口径 —— T3 的契约是权威，T4 适配它。**

三处不兼容：
`env=cloud-aliyun-gz|home-residential` vs T3 的 `epoch=datacenter|home_broadband`；
`report --group-by env` vs T3 的 `report --compare` / `report --epoch`；
T4 手写的 marker 行只有 `type`/`event`/环境字段/`ts`，**没有** `schema`/`duration_s`/`epoch`。
另外 T4 只在正文说"迁移完成后同样追加 `t4_migration_done`"，**没给命令，迁移窗口无法可靠闭合**。

**决定：完全采用 T3 的契约，T4 不引入任何新字段。**
取舍理由已写进 §8.3：这是**最小改动**且方向正确 —— T3 是账本的所有者；
让消费者（T4）去改生产者（T3）的 schema，需要先改 T3 设计、实现和测试，
而 **T3 连实现都还没有**，为一个还没写的组件增加需求只会让它更晚交付。

**改动位置**：

- **§8.2 整节重写**。开头 ⚠️ 框点名上一版三个各自独立的错误：
  (1) 往 `sessions.jsonl` 手写 marker（T3 没有 `type=marker` 这种记录类型）；
  (2) `sed -i` 全文件替换 placeholder（**重写整个 JSONL，违反 append-only**，T3-design §11.9）；
  (3) 备份写在动作之后（按代码块顺序执行等于改完才备份，违反硬约束 11 —— 这一半也是 F14）。
  给出新的分工表：**统计边界由 `epoch` 承载，人类可读事件线放另一个文件**。
- **§8.2.1 迁移开始**：**备份在最前面**（`cp -a` + `cmp` 确认一致），
  再把 `t4_migration_begin` 写进 **`/opt/hermes/backups/t4-migration/events.jsonl`**。
  **时间戳在写入时就算好**，一次追加一条完整 JSON ⇒ **没有 placeholder，也就不需要 `sed -i`**。
  附带 `events.jsonl` 的 JSON 合法性校验命令。
- **§8.2.2 切换 `epoch`**：给了 systemd drop-in 的完整命令 + `systemctl show -p Environment` 验证。
  **并指出了一个 T3 设计没覆盖的问题**：T3 把未闭合区间放在 `state.json` 里，
  **若在有区间开着时改 epoch，那段区间归属未定义**。
  ⇒ 规定**切换时机必须在停机窗口内**（QQ 已离线 ⇒ 无 `ONLINE` 区间开着 ⇒ 不存在跨界区间），
  并把这一步作为 7c 插进 §6.0 的顺序表和附录 B。
  drop-in 机制本身**标注为存疑**（T3 未实现，epoch 到底从哪读要到执行时才确定），
  但明确「唯一可靠的验收是 §8.2.3 那条『新区间的 epoch 确实变了』」。
- **§8.2.3 迁移完成**：给了 `t4_migration_done` 的**完整可执行命令**（上一版只有一句正文），
  ⇒ **迁移窗口到这里才可靠闭合**。
  并新增**必须验证 epoch 真的落到新区间上**的步骤（`LAST5_EPOCHS` / `EPOCH_COUNTS` /
  `state.json` 的 `epoch`），含"切换没生效"时的补救与标注要求。
  说明了停机那段区间**不需要手工排除** —— T3 会把它标成 `end_censored=true`，
  而删失区间默认排除在中位数之外（**这正是"用 T3 契约而不是自己发明"的收益**）。
- **§8.3 重写为「完全采用 T3 的 schema，本 runbook 不新增字段」**，给出三个字段的逐条处置表
  （`env` 删除→`epoch`；`backend_host` 删除→冗余，溯源查 `events.jsonl`；
  `egress_ip_changed` 移出账本→§8.3.1）。
- **§8.4** `report --group-by env` → **`report --compare`**（并给 `--epoch` 单侧查询）；
  加 ⚠️ 说明为什么不能用旧命令（不是 T3 接口，且 `env` 字段根本不存在，
  真跑多半未知参数报错，或更糟：分组为空却看起来"跑成功了"）。
  判读规则表全部改用 `home_broadband` / `datacenter`，
  并把 T3-design §10.5 的 `n<10` 硬性防呆原文写进表格。
- **§9.2「本次新增」** 表格中那一行（原来写 `env`/`backend_host`/`egress_ip_changed` 三个字段）
  改写为 §8.1/§8.2 的两道闸 + `epoch` 切换时机，另起一行写 §8.3.1 的独立 IP 日志。
- §6.0 顺序表与附录 B 全部同步（新增 6a/6b/6c、7b、7c、16 步）。

**本地实测**：begin/done 两条命令都能产出合法 append-only JSONL（`EVENTS_JSONL_VALID=true`）；
§8.1.2 闸门能把混入账本的旧式 marker 行抓出来并阻断。见文末「验证输出」。

### F13 `egress_ip_changed` 缺 timer 与账本集成，采集失败 `exit 0`（中）

**处置：全采纳（账本集成那条按 F7 的口径改为"不集成进账本"）。**

三个问题：
(a) 只给了脚本，然后一句"用 systemd timer 每 5 分钟跑一次即可"，
**没有 service/timer/安装/启用/权限/验证/回滚**；
(b) `[ -z "$IP" ] && exit 0` **把「完全没采集到」报告成成功退出** ⇒
日志里"没有新行"同时意味着「IP 没变」和「根本没采到」，而两者对结论的影响正好相反；
(c) `egress_ip_changed` 不在 T3 schema 里。

**改动位置**：§8.3.1（新）。

- **(c) 的处置**：**不往账本里塞**。做成独立本地日志 + §8.4 阶段的人工时间戳关联。
  理由写明：往 append-only 的 session 流里塞 T3 不认识的字段，会被 §8.1.2 的闸门当非法行拦下。
  同时保留了"为什么仍然需要它"的论证（H1 §4.1「频繁换 IP 本身是风控信号」，
  不排除重拨这个混杂因素就无法解释 §8.4 的结果）。
- **(b) 的处置**：脚本重写。`curl` 缺失 ⇒ 写 `unknown reason=curl_missing` + **`exit 2`**；
  取不到/返回非 IP（限流页、HTML、空响应）⇒ 写 `unknown reason=fetch_failed curl_rc=N` + **`exit 1`**。
  加了 IP 格式正则校验（原版只判空，限流返回的 HTML 会被当成合法 IP 写进日志）。
  连续 `unknown` 不重复刷屏，但第一条一定写下来。
- **(a) 的处置**：给出完整 `t4-egress-log.service` + `.timer`
  （`OnBootSec=2min` / `OnUnitActiveSec=5min` / `AccuracySec=30s` / `Persistent=true`）、
  `systemd-analyze verify`、`enable --now`、`list-timers`、`is-failed` 验证，以及**卸载/回滚**段。
  service 用了很紧的沙箱（`MemoryDenyWriteExecute=yes`、`CapabilityBoundingSet=` 空），
  并解释了为什么这里可以而 QQ 单元不行（它是 shell+curl，不是 Electron —— §4.6 那张表只针对 QQ 单元）。
- 保留并强化了原有纪律：⛔ 这个脚本除了写本地文件之外什么都不做（不通知、不重启、不告警），
  告警是 T3 的事（它有 T3-design §8 那一整套权限边界）。
- §8.4 判读表新增一行：**出口 IP 日志里 `unknown` 占比高 ⇒ 无法排除重拨这个混杂因素**，
  先修采集，别把"没采到"读成"没换过 IP"。

**本地实测**（五个分支 + 与旧版对照，见文末「验证输出」）。最关键的对照：
同样在"`curl` 不存在"的情况下，**旧脚本 `rc=0` 且日志 0 行（静默成功）**，
**新脚本 `rc=2` 且写下 `unknown reason=curl_missing`**。

---

## 本次修订额外发现的缺陷（审计 17 条之外）

这些不在 T4 审计的 finding 列表里，是修订过程中实测查出来的，**都已修**。
列出来是因为它们和已知 finding 属于同一类错误，说明那几类问题不是孤例。

| # | 缺陷 | 与哪条同类 | 处置 |
|---|---|---|---|
| X1 | `pgrep` 的 rc≥2（语法/致命错误，例如 **busybox 的 pgrep 没有 `-a`**）被当成「没有 QQ 进程」放行 | **F4**（失败开放） | 全文四处 `pgrep` 调用改成三态 `case`，rc≥2 一律阻断 |
| X2 | §4.8 那条"只看结构不看 token"的 `jq` **只处理数组**，`network` 下**非数组**节点里的 token 会被原样打印 | **F5**（硬约束 10） | 换成递归 redact 的 Python，对所有秘密类键名生效；实测旧命令 `OLD_LEAKS=1`、新命令 `NEW_LEAKS=0` |
| X3 | §6.6 采样循环的 `read -r -t 60 _ </dev/null` **根本不会等待**（读 `/dev/null` 立即 EOF），"六个分钟级样本"实际在毫秒内跑完 | 无对应 finding | 改为 `sleep 60` |
| X4 | §6.2 的 `journalctl -u napcat-native.service \| grep -i webui` 会把首启动打印的**带 token 的 WebUI URL** 整行打出来 | **F5** | 删除该建议，改用 `ss -ltnp` 查 6099 监听判断就绪 |
| X5 | 本次修订替换 §4.3 时留下一份**重复的旧 launcher 代码块**（硬编码 LOCK、引用已改号的小节） | 自引入 | 已删除；全文 `bin/launcher` 现只有一处定义 |

---

## 验证输出（全部为本机真实执行结果）

⚠️ **说明边界**：本次修订**没有 SSH、没有碰生产**。下面是能在本地离线验证的部分：
所有内嵌脚本的**语法/编译**、以及**逻辑分支的行为**（用 fake `docker`/`pgrep`/`systemctl`/`curl` 注入
和构造的账本 fixture）。**真机上的实际输出（3011 监听、扫码、epoch 切换生效等）无法在此验证**，
必须在执行时按各节的"预期"逐条核对。

### V1 备份与规模

```text
BACKUP:
  -rw-r--r--@ 1 cornna  staff  126775 Aug 31 02:54 docs/orchestration/tasks/T4-home-migration-runbook.md.bak-pre-rev-20260831T102507Z
SIZE:  before=2300 lines / after=4012 lines
```

### V2 F6 的事实主张复核（`ops/qq_sentinel` 确实不存在）

```text
OPS_DIR: ls: ops: No such file or directory
T3_RESULT: ls: docs/orchestration/results/T3.md: No such file or directory
```

### V3 全文 31 个内嵌脚本块的语法/编译检查（最终版，含 T3 rev-1 适配后）

```text
blocks=31 {'AK': 1, 'CHK': 2, 'CONF': 1, 'ENV': 1, 'GATE': 4, 'PY': 9, 'SH': 6, 'SSHD': 1, 'UNIT': 6}

=== bash/sh 语法检查 ===
OK    SH_1_L1196     (bash)      OK    GATE_1_L457    (bash)
OK    SH_2_L1254     (bash)      OK    GATE_2_L664    (bash)
OK    SH_3_L1329     (bash)      OK    GATE_3_L796    (bash)
OK    SH_4_L1642     (bash)      OK    GATE_4_L3263   (bash)
OK    SH_5_L1685     (bash)      OK    CHK_1_L2603    (bash)
OK    SH_6_L3604     (sh)        OK    CHK_2_L3036    (bash)

=== python 编译检查 ===
OK    PY_1 .. PY_9   （9/9 全部 py_compile 通过）

=== §3.5 内嵌的三段 REMOTE 脚本 ===
rem1.sh OK   rem2.sh OK   rem3.sh OK

FINAL_ALL_BLOCKS_SYNTAX_FAIL=0
```

### V4 硬约束的静态核验

```text
=== 硬约束 8：StartLimitIntervalSec/Burst 必须在 [Unit] 段 ===
UNIT_1(napcat-native)   OK  StartLimitIntervalSec=3600 in [Unit]; StartLimitBurst=1 in [Unit]
UNIT_2(napcat docker)   OK  StartLimitIntervalSec=3600 in [Unit]; StartLimitBurst=1 in [Unit]
UNIT_3(onebot-rtunnel)  OK  StartLimitIntervalSec=0 in [Unit]; StartLimitBurst=0 in [Unit]
UNIT_4(drill)           OK  StartLimitIntervalSec=0 in [Unit]
UNIT_5/6(egress svc/timer)  --  (无 StartLimit 指令)

=== 硬约束 7：不得使用 Conflicts= ===
OK 没有任何生效的 Conflicts= 指令（两处命中均为解释性注释，行首为 #）

=== 硬约束 1：不得调用 SetConfig ===
OK 全文无 SetConfig

=== systemd unit 指令拼写 ===
6 个 unit + 1 个 drop-in，unknown_directives=0
```

### V5 F4 —— preflight 失败关闭的四个分支（fake `docker` / `pgrep` 注入）

```text
=== TEST D: docker 存在但 `docker ps` 失败 ===
preflight-root: REFUSE: docker is installed but 'docker ps' failed (fail-closed):
TEST_D_rc=1                          ← 上一版这里会打印 "preflight: OK"

=== TEST E: docker 报出 napcat 容器 ===
napcat-anything
preflight-root: REFUSE: a running docker container matches 'napcat' — two runtimes would double-login
TEST_E_rc=1

=== X1: pgrep 不支持 -a（模拟 busybox，rc=2）===
preflight-root: REFUSE: pgrep exited rc=2 — cannot verify process tree (fail-closed; busybox pgrep lacks -a?)
TEST_PGREP_BADFLAG_rc=1

=== pgrep 无匹配（rc=1）应放行 ===
preflight-root: OK
TEST_PGREP_NOMATCH_rc=0
```

### V6 F5 —— WebUI 凭据检查的四个分支 + 泄漏计数

```text
=== A 正常 ===            WEBUI_JSON_PRESENT=true / MODE=600 / PARSE_OK=true
                          WEBUI_CRED_KEYS=token / WEBUI_TOKEN_PRESENT=true   rc=0
=== B token 为空白 ===    WEBUI_TOKEN_PRESENT=false                          rc=1
=== C 坏 JSON ===         WEBUI_JSON_PARSE_OK=false reason=JSONDecodeError   rc=1
=== D 文件不存在 ===      WEBUI_JSON_PRESENT=false                           rc=1

SECRET_LEAK_COUNT=0 (期望 0)   ← 四个分支的全部输出里都没有出现 token 值
```

### V7 X2 —— 配置结构 dump 的泄漏对照（本机有 jq，做了真实对照）

构造的 fixture 里 `network.httpServers` 是**对象**（不是数组）且带 token：

```text
=== 新版（递归 redact）===
"token": "<redacted:len=20>"   "token": "<redacted:len=45>"   "webuiToken": "<redacted:len=17>"
NEW_LEAKS=0

=== 上一版 jq ===
"httpServers": { "name": "http", "enable": false,
                 "token": "SUPERSECRET-http-token-in-an-OBJECT-not-array" }
OLD_LEAKS=1                    ← 非数组节点的 token 原样打印出来了
```

### V8 F11 —— kicked 检查的退出码语义

```text
K=0 GREP_RC=1   → not blocked (正确：读到了日志，只是没有 kicked)
K=1 GREP_RC=0   → KICKED_CHECK=BLOCK reason=kicked_seen (正确)
文件读不到       → GREP_RC=2 → BLOCK (正确：读不到 ≠ 没有 kicked)
```

### V9 F16 —— 探针铁律闸（fake `systemctl` 注入）

```text
=== A: hermes active → 必须拒绝 ===
PROBE_REFUSED=hermes_active
Hermes 正持有一条到同一个 NapCat 的持久 WS。……改用 §6.6.1 的 runtime state 只读采样。
rc=3

=== B: hermes inactive → 放行 ===
PROBE_GUARD=ok(hermes_not_active)

=== C: 显式绕过 ===
PROBE_GUARD=bypassed
```

探针的三条可审计 grep（在改造后的脚本上实测）：

```text
grep 1（send_msg 等，必须 0）        → 0
grep 2（subprocess，应只有一处）      → 30: subprocess.run(["systemctl","is-active","--quiet","hermes.service"], ...)
grep 3（恢复动词，必须 0）            → 0
python3 -m py_compile               → PROBE_COMPILES_OK
```

### V10 X3 —— 上一版采样循环根本不会等待

```text
$ bash -c 'read -r -t 60 _ </dev/null; echo "read_rc=$?"'
read_rc=1
elapsed=0s   ← 「六个分钟级样本」实际在毫秒内跑完
```

### V11 F6 —— baseline 闸门的六个分支（构造的账本 fixture）

```text
=== A 健康账本（12 段非删失，跨 9 天）===
EPOCH_DATACENTER_NONCENSORED=12 (need >=10) / LEDGER_SPAN_DAYS=9.00 (need >=7)
BASELINE_GATE=PASS   rc=0

=== B 混进手写 marker（上一版 §8.2 教的做法）===
BASELINE_GATE=BLOCK reason=non_session_lines_in_ledger
  line 13: missing:schema,duration_s,epoch,start_censored     rc=1

=== C 只有 1 行旧记录 ===   ← 上一版 `wc -l >= 1` 会放行！
EPOCH_DATACENTER_NONCENSORED=1 (need >=10)
BASELINE_GATE=BLOCK reason=noncensored_datacenter_sessions<10   rc=1

=== D 12 段但全是删失区间 ===
EPOCH_DATACENTER_NONCENSORED=0
BASELINE_GATE=BLOCK reason=noncensored_datacenter_sessions<10   rc=1

=== E 跨度只有 3 天 ===
BASELINE_GATE=BLOCK reason=span_days<7                          rc=1

=== F 账本不存在 ===
BASELINE_GATE=BLOCK reason=ledger_missing                       rc=1
```

### V12 F7 —— begin/done marker 命令端到端

```text
WROTE {"event": "t4_migration_begin", "ts": "2026-08-31T11:01:54Z", "backup_ts": "...",
       "epoch_before": "datacenter", "epoch_after": "home_broadband", ...}
WROTE {"event": "t4_migration_done", "ts": "2026-08-31T11:01:54Z",
       "epoch_now": "home_broadband", "backend_host": "home", ...}
EVENTS_JSONL_VALID=true      ← append-only，两行都是完整合法 JSON，无 placeholder、无 sed
```

### V13 F9 —— docker-guard 的三个分支（fake `docker` 注入）

```text
无同名容器            → docker-guard: OK (no container named 'napcat')          rc=0
存在同名容器(Exited)  → REFUSE: a container named 'napcat' already exists ...   rc=1
docker ps -aq 失败    → REFUSE: 'docker ps -aq' failed (fail-closed)            rc=1
```

### V14 F13 —— 出口 IP 采集器的五个分支 + 与旧版的关键对照

```text
A curl 不存在      → t4-egress-log: curl not found            rc=2
                     日志: 2026-08-31T11:03:40Z unknown reason=curl_missing
B curl 返回 HTML   → fetch failed (curl_rc=0)                  rc=1
                     日志: ... unknown reason=fetch_failed curl_rc=0
C 正常 IP          → rc=0，日志追加 "... 1.2.3.4"
D IP 未变          → rc=0，日志仍 2 行（不写噪声）
E IP 变化          → rc=0，日志追加 "... 5.6.7.8"

=== 关键对照：同样在「curl 不存在」的情况下 ===
旧版 rc=0 （静默成功！）
旧版日志行数: 0  ← 无法区分「没换 IP」和「没采到」
```

---

## 遗留风险与存疑（如实标注，未用叙述掩盖）

1. **T3 尚未实现，§8 的大部分内容无法端到端验证。**
   §8.1.1 的部署闸、§8.2.2 的 epoch 切换机制（systemd drop-in）都是**按 `T3-design.md` 的契约写的**，
   但 `ops/qq_sentinel` 还不存在（V2）。执行时若 T3 的实现与设计有出入（尤其是
   epoch 从哪里读、`state.json` 的结构、`report` 的具体输出格式），**以 T3 的实现和 README 为准**。
   文中已在相应位置标注 ⚠️ 存疑。
2. **「哨兵连续运行 ≥7 天」目前只是弱验证。** 用账本跨度近似，证明不了中间没断过。
   T3-design 没有定义可查的"哨兵运行时长/断点"字段。已建议 T3 补上（§8.1.2 的存疑框）。
3. **原生路径下 `ACCOUNT` 是否真的生效，无实测数据。**
   D-QQ-05 实测活单元 env 里**没有** `ACCOUNT`，而 H1 §4.1 那条契约描述的是 Docker entrypoint。
   §4.2 已标注存疑，并要求不得把"配了 ACCOUNT 就会自动快速登录"当作前提。
4. **§4.7.1 推荐"不 enable"的第 2 条理由（收益大概率不存在）依赖一个未实测的推断** ——
   「干净关机后快速登录票据是否仍有效」没有数据。已在文中显式降权，
   推荐主要靠第 1 条（自动启动会偷走人的那次尝试额度）和第 3 条（绕过 §3.5 放行闸）。
   并给了明确的**复议条件**。
5. **`ExecStartPre=+` 的 `privileged` 标志依赖 systemd 版本。**
   §4.4 给了 `systemctl show -p ExecStartPre` 的验证命令；若该 systemd 太老不支持 `+` 前缀，
   root helper 会以 `napcat-native` 身份跑并因失败关闭而**拒绝启动**（安全，但起不来）。
   文中已写明这个症状与排查方向。
6. **§3.5 闸门对 SSH rc 的判定有一处理论歧义**：`ssh` 自身失败返回 255，
   而远端脚本理论上也可能返回 255。本文的远端脚本只 `exit 0/1`，所以实践中不冲突，
   但这是一个未消除的边界情况。
7. **macOS 路径已被删除**（F17）。若将来确实只有 Mac 可用，
   "在 Mac 上跑开机自启 Linux VM"这条替代路径**本 runbook 未验证**，需要单独验证宿主重启后 VM 自动回来。
8. **本次修订未执行任何真机命令**。所有"预期输出"仍是**待现场核对**的断言，
   不是已观测到的事实。V3–V14 只覆盖语法、编译和可注入的逻辑分支。

---

## 追加：与并行修订的 `T3-design.md`（rev-1）二次对齐

修订过程中另一个 agent 并行改完了 `T3-design.md`（63 KB → 133 KB，mtime 03:54，
本 agent **全程未写该文件**，已用 `git diff --stat` 与 mtime 双重确认）。
本节记录**二次核对结果与追加适配**。

### 未变（我的适配依然成立）

T3 rev-1 明确写了「命令形态不变（修订单锁定：`report --compare` 与 `report --epoch ...`）」、
「`epoch` 的取值集合不变（`datacenter` / `home_broadband`），字段名不变」、
「**没有重命名或删除**任何既有字段」。
⇒ §8.2 / §8.3 / §8.4 采用 T3 契约的方向**正确且仍然有效**。
（`schema` 从 1 提到 2，但我的闸门只校验该字段**存在**，不锁版本号，不受影响。）

### 追加适配 1：pilot 闸门多了一条前置

T3 §10.6.1 的表格现在是**四条**，比我最初写的多了
**`A4_WRITER_UNVERIFIABLE` 已清除（§8.3 沙箱硬条件全过）**。
⇒ §8.1.2 的前置表已补上这一行。

### 追加适配 2：「非删失」升级为「非删失**且证据完整**」

T3 rev-1 §10.2.2 新增：**含 `account_evidence="unknown"` 的区间不得进中位数**（其审计 F-07）。
⇒ §8.1.2 的口径说明和闸门脚本都补上了 `account_evidence != "unknown"`。
**不补的话，T4 的闸门会比 T3 自己的标准更宽松**，等于闸门形同虚设。

**本地实测**：同一批 12 段非删失区间，其中 5 段 `account_evidence="unknown"` 时
⇒ `EPOCH_DATACENTER_NONCENSORED=7` ⇒ `BASELINE_GATE=BLOCK`；
全部 `online` 时 ⇒ `=12` ⇒ `PASS`。

### 追加适配 3：跨 epoch 区间的归属，T3 已经定义了

我原本在 §8.2.2 标注「T3-design 没有规定跨界区间归属」并据此要求"必须在停机窗口切"。
**T3 rev-1 §10.4.1 已经把它定义清楚**（其审计 F-08）：
每拍开头比对环境变量，变了就 `end_reason="epoch_change"` + `end_censored=true` 关掉当前区间，
再以 `start_censored=true` 开新区间，**区间永不跨 epoch**。

⇒ **我删掉了那条已经不成立的"未定义"断言**，改写为：
数据污染问题**由 T3 保证，不用 T4 操心**；但"在停机窗口内切"仍然推荐，
理由降级为一条更简单的 —— 此时没有 `ONLINE` 区间开着，切换**不会白白烧掉一段删失区间**
（不致命，但样本量本来就紧张）。
另按 T3 §10.4.1 的建议，补了"迁移当天设 `mute_until` 覆盖扫码窗口，账本照记、静音只影响发送"。

### 追加适配 4（最重要）：迁移**后**的判定规则被 T3 rev-1 推翻了

我原先在 §8.4 和附录 B 写的是「迁移后再收满 ≥7 天 / ≥10 段」——
这照抄自 T3 初稿，而 **T3 rev-1 §10.6.2 把它单独拎出来定性为「自毁式的错误」**（其审计 F-09）：

> 如果家宽迁移确实管用、账号连续在线超过 7 天，那么这 7 天里**一段完成区间都不会产生**。
> 于是「迁移成功」这个结果，恰恰无法通过验收。

⇒ 为了凑 10 段而等待 = **主动等待失败发生**；凑出的样本还有选择偏差；
最坏会诱使人**去人为制造掉线来"补样本"**。

**改动**：§8.4 重写判读部分：
- 加 ⚠️ 框点明「迁移后**不是**"再收满 ≥7 天 / ≥10 段"」，并写出为什么它自毁。
- 换成 T3 §10.6.2 的**四行先命中先返回**结论表
  （`POST_MIGRATION_MIN_DAYS=7` / `POST_MIGRATION_MAX_WAIT_DAYS=30`）。
- 写出第 2 行（零失败）的 **rule of three** 手算口径：
  `λ₀ = failures/exposure_h`，`λ_upper = 3/T`，**`3/T < λ₀/2` ⇒ 显著改善**，
  否则打印 `T_needed = 6/λ₀`。并给了 T3 那个直观算例：
  基线每 2 小时掉一次（`λ₀=0.5`）⇒ **连续在线 12 小时零掉线即可在 95% 置信下排除"掉线率没变"**。
  这一行**才是迁移成功时最可能命中的那一行**。
- 原来那张"中位数怎么读"的表降级为**命中第 1 行时**才适用。
- 补了一条验收纪律：报告**必须打印暴露/失败/删失三元组**（T3 §10.5），
  「只有中位数的报告说明 T3 实现没做完，别拿它下结论」。
- 附录 B 的"之后"清单同步改写（7 天出报告、零失败看 rule-of-three、30 天必须出结论、
  **明确写"不要为了凑样本去等掉线"**）。

> **这条如果不改，T4 会把一次成功的迁移判成"数据不足"**，
> 是本次二次对齐里唯一一条会直接导致错误结论的问题。
