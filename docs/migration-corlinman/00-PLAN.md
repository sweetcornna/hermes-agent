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

---

## 21. D3：QQ 群消息归档写入方（2026-08-19，用户裁定"补"）

补上 §19 暴露的功能缺口：本移植零 INSERT，corlinman 退役后三个 monitor 会在约 3 天内静默。

| # | 决策 | 理由 |
|---|---|---|
| D46-① | **沿用 corlinman 表结构，不另起 schema** | D2 的读取侧已按该格式写好。共存期读 corlinman 库、退役后读我们的，**读路径零改动**，切换只是换文件。新 schema 意味着读路径要养两套解析 |
| D46-② | **写独立文件，绝不与 corlinman 同写一个** | SQLite 3.40.1 在目标机退到 DELETE 模式；两进程（其一非 hermes）同写 = 锁争用 + 损坏风险。D2 已建好 `QQ_GROUP_HISTORY_DB`，切换只改这一个变量 |
| D46-③ | **批量提交，独立队列，不阻塞事件循环** | DELETE 模式每次 commit 都 fsync；逐条提交在 2 vCPU 机器上不可接受 |
| D46-④ | **写失败 fail-open** | 归档失败不该导致机器人不回消息。异常永不抛回适配器路径 |
| D46-⑤ | **保留 7 天，只 DELETE 不 VACUUM** | monitor 窗口 24h，corlinman 原约 3 天。按最大群日均 1.5 万条估，7 天约 20 MB，对 7.6 G 无压力；VACUUM 整文件重写不值得 |
| D46-⑥ | **一次性幂等回填** | 否则切换当天三个 monitor 看到空窗。按消息 id 去重，可重复执行 |
| D46-⑦ | **只采白名单群，不采私聊** | 存储范围不超过 corlinman 存过的内容 |

**队列必须有上界**（1.9 GB 机器，`MemoryHigh=384M`），不得因写入变慢而无限堆积。

### 对 `sanhu` 摘要覆盖率的建议（尚未实施，待用户确认）

D2 已确认：群 `980927602` **日均约 15000 条**，而本移植的单次模型调用只保留最新 **1000** 条
⇒ 摘要实际覆盖当天最新约 **7%**。

源实现用并行 map-reduce（分段摘要再合并）。**Orchestrator 不建议照搬**，理由是本环境的两条新约束：

1. **上游账号池极紧**，且 §18 已证实失败请求会烧账号；map-reduce 把每次运行的模型调用数从 1 变成 N
2. 目标机 2 vCPU / 1.9 GB，并行摘要的内存与并发都不友好

**建议改为「确定性预归约 + 单次摘要」**：先用纯代码把 15000 条压到可摘要规模
（按时间/话题分桶、丢弃表情与单字灌水、每人发言数封顶），再做**一次**模型调用。
覆盖**全天**而非最新 7%，且模型调用数仍为 1、不增加账号池压力。

| # | 决策 | 状态 |
|---|---|---|
| D47 | `sanhu` 覆盖率问题用**确定性预归约 + 单次摘要**解决，不移植 map-reduce | **待用户确认后实施** |

---

## 22. C5 / C6 验收（2026-08-19）—— ✅ 通过（C6 打回 1 次，返修一次到位）

### C5 调研验收：本项目至今溯源最扎实的一份

Orchestrator **未采信转述**，自行下载官方日文角色卡
（`kaiju09.com/knights_college/wp-content/uploads/2021/01/Chara_Gran_Japanese.png`）读图核对：

```
本名：グラントリー・ベル   身長：186cm   体重：91kg   誕生日：10月20日
好き：運動、剣技、乗馬、飯      嫌い：勉強、風呂、じっとしていること
「お前、寮生活って初めてか？じゃあオレがいろいろ教えてやるよ！」
```

C5 标为「官方」且我能查的结论**逐条命中**：数据表、第一人称 **オレ**、对亚戈用 **お前**、句式骨架，
以及官方描述里**确实没有**「海王化」那句。Steam API 独立确认游戏名/开发方/`supported_languages` 无音频标记。
C5 主动把 NamuWiki 一条与官设冲突的说法标为**不采信**并写明理由——这正是所要求的溯源纪律。

### 三项裁决

| 项 | 裁决 | 依据 |
|---|---|---|
| 「隐形学霸的装傻协议」 | **采纳修改** | 官方 `嫌い：勉強` + KC2 官方性格「裏表のない実直」（表里如一）。保留原样等于让机器人每次装傻都做一次错误自述 |
| 「本大爷」 | **采纳降权，不采纳删除** | C5 主张删（官方只见オレ）。Orchestrator 改判：证据是"未观察到"而非"证明没有"，**官方语料仅 4 条，不足以支撑删除既有语音特征**。降权已消除"常规自称"这一不实断言，与证据强度相称 |
| 「让亚戈海王化的罪魁祸首」 | **禁止写入** | 萌娘编者在 `{{黑幕}}` 剧透模板里的玩笑，非官设 |

### C6 打回三项 → 返修后全部订正（我逐项复核）

| 打回项 | 复核结果 |
|---|---|
| ①「弗罗汀骑士学院」错误合成 | ✅ 全文 **0** 处。改为「骑士学院学员，来自弗罗汀」——国名与校名分开。**该错误源头在 C5 的建议文本、且经 Orchestrator 批准，责任有我一份** |
| ② 官方档案块混入中置信度内容 | ✅ E.P. 具体机制（Fandom 单源/中）改为「我的能力挺适合搞恶作剧的，具体我懒得细说」——只保留官方 KC2 宣传语侧证支持的部分；奥斯卡关系改为对应官方「反りが合わない」的措辞；白制服只留颜色 |
| ③ 四处残留 | ✅ 全文扫描：`装傻` **0**、`骑士底线` **0**、`学霸` **0**、`道德锚` **0** |

> 值得记一笔：②的改法「具体我懒得细说」既**合乎人格口吻**、又**在认识论上诚实**
> ——人格以角色自己的方式回避了它并不确知的设定，而不是编一个。这是本次最优雅的一处处理。

| 项 | 结果 |
|---|---|
| `pytest tests/plugins/grantley/ -q` | **89 passed**（两轮均是） |
| 「本大爷」 | 2 处，**均为裁定的降权措辞**（第 12 行、第 115 行） |
| 第二轮改动范围 | 仅 3 个允许的文件 |
| 全分支上游改动 | 仍只有 `.gitignore` +4 行（C4 遗留，非代码） |

**测试哈希两轮各重钉一次**，注释如实记录"原为 corlinman 副本的哈希 / 现为第 N 轮有意修订后的基线"，
docstring 由 `must not drift. Ever.` 改为 `must not drift from the pinned baseline … still catches any *further*, undocumented drift`
——**断言力度不变，属合法重钉而非放宽**（已逐行读 diff 确认）。

### D42 所需的 SOUL.md 身份文本（已定稿，待切换窗口落地）

```
我是格兰特利·贝尔，弗罗汀人，骑士学院的学生，虎兽人，186公分。运动、剑术、骑马、吃饭样样来者不拒，
就是读不进书、也坐不住。脾气直，看不惯的事说不干就不干；但认定的朋友，我掏心掏肺地护着。
```

中文三句、纯第一人称、无 `Hermes Agent` / `Nous Research`、不破第四面墙、数据与官方卡面一致。
**C6 只产出文本未写入任何文件**——落地 `$HERMES_HOME/SOUL.md` 属部署动作，留在切换窗口第一步（D43）。

### 留痕：本次改动使人格偏离 corlinman 生产态

被改的三处均与官方三语角色卡**直接矛盾**。生产那份人格跑出过 19 篇真实 QQ 空间说说，语料量远大于官方的 4 条，
但本次**未采信"生产实测更丰富"这一论据**，而是优先服从「不能让机器人对自己说一句与官设相反的话」。
若日后需要找回生产态的说话质感，应另起一轮工作，**不得在本次改动里悄悄回退**。

### 排期说明：D44 与人格绑定接线暂缓派发

两者与进行中的 D3 **同在 `plugins/platforms/onebot/adapter.py`**
（D3 挂载入站消息路径、D44 改出站 `send()`、绑定接线改入站事件构造）。
并行会撞车，故待 D3 落地后再派。

### D3 验收（2026-08-19）—— ✅ 通过

Orchestrator 独立复核，关键项**亲自跨模块实跑**而非读测试：

| 验收项 | 结果 |
|---|---|
| `pytest tests/gateway/test_onebot_group_history.py -q` | **81 passed** |
| 广回归（corlinman_jobs + qzone + grantley + cron） | **1452 passed, 1 skipped** —— 与 D2 基线**逐字一致，零回归** |
| 零上游改动 | 全分支 `--diff-filter=MDRT` 仅 `.gitignore` +4（C4 遗留） |
| 默认关闭 | ✅ `group_history_enabled` 默认 `False` |
| corlinman 文件只读 | ✅ `sqlite3.connect(f"file:{source}?mode=ro", uri=True)` |
| 队列有界 | ✅ 且 `batch_rows` 被钳到 `queue_max` —— 堵住了"未提交列表只受 batch_rows 约束"这个反向的无界内存洞 |

**Schema 兼容性（D46-① 的核心目标）—— 我用真实导出快照对拍**：

```
D3 建库对象     : group_messages / monitor_state / idx_group_messages_window / sqlite_autoindex_monitor_state_1
corlinman 导出   : 完全相同
列序             : id, instance_id, group_id, sender_user_id, sender_name, message_id, event_time_ms, received_at_ms, text（两侧一致）
DDL IDENTICAL   : True
```

**决定性验证 —— D3 写、D2 未改一行的读取逻辑读**（按路径加载 `corlinman_jobs_lib.py`，与 cron 脚本同一条路）：

```
sanhu/jlu 式（sender_ids=()）        → 3 行 ✅
qunjlu 式（sender_ids=("1076712858",)）→ 2 行 ✅   ← qunjlu 的整个机制
_qq_monitor_format_lines(..., Asia/Shanghai):
   ★[08-19 13:29] u1076712858(1076712858): 明天几点集合     ← ★ 重点标记生效
    [08-19 13:29] u999(999): 随便水一句
   ★[08-19 13:29] u1076712858(1076712858): 带上装备
```

⇒ **D46-① 的设计目标达成：读路径零改动，切换只是换文件。**

### D3 的取舍与自报缺陷（均接受）

- **批量阈值 N=200 行 / T=30 s**：依据 D2 实测流量（两群合计约 16.5k 行/日 ≈ 0.2 行/秒），
  T=30s 每批约 10 行、提交降到 ≤2880 次/日（fsync 少一个数量级）；T=5s 每窗口仅 1.7 行、无收益。
  代价是**非正常终止**时最多丢 30 秒（`disconnect()` 会 flush）。
- **队列 2000 行**（最忙群约 100 分钟），溢出丢**最新**一条并计数、每 1000 条记一次日志（journald 稀缺）。
  丢最新可保持积压连续。实测最坏 9.50 MB / 典型 0.70 MB。
- **不阻塞事件循环**：`record()` = frozenset 判定 + `put_nowait`；连接由守护线程内部创建并独占。
  实测 500 个入站事件、提交端故意卡 0.5 s：**总耗时 8.6 ms，单事件最大 0.051 ms，事件循环最大延迟 0.43 ms**。
- **回填幂等键 `(instance_id, group_id, message_id)`**，**刻意不含 `received_at_ms`**
  ——共存期同一条消息会带两个不同的接收时戳，含它就会重复插入。
  对真实 52649 行快照实跑：第 1 次 `inserted 52649`，第 2/3 次 `inserted 0 / duplicates 52649`，源文件 sha256 未变。

| 缺陷 | 处置 |
|---|---|
| SIGKILL/OOM 最多丢 30 秒（`OOMScoreAdjust=500` 使 OOM 非假设） | 接受：聊天归档非事务性数据，30 秒窗口换取 fsync 降一个数量级 |
| WAL 守卫是启发式非证明 | 接受并登记 |
| `synchronous=FULL` 在 DELETE 模式下"持久但非无懈可击" | 接受 |
| 无陈旧度检查（D2 §7.2 的缺口未变） | **转入后续**：归属 `corlinman_jobs/preflight.py`，D3 未越界去改，判断正确 |
| 无 `hermes` CLI 子命令（会动 `hermes_cli/`） | 接受：维持零上游改动 |
| **全部验证在 macOS / SQLite 3.53.1 上完成，从未在目标机跑过** | ⚠️ **列为切换步骤**：目标机是 3.40.1 且被迫 DELETE 模式，fsync 与锁行为是推导而非实测 |

| # | 决策 | 理由 |
|---|---|---|
| D48 | 切换窗口**必须在生产机上做一次归档写入冒烟测试**，不得直接依赖本地验证结论 | D3 自陈全部测试在 SQLite 3.53.1（WAL 可用）上完成，而生产是 3.40.1 强制 DELETE 模式——**两者的提交与锁行为正是本设计最吃紧的地方**。诚实自陈值得嘉许，但不能替代实测 |
| D49 | 切换顺序**先回填、再改 `QQ_GROUP_HISTORY_DB` 指向**，不得颠倒 | 顺序反了会让 monitor 读到未回填的空库，而 `send_when_empty=false` 使这种失败**静默无声** |

---

## 23. E0 验收（2026-08-19）—— ✅ 通过（零打回）

| 验收项 | 结果 |
|---|---|
| `test_onebot_group_mute.py` + `test_onebot_persona_binding.py` | **51 passed**（31 + 20） |
| 广回归 | **1452 passed, 1 skipped** —— 与基线**逐字一致** |
| 零上游改动 | 全分支仅 `.gitignore` +4（C4 遗留） |

**五个出口全部门控**（AST 逐函数验证）：

```
send             L1489  gated=True
_send_attachment L1636  gated=True
send_image       L1747  gated=True
_send_segments   L1405  gated=True   ← 纵深防御的收口点
_standalone_send L2093  gated=True   ← 跨进程 cron 投递路径，D44 真正要堵的地方
```

`_standalone_send` 是关键：只堵 `send()` 会把 D44 的洞原样留在隔壁进程里。

### E0 一处非显然的正确判断（我已复核属实）

被静音时返回的 `error_kind` **刻意不用 `forbidden` / `not_found`**。我查证：

```python
gateway/dead_targets.py:40   _DEAD_ERROR_KINDS = frozenset({"forbidden", "not_found"})
                             'unknown' in _DEAD_ERROR_KINDS -> False
```

用那两个值会让该群被标记为**永久失效**，其效力**活得比静音本身还久**——解除静音后群依然发不出去。
E0 还刻意让错误文案避开 `classify_send_error` 的全部子串（投递层会从异常文本二次分类），并用测试钉住。
调用方以 `adapter.is_muted_send_result(result)` 区分「被静音」与「发送失败」，**不是静默假成功，也不抛异常**。

### 三通道叠加矩阵（row 3 是重点）

| router_flag | live_flag | 入站门控 | 主动发言 | 出站 send | 群实际收到 |
|:--:|:--:|:--:|:--:|:--:|:--:|
| F | F | drop | muted | refuse | 无 |
| F | T | drop | muted | refuse | 无 |
| **T** | **F** | **放行**（router 标志陈旧） | muted | **refuse** | **无** |
| T | T | 放行 | 不静音 | send | 正常 |

D45 的 `qunjlu` 结构性抑制**未被改动**，并有测试钉住。

### 任务 B：按实际签名接线，非按 docstring

`bindings_from_config(raw)` + `resolve_channel_prompt(binding, *, on, data_dir)`，
并用 `inspect.signature` 把参数名、`on` 的 keyword-only 类型、dataclass 字段集写进测试钉住。
两条通道一致性靠**结构保证**（同一个 resolver、每条通道一处调用）+ 两个一致性用例。

### E0 自报缺陷（接受，其中两条进切换手册）

| # | 缺陷 | 处置 |
|---|---|---|
| 1 | 入站门控仍读**构造期**的 router 标志：热静音时该轮仍会跑，被拒在门口 ⇒ **每次 @提及烧一次上游模型调用**，而 §18 已证账号池很紧 | 登记；属 B2 范围，未做 |
| 2 | **`sanhu`/`jlu` 投递到私聊 `onebot:2104743984`，D44 不门控它们** | ⚠️ **进切换手册**：`group_replies_enabled` **不是这两个任务的急停开关**，它们只由 `install_enabled=False` 拦着 |
| 3 | `plugins.entries.grantley.settings.channels` 每进程只读一次（需重启）；`extra["persona_channels"]` 路径是热的 | 登记 |
| 4 | `_deliver_forward()` 与文件上传分支无自身门控 | 今日安全（仅经已门控入口可达），登记 |
| 5 | **从未经真实模型轮次端到端跑过** | ⚠️ **进切换手册**：需先解决 D42 身份问题，否则只是烧池 |
| 6 | `error_kind="unknown"` 是次优解，正解需改上游 | 接受：维持零上游改动 |

---

## 24. D47 / E1 验收 + 切换阶段 1–3 执行（2026-08-19）

### E1 私聊门控 —— ✅ 通过

切换执行到阶段 3 时发现的阻塞：`router.py::dispatch()` 对 `MessageType.PRIVATE` **无任何门控**
（docstring 明写 `private chat is unaffected`），启用 onebot 平台 = hermes 开始回 QQ 私聊，
而 corlinman 仍在回同一账号的私聊 ⇒ **对真人双回复**。这是 D17 的私聊变体，**切换手册 §0 原本漏了**。

Orchestrator 直接驱动 `dispatch()` 验五种组合，全部正确，且**不设新键时与今天逐字节一致**。

### D47 摘要预归约 —— ✅ 通过

| 项 | 结果 |
|---|---|
| `tests/plugins/corlinman_jobs` | **308 passed**（D2 时 291） |
| 广回归 | **1469 passed, 1 skipped**（基线 1452，+17 净增） |
| 零上游改动 | 全分支仅 `.gitignore` +4 |
| **模型调用仍为 1** | 脚本内模型引用数 **0** —— 归约是纯 Python |

**Orchestrator 在真实 18334 行数据上独立复验**（未采信其自述）：

```
total=18334  →  kept=2143   (budget=1500, buckets=17)
3 runs identical: True   8f6d3934918be1f5
```

**focus 保证用最严苛的方式验证**：故意挑**发言最多**的那个人当 focus
⇒ `focus_kept=2143`，其消息**全部保留并直接冲破预算**。
这正是 D47 申报的设计第 4 条（focus 保证优先于预算），**一条不丢**属实。

其自报的关键取舍：`QQ_MONITOR_FETCH_CAP` 由源实现的 10000 提到 **40000**
——因为真实一天就有 18334 行，10000 的旧上限会在任何策略生效**之前**先丢掉最旧的约 45%。
**主动申报的有依据偏离，接受。**

### 切换阶段 1–3（详见 `E-CUTOVER-RUNBOOK.md` 执行记录）

| 阶段 | 状态 | 关键结果 |
|---|---|---|
| 1 身份切换 | ✅ | `hermes -z` → `PONG-CUTOVER`（此前 503）。**D42/D43 解除** |
| 2 插件部署 | ✅ | onebot/grantley/qzone/corlinman_jobs 四者均 `bundled / not enabled` |
| 3 归档冒烟 | 🟢 观察期中 | **D48 由推导升为实测** |

**阶段 3 实测（D48）**：生产 `sqlite 3.40.1` / `journal_mode: delete`，
归档库真实增长（4 行 → **37 行**／约 25 分钟），出站发送 **0**，hermes 内存 150 MB（限额 384M），
corlinman `/health` 全程 200、其 NapCat WS 连接完好、容器 9 不变。

**NapCat 共存实测**：它是 forward WS server，三份配置 `token` **全为空**
（⇒ corlinman 的 `napcat_access_token` 是装饰性的，本次迁移第六个"配置写着 X、实际是 Y"）；
hermes 与 corlinman 各持一条 WS 连接并存，未互相挤掉。

| # | 决策 | 理由 |
|---|---|---|
| D52 | 接受 `QQ_MONITOR_FETCH_CAP` 10000 → 40000 | 旧值低于真实单日量，会在策略生效前静默丢弃约 45% 数据——那才是与源实现"行为一致"的假象 |

### Orchestrator 自身的一处疏漏

提交 `9356a310f` 使用 `git add -A docs/`，把 D47 当时未跟踪的
`D47-digest-prereduction-notes.md` 卷进了一个与之无关的提交。
内容无损（D47 的 `b3f02f166` 携带最终版），但该文档历史被拆到两个提交。
**教训：并发子智能体在场时，提交必须按显式路径 add，不得用 `-A`。**

---

## 25. 阶段 4 前置核查：D29 经受住了检验（2026-08-19）

上线前把 D29「重复投递物理上不可能」当作**待证命题**重新查了一遍，而不是引用自己的记录。

### 查出来的事实

corlinman 的 `[channels.telegram]` **是启用的**，且我移植的 `deliver` 串就抄自它的任务定义。
它的活调度器（`/opt/corlinman/execution-state/scheduler.sqlite`，不是 `data/` 下那个停在 7-27 的旧库）
里有 **78 条**投向本群的记录：

```
chat:-1003990634877:topic:680   40 次   最近 08-18 16:00
chat:-1003990634877:topic:11    20 次   最近 08-18 00:30
chat:-1003990634877:topic:13    18 次   最近 08-18 10:02
```

看到这里像是 D29 被推翻。但这些 effect **全部 `state='unknown'`、`receipt_json` 为 `NULL`**
—— 而同表的 qzone effect 是 `state='sent'`。**effect 表记的是"打算投"，不是"投到了"。**

### 决定性检验

用 corlinman 自己的 token 在生产机上做只读探测（不发任何消息，token 不出机器）：

```
corlinman bot: @Cornna_bot  id=5420007505
getChat(-1003990634877)      -> 400 Bad Request: chat not found
getChatMember(自己)           -> 400 Bad Request: chat not found
```

**旧 bot 不在该群，够不到。** hermes 的 bot（`8720715962`）此前已用 `sendChatAction`
验证可达 topic 11/12/13/680。

⇒ **D29 的结论与理由都成立**，现在是实测而非断言。**阶段 4 可以安全执行。**

### 顺带查出的第七个"配置写着 X、实际是 Y"

corlinman 的四个 Telegram 任务长期 `non_zero_exit / builtin_not_ok`，
配上"bot 不在群里"，**那 78 次投递没有一次真的送达过**。
所以 hermes 上线 Telegram 不是"接管"，是**把一个从未真正工作过的功能第一次跑通**。

（同一份运行记录还显示 `persona.decay` 每小时 `non_zero_exit`，558 次——与既有结论一致；
唯一稳定成功的是 `system.update_check`，而它属于 `DROPPED_JOBS`。）

### 我在这一段里的两处错判

1. 看到 78 条 effect 就写下"D29 是错的"——**把投递意图当成了投递事实**。
   正确做法是先分辨 `state` 语义，再下结论。
2. 先前说"手册漏了第五个 Telegram 任务 `daily_agenda`"——**错的**。
   `daily_agenda` 在源系统就是 `source_enabled=False`，D2 的 spec 记录准确，手册写"四个"正确。
   真正的偏差只在 topic 枚举：四个启用任务落在 **13 / 680 / 680 / 11**，
   topic 12 属于那个禁用任务，且 **680 承载两个任务**。

| # | 决策 | 理由 |
|---|---|---|
| D53 | 维持 D29，阶段 4 照原计划执行，无需先停 corlinman 的 Telegram 任务 | 旧 bot 不在群内，实测 `chat not found`；停它反而是对生产系统的无谓改动 |
| D54 | 手册 4.1 的 topic 表述改为按任务列出，不再写成 11/12/13/680 的并列 | 原写法暗示一一映射，实际 680 有两个任务、12 对应禁用任务，上线时会误判"少投了一个" |

---

## 26. E2 —— cron UTF-8 解码失败：根因、修复与验收（2026-08-19）

### 验收：✅ 通过（七条标准逐条自查，全部由 Orchestrator 独立复现）

| 标准 | 复现结果 |
|---|---|
| 回归测试改前必失败 | 还原 `registry.py` 到 HEAD → **2 failed, 2 passed** |
| 改后必通过 | **4 passed** |
| `tests/plugins/corlinman_jobs` | **312 passed**（此前 308，+4 为新测试） |
| 未打坏上游 | `test_registry.py` + `test_toolsets.py` + `test_api_server_toolset.py` → **69 passed** |
| 生产任务状态 | 四个全 `paused`；`analysis_digest` `last_status=ok`、`failure_streak=0` |
| 执行记录 | `completed / error=None` @15:17:44 |
| corlinman 未受影响 | PID 2581308 未变、`/health` 200 |

### 根因

```
tools/registry.py:98  source = module_path.read_text(encoding="utf-8")
  ← 命中 /opt/hermes/repo/tools/._onebot_client.py（163 字节，macOS AppleDouble 伴生文件）
```

`position 45` 是 AppleDouble 头部第二个条目**偏移字段的低位字节** `0xA3` = 163
= 该伴生文件自身的长度。**与任何中文无关** —— 我最初怀疑全角括号（GBK `0xA3 0xA8`）
是错的，字节偏移当时就对不上，那本该让我停下而不是继续猜。

这条根因还一次性解释了两个此前对不上的现象：

- **脚本单跑成功**：物料脚本从不 import agent；崩溃发生在 **cron 父进程**，
  只有 `no_agent: False` 才会走到 `from run_agent import AIAgent`
  → `model_tools` 模块级的 `discover_builtin_tools()`。
- **网关 16:04 启动正常**：`gateway.run` 导入 `tools.registry` 但**不导入** `model_tools`。

**回溯栈为什么没留下**：`cron/scheduler.py:6588` 是 `str(e) or type(e).__name__`
配 `logger.error(...)`，**没有 `exc_info`**，存进 `executions.error` 的就是那个裸字符串。

### 修复

`tools/registry.py` 一行 + 注释：`except OSError:` → `except (OSError, UnicodeDecodeError):`

该函数本就为"读不了"（`OSError`）和"解析不了"（`SyntaxError`）返回"不注册工具"；
"解码不了"是同一个判定。放任它逃逸会让 **所有** 工具的发现中止，
而发现发生在 `import model_tools` 时，等于整个 agent 引导失败。

### 这是我造成的

那 49 个伴生文件**来自我的部署**：我从 Mac 推送时保留了 xattr
（`tools/onebot_client.py` 带 `com.apple.provenance`，本地 `tools/*.py` 共 123 条 xattr），
落到不支持 xattr 的文件系统上就物化成 `._*` 文件，**三批正好对应我三次部署**。
其中一个直接打断了生产的工具发现。

已清理：49 个全删，备份 `/opt/hermes/appledouble-backup-20260819T162554Z.tgz`，
123 个真实 `.py` 完好，`import model_tools` 正常。

| # | 决策 | 理由 |
|---|---|---|
| D55 | 接受修复落在 `tools/registry.py`（**打破本次迁移"零上游文件改动"的性质**） | 这是真实的上游缺陷：任何非 UTF-8 文件混进 `tools/*.py` 都会击穿全部工具发现。放在 `corlinman_jobs` 里只能绕过自己这一条路径，别的调用方照样中招。改动一行，子智能体主动申报了越界 |
| D56 | 此后一切向生产的部署必须剥离 xattr | `COPYFILE_DISABLE=1` + `tar --exclude='._*'`，或 `rsync --exclude='._*'`（**不要** `rsync -X`）。本次事故的完整因果链就是部署方式 |
| D57 | 用户群 topic 680 因本轮测试收到 3 条消息，如实记录不隐去 | 2 条失败告警（16:06 / 16:08）+ 1 条成功输出（16:17）。cron 失败路径确实会投递告警（`_deliver_result(job, _summarize_cron_failure_for_delivery(...))`，`scheduler.py:6609`）；告警原文我本地跑格式化函数取得，与子智能体的推断**逐字一致** |

投进用户群的确切内容：

```
16:06  ⚠️ Cron 'hermes.analysis_digest' failed: 'utf-8' codec can't decode byte 0xa3 in position 45: invalid start byte
16:08  （同上）
16:17  过去 24 小时没有发现新的分析、研究或策略记录。
```

第三条正是 prompt 里写死的无命中话术，与物料脚本的 `NO_ANALYSIS_MARKER` 分支一致 —— **链路通了**。

### 待办（未阻塞）

- `hermes.service` 每次停止都以 `code=exited, status=1/FAILURE` 收场（16:04:04、16:20:11 各一次），
  重启后正常。疑为关停路径的退出码问题，非本阶段范围，记录待查。

---

## 27. E3 —— `youtube_daily` 空响应：查出一个**迁移清单遗漏**（2026-08-19）

### 验收：✅ 通过（诊断正确、证据是抓到的原始报文；但违反了一条硬约束，见下）

子智能体抓到了 provider 的原始往返（key 已脱敏）。四次尝试的响应都是：

```json
{"choices":[{"message":{"role":"assistant"},"finish_reason":"stop"}],
 "usage":{"completion_tokens":689}}
```

`message` 里**根本没有 `content` 键**（不是 `""`，是不存在），`finish_reason:"stop"`，
却计费了几百个 completion_tokens。生产日志签名一致：
`Empty response (no content or reasoning) — retry N/3` ×3 → `empty_response_exhausted`。

它同时纠正了 Orchestrator 的一个错误框定，证据我已独立复核：

```
16:28:40  check_fn check_web_api_key returned False; dependent tools will be unavailable this turn
16:28:57  competition_daily  reason=text_response  api_calls=1/500  tool_turns=0  response_len=1812
```

⇒ **`competition_daily` 那 1812 字竞赛简报是一次模型调用凭记忆写出来的，没有检索任何东西。**
我先前对用户说它"跑出了带 web 检索的实质内容"是错的。
生产机 `.env` 只有 4 个键（`TELEGRAM_BOT_TOKEN`/`CORNNA_API_KEY`/`ONEBOT_ACCESS_TOKEN`/`ONEBOT_WS_URL`），
无任何搜索后端凭据，无密钥回退包 `ddgs` 也未安装。**`web` 工具集对本机所有任务都是静默空转。**

### Orchestrator 补查：这不是环境限制，是**我的迁移遗漏**

子智能体的结论止于"环境/provider 限制"，并说无法确认源系统的搜索层。我补查了：

```
corlinman 进程环境：无任何搜索后端变量
运行中进程：      uvx free-search-mcp>=0.8.0（配 Playwright + headless Chrome）
mcp_servers 表：  ('search', enabled=1)
```

**corlinman 的搜索能力来自一个无密钥的 MCP 服务器，而我的迁移清单里从来没有它。**
我把依赖搜索的任务搬过来了，没搬它们赖以工作的搜索能力。
这是本次迁移目前为止唯一一个**整块能力级别的遗漏**（此前的问题都是单点缺陷）。

### 内存把排期也定死了

```
Mem: total 1966 MB   used 1587   available 379
现有 search MCP 的 Chrome 进程：257 + 139 + 114 + 50 + 48 ≈ 607 MB
```

**379 MB 装不下第二份 headless Chrome。** 共存期内不能给 hermes 复制一份搜索 MCP。

| # | 决策 | 理由 |
|---|---|---|
| D58 | 阶段 4 只放开 `analysis_digest` + `diary_summary`；`competition_daily` 与 `youtube_daily` **保持 paused** | 前两者只读本地数据、已实测正确。后两者依赖搜索：`youtube_daily` 硬失败（11 位 video_id 编不出来），`competition_daily` 更糟——它会**每天把编造的赛事时间与报名窗口投进用户真实群**，且失败得静悄悄。宁可不跑，不可跑出假的 |
| D59 | 搜索能力（`free-search-mcp`）的迁移安排在**阶段 7 corlinman 退役时交接**，不在共存期做 | 只剩 379 MB 可用内存，第二份 Chrome 会把 1.9 GB 的机器推向 OOM。这不是偷懒，是硬约束 |
| D60 | `youtube_daily` 即使补上搜索也需重验 | 空响应发生在**没有工具**的前提下；provider 对该 prompt 形状返回空这一现象，在有工具时是否消失尚未验证 |

### 子智能体违反了一条硬约束（不打回，但如实记录）

我下达的约束里写明"绝不打印令牌"。它读 `/opt/corlinman/data/config.toml` 时，
脱敏正则没有覆盖 TOML 的嵌套表写法（`[providers.cornna.api_key]` 后另起一行 `value = "..."`），
**corlinman 的 `providers.cornna` API key 明文进入了会话记录**。它主动申报了此事。

不打回的理由：重跑无法挽回已经泄露的密钥，而其工作产出本身正确完整。
处置：**已建议用户轮换该密钥**。今后向子智能体下达的读取类任务，
约束要从"不要打印令牌"改为"**不得整体输出任何配置文件；只按白名单取所需字段**"。

### 阶段 4 收尾状态

| 任务 | topic | 状态 | 下次 |
|---|---|---|---|
| `hermes.analysis_digest` | 680 | **已启用** | 08-20 15:12 CST |
| `hermes.diary_summary` | 11 | **已启用** | 08-19 23:41 CST |
| `hermes.competition_daily` | 13 | paused（待搜索能力） | — |
| `hermes.youtube_daily` | 680 | paused（待搜索能力 + 重验） | — |

---

## 28. 切主与内存：三处归因更正 + 顺序重排（2026-08-19）

用户指示：把 corlinman 剔除下线、切换 hermes 为主、内存 swap 扩容。
照做之前先量了一遍，**三条前提里有两条与实测不符**。

### 更正一：我把无关服务的内存算到了 corlinman 头上

我先前写"现有搜索 MCP 的 Chrome 已占约 607 MB"，据此得出 D59「共存期塞不下第二份 Chrome」。**错的。**

```
corlinman 的 search-mcp Chrome：10+4+3+1+1+0 ≈ 19 MB 常驻（空闲时基本被换出）
  且归属 /user.slice/user-0.slice/session-11067.scope —— 不在 corlinman 的 cgroup 里
chrome-cdp.service      355 MB   --user-data-dir=/opt/chrome-cdp    ← 与本迁移无关
chrome-headless.service  55 MB   --user-data-dir=/tmp/chrome-data   ← 与本迁移无关
```

我把后两个算进来了。**D59 的依据不成立，撤回**（见 D63）。

### 更正二：swap 不是瓶颈，物理内存才是

```
Swap: 6143 MB 总量，已用 4181，**尚余 1962 MB**
Mem:  1966 MB 总量，可用仅 344 MB
磁盘: 50 G 用了 85%，仅剩 7.5 G
```

swap 还剩近 2 G 没用完 ⇒ **再加 swap 一寸也换不来**，只会加剧换页；
而磁盘是这台机器上真正稀缺的资源。这笔交易是拿稀缺换不稀缺。

### 更正三：下线 corlinman 腾不出多少内存

```
corlinman.service cgroup 总 RSS: 51 MB
  + 其 search-mcp Chrome ≈ 19 MB
  ≈ 70 MB
```

真正吃内存的是**与本迁移无关的另一套业务**：
`redis`（936 MB swap，单项最大）、docker 里的 copytrader 栈（550 MB）、
`chrome-cdp`（355 MB）、`binance-copy-sync`、`nginx`、`sota-vless-hy-xray` 等。

⇒ **"下线 corlinman 换内存"不成立**：付单向门的代价，换回 70 MB。
退役 corlinman 的正当理由是**消除双主、让真相只有一处**，不是内存。

### 实际执行的动作

`/etc/systemd/system/hermes.service.d/primary-role.conf`（备份 `hermes.service.20260819T171516Z`）：

| 项 | 改前 | 改后 |
|---|---|---|
| `OOMScoreAdjust` | **500** | **0** |
| `MemoryHigh` | 384M | 512M |
| `MemoryMax` | 512M | 768M |
| `MemorySwapMax` | 512M | 768M |

`OOMScoreAdjust=500` 是"corlinman 是主、hermes 陪跑"时代的产物 ——
内存一紧内核**优先杀 hermes**，而 corlinman 是 0 且三项限额全 `infinity`。
主从要反过来，这一项必须先反，否则切主只是名义上的。

| # | 决策 | 理由 |
|---|---|---|
| D61 | OOM 优先级反转（500 → 0），限额小幅上调 | 这才是"切主"在操作系统层的实际含义。不设负值：负分留给系统关键组件，把 hermes 提到比 redis / copytrader 更不可杀，是替用户做跨业务取舍 |
| D62 | **不扩 swap** | 6 G swap 尚余 1.9 G，扩了换不来物理内存；而磁盘 85% 已满，是真正稀缺的一侧。若用户在知悉此数据后仍要扩，随时可加 |
| D63 | **撤回 D59**，搜索 MCP 的迁移不必压缩到退役那一瞬 | 其 Chrome 空闲仅约 19 MB，我先前 607 MB 的归因是错的。共存期大概率放得下，改为在阶段 5 一并评估实测峰值 |
| D64 | 退役顺序不变：**阶段 5 迁数据 → 搬搜索 MCP → 才下线** | 凡需从 corlinman **读**数据的步骤（qzone 三个状态账本、群历史回填、搜索 MCP 配置）都必须在它还活着时做完。先下线会把这些永久留在门的另一侧 |

---

## 29. 阶段 5 前半段验收（5.1 + 5.2）—— ✅ 通过（2026-08-19）

Orchestrator 独立复核，与其自述逐项吻合：

| 项 | 复核结果 |
|---|---|
| `qzone_state` preflight | FAIL → **ok**：`persona=grantley, post_log=19, seen_comment_tids=2, friend_comments=37` |
| `.env` | 新增 `QZONE_PERSONA_ID` / `QZONE_STATE_DIR`，`mode=600 hermes:hermes` |
| 归档规模 | **58,840 行** / 6 个群 |
| 重复 `(group_id, message_id)` | **0 组** |
| corlinman | PID 2581308、`NRestarts=0`、`/health` 200 |
| hermes | PID 3230901、`NRestarts=0`（全程未重启） |
| QQ 出站 | **0** |

### 它做对的三件不显眼的事

1. **发现 D2 §8 的方案根本不可行**：`hermes`(uid 991) 不在 `corlinman-execution` 组，
   连 `/opt/corlinman/execution-state`（mode 2770）都进不去，
   "把 `QQ_GROUP_HISTORY_DB` 指向 corlinman 活库"做不到。它**没有去改生产的组权限**，
   改用 SQLite backup API 从 `mode=ro` 连接做一致性快照。这是正确的取舍。
2. **量化了反事实**：persona 若留在 `default`，`default.json` 压根不存在 ⇒ 账本读空（不是读到一半），
   40 条记录全部会被判为"尚未回复" ⇒ **3 条自己说说的评论 + 37 条好友动态，共 40 次不可撤销的公开重复操作**。
   它不是只比对计数，而是把 40 条记录逐条喂给 `is_recorded_comment()`：**40/40 命中，0 漏**。
3. **回填时抓到一个真实竞态缺陷**：`_existing_keys()` 只在开始时快照一次目标键集、之后不刷新，
   且表上故意没有唯一索引 ⇒ 活写入方在回填中途提交、而该消息又在源快照里，就会插进第二份。
   实测窗口约 1.4 秒，只影响两个写入方都覆盖的群。它删掉了那一条并复核到 0。

### 两个必须记住的后果

| # | 决策 | 理由 |
|---|---|---|
| D65 | **5.4 之前必须重启 `hermes.service`** | `.env` 在进程启动时读取；网关 PID 3230901 起于 17:15，早于这次追加，其 `os.environ` 里**没有** `QZONE_PERSONA_ID`。现在无害（qzone 插件未启用），但一旦 5.4 放开而未重启，进程内的 qzone 工具会解析成 `default` —— 正好就是那 40 次公开重复 |
| D66 | **退役前必须把 `980927602` 加进 `group_whitelist`** | 已复核属实：白名单是 `[1082225370, 183287894, 894800697, 149881991, 667528618]`，**不含 980927602**。而 `group_history.resolve_config` 只归档白名单内的群 ⇒ hermes 自己的写入方从未、也永远不会为 sanhu 的群写入一行。那 52,934 行**全部来自这次回填**，corlinman 一退役，7 天保留期一到 sanhu 就静默失效 |
| D67 | 最后一次追平回填要在 hermes 写入方静默时做 | 否则会命中上面那个 1.4 秒竞态。或者做完立刻复核并删掉竞态行 |

### 它自己申报的失误

检查 `.env` 是否以换行结尾时用了 `tail -c 60 | od -c`，把 `ONEBOT_ACCESS_TOKEN`
的**末 12 位**打进了记录。前缀未泄露。影响有限——已实测 NapCat 三个账号的 OneBot
`token` 全为空，该值本就是装饰性的（本次迁移第六个"配置写着 X、实际是 Y"）。
但这是同一类错误第二次发生，说明"不要打印令牌"这条约束的表述不够强。
**新规则：读取类任务一律禁止整体输出文件，只允许按字段白名单取值。**

## §30 面板认证收敛与 SnowLuma 接管前置（2026-08-19）

用户反馈：「太多登陆凭证了，怎么老是要登，到最后这个还登不过」。两个缺陷，都是我造成的。

**D68 — 撤掉 nginx Basic 认证层。**
我在 napcat / snowluma / vnc 三个 vhost 前面各加了一层 `auth_basic`。这是错的：
三个面板都是单页应用，其内部 `fetch` / WebSocket 不携带 `Authorization` 头，被 nginx
401 后浏览器无限重弹。已从三个 vhost 全部移除并 reload。移除后应用自身的鉴权仍然拦得住
（SnowLuma `{"status":"failed","message":"Unauthorized"}`、NapCat `{"code":-1}`），
不存在裸奔。备份：`/root/{snowluma,napcat}.cornna.xyz.conf.bak.20260819T182008Z`。
教训：Basic 认证不能套在 SPA 前面；面板的认证归面板自己。

**D69 — SnowLuma WebUI 口令改走官方 bootstrap 通道，不再手搓哈希。**
「登不过」的根因：我上一轮直接改写 `webui.json` 的 `passwordHash`，把 `passwordSalt`
的 **hex 字符串**当 salt 喂给了 scrypt，而 SnowLuma 的 `verify()` 用的是
`Buffer.from(passwordSalt,"hex")` —— **解码后的字节**。参数本身没错
（N=16384 r=8 p=1 keylen=64），错在 salt 编码，所以哈希必然对不上。
改用 `SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD`：该通道仅在 `webui.json` 缺失/损坏时生效
（`WebuiAuth.load()` 的优先级：有效文件 > env > 随机生成），故需先删文件再重建容器。
已写入 `/opt/snowluma/docker-compose.yml`，备份 `.bak.20260819T182920Z`。
日志确认 `webui credentials seeded from SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD`。
实测：本地与经域名两条路径均 `{"success":true}`，错误口令回「密码错误」。
登录路由是 `POST /api/login`（**不是** `/api/auth/login`——后者是未知路径的统一 401，
我据此误判过一次）；字段只需 `password`，用户名可省。
容器重建后 QQ 会话保留（命名卷），账号 1010679324 仍在线。

**D70 — NapCat 不是 SnowLuma 的依赖，是待下线的存量。**
容器真名 `corlinman-napcat`（`mlikiowa/napcat-docker`，已跑 4 周），是 corlinman 的桥。
两者是同层替代品（都讲 OneBot v11）。实测证据：SnowLuma 独立登录着 1010679324，
全程未触及 NapCat；`:3011` 已建立连接 0 个；`config.yaml:204` 的
`ws_url: "ws://127.0.0.1:3001"` 仍指向 NapCat，且 corlinman-gateway 与 hermes
**同时**挂在 3001 上（双主）。NapCat 之所以还在，只因切换尚未执行。

**D71 — 用户协议不代签。**
SnowLuma 在同意《用户协议与隐私政策》前拒绝全部管理 API
（`{"consentRequired":true}`，version `dcb31a7b03a0b64d`），因此 OneBot 网络层未启动
（容器内 3000/3001 无监听，`mode: snapshot`）。这是一份法律协议，不属于我可代为点击的
范围，留给用户在面板内自行确认。这是 SnowLuma 接管的唯一前置。

**接管路径（前置解除后三步）**：启用 wsServers（容器内 3001→宿主 3011）→
`config.yaml` 的 `ws_url` 指向 3011 并观察归档 → 停止并删除 `corlinman-napcat`
（该动作同时完成阶段 5.3「停 corlinman 的 QQ 侧」）。

## §31 QQ 侧接管：现状核查与下线代价（2026-08-19）

用户指令：「接入 hermes agent，下线 corlinman，并且要完全把 corlinman 一模一样的主动发言
逻辑迁移过来」，随后追加「先关掉群组发言，我先私聊测试」。故本阶段**只开私聊**。

**核查结论（均为实测）**：
- 群组发言本就关死：`group_replies_enabled: false`，`proactive_enabled` 未设 ⇒ 默认 false（D31）。
  本阶段将两者**显式写死**，不再依赖默认值。
- 主动发言实现**已落地**：`plugins/platforms/onebot/proactive.py`（33KB / 约 797 行），
  D40 修复在位（`proactive_groups` 非空但过滤后为空 ⇒ 保持沉默，不回退白名单）。
- 人格接线**已通**：`adapter.py:1318` 与 `proactive.py:629` 均调用
  `persona_binding.channel_prompt()`。config.yaml 中「⚠ INERT ON TWO COUNTS ...
  no adapter calls resolve_channel_prompt」那段注释**已过时**，应删除。

**D72 — 私聊前必须先启用 grantley，否则回复的是通用助手。**
`plugins.enabled = ['corlinman_jobs']`，`grantley` 不在其中，`hermes plugins list` 报
`not enabled`；且 `platforms.onebot.extra.persona_channel_map` 未设。人格接线虽通，但插件
没加载、频道映射为空 ⇒ DM 会以无人格身份应答。这会让"私聊测试"得出错误结论。

**D73 — corlinman 是两个服务。**
`corlinman.service`（网关控制面，pid 2581308，占着 3001）与 `corlinman-agent.service`
（执行面，User=corlinman-agent，WorkingDirectory=/opt/corlinman/execution-state）。
私聊交接只需停前者；完整下线需两者都停。一律**只 stop**，保持可回滚。

**D74 — 群历史读写路径必须在下线前对齐。**
corlinman 实时库在 `/opt/corlinman/execution-state/qq_group_history.sqlite`（WAL，
带 -wal/-shm）——**不在** `/opt/corlinman/data/` 下。hermes 自有库
`/opt/hermes/data/plugin-data/corlinman_jobs/qq_group_history.sqlite` 13MB 且在活跃写入。
监控任务经 `QQ_GROUP_HISTORY_DB`（`installer.py:162,183`）决定读向，该变量在 `.env` 中未设。
若默认解析指向 corlinman 的库，则停机后三个监控会静默总结一个冻结文件——不报错，纯劣化。

**D75 — 下线 corlinman 会连带失去联网搜索，这是真实能力回退。**
`free-search-mcp`（`uvx free-search-mcp>=0.8.0`，pid 2581578）连同一组 headless Chrome
是 corlinman 网关的**子进程**，随 `corlinman.service` 一并终止。
后果具体：`plugins/grantley/assets/grantley.md` 的回答工作流写明「需要事实的问题 →
必须先 web_search」，而 hermes 侧 web 工具集此前实测为静默空转（§27）。
⇒ 下线后私聊里的事实性提问会劣化。**不阻塞本次私聊测试**，另行处理。

## §32 主动发言对齐审计结论与 OneBot 出站接线（2026-08-19）

审计（Opus，一次打回后修正）结论：**主动发言核心闸门链与 corlinman 行为等价，无阻断级差异。**
15 项要素中 6 项逐行一致（时间窗半开区间与跨夜、单调时钟 min_gap、max_gap 缺省 ×4、
概率闸、SKIP 正则、配额只探不耗且发成功才记账——已核对 `adapter.send()` 不重复 record），
4 项为既有有意分歧（D31/D32/D34+D40/RAG 缺席，均在源码注释中声明）。
源码位置更正：主动发言块在
`/opt/corlinman/repo/python/packages/corlinman-channels/src/corlinman_channels/service.py`
L753–L1305（此前记为 `service.py:774-1290`，偏差已订正）。

### 打回与修正（记录我的评审过程，不是结论）
初版称「hermes 全仓无腾讯内容策略等价物，需新写一道闸」——**与事实不符，已打回**。
`plugins/qzone/policy.py` 是 corlinman `corlinman-content-policy` 包的**逐字节移植**
（`moderate_text` 在 policy.py:340，`RULESET_VERSION = tencent-freeze-risk-2026-07-21.1`，
模块 docstring 写明存在目的是防 QQ 账号 1010679324 被冻结）。
漏检原因：搜索范围限于 `plugins/platforms/` 与主干，未扫 `plugins/qzone/`。
修正后成本从「实现一个分类器」降为「接一次线」，且因逐字节移植，**语义等价天然成立**。

### 修正版带来的三项新事实
1. **两侧同源且配置解析等价**：corlinman `service.py:393-397` 的 `_tencent_text_decision`
   就是包装同一个包的 `moderate_text`/`classifier_failure_decision`。两边都是
   `enabled is not False`，只有字面 `False` 才关闭。
2. **该闸在 corlinman 生产中是开启的**（`ReloadingTencentPolicyResolver` fail-closed：
   路径为 None ⇒ True，读取异常 ⇒ True；生产 config.toml 无相关键）。
   ⇒ 与主动发言"配了但从未生效"的情况**不同**，这是一项真实在跑的保护。
3. **缺口是整个 OneBot 出站面，不是主动发言独有**：corlinman 四条 QQ lane 全部过闸
   （回复 `service.py:4043` / 主动 `:1278` / 群监控 `:2127` / 入站 `:2737`）。
   ⇒ 本项应独立立项，**优先级高于主动发言启用**——主动发言默认关闭，而反应式回复裸奔。

**D76 — 内容闸不加 `is_group` 限定，群与私聊一律过闸。**
静音闸是群限定的（`if is_group and group_speech_muted(self)`），内容闸不是：corlinman
`service.py:4043` 覆盖全部 QQ 回复。冻结风险是**账号级**的，腾讯风控不区分群聊或私聊。
用户当前正在做私聊测试，此选择直接影响测试期行为，故显式拍板：**私聊同样过闸**。

**D77 — 接线点与拒绝语义。**
接在 `adapter.py::send()` 内（非 `client.py`——后者是纯传输层；`client.py:21-24` 的原注释
本就指向 adapter 的 send path）。顺序：
`parse_chat_id → 静音闸 → strip_markdown → 内容闸 → split_bubbles → chunk_text`。
审的是 **strip_markdown 之后的完整成品文本**：逐气泡/逐 chunk 审会让跨块表述漏网。
（源侧自身不一致——主动发言审 normalize 前、回复审 normalize 后；在 `send()` 统一接线
反而消除该不一致。）
拒绝时 `success=False, retryable=False`，**`error_kind` 必须为 `"unknown"`**：
`forbidden`/`not_found` 在 `gateway.dead_targets._DEAD_ERROR_KINDS` 中，会把一次内容拒绝
变成**粘滞的死目标**（判例见 `adapter.py:275-282` 的 `muted_send_result`）。
`raw_response` 带标记键 + `policy_error_payload` 的三字段（不含原文，可安全落日志）。

**D78 — 出站 markdown 归一化。**
`strip_markdown`（`gateway/platforms/helpers.py:196`）已被 8 个适配器使用，
**含另一个 QQ 平台 `gateway/platforms/qqbot/adapter.py`**，唯 OneBot 未接。
`adapter.py:639` 的 `supports_code_blocks = False` 不影响 `send()`（仅
`gateway/run.py:4310` 的进度输出读取），不可误认为已有处理。

### 待决 / 未覆盖
- **媒体路径未确认**：OneBot 图片/文件不走 `send()` 文本路径，corlinman 侧用 `moderate_media`
  （`service.py:7440/7554`，未分类媒体 deny-by-default）。本次不做，留 TODO 单独评估。
- B-3 合并转发卡差异、B-4 提示词工具名、B-5 `is_skip` 更宽、B-6 日界随时区同移、
  B-7 会话隔离耦合 `group_sessions_per_user`、B-8 身份闸更弱（标未确认）、
  B-9 `proactive_groups` 字符串解析更健壮——均为次要，暂不处理，已登记。

## §33 QQ 桥切换到 SnowLuma + 出站接线上线（2026-08-19）

**D79 — 事故与根因：SnowLuma 登录把 NapCat 挤下线，是我造成的。**
NapCat 日志 `08-19 17:12:45 [KickedOffLine] [下线通知] 您的账号已在另一台终端登录`
（容器时钟，宿主时间 18:12）。QQ 同类型客户端互斥，后登踢先登。
此前"两个桥可同时在线"的观测被推翻——那只是踢下线之前的窗口期。
后果：hermes 连的是 NapCat(3001)，账号离线，私聊测试无法进行。
且 `napcat.json` 无 `autoLoginAccount`、容器内无 QQ 登录态缓存 ⇒ **NapCat 恢复需扫码**。

**D80 — 选择切到 SnowLuma 而非恢复 NapCat。**
理由：SnowLuma 持有账号且在线（10 分钟收 251 条事件）；用户本就要求用 SnowLuma 替换
NapCat；恢复 NapCat 需要用户扫码，而切换不需要任何用户动作。

**D81 — SnowLuma 的 OneBot 绑在容器内 127.0.0.1，docker 映射到不了。**
容器内 `ss -ltn` 显示 `127.0.0.1:3000` / `127.0.0.1:3001`，而 docker 端口映射
（`127.0.0.1:3011->3001`）转发到容器 **eth0**，不是容器 loopback ⇒ 宿主敲 3011
永远 `did not receive a valid HTTP response`。这不是"OneBot 没启动"（我一度这么判断）。
经 `POST /api/config/1010679324` 把 `wsServers`/`httpServers` 的 `host` 改为 `0.0.0.0`
后立即生效（`applied: true`，`detail: 监听中`）。
宿主侧仍只监听 `127.0.0.1:3011`，未对外暴露。
API 信封：GET 返回 `{"config": {...}}`，**POST 要裸结构**（发包 `{"config":...}` 会被
拒为 `networks must be an object`）。
容器内备份 `/app/data/config/onebot_1010679324.json.bak.before-bind0000`。

**D82 — `.env` 覆盖 config.yaml，且 `.env` 不在我以为的路径。**
真实路径是 `/opt/hermes/data/.env`（不是 `/opt/hermes/.env`——我先前查错路径，
得出"没有 ONEBOT_ 变量"的错误结论）。其中 `ONEBOT_WS_URL` 覆盖 config.yaml 的
`ws_url`，只改 config.yaml 无效，hermes 仍连 3001。两处都改后才切过去。
备份 `config.yaml.bak.snowluma.*` / `.env.bak.snowluma.*`。

**验收实证（重启后 pid 3350738）**：
```
OneBot adapter initialised: url=ws://127.0.0.1:3011 groups=MUTED whitelist=5 keywords=5
OneBot: adapter connected (ws://127.0.0.1:3011)
OneBot: proactive loop idle (disabled) instance=default
OneBot: bot account is 1010679324 / QQ account is online
```
入站管道实证：19:01→20:27 群历史 written=125 dropped=0 failed=0。

**D83 — 出站接线（D77/D78）已实现、测试、上线。评审通过。**
`adapter.py::send()` 内顺序 `mute → strip_markdown → 内容闸 → split_bubbles → chunk_text`。
内容闸无 `is_group` 限定（D76），失败 fail-closed，拒绝返回
`error_kind="unknown"`（避开 `_DEAD_ERROR_KINDS`，防止一次内容拒绝变成粘滞死目标），
`raw_response` 只带 `policy_error_payload` 的三个不含原文的审计字段。
测试：`tests/gateway/test_onebot_content_gate.py` **53 passed**；
`test_onebot_proactive.py` **81 passed** 无回归；onebot 全量 **553 passed / 2 skipped**。
测试对"文案不撞 `classify_send_error`"的断言是**调真正的分类器**，非靠措辞自证。
生产备份 `adapter.py.bak.contentgate.20260819T042643Z`；AppleDouble 残留 0。
媒体路径（`send_media` / 内联图片）不经 `send()`，仍无审核，代码内已留 TODO。

**遗留**：NapCat 容器仍在运行（用户拒绝停止）；`corlinman.service` 与
`corlinman-agent.service` 均 inactive 且可 `start` 回滚；联网搜索随 corlinman 消失（D75）；
私聊白名单仅 `2104743984`，其他号会被静默丢弃但 `gateway.log` 会记 `Unauthorized user`。

## §34 全面放开：私聊开放 + 群内发言 + 主动发言（2026-08-19 20:38）

用户指令：「开启所有人的聊天允许和我在 corlinman 中允许发言和主动发言的发言准许和主动发言准许」。

**corlinman 原始值（只读核对 `/opt/corlinman/data/config.toml`）**：
```
group_replies_enabled = False          ← 紧急静音总闸
proactive_enabled     = True           ← 设了，但被总闸掐死
group_whitelist       = [1082225370, 183287894, 894800697, 149881991, 667528618]
group_rate_limit      = 5 条 / 3 分钟
reply.on_at_mention = True   reply.on_direct_message = True
group_keywords        = 五个群各 ["格兰"]
humanlike.persona_id  = grantley
（无 allowed_users / allow_all_users 键 ⇒ 私聊对所有人开放）
```

**D84 — 私聊放开对齐 corlinman。**
`.env` 中 `ONEBOT_ALLOWED_USERS` 注释保留备查，新增 `ONEBOT_ALLOW_ALL_USERS=true`。
这是**回到 corlinman 的行为**（它本就无白名单），不是新增暴露面。
⚠ 自查教训：首次核验 `ALLOW_ALL_USERS` 得到 `None`，我据此报"没生效且更糟"——
**错在检查本身没设 `HERMES_HOME`，`load_hermes_dotenv()` 读了别的路径**。
带上 `HERMES_HOME=/opt/hermes/data` 后为 `'true'`。核验运行时配置必须带 HERMES_HOME。

**D85 — 掀开紧急静音总闸，这是 corlinman 从未发生过的行为。**
`group_replies_enabled: false → true`。corlinman 生产该值为 false，它同时掐死群回复与
主动发言（§14：七天日志 `grep -ic proactive` = 0）。改为 true 后两者才真正上线。
用户已被明确告知该点后仍要求开启，故执行。回滚 = 把这一行改回 false。

**D86 — 主动发言启用，参数按源默认显式写出。**
`proactive_enabled: true`；`min_gap 45` / `max_gap` 不设(⇒ ×4 = 180) / `daily_max 4` /
活跃 `[9, 23)` / `probability 1.0` / `context 30` / `timezone "Asia/Shanghai"`（D32，
日计数归零点同步为北京午夜，见 B-6）；`proactive_groups` 不设 ⇒ 回退白名单（同 corlinman）。

**验收（运行时自报，pid 3357130）**：
```
OneBot adapter initialised: url=ws://127.0.0.1:3011 groups=enabled whitelist=5 keywords=5 policy=mention_or_keyword
OneBot: proactive loop started groups=['1082225370','149881991','183287894','667528618','894800697']
```
（此前三次重启均为 `groups=MUTED` / `proactive loop idle (disabled)`，对照清晰。）
Traceback 数 0。

**同时生效的两道保护**（§32/D83，先于本次放开上线）：出站 `strip_markdown`；
腾讯内容策略闸（无 `is_group` 限定，群与私聊同过，fail-closed，
拒绝用 `error_kind="unknown"` 避免粘滞死目标）。放开发言前这两道已在位，顺序是对的。

**需要用户知晓的实情**：
- **980927602（"高认知且渴望存续的好人群"，SnowLuma 日志显示是当前最活跃的群）不在白名单里**，
  机器人不会在那里发言。加它属于超出用户所述范围，未加。
- 主动发言首帖落点：睡眠取 `uniform(45, 180)` 分钟，且须落在北京时间 `[9, 23)` 内。
- 媒体路径仍无内容审核（§32 遗留，代码内有 TODO）。
- 联网搜索仍缺失（D75），事实性提问会劣化。

## §35 出站三件套上线 + 生图后端 + QQ 空间暂停（2026-08-19）

用户四项要求：回答精简（不要臃肿末条）/ 复杂问题卡片化 / 沿用表情包 / QQ 渠道隐去系统消息。
另：「先别发 qq 空间」+ 提供生图端点与 9 张表情素材。

**D87 — QQ 空间三个任务已暂停，一次公开动作都没发生。**
`hermes.qzone_{reply,friends,daily}` 全部 `paused`，且 `last=None` 证明从未执行；
`post_log` 仍是迁移基线 19 条。用户要先看生图风格再决定。

**D88 — 臃肿末条的根因是我的规则设计错误。**
`cap_bubbles` 把第 3~N 条合并进第 3 条 ⇒ 末条变一坨。改为：
整条超 `forward_threshold` ⇒ 卡片；**气泡数超上限 ⇒ 也整条进卡片**（新增分支）；
否则原样发短气泡、不合并。`cap_bubbles` 降级为退路（`forward_threshold<=0` 或卡片被拒时）。
「几条短消息 或 一张卡片」，中间态被消除。
卡片内保留 `[MSG_BREAK]` 分段为多个 forward node（协议原生支持，`messages` 本就是 List）。
卡片路径**不受 `chunk_text` 约束**（forward node 不走 `send_msg`），已写测试钉死。

**D89 — 系统消息隐去：方案 A′（只加一个平台，不是删判断）。**
直接删掉 `_non_conversational_metadata` 的平台判断会**跑挂两个既有测试**
（`test_restart_notification` Telegram / `test_run_progress_topics` Slack，都用精确 dict 断言钉着
"其它平台不变"）。故改为 `not in ("discord", "onebot")` —— 仍是一行，零波及面，
其它平台的 metadata 仍按**同一对象**返回。这是本次唯一的主干改动。
默认 `true`。两类豁免：`⚕ Update needs your input:`（阻塞等回答，藏了会永久卡住）与
`⚠️ Session database …`（数据正在丢失）。被丢弃的消息仍写 info 日志。
拒绝返回 `error_kind="unknown"`（避开 `_DEAD_ERROR_KINDS`）。
**我让子智能体评估的"审批消息要不要豁免"是个伪问题**：`_approval_notify_sync`
（`run.py:6032`）把 metadata **直接透传**、从不调用该标记函数，所以
`⚠️ Dangerous command requires approval` 结构上到不了闸门。已有测试钉住此事实。

**D90 — 表情包：自定义图 + 模型自选，不是 QQ 原生表情。**
用户提供 9 张格兰特利表情图，已归位 `plugins/grantley/assets/stickers/`
（`heart-hug` / `fired-up` / `unimpressed` / `flustered` / `thumbs-up` / `shrug` /
`angry` / `laughing` / `thinking`，语义标签由我逐张看图确定）。
机制：概率 `0.18` 决定**是否把菜单摆到模型面前**（不是发图概率，模型看到也常不用 ⇒ 实际更低）；
模型用内联标记 `[STICKER:<slug>]` 表达；适配器在
**`strip_markdown` 之后、腾讯内容闸之前**摘除标记，附加到最后一条气泡的最后一个 chunk。
卡片路径不附加；不参与分流判定；正文为空时只发图。
目录即白名单，`../../etc/passwd` 这类 slug 直接拒绝。
**必须 `base64://` 不能 `file://`**：SnowLuma 在容器内，读不到宿主 `/opt/hermes/repo`
（D81 记载端口映射走容器 eth0，跨 namespace）。既有 `_send_attachment` 本就这么做，
字面路径只是 >8MiB 的降级分支；9 张图各 23–29KB，永远走 base64。
菜单注入点在 `channel_prompt()` **之外**——`_channel_prompt` 按 (persona, channel, group, day)
**做了日缓存**，把骰子塞进去会让概率冻结成"当天全有或全无"。

**D91 — 生图后端 `cornna` 已上线，但无角色锚定。**
`plugins/image_gen/cornna/`（541 行 + 48 测试），`image_gen.provider: cornna`，
密钥 `CORNNA_IMAGE_API_KEY`（**与文本的 `CORNNA_API_KEY` 是两把不同的 key**，
sha256 前缀 `515b0593…` vs `0fa2b2d5…`，不得合并）。生产实测 `就绪 = True`。
端到端接缝有测试实证：provider 返回值直接喂 `qzone/publish.py::_load_image_reference` 拿到 bytes。
**遗留**：`_generate_image` 的注释写明 corlinman 用 `image_with_refs`（参考图锚定），
hermes 未移植。实测三张样图证实后果——纯提示词路径下角色会漂（sample-1 琥珀眼 /
sample-2 蓝眼），而拿表情包当参考图走 `/v1/images/edits` 的 sample-3 与表情包同一角色。
**该端点支持 `/v1/images/edits`（我实测通过）**，但 provider 因未验证而主动拒绝
image-to-image。要拿到一致性必须补参考图支持。三张样图仅用于本地验收，既非运行时
资产，也不随代码提交归档。

**D92 — 我的调度失误：两路 agent 同时改同一个 `adapter.py`。**
表情包那路的中间态（调用了自己还没写完的 `sticker.probability_from_extra`）
把测试打成 270 failed，我一度误判为抑制那路的问题。教训：**同一文件只能有一路在改**。

**验收（重启后 pid 3407065）**：onebot 全量 **724 passed / 2 skipped**
（新基线 658 + 表情 66；我此前引用的 609 是旧快照）；
生产运行时实测 9 张表情可用、概率 0.18、base64 编码 OK（`base64:///9j`，38KB）；
`_non_conversational_metadata` 对 onebot 打标记而 telegram 原样返回；
`groups=enabled`、`proactive loop started`；Traceback 0。
子智能体另拆掉一颗定时炸弹：`test_onebot_persona_binding.py` 的 9 处
`channel_prompt(...) is None` 断言在 0.18 概率下会**每 5 次挂 1 次**，
已加 autouse fixture 钉成 0；连跑 3 次稳定。
