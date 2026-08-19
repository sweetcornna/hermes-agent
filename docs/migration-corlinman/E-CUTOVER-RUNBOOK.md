# corlinman → hermes 切换手册

**编排者**: 主智能体（Orchestrator）｜**成文** 2026-08-19｜**状态**：待执行

> 本文档是**唯一的切换执行依据**。每一步都写明：做什么、如何验证、如何回滚。
> 未通过验证的步骤**不得**进入下一步。

---

## 0. 执行前必须理解的四件事

**① corlinman 仍在线上服务，且与 hermes 共用同一个 QQ 账号与同一个 NapCat 容器。**
QQ 空间账号此刻仍在每天真实发帖（生产 19 篇真实说说，`qzone_daily`/`qzone_reply`/`qzone_friends` 成功率 87–100%）。
**两侧同时启用 = 重复发帖、重复回评，对外可见且不可撤回**（D17）。

**② Telegram 侧没有这个风险。** corlinman 用旧 bot `@Cornna_bot`(5420007505)，它**不在**目标群、投递本就失败；
hermes 用新 bot `@sweetcornna2_bot`(8720715962)。物理上只有一侧能送达（D29）。

**③ `group_replies_enabled` 不是万能急停开关。**
它管：入站群回复、主动发言、出站 `send()` 的群目标（五个出口，含跨进程 cron 路径）。
它**不管**：`sanhu` / `jlu` —— 这两个投递到**私聊** `2104743984`，只由 `install_enabled=False` 拦着（E0 缺陷 #2）。

**④ 上游账号池很紧，失败请求会烧账号。** 每次失败约 30 秒自愈。
在 D42 未解决前做任何端到端验证 = 假阴性 + 平白烧池（D43）。

---

## 阶段 1：身份切换（D42/D43）—— **必须第一步，无前置**

不做这一步，后面每一步的验证结果都不可信。

### 1.1 落地身份文本

把 `$HERMES_HOME/SOUL.md`（`/opt/hermes/data/SOUL.md`）首段替换为：

```
我是格兰特利·贝尔，弗罗汀人，骑士学院的学生，虎兽人，186公分。运动、剑术、骑马、吃饭样样
来者不拒，就是读不进书、也坐不住。脾气直，看不惯的事说不干就不干；但认定的朋友，我掏心掏肺地护着。
```

**先备份**：`cp -a /opt/hermes/data/SOUL.md /opt/hermes/data/SOUL.md.bak.cutover.$(date -u +%Y%m%dT%H%M%SZ)`
（B3 已留过一份 `SOUL.md.bak.b3.20260819T035420Z`，勿覆盖）

### 1.2 验证

```bash
hermes -z "Reply with exactly this and nothing else: PONG-CUTOVER"
```

- **通过**：返回 `PONG-CUTOVER`
- **失败**：`429 All available accounts exhausted` / `503 No available accounts`
  ⇒ 身份句仍被上游拒绝。**停止，不要重试**（每次重试烧一个账号）。
  先查 `agent/prompt_builder.py::HERMES_AGENT_HELP_GUIDANCE` 里第二处 "Hermes Agent (by Nous Research)"。

### 1.3 回滚
还原备份的 `SOUL.md`。

---

## 阶段 2：插件代码部署

生产机 `/opt/hermes/repo` 目前是**上游主线**，本次迁移的三个插件**尚未部署**（B3 缺陷 #3）。
配置已按插件期望的精确形状落地，但代码不在 ⇒ 全部处于惰性状态。

### 2.1 部署

把本分支的以下目录部署到生产机：

```
plugins/platforms/onebot/     OneBot v11 平台适配器 + 主动发言 + 群历史归档 + 静音门控 + 人格绑定
plugins/grantley/             格兰人格系统（人格文档、生活事件、衰减、记忆 provider、频道绑定）
plugins/qzone/                QQ空间工具族（5 个工具 + 腾讯内容合规层）
plugins/corlinman_jobs/       9 个定时任务 + 3 个 QQ monitor 的规格/预检/安装器
tools/onebot_client.py        同步 OneBot 客户端
```

### 2.2 验证

```bash
hermes plugins list          # 四个插件应可见，状态 not enabled
```

**此时不要 enable 任何插件。**

### 2.3 回滚
删除部署的目录，`/opt/hermes/repo` 回到上游主线。

---

## 阶段 3：归档写入方冒烟测试（D48）—— **不可跳过**

D3 的全部验证在 macOS / SQLite **3.53.1** 上完成；生产是 **3.40.1 且被迫 DELETE 模式**
——**这恰恰是该设计最吃紧的地方**（每次提交一次 fsync、锁行为）。推导不能替代实测。

### 3.1 开启归档（只归档，不发言）

设 `group_history_enabled: true` + `group_history_groups`。
写入目标是**hermes 自己的库文件**，与 corlinman 的库是**两个不同文件**（D46-②），corlinman 不受影响。

### 3.2 验证

- 确认 hermes 的库文件行数在增长
- 确认 corlinman 的库文件 **mtime 与 sha256 未变**（它只被 `mode=ro` 打开过）
- 确认 hermes cgroup 内存峰值仍远低于 `MemoryHigh=384M`
- 确认 journald 未出现锁争用错误

**让它跑满 ≥1 天**再进下一阶段。

### 3.3 回滚
`group_history_enabled: false`。

---

## 阶段 4：Telegram 先行上线（D29）—— 低风险渠道实测全链路

Telegram 侧**物理上不可能重复投递**，是把 provider → 生成 → 投递整条链路暴露出来的最安全场所。

### 4.1 启用四个 Telegram 任务

`hermes.competition_daily` / `hermes.diary_summary` / `hermes.analysis_digest` / `hermes.youtube_daily`

目标 `-1003990634877`（`Corn Agents`，forum supergroup），topic `11` / `12` / `13` / `680`
——四个 topic 已验证有效，bot 为 administrator。

### 4.2 验证
逐个 `hermes cron trigger <name>`，确认消息真的落进对应 topic。

### 4.3 回滚
`hermes cron pause <name>`。

---

## 阶段 5：QQ 侧切换 —— **唯一的高风险阶段**

> **进入本阶段前，corlinman 的 qzone 三个任务必须先停。**
> 它们此刻仍在真实发帖，两侧同开会重复发布，**对外可见且不可撤回**（D17）。

### 5.1 迁移 qzone 状态目录

把 `/opt/corlinman/execution-state/` 下三个目录拷入 `$HERMES_HOME/plugin-data/qzone/`：
`qzone_post_log/` `qzone_seen_comments/` `qzone_friend_comments/`（零转换，可直接拷）

**必须设 `QZONE_PERSONA_ID=grantley`**，否则工具会去读空的 `default.json`，
**首次运行会把全部历史评论重复回复一遍**。

### 5.2 群历史回填 —— 顺序不可颠倒（D49）

```
① 先回填：backfill --days 7 --dry-run  →  正式跑  →  再跑一次，期望 inserted: 0
② 再把 QQ_GROUP_HISTORY_DB 指向 hermes 自己的库（或 unset，两者默认同一路径）
```

**顺序反了**：monitor 会读到未回填的空库，而 `send_when_empty=false` 使这种失败**静默无声**。

### 5.3 停 corlinman 的 QQ 侧
停掉 corlinman 的 qzone 三个任务与 QQ monitor。

### 5.4 分级放开 hermes 的 QQ 侧

```bash
hermes config set plugins.enabled '["onebot","grantley","qzone","corlinman_jobs"]'
hermes config set platforms.onebot.enabled true          # 仅私聊，群仍静默
hermes config set platforms.onebot.extra.group_replies_enabled true   # 唯一打开群回复的开关
```

**每一步之间停下来观察**，不要一次跑完。

### 5.5 验证
`hermes corlinman-jobs status`；逐个 `hermes cron trigger`，观察真实投递。

### 5.6 回滚
三项分别设回 `[]` / `false` / `false`。qzone 状态目录保留（不会因回滚而损坏）。

---

## 阶段 6：主动发言（**独立决策，不属于"恢复原状"**）

⚠️ **主动发言在 corlinman 生产中从未发出过一条**（被 `group_replies_enabled=false` 掐死，
7 天 journal 零 proactive 记录）。**启用它 = 引入一项生产从未发生过的对外行为**（D31）：
格兰会开始自发向 **5 个真实 QQ 群**发言。

建议首次启用时：**只开一个群**、`proactive_probability` 调低、`proactive_daily_max: 1`。

---

## 阶段 7：退役 corlinman —— **唯一的单向门**

前六个阶段每一步都可独立回滚，corlinman 的文件全程只被 `mode=ro` 打开。
**退役 corlinman 之后，回滚路径消失。**

退役前必须确认：

- [ ] 阶段 3 的归档写入方已在生产稳定运行 ≥1 天，且 monitor 能从 hermes 自己的库读出数据
- [ ] 三个 monitor 已用 hermes 的库真实跑通至少一轮
- [ ] `personas.sqlite` 的取舍已定（活库只剩 `grantley`，`lycaon`/`vivian` 只在旧目录，两份均已导出）

---

## 附：已知缺陷清单（切换时需知晓，非阻塞）

| 缺陷 | 影响 |
|---|---|
| 入站门控读构造期 router 标志 | 热静音时每次 @提及仍烧一次模型调用 |
| `sanhu`/`jlu` 投私聊，不受 `group_replies_enabled` 管 | 该开关不是这两个任务的急停 |
| 人格绑定每进程只读一次 | 改 `plugins.entries.grantley.settings.channels` 需重启 |
| `sanhu` 摘要只覆盖当天最新约 7% | 群 `980927602` 日均约 1.5 万条，单次调用上限 1000 条（D47 待实施） |
| `toolsets` 在 CLI 作用域未生效，实际带 27 个工具 | B1 遗留，对内存与 token 均有影响 |
| 四个 Telegram 任务的 prompt 中 `qzone_reply` 属 RECONSTRUCTED | 从未对真实 QQ 会话跑过 |
| 读回的 QQ 空间文本未做提示词注入过滤 | 源系统亦无此防护 |
| `evolution.darwin_curate` / `system.update_check` / `grantley.qzone_reply` 未迁移 | 弃迁理由见 `specs.py::DROPPED_JOBS` |
