# T4 修订单（打回重修）

**处置：打回，当前版本不得执行迁移。** 审计结论见 `docs/orchestration/audits/T4.md`
（gpt-auditor 异构第二意见，已含主线程复核与严重度调整）。

## 致命（全部必修）

- **F2** 设备身份目录 — 新机 `/opt/napcat/ntqq` 非空必须**硬阻断**。禁止 `mv`/`rm`/重建。
  是复制品也只能隔离并报告。这是 H1 的硬约束 2，没有例外。
- **F3** §3.5 五条放行闸 — 主线程已核对 `T4:600-604`：`\|\|` 与 `\|` 是**字面反斜杠**，
  粘贴执行时 shell 把 `\|` 当字面字符，预期的 `DEAD` / `0` 不会可靠产生。
  移进 fenced code block 用真管道；第 3 行不能只跑 `pgrep`，最终闸门必须同时验证
  完整进程树 + 3011 + 6099 + 容器 + cgroup；用 `if ...; then exit 1; fi` 硬阻断，不靠人眼看输出。
- **F4** preflight 失败开放 — `docker ps` / `ss` 失败被 `2>/dev/null` 吞掉后仍打印 `OK`。
  改成失败关闭；需要 root 的检查拆成 root-owned 只读 helper；**不要把 `napcat-native` 加进 `docker` 组**。
- **F5** WebUI token 被打印到终端 — 主线程确认成立。文档里的 ⛔ 警告不算数，
  「打印到终端」本身就是硬约束 10 禁止的「输出」。改成只检查字段存在非空
  （如 `WEBUI_TOKEN_PRESENT=true`），删掉打印 token 和打印整个 `webui.json` 的兜底命令。

## F1：主线程已下调为「中」，按下面的口径处理

审计员把硬约束 5「不得设计自动重启/自动重登/自动密码登录」读成了禁止 systemd 的 `Restart=`。
**这是过度解读**——那条约束针对的是 watchdog 模式（检测异常 → `systemctl restart`，即已下线的
`napcat-watch.py`），而 **H1 §10 恰恰建议**把 `Restart=always` 改成「`on-failure` + 合理退避」。
`StartLimitIntervalSec=3600` + `StartLimitBurst=1` = 每小时至多一次自动重启，之后进入 `failed`
且必须人工 `reset-failed`——**这本身就是人工闸**。

⇒ **保留 `Restart=on-failure` / `RestartSec=45s` / `StartLimitBurst=1`，不要改成 `Restart=no`。**

只需处理保留的有效部分：**开机 `enable` 的取舍要显式讨论**（机器重启后是让 QQ 自己回来发起一次登录，
还是停在那里等人），给出推荐与理由，不要默认 enable 一笔带过。

## 高

- **F6** T3 采集器**当前不存在**（`ops/qq_sentinel/` 无文件，`results/T3.md` 无文件，
  `T3-design.md` 自己写着「待实现」）。停旧实例前硬性要求 T3 已实现+部署+selftest 通过；
  baseline 放行条件改为 T3 的真实前置：哨兵连续运行 ≥7 天、`epoch=datacenter` 非删失区间 ≥10 段、
  `A0_SOURCE_INCOMPATIBLE` 已清除。**`wc -l ≥1` 不是闸门**，禁止 marker 或旧历史满足它。
- **F7** 账本 schema 冲突 — **T3 的契约是权威，T4 适配它**：用
  `epoch=datacenter|home_broadband`（不是 `env=cloud-aliyun-gz/home-residential`）、
  `report --compare`（不是 `report --group-by env`）、session 记录含 `schema`/`duration_s`/`epoch`。
  不要手写 ledger 不支持的 marker 行。begin/done 两条 marker 都要给完整可验证命令。
- **F8** watchdog 核验 — 实测现状是 `napcat-watch.service` / `qq-watchdog.service` / `qq-watchdog.timer`
  三者**均 masked**，`napcat-native-health.timer` enabled（只读探针，不负责恢复，**是合法的**）。
  当前 `|| echo NO_TIMERS` 期望 `NO_TIMERS` 会把合法的 health timer 判成异常。
  改成逐个核对三件套 `inactive` + `masked`；允许 health timer enabled 但检查其 ExecStart 无恢复动作；
  遇到 active watchdog 先 `disable --now` 再 `mask`，验证 `is-enabled` 返回 `masked`。
- **F9** Docker 回退路径挂错 config 目录（native 写 `/opt/napcat-native/config-candidate`，
  Docker 挂 `/opt/napcat/config` 且从未创建填充）；`ExecStartPre=-/usr/bin/docker rm -f napcat`
  无备份删容器。统一配置源；删掉自动 `docker rm -f`，改为发现同名容器时先停止并阻断。
- **F10** §10 仍引用**不存在的** `napcat.service`——全部改成 `napcat-native.service`，
  并区分 active unit 实际读的 `/etc/napcat-native/env` 与 Docker 时代残留的 `/etc/napcat/napcat.env`。
- **F11** kicked 检查的 `|| true` 会把 journalctl 失败伪装成「0 次 kicked」。分离采集与匹配两个状态，
  删掉 `|| true`，先验证日志可读再解释计数。

## 中 / 低

F12（用户组创建顺序）、F13（`egress_ip_changed` 缺 timer 与账本集成，且采集失败 `exit 0`）、
F14（marker 与 UFW 的备份在动作之后；`sed -i` 违反 append-only）、F15（`permitlisten` 验证漏
`ExitOnForwardFailure=yes`）、F16（Hermes active 后不得再开第二条 WS——探针必须在 Hermes 启动前跑）、
F17（macOS 附录不是可执行路径：要么补齐，要么明确删除 macOS 作为受支持路径）。

**F16 额外强调**：T3 审计的 F-01 是同一类错误。这条铁律要写成可测试断言，不能停在口头约定。

## 交付

就地修订 `docs/orchestration/tasks/T4-home-migration-runbook.md`；修订前先
`cp -a docs/orchestration/tasks/T4-home-migration-runbook.md docs/orchestration/tasks/T4-home-migration-runbook.md.bak-pre-rev-$(date -u +%Y%m%dT%H%M%SZ)`。
另写 `docs/orchestration/results/T4-rev.md`，逐条列出每个 finding 的处置与改动位置。
边写边落盘，不要攒到最后一次性输出。
