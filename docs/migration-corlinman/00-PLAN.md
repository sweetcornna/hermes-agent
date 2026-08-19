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

### C1 验收（2026-08-19）—— ✅ 通过

Orchestrator 独立复核，未采信自述：

- `.venv/bin/python -m pytest tests/plugins/grantley/ -q` → **89 passed**（亲自执行）
- 5 个提交在 `feat/corlinman-migration`：`674b9b7b3` / `6bd514a24` / `de22f0cd5` / `e6f25db60` / `7b5ffd047`
- **D18 实测验证**：用真实 `grantley.yaml` 抽样 300 次，生活状态分布
  `{at_academy: 105, on_mission: 104, traveling: 91}` —— 修复前应为 300/300 全 `at_academy`。
  travel 样例 `location='深山温泉' activity='替商会找回被劫的家传剑'`，两者不同，
  证明原本不可达的重抽分支现已生效。

三个失败测试的修复方向均正确（未以放宽断言蒙混过关）：
LLM 检查改为 AST 导入图遍历（可追踪相对导入、不受 docstring 干扰、别名导入无法隐藏）；
归档语义改为断言 `history[1].from == first["current"]`（真正验证"归档的是上一个 current"）
并补充"相同 beat 不重复归档"用例。

C1 主动指出的遗留项已处理：六份规格文档已提交（`304507f57`）。

C1 报告的未决风险（转入后续批次）：
- 无实机验证 —— `memory.provider: grantley` 在真实会话中被激活并调用，从未运行过
- 部署命名空间未验证 —— `$HERMES_HOME/plugins/` 下的相对导入未实测
- 1928 行原始对话**没有蒸馏实现**，且源系统的蒸馏管线从未运行过；
  **切勿**把原始行批量灌进 `life_events`，会淹没生活节拍
- qzone 三个状态目录尚未搬迁，是 C3 的前置；跳过会导致首次运行重复回复

### B2 验收（2026-08-19）—— ✅ 通过

Orchestrator 独立复核：

- `pytest` 六个 onebot 测试文件 → **304 passed**（亲自执行）
- 5 个提交：`745419d12` / `f330ac152` / `a2d049186` / `8144dd61c` / `7eaca3bc1`
- 注册链路已用真实机制端到端验证：`Platform('onebot') -> Platform.ONEBOT`，剩余抽象方法 0 个，零核心文件改动
- 更广回归 `tests/gateway/ -k "platform or plugin or delivery"` → 964 passed；2 处失败经移出本次文件后复现，确认为既有问题

交付规模：适配器侧 4120 行（含 `plugin.yaml` 132 行 + README 290 行），
共享同步客户端 `tools/onebot_client.py` 305 行（为 C3 qzone 工具族预留），测试 2837 行。

**B2 发现的上游缺陷（已复核属实）**：`tests/gateway/test_plugin_platform_interface.py:17`
的 `PROJECT_ROOT = Path(__file__).parent.parent` 解析为 `tests/` 而非仓库根，
于是去扫 `tests/plugins/platforms`，发现零个平台，两个测试恒为 skip。
**上游 23 个平台适配器从未被该契约测试实际验证过。** B2 未擅自修复（改 `parents[2]`
会让 23 个平台突然全部受检，属独立决策），转而在 `test_onebot_plugin.py::TestRegister`
中对本平台做了非空转的等价覆盖。→ 可回馈上游。

关键取舍：S9 附件上限 8 MiB（而非源实现的 30 MiB，内存预算所迫）；
并发默认 2（源实现为 8）；**S17 幂等性一致贯彻**——echo 超时视为「可能已送达」的乐观成功，
而非误报失败，因为误报失败会触发重试并导致真实社交动态重复发帖。

### 批次 3 派发（2026-08-19）
- **C3** qzone 工具族移植（Opus）—— 依赖 B2 的 `tools/onebot_client.py`；S14/S15/S16/S17 由其裁定
- **C4** 迁移数据导出与切换前备份（Sonnet）—— 重启，前次因额度中断零产出

---

## 10. 并发会话冲突与共享 venv 耦合（2026-08-19）

同机另一会话（social_workflow 的桌面端改造）曾误在本检出的 `apps/desktop/` 下工作
（18 文件 / 约 460 行，把产品名改为 `social_workflow`），且其 `sw-desktop` 分支基点误落在
本迁移分支的提交之上。经沟通已完全撤离：改动迁至独立 worktree
`/Users/cornna/project/sw-hermes-desktop`（分支 `sw-desktop`，基点重置为干净的 `origin/main`），
stash 栈已清空，本工作树复核只剩迁移工作自身文件。

**处置原则**：未擅自回滚或暂存对方任何改动——无法确认是否为用户有意为之，
擅自丢弃他人工作不可接受。改为主动联系对方会话说明情况并请其确认。

### ⚠️ 仍待用户处置：全局 `hermes` shim 指向本检出

`~/.local/bin/hermes` → `/Users/cornna/project/hermes-agent/.venv/bin/hermes`，
且为 editable 安装（`hermes_cli` 从 `/Users/cornna/project/hermes-agent/hermes_cli` 加载）。
因此**本机全局 `hermes` 命令执行的是本迁移分支上的代码**：

- 新增的三个 bundled 插件（`onebot` 平台、`grantley`、`qzone`）会被插件发现机制枚举；
  无配置不启用，但 `onebot` 已进入平台注册表（`Platform('onebot') -> Platform.ONEBOT`）
- 本分支一旦切换，全局 `hermes` 行为随之改变

对方会话已自建独立 venv 解耦，不再依赖该 shim。**shim 本身保持现状，交由用户决定。**

### 本次迁移对 hermes 上游代码的侵入性 —— 零

`git diff --stat 8911e2e0e..HEAD`（排除迁移文档）= **12290 insertions / 0 deletions / 0 处既有文件修改**。
全部为新增文件：`plugins/platforms/onebot/`、`plugins/grantley/`、`tools/onebot_client.py` 及对应测试。
`tools/mcp_tool.py`、审批机制、profile 机制均未触碰。→ 后续同步上游不会产生冲突。

### C3 / C4 验收（2026-08-19）—— ✅ 均通过

**C4 数据导出**：20 MB / 32 文件，30 个文件 sha256 在服务器端与本地各算一遍**全部吻合**，
6 个数据库导出后重新打开通过 `integrity_check` 且行数逐一对上。密钥全程在服务器端脱敏，
原值未进入任何上下文。已 gitignore（`c43ad1680`）。

> **新发现的生产缺陷（Orchestrator 已亲自复核）**：live `personas.sqlite` **只剩 grantley 一行**，
> `lycaon`（Telegram 绑定角色）与 `vivian` 在活库中不存在，仅旧目录尚存三行。
> 这是 2026-07-27 存储拆分**漏搬**的数据，非过期快照问题。C4 两份都导出，未替用户做取舍。
>
> **另一个被避开的陷阱**：旧 `personas.sqlite` 主文件仅 4 KB，三个人格正文全在 152 KB 的 WAL 里——
> 直接 `cp` 会静默丢失全部数据。C4 改用 `Connection.backup()`。
>
> **`qq_group_history.sqlite`（3 个 monitor 的数据源）仅保留约 3 天**（52649 行），
> 更早的群消息全机器无存档 —— D2 迁移 monitor 时须按此窗口设计。

**C3 qzone 工具族**：5 个工具（`qzone_publish`/`list_feed`/`get_post`/`post_comment`/`list_friends`），
注册进 `onebot` toolset 复用平台自动 toolset，无核心文件改动。
Orchestrator 独立复核：`tests/plugins/qzone/` **219 passed**；回归 grantley + onebot **393 passed**。

S14–S17 裁定：发布主机取 `h5.qzone.qq.com`（19 篇真实发布在其背后），**不设环境变量覆盖**
（请求携带借来的 cookie，可重定向的 URL 会把它送到任意地方）、**不做自动回退**
（对发布重试第二个主机本身就是重复发帖）；传输层用标准库 `urllib` + 可注入 seam，219 个测试全离线；
S17 幂等性**从"记录"升级为"强制"**——以内容指纹为键，传输失败记为 `unknown` 后，
相同内容的下一次尝试**不发包直接拒绝**（6 小时窗口），且交互式调用同样受保护。

> **状态文件零转换迁移**：根目录 `<hermes home>/plugin-data/qzone/`，可用 `QZONE_STATE_DIR` 覆盖，
> 现有 `/opt/corlinman/execution-state/` 三个目录可直接拷入。
> **切换时必须设 `QZONE_PERSONA_ID=grantley`**，否则工具会去读空的 `default.json` 并在首次运行重复回复全部评论。

**C3 的分支叙述不可信**：它声称把 `feat/corlinman-migration` 快进到了隔壁会话的 `sw-desktop`。
经核查实际状态干净——本分支 19 个提交顺序完整、未混入任何 `apps/desktop` 改动，
`sw-desktop` 也未含我方提交（两分支各自独有 19 / 134）。判定为撞上隔壁会话重置分支的瞬时状态，结果无害。
**再次印证：交付自述必须复核，不能采信。**

| # | 决策 | 理由 |
|---|---|---|
| D19 | **腾讯内容合规层必须在任何发布类任务启用前补齐** | corlinman 发布前对每条正文做审核（fail-closed、未分类媒体默认拒绝）并对读回文本脱敏，两者均未移植。规则集名为 `tencent-freeze-risk-2026-07-21.1`——**freeze 指账号封禁风险**。该层 318 行、零依赖、纯标准库，不移植是范围裁决而非技术限制。「少一道审核」是实打实的行为改变，风险落点是用户的 QQ 账号 |
| D20 | 政策拒绝**不得**写入 `unknown` 账本 | 拒绝意味着什么都没发出，重试必须保持自由；误记为 `unknown` 会错误封锁合法重试 6 小时 |

### 批次 4 派发
- **D0** 腾讯内容合规层移植（Sonnet）—— D19 的执行
- **D1** 12 个定时任务 → hermes 原生 cron（Opus）

### 已接受的缺口（不阻塞）
- `image_with_refs`（角色立绘锚定生图）hermes 无对应能力，`generate` 退化为提示词字符串；
  其 ~17 个测试用例未移植。**影响可忽略**：A2 确认 grantley 零立绘资产，生产 19 篇说说均为纯文本
- 读回的动态文本（他人评论）未做提示词注入过滤，进入 agent 上下文。源系统亦无此防护（其脱敏针对合规而非注入）

---

## 11. Telegram 配置（2026-08-19，用户提供新 token）

用户提供了一把**新的** Telegram bot token（与生产在用的不是同一个）：

| | 旧（corlinman 生产在用） | 新（用户本次提供） |
|---|---|---|
| 机器人 | `@Cornna_bot` | `@sweetcornna2_bot` |
| id | 5420007505 | 8720715962 |
| `getMe` | ✅ 有效 | ✅ 有效 |
| `getChat -1003990634877` | ❌ `Bad Request: chat not found` | ❌ **同样 chat not found** |

**关键判定**：两把 token 都够不到目标频道 ⇒ 问题不在"某个机器人没被拉进群"，
而在 **chat id `-1003990634877` 本身失效或双方都不是成员**。换 token 不能修复四个 Telegram 任务的投递。

新机器人 `getUpdates` 返回 **0 条**（无 webhook 占用，查询安全，不干扰生产的旧 bot 轮询），
说明它尚未收到任何会话消息，因此**无法自动发现正确的 chat id**。

### 已完成的配置
- 生产机安装 `python-telegram-bot==22.8`（**只装这一个包，不引入整个 `messaging` extra**——
  后者会拖入 `discord.py[voice]`、`slack-bolt`、`aiohttp`、`brotlicffi`，对 1.9 GB 机器是负担）。
  实测 venv 123 MB → **127 MB（仅 +4 MB）**，无源码编译，磁盘 7.7 G 可用。
- token 写入 `/opt/hermes/data/.env`（`$HERMES_HOME/.env`，hermes 读取 bot token 的规范位置），
  权限 **0600、属主 hermes**。通过 stdin 传输，未出现在服务器端进程参数中。**未写入仓库任何文件。**
- 验证：`hermes status` → **`Telegram ✓ configured`**
- **刻意未启动实时轮询**：人格绑定与任务尚未接好，且 corlinman 仍在线上；
  贸然上线会让机器人以默认配置回消息。

### 仍需用户处理
目标频道不可达。任一方式即可解决：
1. 把 `@sweetcornna2_bot` 拉进目标群/频道并在其中发一条消息 → 届时可用 `getUpdates` 自动发现正确 chat id
2. 或直接提供正确的 chat id

| # | 决策 | 理由 |
|---|---|---|
| D21 | 只安装 `python-telegram-bot`，不启用 `messaging` extra | 目标机内存与磁盘紧张；其余 6 个包全部无用 |
| D22 | token 存 `$HERMES_HOME/.env`（0600），不进 systemd `Environment=`、不进仓库 | `systemctl show` 可被非 root 读取部分属性；仓库会被推送 |

### ⚠️ 新增待办：LLM Provider 尚未迁移
`hermes status` 显示全部 provider 均为 not configured。corlinman 生产使用
`cornna`（`openai_compatible`，`https://api.cornna.xyz/antigravity/`，默认模型 `gemini-3.7-flash-tiered`，
别名含 `claude-opus-4-6-thinking`、`claude-sonnet-4-6`）。
**没有 provider，迁移过来的 12 个任务全部无法产出内容。** 归入原计划的 B3（配置迁移），需尽快派发。

---

## 12. 批次 4 中段盘点与续跑（2026-08-19，会话二）

会话恢复时的实际状态盘点（Orchestrator 亲自核验，未采信任何自述）：

| 任务 | 状态 | 证据 |
|---|---|---|
| D0 腾讯合规层 | ✅ **通过** | 已提交 `9a3160031`；见下 |
| D1 12 个定时任务 | ⚠️ **半成品，已唤回续跑** | 见下 |
| B3 配置迁移 | 🔵 **本次新派（Opus）** | provider 缺失是 12 个任务的总阻塞 |

### D0 验收（Orchestrator 独立复核）

- `.venv/bin/python -m pytest tests/plugins/qzone/ tests/plugins/grantley/ -q` → **356 passed**（亲自执行）
- 交付：`plugins/qzone/policy.py` 418 行 + 两个测试文件 586 行 + 三处 wiring，共 1285 insertions
- **零依赖属实**：`policy.py` 的 import 全部为标准库（`hashlib`/`re`/`unicodedata`/`dataclasses`/`enum`/`typing`），无第三方
- **fail-closed 属实**：四个调用点全部 `try/except` 包裹分类器，异常落到 `classifier_failure_decision`；未分类媒体默认拒绝
- **D20 属实**：政策拒绝走 `_policy_error()` 返回 `content_policy_blocked`，在 S17 幂等账本写入**之前**返回，不污染 `unknown` 账本、不封锁合法重试
- 入站脱敏属实：`feed.py::_redact_feeds` 在文本进入模型 prompt 前替换，并回报 `policy_redactions` 计数供运维观察
- 规则集版本 `tencent-freeze-risk-2026-07-21.1` 与源系统一致

### D1 中断态盘点 —— 与 B2 上次的失败模式完全相同

4 个文件 1916 行落盘，**全部 untracked**，且**缺 `plugin.yaml` 与 `__init__.py` ⇒ 插件不会被发现，1916 行代码全是死的**；
`installer.py` 被 `specs.py` 与 `corlinman_jobs_lib.py` 的 docstring 引用却根本不存在；零测试、零文档。

```
plugins/corlinman_jobs/specs.py                       527 行
plugins/corlinman_jobs/prompts.py                     228 行
plugins/corlinman_jobs/preflight.py                   301 行
plugins/corlinman_jobs/scripts/corlinman_jobs_lib.py  860 行
```

**已落盘部分判定为可用基础，不推倒重写**：`specs.py` 是单一事实源、12 个 JobSpec 齐备、
三条不变量（显式时区 / 全部 `install_enabled=False` / 错峰避开整点）已编码进数据结构而非散落在安装逻辑里；
`prompts.py` 逐条标注 `VERBATIM` / `RECONSTRUCTED` 溯源；`preflight.py` 把检查表达为可测数据而非文本。
续跑指令：补 `installer.py`、`plugin.yaml`、`__init__.py`、测试、`D1-cron-port-notes.md`。

| # | 决策 | 理由 |
|---|---|---|
| D23 | D1 续跑而非重做，且明确禁止推倒已落盘的 4 个文件 | 中断原因是额度而非质量；抽查显示设计取向正确（不变量编码进数据、prompt 带溯源标记）。重做等于丢弃 1916 行可用产出 |
| D24 | B3 提到最高优先级，与 D1 并行 | 无 provider 则 12 个任务即使装好也产不出任何内容，是整条链路的总阻塞；且 B3 在服务器侧、D1 在本地代码侧，互不冲突 |

### 环境修复

外部进程曾重建本仓库 `.venv`，**`pytest` 被删掉**（`No module named pytest`），
导致任何"跑测试验收"的动作会直接失败或被误判为代码问题。
已补装 `pytest==9.1.1` + `pytest-asyncio==1.3.0`（`uv pip install --python .venv/bin/python`），未重建 venv
——本机全局 `hermes` shim 挂在这个 venv 上，重建会波及其他会话。

### 待处置的游离改动

`contributors/emails/agent@Agents-Mac-mini.local`：`skip-agent` → `momomojo`，
非本次迁移产生，来源不明（疑似提交时被某工具自动改写）。
**未擅自回滚**——无法确认是否为用户有意为之。交由用户决定。

### 独立基线快照（2026-08-19 12:03 JST，Orchestrator 亲测，用于事后核验"线上零扰动")

| 项 | 值 |
|---|---|
| 运行中容器 | **9** |
| running systemd 服务 | **27** |
| 监听 socket | **21** |
| corlinman `/health` | **200** |
| `hermes.service` | inactive |
| 可用内存 | **383 MB**（total 1966 / used 1583） |
| 根分区可用 | **7.7 G**（85% used） |

> 与 §6 的 B1 记录（26 服务 / 20 端口）相比 +1 服务 +1 端口。取此快照为新基线，
> 切换后以本表而非 B1 表做对照。**子智能体自报的"零扰动"一律与本表核对，不采信其自取的基线。**

### D2 前置：3 个 QQ monitor 的时区补偿（Orchestrator 裁定）

A1 §4 已确认：三个 monitor **都没有显式 `timezone`**，`proactive_timezone` 也未设，
因此按 `_qq_monitor_tzinfo` 的回退链走进程本地时区 = **`Asia/Tokyo`**。
而 8 个 `hermes.*` 定时任务的 `source_timezone` **全部显式写着 `Asia/Shanghai`**
（3 个内置 builtin 用 UTC，但都是小时级/6 小时级，整点偏移下时区无关）。

⇒ 迁移到进程级 `HERMES_TIMEZONE=Asia/Shanghai` 后：**12 个任务逐字复现，3 个 monitor 会整体晚 1 小时**。

| monitor | 配置写的 | 生产**实际**触发（北京时间） | 迁移后应写 |
|---|---|---|---|
| `qunjlu` | 09:00 | **08:00** | `08:00 Asia/Shanghai` |
| `sanhu` | 10:00 | **09:00** | `09:00 Asia/Shanghai` |
| `jlu` | 11:00 | **10:00** | `10:00 Asia/Shanghai` |

| # | 决策 | 理由 |
|---|---|---|
| D25 | 三个 monitor 按**实际观测行为**（-1 小时）迁移，而非按配置字面值 | 与 D16 同一原则：迁移复现行为，不猜测意图、不顺手"修正"。收件人 `2104743984` 已按实际时间接收多时；按字面值迁会造成对外可见的时间漂移。**字面值与实际值的差异已在此显式记录**，用户若要改回 10:00/11:00/09:00 是一次独立的业务决策 |
| D26 | `qunjlu` 迁移后保持**被抑制**状态 | 生产中 `group_replies_enabled=false` 静默屏蔽了所有群目标投递，`qunjlu` 回发群 `183287894` 实际从未送达。迁移若"顺手放开"等于让一个事实上停用的播报突然开始向 QQ 群发消息 |

**D2 的数据窗口约束**：`qq_group_history.sqlite`（3 个 monitor 的唯一数据源）**仅保留约 3 天**（52649 行），
更早的群消息全机器无存档。`window_minutes=1440` 落在窗口内，安全；但任何"回补历史"的设想不成立。

### P2 补充诊断 —— 根因成立，但**修法应当推翻**（2026-08-19，Orchestrator 亲查）

P2 原记录的根因经复核**成立**：`/opt/corlinman/data/.napcat/managed` 确实不存在
（`.napcat/` 下只有 `app`、`ntqq`、`legacy-secrets.env`），
manager 单元带 `ProtectSystem=strict` + `ReadWritePaths=... /opt/corlinman/data/.napcat/managed ...`，
缺路径 ⇒ `Failed at step NAMESPACE ... No such file or directory`。

**但 P2 提出的处置（"建目录，放到切换窗口带验证执行"）应当放弃**，理由是本次新查到的三件事：

1. **线上 QQ 桥接是容器，不是这两个 systemd 单元。**
   `docker ps` → `corlinman-napcat` **Up 4 weeks**，发布 `127.0.0.1:3001`/`127.0.0.1:6099`；
   `/opt/corlinman/data/config.toml` 指向 `ws://127.0.0.1:3001` + `http://127.0.0.1:6099`
   —— 正是该容器的端口。
2. **`corlinman-napcat.service` 是被容器取代的原生部署残留，且根本跑不起来。**
   `ExecStart=/opt/corlinman/napcat/NapCat-v4.18.4-amd64.AppImage`，直接执行的真实报错是
   `FATAL: Running as root without --no-sandbox is not supported`（Electron），退出码 **127**。
   它每 5 秒重启一次，已 **317,380** 次。
3. **没有任何单元依赖这两者。**
   `corlinman-napcat.service` 只有 `PartOf=corlinman.service`（单向：corlinman 停时它跟着停，
   反向不成立）；`corlinman.service` 自身只有 `Requires=corlinman-agent.service`。
   manager 的 `/run/corlinman-napcat/` 至今不存在，其 socket 从未被任何客户端连上。

而 manager 的命令行是 `--mode native` + `CORLINMAN_NAPCAT_APPIMAGE=<AppImage>`
—— **建目录恰恰会让它启动并去接管一个"原生 NapCat"，而线上跑的是容器**。
这正是 P2 自己担心的"可能重写 NapCat OB11 配置、危及线上 QQ 桥接"，且概率不低。

| # | 决策 | 理由 |
|---|---|---|
| D27 | P2 改为 **`systemctl disable --now` + `mask` 这两个单元**，不建 `managed` 目录 | 两者均为被容器取代的原生部署残留，零依赖、零在用。停掉是**零风险**；建目录是**引入风险**。同时消除约 30 次/分钟的重启与 61% 的 journal 写入量 |
| D28 | 该操作可**提前到切换窗口之前**独立执行 | 原定放进切换窗口是因为当时判定"可能危及线上桥接"。既已证明与线上桥接无关，就不必和高风险的切换动作捆绑——反而应当先做，让 hermes 上线时 journal 已经干净 |

**待用户确认后执行**（涉及停用生产机上的既有服务单元，虽经证明为残留，仍属对外可见的状态变更）。
回滚：`systemctl unmask --now <unit> && systemctl enable --now <unit>`，恢复到当前的崩溃循环态。

### P3 复核 —— 修复有效

journald 保留窗口实测：最早条目 `2026-08-18T17:13` → 最新 `2026-08-19T12:04`，
约 **19 小时**（修复前 7.5 小时），磁盘占用 234 MB / 上限 300 MB。符合预期。
D27 落地后重启风暴消失，窗口应进一步大幅延长。

---

## 13. Telegram 投递阻塞解除（2026-08-19，用户已把 bot 拉进群）

用户按 §11 的请求把 `@sweetcornna2_bot` 加入目标群并发了消息。Orchestrator 立即复验：

| 检查 | 结果 |
|---|---|
| `getUpdates` | ok，**6 条**（此前为 0） |
| `getChat -1003990634877` | ✅ **ok=True**，`title='Corn Agents'`，`type=supergroup`，**`is_forum=True`** |
| bot 在该群的身份 | ✅ **administrator**，`can_manage_topics=True`，`is_anonymous=True` |
| 群默认权限 | `can_send_messages=True` |
| forum topic `11` / `12` / `13` / `680` | ✅ **四个全部有效** |
| general 话题 | ✅ 有效 |

**⇒ §7 与 §11 判定的「chat id 本身失效」已被推翻——真实原因是机器人不在群里。**
`-1003990634877` 是有效 id，**四个 Telegram 任务的原始投递目标无需任何改动**（D16 的"照原样迁移"因此成为完全正确的选择：
若当初"顺手改成一个能用的新 chat id"，现在反而要改回来）。

探测手法：`sendChatAction(action=typing, message_thread_id=<tid>)` —— 瞬时输入指示，
**不产生任何消息、不留任何记录**，但会校验 `message_thread_id` 的有效性。
刻意未做真实 `sendMessage` 测试：那是对外可见且不可撤回的动作，留到切换窗口随"上线"决定一并执行。
token 全程只在服务器端从 `/opt/hermes/data/.env` 读取，未经过本地、未进入命令行参数（`ps` 不可见）。

### 由此推出的切换风险重估

| 渠道 | 重叠窗口的重复投递风险 | 依据 |
|---|---|---|
| **Telegram** | ✅ **零风险** | corlinman 生产用的是**旧** bot `@Cornna_bot`(5420007505)，它**不在**该群 ⇒ corlinman 侧本就投递失败。hermes 用新 bot `@sweetcornna2_bot`(8720715962) ⇒ 只有一侧能送达，物理上不可能重复 |
| **QQ / QQ空间** | ⚠️ **高风险，D17 依然成立** | 双方共用同一个 NapCat 容器与同一个 QQ 空间账号，两侧同时启用会重复发帖/重复回评，**对外可见且不可撤回** |

| # | 决策 | 理由 |
|---|---|---|
| D29 | 四个 Telegram 任务可在 QQ 侧之前**独立先行上线**，不必等待完整切换窗口 | 重复投递物理上不可能（新旧 bot 分属不同身份，旧 bot 够不到该群），风险面与 QQ 侧完全解耦。先上 Telegram 能在低风险渠道上实测整条链路（provider → 生成 → 投递），把问题暴露在不可重复发帖的渠道里 |
| D30 | 保留 `is_anonymous=True` 现状，不调整 bot 权限 | 这是用户在 Telegram 侧的既有设置，不属于迁移范围；匿名管理员发言不影响投递 |

---

## 14. 新增范围：主动发言（proactive speech）原生化迁移 —— B4（2026-08-19）

用户追加要求：把 corlinman 的"主动发言"逻辑也以 hermes 原生方式迁移。

### 侦察结论（Orchestrator 亲查）

**corlinman 侧实现**：`corlinman-channels/src/corlinman_channels/service.py`（全文 8671 行），
主动发言块约 **517 行**（L774–L1290），配套测试 **1206 行**：
`test_qq_proactive.py`(681) / `test_qq_speech_cap.py`(158) / `test_qq_hot_apply.py`(367)。

**hermes 侧**：**零原生对应物**。全仓库 `proactive` 命中项全部无关
（上下文剪枝 `proactive_prune`、token 预刷新、Teams/WeCom 的"服务端主动发送 API"）。
`/loop` 的别名恰好叫 `proactive`，但那是**会话级重复提示词**，与"渠道里自发说话"完全是两回事。
⇒ 这是继 A3 §Gaps 的 G1–G4 之后的第五个从零构建项，记为 **G5**。

B2 已预留接缝：`plugins/platforms/onebot/README.md:201`
`"# 5 messages per group per 3 minutes, shared by replies and by any future proactive speaking."`

### ⚠️ 决定性发现：主动发言在生产中**从未发过一条**

生产配置只设了一个扁平键 `proactive_enabled = true`（其余全走默认：
5 个白名单群、`min_gap 45min`、`daily_max 4`、活跃时段 9–23、`proactive_timezone=""`）。

**但 `service.py:1223` 有一道总闸**：

```python
# The emergency mute silences ALL group speech, proactive included.
if not bool(_attr(params.config, "group_replies_enabled", True)):
    continue
```

而生产 `group_replies_enabled = false`。**⇒ 主动发言被紧急静音总闸完全掐住。**
佐证：`journalctl -u corlinman.service --since "7 days ago" | grep -ic proactive` → **0**。

这是本次迁移撞上的**第四个同类陷阱**（前三个：存储目录静默拆分、`persona.decay` 1260 次全失败、
`qunjlu` 被 `group_replies_enabled` 静默屏蔽）。共同特征都是"配置写着开，实际从未生效"。

**对迁移的直接影响：没有可复现的生产行为，只有可移植的机制。**
D25 的"按实际观测行为迁移"在此**不适用**——观测行为是"从不说话"。
验收因此不能对照生产，只能对照源码逻辑与移植过来的测试用例。

| # | 决策 | 理由 |
|---|---|---|
| D31 | B4 落地后**默认关闭**，且交付文档必须显著标注"启用 = 引入一项生产从未发生过的对外行为" | 一旦启用，机器人会开始自发向 **5 个真实 QQ 群**发言。这不是"恢复原有功能"，是**新增行为**。把它说成"迁移完成即恢复"是误导 |
| D32 | 活跃时段按**配置字面值** 9–23 + 显式 `Asia/Shanghai`，不做 D25 式的 -1 小时补偿 | D25 的补偿前提是"存在需要复现的实际触发时刻"。主动发言从未触发过，不存在观测基准，此时唯一诚实的依据就是配置的标称意图。差异（生产若曾生效会是 JST 9–23 = 北京 8–22）已在此显式记录 |
| D33 | 速率闸**必须与反应式回复共享**同一个滑动窗口（每群 3 分钟 5 条），不得各算各的 | 源实现即如此（`_QQ_GROUP_SPEECH` 同一实例），B2 的 README 也已如此承诺。两套独立计数会让实际发言量翻倍 |
| D34 | `proactive_groups` 超出 `group_whitelist` 的部分一律忽略 | 源实现的硬边界（`service.py:882` "@mentions 都不能绕过白名单——主动发言同样不能"）。白名单是对外发言的最后一道防线 |

### 移植时必须保留的行为细节（易漏，漏掉即行为漂移）

- `_QQ_GROUP_RECENT` 缓冲区**同时存入群消息与机器人自己发出的消息**，用途有二：
  给主动发言注入语境；以及判断"自上次发言以来没人说过话"⇒ 不再追发（防刷屏）
- 提示词里显式告知模型"记录里「你自己」开头的行是你之前发过的"，并要求不复述、不重复回答
- `_qq_proactive_is_skip()` —— 模型可以选择**不说话**（SKIP 逃生口），这是"像人"的关键
- 概率闸 `proactive_probability`（"一个人不会每次瞥手机都发言"）
- 健康闸：`online` / `account_online` / `identity_ready` 任一不满足即跳过
- 常驻循环 + 60 秒空闲重检 ⇒ 管理端保存配置可**热生效**，无需重启渠道
- 出站整形与回复路径**共用**（`_normalize_for_channel` / `_split_on_msg_break` / `chunk_reply`），
  否则 markdown 与 `[MSG_BREAK]` 会原样打进 QQ 纯文本
- RAG 语境查询**只取他人发言**，排除自己的话（否则就是自己回音自己）

**待裁定项（交由 B4 决策并说明理由）**：源实现的 RAG 走 gateway 的 `kb.sqlite` 语料库，
hermes 无此物。可选：接 hermes 原生记忆检索（C1 已交付 `plugins/grantley/memory_provider.py`）、
或省略并显式记为缺口。

---

## 15. D1 验收（2026-08-19）—— ✅ 通过

Orchestrator 独立复核，**未采信任何自述数字**，全部亲自执行：

| 验收项 | 核验方式 | 结果 |
|---|---|---|
| 测试 | `.venv/bin/python -m pytest tests/plugins/corlinman_jobs/ -q` | **248 passed** |
| 零侵入 | `git diff --diff-filter=MDRT --stat 9a3160031..HEAD` | **空**（无任何既有文件被改/删） |
| 变更类型 | `git diff --name-status \| awk '{print $1}' \| uniq -c` | **13 A** —— 13 个文件全部为新增 |
| 行数 | `wc -l` 逐文件 | 与自述**逐项吻合**，合计 5792 |
| 提交 | `git log` | 6 个提交在 `feat/corlinman-migration` |
| **插件可被发现** | `hermes plugins list`（真实 CLI，隔离 HERMES_HOME） | ✅ `corlinman_jobs \| not enabled \| 1.0.0 \| bundled` |

**上一轮的致命缺口已闭合**：`plugin.yaml`(73) + `__init__.py`(70) 到位，1916 行死代码变成可发现、可操作的插件。
B2 上次犯的同一个错误没有重演。

### 安全不变量 —— 直接从数据结构验证，不看测试自述

```
JobSpec 9 + Dropped 3 = 12                      ✅ 与源任务总数吻合
any install_enabled=True   -> NONE              ✅ 无任何任务被启用
timezones                  -> {'Asia/Shanghai'} ✅ 单一显式时区（D8）
schedules landing minute 0 -> NONE              ✅ 全部错峰避开整点（P1）
writes_public_feed         -> qzone_reply / qzone_friends / qzone_daily
  其 dry_run_agent_safe    -> [False,False,False] ✅ 三个公开发帖任务禁止 agent 干跑（D17）
```

### 逐项复核它自报的四处偏离

**1. 「12 个任务」实为 9 个安装 + 3 个弃迁** —— 接受，三条理由均经独立核实：

| 弃迁 | 理由 | Orchestrator 核实 |
|---|---|---|
| `system.update_check` | hermes 自带且缓存周期相同，再加一个轮询器是重复 | ✅ **属实**：`hermes_cli/banner.py:385 check_for_updates()`，`banner.py:135 _UPDATE_CHECK_CACHE_SECONDS = 6 * 3600` —— 与源任务的 6 小时完全一致。这是唯一一个在生产中健康(90/90)却被弃迁的任务 |
| `evolution.darwin_curate` | 消费端（evolution 引擎）本就未移植，且 79/79 全失败、从未扫过一个 skill | ✅ 合理：迁移它等于既要写新 curator 又要发明它的消费者 |
| `grantley.qzone_reply` | 决策 D9 | ✅ 本就是我先前的裁定 |

**2. `plugin.yaml` 不设 `config_schema`（偏离我"参照 grantley/qzone"的指令）** —— 接受。
理由是"设置块会成为 cron 表达式的第二个家"，与 `specs.py` 自身"单一事实源、任何地方都不重述 cron 表达式"
的设计原则一致。**偏离已主动申报并写进 commit message**，属有原则的取舍而非疏漏。

**3. `persona.decay` 的 stdout 改走 stderr** —— 接受。源实现向 stdout 打 JSON；而 hermes 中
`no_agent` 任务的 stdout **就是被投递的消息正文**，照搬会导致每天 24 次本地投递噪音。
代价是成功时载荷被丢弃，已附一行回退方式。

**4. `enabled_toolsets=()` 改写为 `["no_mcp"]`** —— ✅ **这是一个真陷阱，且它抓对了**。
我亲自查证 `cron/scheduler.py:444-446`：

```python
per_job = job.get("enabled_toolsets")
if per_job:                      # ← 空列表为假值
    return _merge_mcp_into_per_job_toolsets(...)
```

空列表会**静默跌落**到平台配置或 `None`（AIAgent 加载完整默认工具集）——
即"想要零工具"反而拿到**更多**工具。工具白名单属安全相关路径，此处失手后果不小。

| # | 决策 | 理由 |
|---|---|---|
| D35 | 接受 9 装 + 3 弃的方案，不要求补齐 12 个 | 三条弃迁理由均经独立核实成立，且**弃迁原因以 `DroppedJob` 数据结构记录在代码里**而非散落在文档中，可被测试断言、不会随文档腐化。强行凑满 12 个意味着为 `evolution.darwin_curate` 发明一个不存在的消费者 |
| D36 | 继承缺陷 `parse_week_match`（裸「单周」/「双周」永不匹配）**照实复现并用具名测试钉住**，不修 | 与 D18 修缺陷的处置看似不一致，差别在影响面：D18 那处让 10 个旅行地点成为死数据（可见的功能缺失），此处仅影响 `daily_agenda`（生产中 `enabled=false`，从未运行）的一个解析分支。忠实移植 + 具名钉住，比顺手改写更利于日后对账 |

### D1 主动申报的遗留风险（转入后续批次，不阻塞）

- **无 agent 干跑**：`dry_run_agent_safe` 已定义但无驱动——干跑能压住投递，压不住 `qzone_publish` 的真实发帖
- **无 uninstall 子命令**：回滚靠 `hermes cron rm` + `rm $HERMES_HOME/scripts/corlinman_*`，已写进文档 §5.4
- `qzone_friends` 的 `on_mission` 跳过未移植（hermes 无 agent 任务的前置门控）——人格一致性瑕疵，无安全影响
- `qzone_reply` 的 prompt 属 RECONSTRUCTED，从未对真实 QQ 会话跑过
- `analysis_digest` 空日路径now 多一次模型调用（源实现直接跳过模型）
- **外部阻塞**：宿主机无 LLM provider（B3）；`plugins/grantley` 须先部署到 `$HERMES_HOME/plugins/grantley/`
  再 `install --force`（安装器会烘焙它解析到的路径）；qzone 三个状态目录须先迁移

---

## 16. Provider 端点实测 —— 迁移文档的前提被推翻（2026-08-19）

用户直接提供了 provider 凭据。Orchestrator 从生产机做只读探测，结果与 A1/§0 记录的不符。

### 实测矩阵（三个 base × 三种协议）

| base | `GET /models` | `POST /chat/completions` | `POST :generateContent` |
|---|---|---|---|
| `/antigravity/v1beta` | 200（**Gemini 原生**，19 模型） | **404** | 400 INVALID_ARGUMENT |
| `/antigravity/v1` | 200（**Anthropic 形状**，28 模型） | **404** | 404 |
| `/antigravity` | 200（同上） | **404** | 404 |

⇒ **该网关不是 OpenAI 兼容的。** `/chat/completions` 在三个 base 上全部 404。

### 实测可用路径 —— 已取得真实 200 回复

```
POST https://api.cornna.xyz/antigravity/v1/messages
     x-api-key: <key>  +  anthropic-version: 2023-06-01
     {"model":"gemini-3.7-flash-tiered","max_tokens":20,
      "messages":[{"role":"user","content":"Reply with exactly: pong"}]}
→ 200  content:[{"type":"text","text":"pong"}]  usage:{input 348, output 14}
```

### ⚠️ 为什么 corlinman 配着一个 404 的 provider 却一直正常工作

```toml
[models]
backend = "grpc_agent"        # ← 关键
[agent]
endpoint = "127.0.0.1:50051"
```

corlinman 的推理**根本不走 `[providers.cornna]` 这个 HTTP provider**，而是委托给
`corlinman-agent.service` 的 gRPC 代理。所以那段 provider 配置**基本是摆设，`kind`/`base_url` 从未被真正验证过**
—— `kind = "openai_compatible"` 是一条从未被执行路径检验过的死配置。

**hermes 没有 `grpc_agent` 后端**，必须直连 HTTP。**照抄 corlinman 的 provider 配置会得到一个 404 的 provider。**

这是本次迁移撞上的**第五个同类陷阱**：前四个是"配置写着开、实际从未生效"，
这一个是"配置写着 A、实际走的是 B，而 A 从未被验证"。**共同教训：corlinman 的配置文件不能当作行为的证据。**

### 模型与别名实测

- **`gemini-3.7-flash-tiered` 可用**（实测 200），但**不在网关模型列表中**（列表最新只到 `gemini-3.6-flash-tiered`）
  ⇒ **列表不可作为校验依据**；不得因"列表里没有"而擅自降级到 3.6
- 两个别名目标均在列表中、有效：`claude-opus-4-6-thinking` ✅ / `claude-sonnet-4-6` ✅
- `image_model = "gemini-3.7-flash-tiered"` 需一并迁移
- `"gemini-3.7-flash-tiered" = "gemini-3.7-flash-tiered"` 为自指恒等映射，可忽略

| # | 决策 | 理由 |
|---|---|---|
| D37 | hermes 侧配 **Anthropic 兼容 provider**，base `https://api.cornna.xyz/antigravity`，**不照抄 corlinman 的 `openai_compatible`** | 实测唯一可用路径。hermes 原生支持：`hermes_cli/models.py:2540 _base_url_looks_like_anthropic_messages()` / `models.py:3297 api_mode="anthropic_messages"` |
| D38 | 采用 Anthropic 面而非 `/v1beta` 的 Gemini 原生面 | Anthropic 面是**超集**（28 模型含全部 claude-* 与 gemini-*，Gemini 面仅 19 且无 claude），且已实测通 |
| D39 | 凭据由用户在对话中直接提供，**只写入 `/opt/hermes/data/.env`（0600/hermes），不进仓库、不进 systemd `Environment=`、不进命令行参数** | `systemctl show` 的部分属性非 root 可读；`ps` 可见 argv；仓库会被推送 |

**安全提示（已告知用户）**：该 key 已出现在对话记录中，建议切换完成后轮换一次。

---

## 17. B4 审查（2026-08-19）—— 打回 1 项，其余通过

Orchestrator 独立复核，全部亲自执行：

| 验收项 | 结果 |
|---|---|
| `pytest tests/gateway/test_onebot_proactive.py -q` | **78 passed** |
| 7 文件 OneBot 回归 | **382 passed** |
| 文件行数 | 逐项吻合（`proactive.py` 797 / 测试 879 / 文档 297 / `adapter.py` 1983 / README 366） |
| 零上游改动 | merge-base `8911e2e0e` 下四路径全部 `absent upstream` |
| **D31 默认关闭** | ✅ `if not _as_bool(get("proactive_enabled", None), False)` |
| **D33 共享速率闸** | ✅ 从共享 `rate_limit` 导入 `_GROUP_SPEECH`/`group_speech_allowed`/`speech_key`，单一进程级滑动窗口 |
| **D32 显式时区** | ✅ `DEFAULT_TIMEZONE = "Asia/Shanghai"`，从不回落进程本地 |

**B4 对 C1 代码的判断经 AST 验证属实**：`GrantleyMemoryProvider.prefetch()` 方法体内 `query` **零引用**
（`Name nodes referencing 'query': False`）——它是状态注入器不是检索器。据此做出的 RAG 裁定（不把它当 RAG 后端，
改为保留"模型自行回忆"的拉取式召回 + 预留 `set_context_provider()` 接缝、默认不挂）建立在正确前提上，接受。

### 打回项：`proactive_groups` 全部落在白名单外时**回退到整个白名单**

源实现 `service.py:888-890`：

```python
groups = tuple(g for g in groups if g in group_whitelist)
if not groups and group_whitelist:
    groups = tuple(sorted(group_whitelist))      # ← 扩散到全部白名单
```

B4 判定为"忠实移植 + 构造上安全"，Orchestrator **不同意**，依据三条：

1. **源码与它自己的 docstring 相矛盾。** 同一函数 docstring 写着
   `Enabled with no resolvable target logs a warning and **stays off** (never spam-guess).`
   全部请求组落在白名单外 = no resolvable target。**docstring 说应当保持关闭，代码却扩散。
   这不是设计取舍，是源实现的缺陷**——移植它等于实现了它的 bug 而非它的意图。
2. **这是 fail-open，方向与运维意图相反。** 运维设 `proactive_groups=[X]`（X 未在白名单）本意是**收窄**到一个群，
   实得**5 个真实 QQ 群全开**。"never leaves the whitelist" 只保证不越界，未保证不扩散。
3. **不存在需保持一致的线上行为** —— 主动发言在生产中从未运行过（§14）。
   与 **D18 判例条件完全相同**，同一标准适用。

| # | 决策 | 理由 |
|---|---|---|
| D40 | 区分两种情况：`proactive_groups` **未设/为空** ⇒ 回退白名单（保留）；**非空但过滤后为空** ⇒ **返回 None 保持沉默 + WARNING**，不得回退 | 前者是 docstring 所述"speak in my whitelisted groups"的自然读法，安全且符合意图；后者是运维输入错误，静默扩散到 5 个真实群不可接受。此为对源实现缺陷的**有意修正**，依据 D18 判例，零回归风险 |

**打回次数：B4 第 1 次。** 按升级策略，同一任务打回 2 次仍不达标才换模型/换方案，当前不触发。
打回**不因质量差**——该项 B4 主动申报、已用测试钉住、已写进文档，属诚实交付；
分歧点在于"忠实移植"与"修正 fail-open 缺陷"孰先，由 Orchestrator 裁定后者。

### B4 申报的遗留项（已接受，转入后续批次）

- **`event.channel_prompt` 未接线** —— C1 的 `plugins/grantley/channel_binding.py` 要求 OneBot 适配器按入站消息设置它，B2 从未接。
  B4 **刻意不在主动发言这条腿上单方面接**（否则人格框架会在"回复"与"主动发言"之间不一致）——判断正确。
  **这是 B2/C1 的既有集成缺口，但在 B4 处才变得关键**（主动发言是纯人格输出，没有用户消息携带框架）。**需独立后续任务。**
- 媒体指令被剥离而非投递；预算状态进程内（重启重置日计数）；主动发言不占 `max_concurrency`（1.9 GB 机器上短暂 3 并发）；
  无 `ONEBOT_PROACTIVE_*` 环境变量形式（有意：开关只留一处，且环境变量无法热生效）

### 计划文档更正

§10 称本次迁移对上游"0 处既有文件修改"。全分支复核后更正：**`.gitignore` 有 +4 行**
（`c43ad1680`，C4 排除 migration-export 包），非代码文件。其余仍为纯新增。

### B4 返修验收（2026-08-19）—— ✅ 通过

Orchestrator **直接驱动 `resolve_config()` 验证三种情况**，不看测试自述：

```
case 1  proactive_groups 未设 / [] / ""      -> groups=[全部 5 个白名单群]     ✅ 保留，安全
case 2  ['183287894','999999999']            -> groups=['183287894']          ✅ 只留白名单内
case 3  ['999999999']                        -> None (SILENT)                 ✅ 不再扩散
        ['999999999','888888888']            -> None (SILENT)                 ✅
```

case 3 同时打出明确 WARNING：
`every requested proactive_groups entry ([...]) is outside group_whitelist — proactive speech stays OFF rather than falling back to the whole whitelist`

- `pytest tests/gateway/test_onebot_proactive.py -q` → **81 passed**（返修前 78）
- 7 文件 OneBot 回归 → **385 passed**（返修前 382）
- 修复提交 `1848c59ea`；B4 全系列 6 个提交

B4 全盘接受打回，未以放宽断言蒙混：原先钉住旧行为的测试被**反转**为断言 `None`，
并新增两个用例分别覆盖"三种空值写法仍回退白名单"与"部分有效时只留白名单内"，
证明修复没有把安全的 case 1 一并改坏。文档 §5 并列引用了源码与其自相矛盾的 docstring，
§7 记录这是唯一一处**反转而非移植**的源用例。

**B4 最终判定：✅ 通过**（打回 1 次，返修一次到位）。

---

## 18. B3 验收（2026-08-19）—— ✅ 通过，并**更正 Orchestrator 两处错误**

### ⚠️ 更正一：D37 / D38 是错的 —— 该网关**是** OpenAI 兼容的

我在 §16 只探测了 `/antigravity`、`/antigravity/v1`、`/antigravity/v1beta` 三个 base 下的 `/chat/completions`，
**从未测试根路径**，据此断定"该网关不是 OpenAI 兼容的"。B3 补测根路径后我复验：

```
GET  https://api.cornna.xyz/v1/models          -> 200
     models: ['claude-opus-4-6-thinking', 'claude-sonnet-4-6', 'gemini-3.7-flash-tiered']
POST https://api.cornna.xyz/v1/chat/completions -> 200  content='ORCH-OK'
```

根 `/v1` 返回的**恰好就是 corlinman 配置的那三个模型**（而 `/antigravity/v1` 的 28 个是超集）。

| # | 决策 | 理由 |
|---|---|---|
| **D37 作废** | ~~配 Anthropic 兼容 provider~~ → **改为 `chat_completions` @ `https://api.cornna.xyz/v1`** | 根路径实测通，且模型列表恰是生产在用的三个 |
| **D38 作废** | ~~采用 Anthropic 面~~ | 见 D41 |
| D41 | **不装 `anthropic` SDK、不用 Anthropic 面** | 生产 venv 零 extras 安装，`find_spec("anthropic") is None`（我已复验）。而 hermes 在 SDK 缺失时**不报错**：`agent/auxiliary_client.py` 的 `except ImportError` 只打一条 warning「falling back to OpenAI-wire」，然后把 base 改写成 `.../antigravity/v1/chat/completions` 发出去 → **404**。⇒ **不装 SDK 就配 anthropic 面 = 一个静默损坏的 provider**。我已逐字复核该分支代码，属实 |

**教训（对 Orchestrator 自己）**：我的探测矩阵按"文档给的 base 的子路径"展开，而没有按"这台网关可能挂载在哪些路径"展开。
**证伪一个协议前，必须先枚举挂载点。** 三个 404 只证明了那三个路径没有该协议，不能证明整台网关没有。

### ⚠️ 更正二：我给 B3 的验收标准第 1 条**在 hermes 里不可能达成**

我要求"`hermes status` 显示 provider `cornna` 为 configured"。B3 指出 `status.py` 的 `◆ API-Key Providers` 段
**硬编码六个内置厂商**（Z.AI/Kimi/StepFun/MiniMax×2/DeepInfra），**没有列出自定义 `providers:` 条目的代码**。
我逐行复核 `hermes_cli/status.py:406-421`，**属实**。该验收标准是我提的伪标准，责任在我，不在 B3。
实际能给的最强证据是 `Model: gemini-3.7-flash-tiered` + `Provider: cornna` 两行，加上真实调用。

### 已核验通过的交付

| 验收项 | 核验结果 |
|---|---|
| 真实模型调用（走 hermes 自身解析链） | ✅ 默认模型 + **两个别名各自 200**，均返回 `PONG-B3-HERMES` |
| `.env` 权限与内容 | ✅ `-rw------- hermes:hermes`，4 键，`TELEGRAM_BOT_TOKEN` **保留未动** |
| 备份 | ✅ `config.yaml.bak.b3.*` / `.env.bak.b3.*` / `SOUL.md.bak.b3.*` 均在；配置块用 `B3-BEGIN…B3-END` 包裹，删该段即完全回滚 |
| QQ 三重锁 | ✅ `platforms.onebot.enabled: false` + `group_replies_enabled: false` + `plugins.enabled` 不含它 |
| Telegram 锁 | ✅ `platforms.telegram.enabled: false` —— **必要**：`.env` 里有 token，内置平台见 token 即自动启用，不钉死则 `systemctl start` 就会开始轮询 |
| 时区 | ✅ `timezone: "Asia/Shanghai"` 显式落盘 |
| 线上零扰动（对照**我自己**的 12:03 基线） | ✅ 容器 9=9、端口 21=21、`/health` 200=200、`hermes.service` inactive；`/opt/corlinman/data/config.toml` mtime **未变**（2026-08-18 23:08:11）；B3 诊断用的 9299 临时监听已清 |
| venv | ✅ 149 M，**零新增包**；`anthropic` SDK 确认未安装 |

> running services 我的基线是 27、现为 26。**非 B3 所致**（其自取基线前后均为 26）。
> 归因于两个 napcat 残留单元约 30 次/分钟的重启抖动（见 §P2/D27），瞬时快照会 ±1。
> 全部实质指标（容器/端口/健康/corlinman 配置 mtime）逐项一致。

### ⛔ 阻塞级发现：网关拒绝 hermes 的默认身份句，**且每次失败都烧账号池**

B3 报告该现象。我做了**排除顺序混淆**的独立复验（每阶段前置连续对照探针）：

```
两次干净对照探针                      -> 200, 200      基线健康
system='You are 格兰特利…'（先发）    -> 200           对照探针 -> 200   池仍健康
两次干净对照探针                      -> 200, 200      基线healthy
system='You are Hermes Agent…'        -> 429           对照探针 -> 503   池被打坏
```

两个字符串**只差一个名字**。⇒ **B3 的结论成立，且比它报告的更严重**：
失败不只是"任务产不出内容"，而是**每次尝试都会把上游账号池打到冷却约 30 秒**。
9 个定时任务按表触发 ⇒ **反复损伤用户自己的中转站**，且会波及用户用同一把 key 的其他用途。

| # | 决策 | 理由 |
|---|---|---|
| D42 | **任何任务启用之前，必须先把 agent 身份改为「格兰特利」**（部署 C1 人格 / profile，或改写 `$HERMES_HOME/SOUL.md` 首句），并以一次完整 agent turn 验证 | 这不是业务意图变更——迁移的全部目的就是这个 agent **是**格兰特利而非 "Hermes Agent"，C1 已移植好人格。不改则 100% 任务失败**且持续损伤账号池** |
| D43 | 该项列为**切换窗口的第一步**，先于任何任务启用与灰度 | 它是所有下游验证的前置；在它之前做任何端到端验证都会得到假阴性，并平白烧池 |

B3 未擅自改 `SOUL.md`（测完已还原，现文件与原始逐字节一致）——**判断正确**，给智能体起名字属产品决策。

### 对 B3 一处结论的更正

B3 称用户提供的 key 与生产配置中的**逐字节相同**，因此"不是新 key，无需轮换"。
**前半句我接受**（指纹比对是服务器端做的，合理）；**后半句不成立**：
该 key 已出现在本次对话记录中，与它是否为同一把无关。**轮换建议维持**。

### B3 申报的遗留项（已接受，登记）

1. **`plugins/grantley/channel_binding.py::resolve_channel_prompt` 全仓库零调用方** —— 与 B4 独立发现的
   `event.channel_prompt` 未接线**是同一个缺口，两个子智能体各自撞到**。⇒ 已确证，**必须独立立项**
2. 生产机 `/opt/hermes/repo` 仍是上游主线，三个插件（onebot / grantley / qzone）**代码尚未部署**，配置目前完全惰性
3. 上游账号池容量本身紧张，小请求也会间歇 503 约 30 秒自愈 ⇒ 除错峰外可能还需退避/重试策略（B3 未配 fallback chain，属行为变更，留待裁定）
4. `toolsets: ["file","terminal"]` 在 CLI 作用域未生效，实际请求带 **27 个工具** —— B1 遗留，对内存与 token 均有影响，需独立处理
5. `providers.cornna: unknown config keys ignored: enabled` 警告 —— 无害噪音，可回馈上游
6. `image_model` 未迁（格兰零立绘资产、19 篇说说全纯文本、C3 的 `generate` 已退化为提示词字符串 ⇒ 无消费者）
7. `[admin]` / `[agent] endpoint` / `[models] backend=grpc_agent` 未迁 —— hermes 无对应概念
8. 诊断期间对用户中转站发了约 30 次请求，十几次把池打到冷却（每次约 30 秒自愈），无持久损害

---

## 19. D2 验收（2026-08-19）—— ✅ 通过（Sonnet，零打回）

Orchestrator 独立复核，全部亲自执行：

| 验收项 | 结果 |
|---|---|
| `pytest tests/plugins/corlinman_jobs/ -q` | **291 passed**（D1 时为 248） |
| 广回归 `corlinman_jobs + qzone + grantley + cron` | **1452 passed, 1 skipped** |
| 零上游改动 | 全分支 `--diff-filter=MDRT` 仅 `.gitignore` +4（C4 遗留，非 D2） |
| 无任务被启用 | ✅ `ALL_SPECS` 12 项 `install_enabled` 全 False |
| 无任务落在整点 | ✅ NONE |
| **时区补偿（D25）** | ✅ `qunjlu` 08:05 / `sanhu` 09:05 / `jlu` 10:05 —— 各较配置字面值 -1 小时 |
| **调度冲突** | ✅ 逐对检查 hour+minute，**零真实冲突**；均避开 `persona.decay` 的每小时 `:17` |

### qunjlu 抑制机制（D26）—— 机制正确，且比"照抄开关名"更稳健

D2 用**两道结构性抑制**而非读运行时配置：`deliver="local"`（cron 投递解析器拿不到目标）
+ `enabled_toolsets=()`（无可发送工具）。

**关键复核**：`()` 正是 D1 发现的那个假值陷阱（空列表在 `cron/scheduler.py:444` 为假 ⇒ 静默拿到**完整**默认工具集）。
我驱动 `installer._spec_job_fields()` 逐 spec 验证实际落盘值：

```
qunjlu / sanhu / jlu    spec=()  ->  payload=['no_mcp']     ✅ 三个 monitor 均正确翻译
```

陷阱在 monitor 上也被正确处理。

### ⚠️ D2 发现的真实行为差异（我已独立复核，属实且比它说的更要紧）

**本移植的 `adapter.send()` 根本不检查 `group_replies_enabled`。**
我用 AST 验证 `plugins/platforms/onebot/adapter.py` 的 `send()`（L1258-1343）：
`references group_replies_enabled inside send(): **False**`。
该标志只在三处被消费：L552 解析、L609 传给 router（**入站回复门控**）、L670 打日志；
另由 `proactive.py` 消费（B4）。

⇒ **与 corlinman 的语义不同**：corlinman 那道闸是"紧急静音**所有**群发言"，
连 monitor 摘要都一并掐死（`qunjlu` 从未送达正因如此）；
**本移植中，一个投递到 `onebot:group:<id>` 的 cron 任务会照发不误。**

D2 因此**拒绝照抄开关名**做抑制——判断正确：照抄会造出一个方向相反的
"配置说关、行为却开"陷阱，而这正是本次迁移已踩过五次的那一类。

| # | 决策 | 理由 |
|---|---|---|
| D44 | **切换前必须让 `adapter.send()` 也遵守 `group_replies_enabled`**，列为切换窗口的**阻塞项** | 该标志被文档与运维直觉当作"群发言总闸"。事故中运维按下它、以为一切群输出停止，而 cron 投递仍在发——这是安全缺口。且 D17 的双发风险本就高，QQ 侧的整个安全论证依赖"静音可信" |
| D45 | D2 的结构性抑制**保留**，不因 D44 落地而改回读标志 | 结构性抑制（无目标 + 无工具）不依赖任何运行时配置读取正确，是更强的保证。两者叠加，不是二选一 |

### D2 申报的两个重大缺口（均经我复核属实）

**1. 本移植没有 `qq_group_history.sqlite` 的写入方。**
全仓库 grep 确认：只有 `preflight.py` 的读取与校验，**零 INSERT**。
只有 corlinman 自己的 dispatch 循环在写；本移植的 OneBot 适配器只保留 30 条内存缓冲。
⇒ **共存期**把 `QQ_GROUP_HISTORY_DB` 指向 corlinman 的活文件可正常工作；
**corlinman 一旦退役，三个 monitor 会在该库约 3 天保留期内全部静默**，除非另建采集管线。
**这是 D2 范围外的真实功能缺口，必须在退役 corlinman 前解决。**

**2. 源实现的并行 map-reduce 摘要未复现。**
源实现对 >1000 条的窗口分段摘要再合并；本移植只做一次模型调用，超出部分**保留最新 1000 条**并标注截断。
D2 用真实数据确认：**`sanhu` 的群 `980927602` 日均约 15000 条**，即几乎每次真实运行都会触发截断
⇒ **摘要实际只覆盖当天最新约 7% 的消息**。这是显著的保真度损失，**需用户裁定**是否补做分段摘要。

### 遗留观察

`hermes.daily_agenda` 的 `enabled_toolsets` 为 `None`（⇒ hermes 完整默认工具集），
与 `specs.py` 自述"every agent job here names its toolsets"不符。
影响有限（该任务在生产即为 `enabled=false`），登记待查。

---

## 20. D27 执行完毕（2026-08-19 13:36 JST，用户放行）—— ✅ 成功，线上零扰动

停用并屏蔽两个被容器取代的原生 NapCat 残留单元。

### 执行

```
systemctl disable --now corlinman-napcat-manager.service   # 移除 multi-user.target.wants 符号链接
systemctl disable --now corlinman-napcat.service
# mask 首次失败：单元文件实体位于 /etc/systemd/system/，与 mask 的符号链接同路径冲突
mv  /etc/systemd/system/corlinman-napcat{,-manager}.service  /root/napcat-units-disabled-20260819T043624Z/
systemctl mask corlinman-napcat.service corlinman-napcat-manager.service
systemctl daemon-reload
```

**为什么必须补 mask（而非止于 disable）**：`corlinman-napcat.service` 带 `PartOf=corlinman.service`。
`disable` 只移除开机自启的 `WantedBy` 符号链接，**不阻止依赖传播与手动启动**——
将来任何一次 `systemctl restart corlinman.service` 都会把崩溃循环拉回来，
而切换窗口几乎必然要重启它。原文件已备份，`.orig` 副本一并保留。

### 结果核验（对照变更前快照）

| 指标 | 变更前 | 变更后 |
|---|---|---|
| `corlinman-napcat` **容器** | Up 4 weeks，3001/6099 | **Up 4 weeks，3001/6099**（未受影响） |
| ws `127.0.0.1:3001` | — | **可连** |
| webui `127.0.0.1:6099` | — | **301（存活）** |
| corlinman `/health` | 200 | **200** |
| `corlinman.service` / `corlinman-agent` | active | **active / active** |
| 运行中容器 | 9 | **9** |
| 监听 socket | 21 | **21** |
| 两单元状态 | `activating`，NRestarts 318441 / 620342 | **`masked` / `inactive`** |
| 可用内存 | 390–407 MB | **407 MB** |

### journald 噪音消除 —— 实测远超预期

| 窗口 | napcat 相关行 | 全系统总行 | 占比 |
|---|---|---|---|
| 变更前 10 分钟（13:26–13:36） | **2658** | 3350 | **79%** |
| 变更后（13:37 起） | **0** | **1** | — |

约 335 行/分钟 → 近乎静默。§5 估计的 61% 偏保守，实际为 **79%**。
叠加 P3 已放宽的 `SystemMaxUse=300M`，journald 保留窗口将从约 19 小时大幅延长
——**hermes 上线后终于具备可用的排障窗口**（原为 7.5 小时）。

### 回滚

```
systemctl unmask corlinman-napcat.service corlinman-napcat-manager.service
cp /root/napcat-units-disabled-20260819T043624Z/corlinman-napcat.service          /etc/systemd/system/
cp /root/napcat-units-disabled-20260819T043624Z/corlinman-napcat-manager.service  /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now corlinman-napcat.service corlinman-napcat-manager.service
```
回滚即恢复到原先的崩溃循环态（这正是变更前的状态，无其他副作用）。

**P2 / D14 就此关闭**，且**未触碰线上 QQ 桥接**——它自始至终是那个 Docker 容器。
