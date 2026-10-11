# T3 修订结果 — `hermes-qq-sentinel` 设计

修订对象：`docs/orchestration/tasks/T3-design.md`（修订前 1034 行）
修订单：`docs/orchestration/tasks/T3-rev.md`
审计：`docs/orchestration/audits/T3.md`
备份：`docs/orchestration/tasks/T3-design.md.bak-pre-rev-20260831T102513Z`（`cp -a`，UTC 时间戳）

本文件边改边写，逐条记录处置。**状态**取值：已修 / 已降级（改措辞）/ 不修（附理由）。

## 处置总表

| Finding | 严重度 | 处置 | 主要改动位置 |
|---|---|---|---|
| F-01 探针触发条件挡不住新 Hermes 竞态 | 致命 | **已修**（删除自动探针，降级为手动子命令 + 三份物证 + 两条时间闸 + 可测试断言） | §3.3 整节重写、§2、§9 T3 |
| F-02 接口白名单没有运行时强制 | 高 | **已修**（唯一封装 `probe_get_status()`；`ONEBOT_HTTP_URL` 拒绝；`prefer_live=False`；出网限制转必选） | §8.2 / §8.2.1 / §8.2.2 / §8.3 |
| F-03 静态扫描不是权限边界 | 高→**降级** | **已降级**（按修订单只改措辞；顺手采纳三条低成本收紧） | §8.1 / §8.5 |
| F-04 `writer_alive` 语义不准 | 高 | **已修**（三值 truth table + 身份校验；额外发现 `ProtectProc=invisible` 会让判据失效） | §3.2.2 / §4.1 / §8.3 |
| F-04b 未来时间戳被判新鲜 | 高 | **已修**（有符号 age 三分类 + `CLOCK_SKEW_TOLERANCE_SECS`） | §3.2.3 |
| F-05 24h 闩锁与进程代际无关 | 高 | **已修**（闩锁绑代际、代际内永不过期；新增 `unknown` 第三态；补 A0 确认拍数） | §4.1.2 / §4.1.3 |
| F-06 账本用原始采样切段、`link_down` 非删失 | 高 | **已修**（账本改用确认后转换；`link_down` 转删失；保留原始采样） | §10.2.1 |
| F-07 unknown 被计入会话时长 | 高 | **已修**（告警视图/测量视图分开；区间须全程 online 证据） | §10.2.2 / §10.2.3 |
| F-08 停机/缺口/epoch 切换无区间语义 | 高 | **已修**（`last_sample_at` + 缺口删失；`max_gap_s` 改实测；epoch 切换事务） | §10.2.4 / §10.4.1 |
| F-09 迁移门槛在成功时无法满足 | 高 | **已修**（pilot 闸门与统计结论分开；零事件 rule-of-three 判定；30 天上限） | §10.6.1 / §10.6.2 |
| F-10 `n<10` 防呆没覆盖所有入口 | 中 | **已修**（统一 `summarize()`；修正无 CLI 片段） | §10.5 / §10.5.1 |
| F-11 告警疲劳与消息预算 | 中 | **已修**（计数定义写死；9.7h 场景 8→5 条；汇总计入上限；新增 flapping 退避；恢复去对称化） | §5.2 / §5.3 / §5.5 / §6.1 |
| F-12 通知超时与心跳无契约 | 中 | **已修**（`NOTIFY_TIMEOUT_SECS` 等 + 一拍时间预算核算 + 先落盘再发送 + 心跳幂等契约） | §9.0 / §7.5.1 |
| F-13 hint 只拉一次 | 中 | **已修**（三个触发点 + 限流 + **hint 升级主动播报**） | §6.3.1 |
| F-14 H1 三次窗口的表述过度概括 | 低 | **已修**（改为逐条标注性质的表格 + 审计建议的表述） | §7.3 |

**全部 15 条均已处置，无「不修」项。** F-03 按修订单降级为措辞问题。

---

## 0. 修订前的独立事实复核

修订单点名「不得辩驳」的三条，我在磁盘上又核了一遍，**全部成立**，并且额外发现一条与之直接相关的部署级冲突（见 F-04 处置）。

| 断言 | 复核方式 | 结果 |
|---|---|---|
| F-02 `onebot_base_url()` 优先 `ONEBOT_HTTP_URL` | `sed -n '57,66p' tools/onebot_client.py` | **成立**。docstring「Prefers ``ONEBOT_HTTP_URL`` and falls back to ``ONEBOT_WS_URL``」，实现 `url = os.getenv("ONEBOT_HTTP_URL", "").strip()`，空才回落 WS |
| F-04 `runtime_status_pid_is_live()` 任一 start_time 为 None 即 `return True` | `sed -n '1168,1188p' gateway/status.py` | **成立**。`if (recorded_start is not None and current_start is not None and current_start != recorded_start): return False`，其余路径直接 `return True` |
| F-04b `_marker_is_stale()` 负 age 判为新鲜 | `sed -n '1716,1723p' gateway/status.py` | **成立**。`age = (datetime.now(timezone.utc) - written_dt).total_seconds()` / `return age > ttl_s`，未来时间戳 age < 0 ⇒ False（不陈旧） |

额外核实（用于写修订内容，不是新 finding）：

- `gateway/status.py:346-378` `_get_process_start_time()` 在 `/proc/<pid>/stat` 不可读时（`PermissionError` / `FileNotFoundError`）返回 `None`，然后 fallback 到 psutil。
- `gateway/status.py:627-638` `_build_pid_record()` 写 `pid` / `kind="hermes-gateway"` / `argv` / `start_time` / `hermes_home`；`write_runtime_status()`（`:1073-1077`）把这四个字段无条件盖进 `gateway_state.json`。
  ⇒ `gateway_state.json` 里**有** `kind` 和 `argv`，设计原来没用它们做身份校验，是白白丢掉的证据。
- `gateway/status.py:223-234, 1012-1035, 920-960` 另有两个独立的 gateway 身份物证：`${HERMES_HOME}/gateway.pid` 与 `${HERMES_HOME}/gateway.lock`（后者由 `fcntl.flock(LOCK_EX|LOCK_NB)` 持有）。
- `gateway/status.py:977-1010` `is_gateway_runtime_lock_active()` 用 `open(path, "a+")` + 抢 `LOCK_EX` 来探测锁 —— **哨兵不能用它**：需要写权限（unit 里 `/opt/hermes/data` 是 `ReadOnlyPaths`），且抢锁本身会和正在启动的 gateway 竞争。这条约束写进了修订后的 §3.2.2。
- `tools/onebot_client.py:113-138` `onebot_call(prefer_live=True)` → `_try_live_client()` → `from plugins.platforms.onebot.client import get_live_client`。
  ⇒ 设计原文的 `prefer_live=True` 与 §10.1「生产代码不 import 任何 Hermes 模块」**自相矛盾**，修订里改成 `prefer_live=False`。
- `plugins/platforms/onebot/adapter.py:1968-2016` 四个 `error_message` 前缀字面量与设计 §6.4.1 **一致**，无需改。
- `gateway/run.py:12723-12733` 连接成功时确实写 `platform_state="connected", error_code=None, error_message=None`，F-05 的担心属实。

---

## 1. 逐条处置

（按 finding 编号；每条给出「状态 / 改法 / 改动位置」。改动位置指修订后的 `T3-design.md` 章节号。）

<!-- 以下小节随修订进度追加 -->
### F-01 短连接触发条件不能排除 Hermes 正在启动或已重启 — **已修（按审计的兜底选项：删除自动探针）**

**改法**：不是「把判据修窄」，而是**把 `run` 路径上的 OneBot 出网分支整个删掉**，
探针降级为运维手动子命令 `python3 -m ops.qq_sentinel probe --confirm-hermes-down`。

理由（写在 §3.3.2，含被放弃方案的错处）：

1. 判据修不到零竞态 —— 「证明此刻没有 gateway」和「建连」之间永远有 TOCTOU 窗口。
2. **更关键：初稿高估了探针的价值。** 初稿说「探针让我们能把区间标成 `end_censored=true`
   而不是伪造一个 `kicked`」——**这句话是错的**。`end_censored` 由 `OBSERVER_DOWN` 观测态本身决定
   （§10.2），根本不需要问 NapCat。探针的返回值不进入任何统计字段，只影响一句告警文案。
   为一句文案冒违反最硬边界的风险，是坏交易。

手动子命令仍受三份物证 + 两条时间闸把守（`gateway_state.json` / `gateway.pid` / `gateway.lock`
任一不是 `DEAD` 即拒绝；`PROBE_GATEWAY_ABSENT_GRACE_SECS = 600`；`PROBE_MIN_INTERVAL_SECS = 300`），
拒绝时退出码 3 并指明是哪份物证挡的。

**可测试断言**（修订单额外强调的那条）已写进 §3.3.3：
`test_run_path_never_probes` —— 旧状态文件（PID 已死）+ 新 Hermes 已启动未刷新，跑 20 拍，
断言探针调用次数 `== 0`；外加 boundary 测试断言 `ops.qq_sentinel.run` 的传递闭包里
不出现 `onebot_call` / `websockets`（`probe.py` 除外）。

**顺带修正的一个错误**：初稿建议用 `gateway.lock` 时会想到 `is_gateway_runtime_lock_active()`——
该函数 `open(path,"a+")` 并抢 `LOCK_EX`（`gateway/status.py:977-1010`），
①需要写权限而 unit 里 `/opt/hermes/data` 是 `ReadOnlyPaths`，②抢锁会和正在启动的 gateway 竞争。
修订后明确：**只读解析 lock 文件的 JSON 内容，绝不 flock**。

**改动位置**：§0 rev-1 变化清单第 1 条、§2 组件图、§3.3（整节重写为 3.3.1~3.3.4）、
§9 测试表 T3 / T6、§11.5、§12.1 文件清单（新增 `probe.py`）、§12.2 常量表。

### F-04 `writer_alive` 语义不准 + 未来时间戳 — **已修**

**改法**：布尔 `writer_alive` → 三值 `WriterState ∈ {LIVE, DEAD, UNVERIFIABLE}`，
并写出审计要求的完整 truth table（9 行，覆盖 PID 缺失 / 进程不存在 / 任一 start_time 缺失 /
不匹配 / `kind` 不符 / `argv` 不像 gateway / cmdline 不可读）。

关键判断：**故意与上游 `runtime_status_pid_is_live()` 不同**。上游在 `start_time` 缺失时
`return True`，那个偏向对 gateway 自己（`--replace` 接管决策）是安全的，对哨兵是反的
（会漏报 `A3_OBSERVER_DOWN`）。这一点写进了 §3.2.2，并要求 T5 契约测试**同时**钉住
「上游是这个行为」和「我们故意不是」。

**额外发现（本次修订新加，不在审计清单里）**：初稿 §8.3 的 unit 写了 `ProtectProc=invisible`，
这会让哨兵读不到 `/proc/<hermes pid>/stat`，而 `gateway/status.py:346-378` 在读失败时返回 `None`
⇒ `start_time` **永远**是 `None` ⇒ PID 复用防护在生产上等于没有。
初稿等于亲手把自己的判据关掉了。§8.3 已改为 `ProtectProc=default` 并加了验证命令。

同时补上 `UNVERIFIABLE` 的完整行为表（modern+fresh ⇒ 正常判定；modern+stale ⇒ `OBSERVER_DOWN`；
legacy/unknown ⇒ **不**升级为 `OBSERVER_DOWN`，因为 legacy 陈旧是正常的），
并新增 `A4_WRITER_UNVERIFIABLE` 告警：判断不了要说出来，但每 24 小时说一次。

**改动位置**：§3.2.1（废弃初稿伪码）、**§3.2.2 新增**、§4.1 判定表 + `UNVERIFIABLE` 旁路表、
§4.1.1 场景表第 8/9/10 行、§4.2（A4）、§5.2（`A4_CONFIRM_TICKS`）、§8.3、§9 T2/T5、§12.2。

**顺带修正的一处事实错误**：初稿字段清单里的 `platforms.onebot.writer_pid` /
`writer_start_time` 我一度以为不存在 —— 复核后确认**存在**（`gateway/status.py:1122-1123`），
但 §1.2 的生产实测样本里**没有**这两个键。所以修订后写明：有就用条目级的，
没有就回落顶层 `pid`/`start_time`，并记录用了哪一种。

### F-04b `_marker_is_stale()` 负 age 判为新鲜 — **已修**

**改法**：新增 §3.2.3，`state_age_secs()` 返回**有符号**秒数，三分类
`FRESH` / `STALE` / `IMPLAUSIBLE_FUTURE`（`age < -CLOCK_SKEW_TOLERANCE_SECS = -120`）。
`IMPLAUSIBLE_FUTURE` **按 STALE 处理**并触发一次 A4。
§9 的 T2 必须有一条 `updated_at = now + 1h` 的负例断言结果是 `STALE`。

**改动位置**：§3.2.3 新增、§4.1 判定表（`state_age != FRESH`）、§4.1.1 场景表第 13 行、
§4.2（A4 触发条件）、§9 T2、§12.2（`CLOCK_SKEW_TOLERANCE_SECS`）。

### F-05 24h source-format 闩锁与进程代际无关 — **已修**

**改法**：闩锁的键从「墙钟 24 小时」改成 **gateway 代际** `(writer_pid, writer_start_time)`，
代际内 `modern` 一旦证实**永不过期、永不回退**，代际变化则**完全不继承**。
同时把 `source_format` 从二值改成**三值**（新增 `unknown`），并给 A0 补上确认拍数。

- 失败模式 1（回滚到旧 adapter）：新代际 ⇒ 重新判定 ⇒ 不再误报 `OBSERVER_DOWN`。
- 失败模式 2（health loop 卡死 > 24h）：代际内 modern 永不过期 ⇒ 陈旧判据继续生效 ⇒
  `OBSERVER_DOWN` 照常发。**哨兵不再在最需要时闭嘴。**
- 失败模式 3（无 `SOURCE_INCOMPATIBLE_CONFIRM_TICKS`）：补 `= 5`。

`unknown` 期间的能力矩阵是这条修法的关键，也是我对审计建议的一处**加强**：
审计只说「新代际用独立的『格式未知』状态」，没说 unknown 期间还能不能告警。
我的判断是**必须能**：`auth_required` 是**正证据**（只有新 adapter 写得出，`adapter.py:1979-1981`），
看见它就同时证明了格式和事实。所以 unknown 期间 A1/A2 保持启用，
只停用「基于 updated_at 陈旧的 OBSERVER_DOWN」和 A0。
否则每次 gateway 重启后的 15 分钟宽限期里，哨兵都会对被踢视而不见。

因此 §4.1 判定表的顺序也改了：**`AUTH_REQUIRED` 提到 `SOURCE_INCOMPATIBLE` 之前**。

**改动位置**：§4.1（前置量 + 判定表顺序）、§4.1.1（场景表 11/12/14/15 行）、
**§4.1.2 整节重写**、**§4.1.3 新增**、§4.2、§5.2、§6.4.3、§12.2。

### F-11 告警疲劳与消息预算 — **已修**

**改法**四条：

1. **写死计数定义**：首次告警 = 第 1 个冷却窗口（初稿 7/8 条歧义的根源）。
2. `COOLDOWN_ESCALATE_AFTER` 6 → **3**。逐条时间线写进 §5.3 表格：
   9.7h 场景 **5 条**（初稿 8 条），11.7h 场景 **5 条**。这张表就是 golden test 的期望值。
3. **汇总消息计入 `GLOBAL_HOURLY_CAP`**（初稿没说，上限实际是 7 条/小时）。
4. **新增 §5.5 episode 级退避 + `A5_FLAPPING`**：1 小时内 ≥3 次往返即进入 flapping 模式，
   停发逐次 A1/A2/恢复，改为每 6 小时一条汇总。这是审计点名的「10 分钟掉线/3 分钟在线」场景。
5. 恢复通知去对称化：`RECOVER_CONFIRM_TICKS` 3 → **10**，抖动后 → **30**。
   理由写在 §6.1：升起晚了人晚知道 3 分钟，恢复发早了人会**停止关注**——代价不对称，参数就不该对称。

**改动位置**：§4.2、§5.2、§5.3（含时间线表）、**§5.5 新增**、§6.1、§9 T1、§12.2。

### F-13 hint 只在首次 A1 确认时获取 — **已修**

**改法**：新增 §6.3.1，三个拉取触发点（首次确认 / episode 进行中且 hint 过期或仍是
`unknown`·`kicked` / 每次冷却重发前），限流 `HINT_MIN_INTERVAL_SECS = 600`，
单 episode 上限 `HINT_MAX_FETCHES_PER_EPISODE = 12`，episode 结束清零，episode 之外永不拉取。

**加了一条审计没要求但问题 4 真正需要的**：hint 从 `unknown`/`kicked` **升级**为
`sms_verify`/`duplicate_login_guard` 时，立即发一条不受冷却约束的更新告警（每 episode 一条）。
理由：**人的下一步动作变了，就必须再说一次**。否则「有 hint 字段」和「问题 4 被闭合」是两回事。

**改动位置**：§6.3（时序表述）、**§6.3.1 新增**、§9 T4、§12.2。

### F-02 接口白名单只是常量，实际调用路径没有强制 — **已修**

**改法**：四张 allowlist 常量**降级为文档 + 防回归断言**（保留，明确写「它们不强制任何东西」），
强制层改为 **§8.2.1 的唯一运行时封装 `probe.probe_get_status()`** + §8.3 的内核沙箱。

封装的硬契约（五条，写在设计里的 docstring 形式）：
1. action 是**字面量** `"get_status"`，不是参数 —— 「用别的 action 调用它」在类型层面不可表达；
2. `ONEBOT_HTTP_URL` 非空即拒绝（`onebot_base_url()` 优先读它，这是审计点名的洞）；
3. `urlsplit` 逐项校验 scheme=`ws` / host∈{127.0.0.1,::1} / port==3011 / path 为空；
4. **`prefer_live=False`** —— 初稿写 `prefer_live=True`，而 `_try_live_client()` 会
   `from plugins.platforms.onebot.client import get_live_client`（`tools/onebot_client.py:146-155`），
   与 §10.1「生产代码不 import 任何 Hermes 模块」直接矛盾。这条是本次修订自己发现的；
5. 调用后再断言 `onebot_base_url()` 仍解析为那个被校验过的 URL（**运行时**断言，不是常量字面值断言）。

附 6 条负例测试表（含 `ws://127.0.0.1:6099` 必须被拒 —— 那正好是 WebUI 端口）。

**另补一个初稿没写的坑**：哨兵从 `.env` 读配置，而 `onebot_call()` 从 `os.environ` 读。
封装内部用 contextmanager 只注入校验过的两个变量并**删除** `ONEBOT_HTTP_URL`，调用后恢复；
⛔ 禁止 `load_dotenv` 整份 `.env` 进 `os.environ`。

**出网收窄从「可选推荐」升级为必选部署条件**（§8.2.2 / §8.3 / §9 T11 硬项），按审计要求。

**改动位置**：§8.2 抬头重写、**§8.2.1 / §8.2.2 新增**、§8.3（`IPAddress*` 转必选）、
§8.4 补「证据强度不一样」的注、§9 T3/T11、§12.1（新增 `probe.py`）。

### F-03 静态扫描不是权限边界 — **已降级（按修订单，只改措辞）**

修订单把本条下调为「表述问题」，采纳审计员自己最后那条建议。

**改法**：§8.5 标题从「第四层：把禁令写成仓库里的一个测试」改为
**「静态扫描 —— 防回归检查，不是权限强制层」**，正文明写审计给的绕过示例全部成立，
并说明它真正的用途（面对未来的我们，不是攻击者）。

顺手采纳了审计「修」段里成本极低的三条（不属于重新设计）：
1. **取消整行豁免** → 改为只允许 `hint.py::_fetch_login_hint()` 一个专用函数用 subprocess，AST 检查；
2. 扫描范围加上 `systemd/*.{service,timer}`（`ExecStartPre=` 之类同样失败）；
3. 断言 `ops.qq_sentinel` 的**传递 import 闭包**只有标准库 + 自身 + `tools.onebot_client`
   （后者仅限 `probe.py` 闭包）——这条同时兜住 F-01 的「run 路径无 OneBot 符号」。

**改动位置**：§8.5 整节、§8.4 表格后的注、§0 一句话结论（不再把静态扫描与沙箱并列为"共同保证"）。

### F-14 对 H1「三次账号出事时主动停 Hermes」的表述过度概括 — **已修（措辞）**

**改法**：§7.3 改为三行表格，逐条标注性质与出处（H1 §8.5 账号掉线处置 / §7.2 切主 / §6 staged 观察期），
并明写「只有 §8.5 是账号掉线处置」。结论改为审计建议的表述：
**Hermes 有三个已记录的非运行窗口，其中一个恰好发生在账号掉线之后；这足以证明告警器不能依附 Hermes 生命周期。**
架构结论不变。

**改动位置**：§7.3。

### F-12 通知超时与每日心跳没有形成可执行契约 — **已修**（心跳部分见 §7.5.1，超时部分见 §9.0）

**改法（心跳）**：新增 §7.5.1。不加第二个 timer，用 `state.json` 里的
`heartbeat.last_sent_local_date` + `attempts_today` 做幂等；错过窗口**照发并标注延迟**
（「延迟本身就是信息」）；失败最多重试 3 次；不补发历史；计入 `GLOBAL_HOURLY_CAP`。
夏令时：`Asia/Shanghai` 自 1991 年起无 DST，明确不需要处理；tzdata 缺失时回落 UTC+8 **并在文案里说**。

**改法（超时与持久化顺序）**：见下一节 §9.0 时间预算（`NOTIFY_TIMEOUT_SECS` 等）。

### F-06 账本按原始采样切段、`link_down` 被当成非删失结束 — **已修**

**改法**（§10.2.1）：

1. 账本改用 `confirm.py` **同一份**去抖判定切段（§2 的图里去抖被提到分叉点之前）。
2. **时间戳取触发确认的那一串采样的第一拍**，不是确认完成时刻 ——
   否则每段区间被人为拉长 `CONFIRM_TICKS × 60` 秒（A2 是 10 分钟，不是误差级别）。
   这一条初稿和审计都没点到，是实现时最容易搞反的地方。
3. **`link_down` 改为删失**。理由写在设计里：OneBot 链路断开时 QQ 账号很可能仍在线
   （隧道重连、NapCat 内部重初始化都断链路不掉账号），当成会话结束会**系统性低估会话时长**。
4. **只有连续确认的 `AUTH_REQUIRED` 是唯一的非删失结束。** 其余一切结束原因都只证明
   「我们看不见了」，不是「账号下线了」。
5. 原始采样保留到 `raw_transitions.jsonl`（append-only，只在原始态变化时写），
   区间记录里加 `raw_transitions` 计数 —— 审计要求的「原始与确认后都保留」。

**改动位置**：§2 组件图、§4.3 末条加注、**§10.2.1 新增**、§10.2 记录示例（`schema` 1→2）、§9 T7。

### F-07 unknown 被折叠进 ONLINE 后进入了会话统计 — **已修**

**改法**（§10.2.2）：**告警视图与测量视图分开**，这是审计的原话，也是修法的核心。

- 告警视图**不变**：unknown 仍然不告警（任务硬要求）。
- 测量视图：区间只能从 `account_evidence == "online"` 的采样**开始**；
  从 unknown 起步的区间，`started_at` 取第一个 online 证据时刻并置 `start_censored=true`；
  中途出现 unknown 不切段（那会造假短会话）但整段标 `account_evidence="mixed"` 并排除出中位数。
- `account_evidence` 从**标量**升级为**全段口径**（`online`/`mixed`/`unknown`/`null`），
  并加 `evidence_online_samples` / `evidence_unknown_samples` 两个计数
  —— 审计指出的「单个标量表达不了区间中途由 unknown 变 online」。
- 报告过滤条件从「非删失」改为「非删失 **且** `account_evidence == "online"`
  **且** `verification_degraded == false`」。

一句话写进设计：**不告警的理由是「不确定」，而不确定的东西同样不能进中位数。**

**改动位置**：§4.1（`ONLINE` 命名段重写，两视图对照表）、**§10.2.2 / §10.2.3 新增**、
§10.2 记录示例、§10.5 报告过滤、§9 T7、§11.2。

### F-08 哨兵停机 / 采样缺口 / epoch 切换没有区间语义 — **已修**

**改法**（§10.2.4 + §10.4）：

- 每拍持久化 `last_sample_at`；`gap > MAX_SAMPLE_GAP_SECS = 180` ⇒
  用 `last_sample_at`（最后一次真的看见的时刻，**不是 now**）关闭旧区间为
  `end_reason="sample_gap"` / `end_censored=true`，新区间 `start_censored=true`。
- `max_gap_s` 改为**实测最大相邻采样间隔**（初稿写死 60，是常量不是观测值，永远暴露不了漏拍）。
- 时钟倒退 `gap < 0` 按缺口处理（与 §3.2.3 的取向一致：负数不当"正常"）。
- epoch 切换做成**显式事务**（§10.4）：检测到 `HERMES_QQ_SENTINEL_EPOCH` 与持久化值不同 ⇒
  先关闭旧 epoch 的活动区间（`end_reason="epoch_change"`, `end_censored=true`）再开新的。
  **区间永不跨 epoch**，`epoch` 字段取值仍是锁定的两个。
- 新增两个 `end_reason` 值 `sample_gap` / `epoch_change`（**新增，不是重命名**；
  两者都恒为删失，不影响被锁定的 `schema`/`duration_s`/`epoch` 契约）。

**改动位置**：**§10.2.4 新增**、§10.4 重写、§10.2 记录示例、§9 T7。

### F-09 迁移门槛在迁移成功时永远无法满足 — **已修**

**改法**（§10.6.1 / §10.6.2）：pilot 闸门与统计结论分开。

- **迁移前**闸门不变（7 天 + 10 段完成区间）——那些失败事件是自然发生的，不需要等坏事。
  只补了两条：区间要按 §10.2.2 的证据口径算，且 `A4` 必须已清除。
- **迁移后**改为四行判定表（先命中先返回）：①两侧 n≥10 走中位数比较；
  ②`failures == 0` 且暴露时间足够 ⇒ **零事件置信上界判定**；
  ③到 30 天上限仍不足 ⇒ 输出三元组由人决定；④其余为「观察中」并打印还差多少。
- 零事件判据用 **rule of three**：`λ_upper = 3/T`，若 `3/T < λ₀/2` 则「显著改善（95% 单侧）」。
  举例写进设计：基线每 2 小时掉一次（λ₀=0.5）⇒ **连续在线 12 小时零掉线**即可下结论。
- `POST_MIGRATION_MAX_WAIT_DAYS = 30` 超期兜底：输出「无足够事件，但也没有观测到失败」并停止等待。
  设计里写明这条是为了防止「等到有结论为止」变成「等到出事为止」。

**放弃的方案**：Kaplan–Meier 完整生存分析。统计上更完备，但要在一个只用标准库、
跑在 oneshot 里的组件中实现生存曲线与置信带；对「2 小时还是 8 小时」这个量级问题，
复杂度换不到实际帮助。rule of three 只需要两个数、可手算复核。
**判断依据的可复核性在这里比统计效率重要。**

**改动位置**：§10.5（报告必须输出 exposure/failures/censored 六项）、**§10.6.1 / §10.6.2 重写**、
§11.8 部署顺序图、§12.2 常量表。

### F-10 `n < 10` 防呆没覆盖所有报告路径 — **已修**

**改法**（§10.5.1）：所有报告入口共用同一个 `summarize()`，防呆写在函数里而不是某条 CLI 分支里
（写在分支里的规则，下一个分支就会忘）。`0 ≤ n_complete < 10` ⇒ 所有位置统计量输出
「样本不足（n=X），不下结论」；exposure/failures/censored 照常输出（它们是事实不是结论）。
覆盖 `--compare`、`--epoch`、每日心跳日报、恢复通知时长播报。
`--include-censored` 每行强制加前缀 `[含删失，不可用于结论]`。

**无 CLI 片段没有删除，而是修正**（它在 CLI 坏掉时确实有用）：加了 `MIN_N` 门槛、
`account_evidence == 'online'` 过滤、`verification_degraded` 保守默认，并输出
exposure/failures/censored。初稿那段对 `n=1` 会直接打印 median。

**改动位置**：§10.5 输出项表、**§10.5.1 新增**、shell 片段重写、§9 T7。

---

## 2. 契约锁定确认（修订单 §契约锁定）

修订单要求「本次修订不得改动这套契约的字段名和命令形态」。**逐条确认未改**：

| 锁定项 | 状态 | 说明 |
|---|---|---|
| `epoch=datacenter\|home_broadband` | **未改** | 只补了初稿留空的「切换时刻怎么处理」（§10.4.1 事务） |
| session 记录含 `schema` / `duration_s` / `epoch` | **未改** | 字段名与语义不变；`schema` 的**值**从 1 提到 2 |
| CLI `report --compare` / `report --epoch ...` | **未改** | 新增的 `probe` 是另一个子命令，不碰 report |
| `sessions.jsonl` append-only | **未改** | 新增的 `raw_transitions.jsonl` 同样 append-only |

**新增（不是重命名/删除）的东西**，因为审计的 F-06/F-07/F-08 在不加字段的情况下无法修正：

- 记录字段：`evidence_online_samples` / `evidence_unknown_samples` / `verification_degraded` /
  `generation_source` / `raw_transitions`；`account_evidence` 语义从「标量诊断」升级为「全段口径」。
- `end_reason` 新增两个取值：`sample_gap` / `epoch_change`（都恒为删失）。
- 新增 CLI 子命令 `probe`（手动，F-01 的落点）。

若主线程认为「新增字段/新增 end_reason 取值」也算改动契约，这三处可以退回：
代价分别是 F-07 无法过滤 mixed 证据、F-08 无法区分缺口与 epoch 切换、F-01 只能整条删掉手动探针。
**我判断新增是被允许的**（锁定的是「字段名和命令形态」，不是「不许增加」），故按新增执行，在此明示。

写入端与读取端的 `schema` 兼容规则已写进 §10.2：读取端按 `schema` 分支，
schema 1 的旧行缺字段时一律按最保守方式处理（`account_evidence` 缺失 ⇒ 未确认；
`verification_degraded` 缺失 ⇒ `true`）。

---

## 3. 本次修订的真实验证输出

这是一次**纯文档修订**，没有改动任何生产代码或测试，因此没有「测试通过」可报。
下面是我为核实审计事实断言而实际执行的命令与原始输出（未 SSH、未碰生产）。

```
$ sed -n '57,66p' tools/onebot_client.py
def onebot_base_url() -> str:
    """The configured OneBot endpoint (no trailing slash).

    Prefers ``ONEBOT_HTTP_URL`` and falls back to ``ONEBOT_WS_URL`` so a
    WebSocket-only deployment configures just the one URL it has.
    """
    url = os.getenv("ONEBOT_HTTP_URL", "").strip()
    if not url:
        url = os.getenv("ONEBOT_WS_URL", "").strip()
    return url.rstrip("/")
```
⇒ **F-02 成立。**

```
$ sed -n '1176,1188p' gateway/status.py
    """
    pid = _pid_from_record(record)
    if pid is None or not _pid_exists(pid):
        return False
    recorded_start = (record or {}).get("start_time")
    current_start = _get_process_start_time(pid)
    if (
        recorded_start is not None
        and current_start is not None
        and current_start != recorded_start
    ):
        return False
    return True
```
⇒ **F-04 成立**（任一 start_time 为 `None` 时跳过比对，直接 `return True`）。

```
$ sed -n '1716,1722p' gateway/status.py
def _marker_is_stale(written_at: str, ttl_s: int) -> bool:
    try:
        written_dt = datetime.fromisoformat(written_at)
        age = (datetime.now(timezone.utc) - written_dt).total_seconds()
        return age > ttl_s
    except (TypeError, ValueError):
        return True
```
⇒ **F-04b 成立**（未来时间戳 age < 0，`age > ttl_s` 为假 ⇒ 判为不陈旧）。

```
$ grep -rn "writer_pid\|writer_start_time" gateway/ plugins/
gateway/status.py:1122:        platform_payload["writer_pid"] = current_record["pid"]
gateway/status.py:1123:        platform_payload["writer_start_time"] = current_record["start_time"]
```
⇒ 条目级写者身份**存在**（我一度以为初稿写错了，核实后是我错）。
但 §1.2 的生产实测样本里没有这两个键 ⇒ 修订后要求「有就用、没有就回落顶层」。

```
$ git diff --check docs/orchestration/tasks/T3-design.md
（无输出）
$ python3 -c "…markdown 校验…"
fence count: 76 even
table rows with mismatched pipe counts: 5
```
5 处「列数不一致」全部是**表格单元格里 `` `grep 'a\|b'` `` 这类反斜杠转义管道**，
其中 4 处是初稿既有内容（§1 事实表、§8.4 三行），我新增的那 1 处（§8.3 验证命令）
已改写为不含管道的 `test -r /proc/$(systemctl show -p MainPID --value hermes.service)/stat`。

**行数**：1034 → 1962 行。

---

## 4. 遗留风险与存疑（不掩盖）

1. **F-01 的手动 `probe` 仍有理论 TOCTOU。** 三份物证 + 10 分钟宽限把窗口压得很小，
   但「运维在终端敲下命令的那一刻 systemd 恰好拉起 Hermes」在理论上仍可能。
   缓解是它必须由人显式发起、且探针只做一次 `get_status`。
   **如果实现者觉得这仍然不可接受，删掉整个 `probe` 子命令不会损失任何统计能力**——
   §3.3.2 已经论证了它对账本零贡献。这个选择留给实现阶段。
2. **`A4_WRITER_UNVERIFIABLE` 在 legacy 生产上可能同时出现 A0 和 A4**（T1 未部署 + `/proc` 配错）。
   两条都是每 24 小时一条，加上心跳 = 3 条/天，不算轰炸，但文案要避免互相矛盾。
   §6.4.4 / §4.2 给了文案方向，**具体措辞未逐字写死**，实现时需要 golden test 定稿。
3. **`rule of three` 判据依赖基线 λ₀ 的质量。** 若 datacenter 侧的 10 段样本本身
   被删失规则过滤得只剩边缘案例，λ₀ 会有偏。§10.5 要求同时打印 exposure/failures/censored
   正是为了让人能看出这一点，但**这套判据没有对基线本身做偏差检验**——存疑，需要人在看报告时判断。
4. **`ProtectProc=default` 的取舍未在生产验证。** 我从 `gateway/status.py:346-378` 的代码
   推断 `ProtectProc=invisible` 会让 `start_time` 恒为 `None`，这是**代码推断，未在机器上实测**
   （任务禁止 SSH）。§8.3 已把它写成部署时必验的一条硬条件，用实测替代推断。
5. **`raw_transitions.jsonl` 的体积估算是量级估算，没有实测数据支撑。**
   64 MiB 的截断保护是拍的，抖动最凶时的真实写入速率需要上线后观察。
6. **未触碰 `tasks/T4-home-migration-runbook.md`**（另一个 agent 在并行改）。
   但 rev-1 删除自动探针这件事**对 T4 有影响**：T4 §6.6 那六条每分钟的短探针
   （`audits/T4.md` F16）现在与 T3 设计一致了 —— 都要求「Hermes 启动前才探」。
   两份文档的最终一致性需要主线程在合并时确认。
