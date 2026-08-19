# corlinman → hermes 迁移总计划

**编排者**: 主智能体（Orchestrator）
**开始时间**: 2026-08-18
**目标**: 把 corlinman 的全部定时任务、NapCat(QQ) 接入、以及"格兰"角色系统，以 hermes 原生方式迁移到 hermes-agent，并部署到 corlinman 现有生产机。

---

## 0. 基线事实（已核实）

### 目标服务器 `root@43.133.12.98` (corlinman.cornna.xyz)
- Debian，VM-0-16-debian，**2 vCPU / 1.9 GB RAM / 6 GB swap（已用 4 GB）**
- 磁盘 50 GB，**已用 89%，剩余 5.8 GB**；`/var/lib/docker` 占 20 GB，可回收约 5.9 GB
- Python 3.11.2 系统自带；`uv`/`uvx` 在 `/root/.local/bin`；node/npm 在 `/usr/bin`
- 已占端口：22, 80, 443, 6005(corlinman gateway), 3001(napcat onebot), 6099(napcat webui),
  8000(uvicorn), 8100, 9222(chrome cdp), 18080/18181/18443/19080/1443/10443
- 运行中：`corlinman.service`, `corlinman-agent.service`(gRPC 50051), `corlinman-napcat` 容器,
  copytrader 7 容器, tradingagents-web, xray, nginx, fail2ban

### corlinman 侧资产
- 数据目录 `/opt/corlinman/data`，代码 `/opt/corlinman/repo`
- 生产实际跑过的 scheduler job（`scheduler.sqlite` 只存运行历史，非定义）：
  `persona.decay`(1260), `system.update_check`(228), `evolution.darwin_curate`(56),
  `hermes.competition_daily`(3), `hermes.qzone_reply`(3), `hermes.analysis_digest`(2),
  `hermes.daily_agenda`(2), `hermes.diary_summary`(2), `hermes.qzone_daily`(2),
  `hermes.qzone_friends`(2), `hermes.youtube_daily`(2), `grantley.qzone_reply`(1)
  — **最后一次执行 2026-07-27，此后静默**
- QQ 渠道 3 个 monitor 每日任务：`sanhu`(10:00 → user 2104743984)、`jlu`(11:00 → user)、
  `qunjlu`(09:00 → group 183287894)
- QQ 渠道：ws `127.0.0.1:3001`，webui `127.0.0.1:6099`，5 个群白名单，关键词"格兰"，
  humanlike persona = `grantley`；Telegram 渠道 persona = `lycaon`
- Provider: `cornna`（openai_compatible, `https://api.cornna.xyz/antigravity/`），
  默认模型 `gemini-3.7-flash-tiered`，别名含 claude-opus-4-6-thinking / claude-sonnet-4-6

### hermes 侧
- 22 个平台适配器，**无 QQ/OneBot** → napcat 接入需从零写适配器
- 原生扩展点：`cron/`(jobs/scheduler/blueprint)、`skills/`、`plugins/`、`tools/`、`plugins/platforms/`
- 参照：`sweetcornna/personal_hermes` 分支 `feat/qzone-publish`（格兰/qzone 原始实现）

---

## 1. 关键决策记录（Orchestrator 自主拍板）

| # | 决策 | 理由 | 备选与放弃原因 |
|---|---|---|---|
| D1 | hermes 与 corlinman **共存部署**，独立 systemd 单元 + 独立数据目录 + 独立端口 | corlinman 仍在服务线上 QQ/TG，直接替换会中断服务；共存可灰度切换、可回滚 | 直接替换：不可回滚，风险不可接受 |
| D2 | **瘦身安装**：浅克隆 + 最小依赖（不装 browser/camoufox/desktop/媒体生成） | 磁盘仅剩 5.8 GB，内存仅剩 190 MB | 全量安装：必然爆盘 |
| D3 | **复用现有 NapCat 容器**，hermes 走 OneBot v11 连 `127.0.0.1:3001` | 1.9 GB 内存起不动第二个 NTQQ 实例 | 新起容器：内存不足 |
| D4 | 任务迁移用 **hermes 原生 cron/blueprint**，不移植 corlinman scheduler runner | 用户明确要求"原生方式" | 移植 runner：非原生，且拖入 corlinman 依赖 |
| D5 | 部署前先回收 docker 空间（build cache + 悬空镜像），不动运行中容器与已停容器的卷 | 释放 ~1.5–2 GB 且零风险 | `prune -a` 会删掉已停 sub2api 三件套的镜像，用户可能还要用 |
| D6 | 新增 SSH 配置 `corlinman-prod` 走 ControlMaster 复用连接 | 高频握手触发了 sshd 拒连（fail2ban / 资源不足） | 每次新建连接：已实测导致失联 |

---

## 2. 任务分解与批次

### 批次 1 — 侦察（进行中，只读）
- **A1** corlinman 任务定义提取 → 迁移契约（Sonnet）
- **A2** 格兰系统全量清单 + 生产态数据 → 迁移契约（Sonnet）
- **A3** hermes 原生扩展点映射 + 部署足迹分析（Opus，架构性）
- **A4** 服务器容量与共存方案（Orchestrator 自办）

### 批次 2 — 骨架（依赖 A3/A4）
- **B1** 服务器瘦身 + hermes 最小化安装 + systemd 单元 + 健康检查
- **B2** OneBot v11 / NapCat 平台适配器
- **B3** 配置迁移：provider / 模型别名 / QQ 群白名单 / 关键词 / 限流

### 批次 3 — 格兰系统（依赖 A2/B2）
- **C1** persona 定义 + system prompt + life seeds
- **C2** life advance / decay 机制
- **C3** qzone 工具族（publish / reply / friends）
- **C4** 记忆 / 日记 / 关系数据迁移

### 批次 4 — 任务迁移（依赖 A1/B1/C*）
- **D1** 12 个 scheduler job → hermes 原生 cron
- **D2** 3 个 QQ monitor 每日播报
- **D3** 任务级验证（干跑 + 单次真实触发）

### 批次 5 — 集成验证与切换
- **E1** 端到端验证；**E2** 灰度切流；**E3** 回滚预案

---

## 3. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 内存仅剩 190 MB，swap 已用 4 GB | hermes 起不来或拖垮 corlinman | 瘦身安装；必要时迁移完成后停 corlinman 释放内存；监控 OOM |
| 磁盘剩 5.8 GB | 安装中途爆盘 | 先回收 docker；浅克隆；装前预留检查 |
| SSH 高频握手被拒 | 部署中断 | ControlMaster 复用连接；降低连接频率 |
| corlinman scheduler 已静默 3 周 | 迁移基准可能非"当前可用状态" | 以定义为准迁移，切换后逐个真实触发验证 |
| hermes 无 QQ 支持 | 适配器需从零写，工作量与风险最高 | 派 Opus；参照 personal_hermes 与 corlinman onebot.py 双实现 |

---

## 4. 执行日志

### 批次 1 — 侦察（2026-08-18）

| 任务 | 模型 | 审查结论 | 打回次数 | 产出 |
|---|---|---|---|---|
| A1 corlinman 任务契约 | Sonnet | ✅ 通过（返修后） | 1 | `A1-corlinman-task-contract.md` |
| A2 格兰系统清单 | Sonnet | ✅ 通过 | 0 | （见对话记录，关键结论已并入本文档） |
| A3 hermes 扩展点 | Opus | ✅ 通过（补全后） | 0（要求补全完整文档 1 次） | `A3-hermes-extension-points.md`（1267 行） |
| A5 OneBot/qzone 移植规格 | Opus | 进行中 | — | `A5-onebot-qzone-port-spec.md` |
| A6 生产静默诊断 | Opus | 进行中 | — | `A6-prod-outage-diagnosis.md` |
| A4 服务器容量 | Orchestrator | ✅ 完成 | — | 本文档 §0 |

**A1 打回理由（第 1 次）**：三处缺口——(a) 6 个任务背后的私有 builtin 插件源码未定位；(b) 完整生产 config.toml 未取回，导致 3 个 monitor 配置与多处默认值判定悬空；(c) `grantley.qzone_reply` 定义未恢复。均由 Orchestrator 补齐证据后退回整合，返修一次通过。

### 批次 1 关键发现（改变迁移前提）

1. **6 个"查无实现"的任务实现在仓库之外**：`/opt/corlinman-private`（模块 `corlinman_private_jobs`，1129 行），经 systemd drop-in `hermes-migration.conf` 注入环境变量加载。已完整取回。
2. **`persona.decay` 1260 次运行 100% 失败**（`data_dir_unavailable`），自 2026-06-04 起从未成功过一次——衰减机制实际上从未生效。
3. **全系统自 2026-07-27 起静默 22 天**：scheduler 零运行、`mk_observations` 零新增，即便服务 07-31 重启过。已派 A6 专查。
4. **格兰的生产态是空的**：`state_json={}`，无 life、无 diary、无立绘资产；`mk_items` 蒸馏记忆为 0，仅有 743 条原始观察（2026-07-18→27）。
5. **主机时区是 Asia/Tokyo (JST, +0900)**，非上海。3 个 QQ monitor 未设显式时区 → 走进程本地时区，标称 10:00 实为北京时间 09:00。**迁移时若直接写 Asia/Shanghai 会整体平移一小时**。
6. **hermes 侧四项必须从零构建**（A3 §Gaps）：G1 OneBot v11 传输层、G2 带衰减的生活事件存储、G3 按频道的动态人格、G4 qzone 工具族。
7. 部署路线确定：零 extras 源码安装，OneBot 适配器与 12 个 cron 任务**零新增依赖**；`memory_high_mb` 必须显式设小整数，否则 `"auto"` 在本机算出 ~1235 MB，压力驱逐永不触发、内核先 OOM。

### 决策补充

| # | 决策 | 理由 |
|---|---|---|
| D7 | 服务器布局用 `/opt/hermes/{repo,venv,data,logs}` + 系统用户 `hermes`，`HERMES_HOME=/opt/hermes/data` | A3 建议的 `/opt/hermes-agent` + `/var/lib/hermes` 等价；已预置的布局更集中、便于整体备份与回滚 |
| D8 | 迁移任务时区一律显式声明，不依赖进程本地时区 | 生产机 JST 与业务预期的北京时间差 1 小时，隐式回退是隐藏地雷 |
| D9 | `grantley.qzone_reply` 不单独迁移 | 定义不可恢复；功能已被 `hermes.qzone_reply` 覆盖（同 action、同 persona） |

### 域名（2026-08-19）

- **`hermes.cornna.xyz` → `43.133.12.98`，Cloudflare 代理开启（橙云），TTL auto**
- 记录 id `7cf59c0c8bfbcd3395b3a20225aa9687`，zone `cornna.xyz`（`47b5bc322d38e55e26cb080493a42331`）
- 配置完全对齐既有的 `corlinman.cornna.xyz`（同 IP、同代理状态），已从外部主机验证解析到同一组 CF anycast 地址
- 凭证：Cloudflare API Token 由用户提供，**仅在命令环境变量中使用，未写入仓库或任何配置文件**

| # | 决策 | 理由 |
|---|---|---|
| D11 | 主机名用 `hermes.cornna.xyz`，与 `corlinman.cornna.xyz` 并列 | 用户未指定主机名；与既有命名一致，且两套系统需并存一段时间，各自独立入口便于灰度与回滚 |
| D12 | 开启 CF 代理（橙云），与 corlinman 一致 | 隐藏源站 IP、复用 CF 边缘 TLS；源站 nginx 侧配置待 B1 安装完成后按 corlinman 现有 vhost 同构落地 |

**待办（依赖 B1 完成）**：源站 nginx 增加 `hermes.cornna.xyz` vhost，反代到 hermes dashboard 的 `127.0.0.1:9119`；证书与 TLS 策略照抄 corlinman vhost。

---

## 5. 重大更正 —— 「22 天静默」不存在（2026-08-19，A6 诊断）

**A1 与 A2 的共同前提被推翻。** 生产在 **2026-07-27 01:40 UTC** 做过一次存储拆分，注入
`CORLINMAN_EXECUTION_STATE_DIR=/opt/corlinman/execution-state`。此后运行期数据写入新目录，
`/opt/corlinman/data/` 下的同名文件冻结为快照。两份独立审计都读了死文件。

| 文件 | 旧目录 `data/`（死） | 新目录 `execution-state/`（活） |
|---|---|---|
| `scheduler.sqlite` | 1563 行，止于 2026-07-27 01:03 UTC | 838 行，最后 2026-08-18 15:30 UTC |
| `memory.sqlite` → `mk_observations` | 758 行 | 2313 行 |
| `agent_journal.sqlite` | 95 MB | 279 MB |

实际只有 **29 分钟**的切换间隙（死表最后一条 01:03:39 → 活表第一条 01:40:39）。

### ⚠️ 拆分是**部分的** —— 迁移 ETL 的头号陷阱
`scheduler_runtime_jobs.json`（任务定义）**至今仍只在旧目录** `/opt/corlinman/data/`，
而运行历史、记忆、人格状态、qzone 数据在新目录。**任何指向单一目录的迁移脚本都会丢数据。**
这个坑已骗过两份独立审计，切换前必须逐项确认数据来源。

### 活目录中新发现的迁移资产
`qzone_post_log/`、`qzone_seen_comments/`、`qzone_friend_comments/`（格兰真实发布/回复历史与去重状态，
A2 原判定为「本机不存在」）、`qq_group_history.sqlite`（monitor 抓取的群消息）、`personas/`、`workspace/`、`files/`。

### 其他已确认事实
- `persona.decay` 恒失败是**真实 bug**：全仓库有 **4 份互相分歧的 `resolve_data_dir` 实现**；
  `persona_decay.py` 用自己的 2 探测版本，探测不到 Starlette 的 `app.state`（真实键名是
  `corlinman_data_dir`/`corlinman`/`corlinman_state`，没有裸 `data_dir`），因此永远失败。
  用 `registry.resolve_data_dir`（4 探测）或 `chat_driver.resolve_data_dir`（带环境变量兜底）的任务则正常。
  **这是必须避免在 hermes 复制的 corlinman 代码缺陷。**
- `system.update_check` 在活表是 90/90 全成功，并非受害者。
- **`corlinman-napcat.service` 与 `corlinman-napcat-manager.service` 疯狂重启**
  （NRestarts 分别为 309304 / 605725，约 30 次/分钟），占用 61% 的 journal 行，
  导致 **journald 保留期只有约 7 小时**。切换前必须先修，否则 hermes 自己的日志也会在 7 小时内被冲掉。
- `certbot.service` 处于 failed 状态（因另一个无关域名），可能在 2026-09-18 前后阻断 corlinman 证书续期。

| # | 决策 | 理由 |
|---|---|---|
| D13 | A1、A2 全部返修，运行期结论一律以 `execution-state/` 为准 | 结论建立在死数据上，直接影响「任务迁移后会不会真的发消息」这类不可逆判断 |
| D14 | 切换前先修 journald 保留期与 napcat 重启风暴 | 否则 hermes 上线后无法排障（日志 7 小时即失） |
| D15 | hermes 侧不复制 corlinman 的 `resolve_data_dir` 模式，数据目录解析走单一显式实现 | 该缺陷已在生产静默失败 1260 次而无人察觉 |

---

## 6. B1 安装完成（2026-08-19）

**hermes 运行时已装在生产机，未启用开机自启、当前手动可启停。线上服务零扰动**
（9 个容器、26 个服务、20 个监听端口与安装前快照完全一致；corlinman `/health` 全程 200）。

### 实测数据（替换 A3 的 UNVERIFIED 估算）

| 项 | 实测 |
|---|---|
| `/opt/hermes` 总计 | **407 MB**（估算 450–670 MB） |
| └ venv（61 个包） | **123 MB**（估算 250–400 MB） |
| 网关稳态 RSS | **105 MB** |
| cgroup 峰值 | **122 MiB**（距 `MemoryHigh=384M` 有 3.1× 余量） |
| `memory_high_mb: "auto"` 实测值 | **1278 MB** ← 坑已实证 |
| 监听端口 | **0 个**（9119 保持空闲） |

- 安装：`uv pip install --only-binary=:all: -e "."`，零 extras、零源码编译（先 `--dry-run` 证明 60 个包全有 wheel）
- 启动：`systemctl start hermes.service` → 以 uid 991 运行 `/opt/hermes/venv/bin/python -m hermes_cli.main gateway run`
- systemd 加固：`MemoryHigh=384M` / `MemoryMax=512M` / `MemorySwapMax=512M` / `OOMScoreAdjust=500`，
  确保全局 OOM 时内核优先选中 hermes 而非 corlinman
- 确认 `websockets` / `httpx` / `croniter` 均可导入 → **OneBot 适配器与 12 个任务零新增依赖**

### B1 发现的问题（已登记）

| # | 问题 | 处置 |
|---|---|---|
| P1 | **SQLite 3.40.1 过旧，hermes 拒绝启用 WAL**，退回 DELETE 模式；apt 无更新版本 | 12 个任务需**错峰调度**避开整点，否则 `database is locked` 争用。已作为 D 批次约束 |
| P2 | `corlinman-napcat-manager.service` 崩溃循环根因：`/opt/corlinman/data/.napcat/managed` 不存在，`226/NAMESPACE` 失败，已重启 60 万次 | **不在本批次修**——建目录可能让 manager 真的接管并重写 NapCat OB11 配置，危及线上 QQ 桥接。放入切换窗口，带验证执行 |
| P3 | journald 保留窗口仅约 7.5 小时 | ✅ **已修**（见下） |
| P4 | `corlinman-agent` 实际监听 unix socket `/run/corlinman-agent/agent.sock`，非 TCP 50051 | 文档更正，无影响 |
| P5 | `/opt/hermes/repo` 对 hermes uid 可写 = 智能体可改写自身源码 | 启用带工具权限的功能前需复核 |
| P6 | 停止服务时退出码 1 导致单元显示 failed（hermes 固有行为） | 刻意不加 `SuccessExitStatus=1`，以免掩盖真实崩溃 |

### journald 保留期修复

根因不是磁盘阈值，而是既有 drop-in `/etc/systemd/journald.conf.d/90-sota-node-limits.conf`
显式设了 `SystemMaxUse=100M`，叠加 napcat 重启风暴（约 1.7 万行/小时）导致窗口仅 7.5 小时。

处置：新增 `/etc/systemd/journald.conf.d/95-hermes-retention.conf`（排序在 90- 之后取得优先级），
放宽到 `SystemMaxUse=300M` / `SystemMaxFileSize=30M`，预计窗口延长至约 22 小时。
**未删除用户原有的 90- 文件**；回滚 = 删除 95- 文件后重启 journald。治本仍是修 P2。

---

## 7. A1 二次返修结论（2026-08-19）—— 生产任务的真实健康状况

以活库 `/opt/corlinman/execution-state/scheduler.sqlite` 重做后，结论按投递渠道**两极分化**：

| 渠道 | 任务 | 真实状态 |
|---|---|---|
| **QQ / QQ空间** | `hermes.qzone_daily`、`hermes.qzone_reply`、`hermes.qzone_friends` | ✅ **成功率 87–100%，正在真实发帖**（非 shadow）。格兰此刻仍在往 QQ 空间发说说、回评论 |
| **Telegram** | `hermes.competition_daily`、`hermes.diary_summary`、`hermes.analysis_digest`、`hermes.youtube_daily` | ❌ **失败率 91–100%**，内容正常生成，卡在最后 `telegram_send_failed` |
| 内置 | `persona.decay`、`evolution.darwin_curate` | ❌ **自诞生起 100% 失败**（`data_dir_unavailable`，见 D15） |
| 内置 | `system.update_check` | ✅ 90/90 全成功 |

### Telegram 投递失败的根因（Orchestrator 实测判定）

- Bot token **有效**：`@Cornna_bot`（id 5420007505），`getMe` 返回 ok
- 目标频道 `-1003990634877`：`getChat` 返回 **`Bad Request: chat not found`**

→ **机器人已不在该频道**（被移除 / 频道删除 / chat id 变更）。属凭证与权限问题，**迁移到 hermes 不会修复它**。

| # | 决策 | 理由 |
|---|---|---|
| D16 | 四个 Telegram 任务照原样迁移，保留原 chat_id 与 topic_id，并在交付说明中标注该缺陷 | 迁移不应掩盖或顺手改写业务目标；频道恢复当天任务即自动可用 |
| D17 | QQ 侧三个任务迁移时必须做**防重发**处理，切换期间新旧两侧不得同时启用 | 它们正在真实发帖，重复发布对外可见且不可撤回 |

**需要用户处理**：把 `@Cornna_bot` 重新加入目标频道，或提供正确的 chat id。

---

## 8. 批次 1 全部通过（2026-08-19）

| 任务 | 模型 | 结论 | 打回 | 产出 |
|---|---|---|---|---|
| A1 corlinman 任务契约 | Sonnet | ✅ 通过 | **2** | `A1-corlinman-task-contract.md` |
| A2 格兰系统清单 | Sonnet | ✅ 通过 | **1** | `A2-grantley-system-inventory.md`（514 行） |
| A3 hermes 扩展点 | Opus | ✅ 通过 | 0 | `A3-hermes-extension-points.md`（1267 行） |
| A5 OneBot/qzone 移植规格 | Opus | ✅ 通过 | 0 | `A5-onebot-qzone-port-spec.md`（919 行） |
| A6 生产静默诊断 | Opus | ✅ 通过 | 0 | `A6-prod-outage-diagnosis.md` |
| B1 服务器安装 | Opus | ✅ 通过 | 0 | 生产机 `/opt/hermes` + `hermes.service` |

**打回均非能力问题**：A1 两次、A2 一次，全部源于「数据源被静默切换」这一环境陷阱——
该陷阱骗过了两个子智能体和 Orchestrator 本人，最终由 A6 的独立诊断挖出。
按升级策略本应在两次打回后换模型重做，此处判定不适用：返修后两份文档均一次通过，
且 A2 返修时未轻信 Orchestrator 的更正，而是独立复核了环境变量、重算了全部数字、
并到源码中验证根因（`registry.py` docstring 自述该函数被 "copy-pasted across six builtin modules"）。

### A2 返修撤回的 5 条结论
1. ~~格兰已静默 22 天~~ → 活库显示每天都有活动，至今
2. ~~scheduler 07-27 后停止~~ → 活库 838 行跑到今天
3. ~~从无真实 QZone 发布，全是 shadow~~ → **`qzone_post_log/grantley.json` 有 19 篇真实说说**，带真实 `tid`/`qzone_url`，2026-07-28→08-17
4. ~~qzone 去重 sidecar 不存在~~ → 三个目录均存在且有内容
5. ~~生产人格正文与仓库有 `WebSearch`/`web_search` 漂移~~ → 活库与仓库**逐字节一致**，漂移只存在于冻结快照

### 复核后仍然成立的结论
grantley 的 life/diary 状态确实为空；`mk_items`/`mk_core` 确实为 0；grantley 确实零立绘资产；
`persona.decay` 确实 100% 失败（根因：`persona_decay.py` 保留了过时的 2 探测本地副本，
而 `registry.py` 后来才加入规范实现）。

### 进行中
- B2 OneBot v11 适配器（Opus）
- C1 格兰人格系统（Opus）
- C4 迁移数据导出与切换前备份（Sonnet）

---

## 9. 会话额度中断与恢复（2026-08-19）

B2、C1、C4 三个子智能体因 API 会话额度同时中断（非任务失败）。盘点后：

| 任务 | 中断时状态 | 处置 |
|---|---|---|
| B2 OneBot 适配器 | 3784 行落盘，5 个模块 `ast.parse` 全部通过、无截断；**但缺 `plugin.yaml` 与 `__init__.py`**，插件无法被发现 ⇒ 代码全是死的；无测试、无文档 | 已唤回续跑 |
| C1 格兰人格 | 约 3600 行实现 + 1015 行测试落盘；**测试 82 通过 / 3 失败**；缺交付文档 | 已唤回续跑 |
| C4 数据导出 | 零产出 | 暂缓，避免再次触发额度；切换前必须完成 |

### Orchestrator 对 C1 三个失败测试的独立判定

全部为**测试缺陷，被测代码正确**，但其中一个牵出真实继承缺陷：

1. `test_life_advance_makes_no_llm_call` —— 用朴素子串查找扫源码找 `"llm"`，被模块自身 docstring 里的 "makes an llm call"（出现在解释它*不*调模型的句子中）判负。要求改为基于 AST 的导入检查。
2. `test_travel_only_pack_redraws_activity_away_from_the_destination` —— 见 D18。
3. `test_life_advance_archives_the_previous_current_to_history` —— 期望 `history` 长度 1，实际 2。对照源实现 `life.py:739` 与移植版 `life.py:520-524`，语义是"beat 有变化即归档"，初始默认态 `at_academy/日常` 被第一次推进改变 ⇒ 归档合理。**代码正确，测试期望错误。**

| # | 决策 | 理由 |
|---|---|---|
| D18 | 修复 `draw_life_beat` 中不可达的 travel 重抽分支：由"严格优先级取第一个非空类别"改为"在所有非空类别中随机选一个" | 严格优先级下 `travel_destination` 排最后，仅当 academy 与 mission 均为空时才选中；而选中后的重抽逻辑又要求那两个池非空 ⇒ **分支永远不可达**。`grantley.yaml` 同时含 academy 与 mission ⇒ `traveling` 状态永不出现，10 个旅行地点成为死数据。已核对 `persona_life_advance.py:108-150`，确认移植忠实、缺陷继承自 corlinman。**修复零回归风险**：`persona.life_advance` 在生产从未启用（`enabled=false`，运行历史零触发），不存在需保持一致的线上行为 |
