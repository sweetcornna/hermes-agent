# T3 — QQ 掉线告警 + 会话时长采集：设计

状态：**rev-1，已按审计修订，待 GPT 轨道实现**（2026-08-31）
上游任务：`docs/orchestration/tasks/T3.md`（含主线程两次范围修正：Telegram 出网实测、T6 合并）
审计：`docs/orchestration/audits/T3.md`（打回）；修订单：`docs/orchestration/tasks/T3-rev.md`；
修订处置：`docs/orchestration/results/T3-rev.md`
实现产物落点：`ops/qq_sentinel/`（新建），测试落点 `tests/ops/`

### rev-1 相对初稿的五个实质变化（实现者必读）

1. **自动 OneBot 短探针被删除**（§3.3）。初稿的触发条件挡不住「systemd 已拉起新 Hermes、
   但它还没写第一次状态」这个竞态，会产生第二条 WS。探针降级为**运维手动子命令**，
   且在任一 gateway 存活物证存在时硬拒绝。
2. **`writer_alive` 从布尔值改成三值** `LIVE / DEAD / UNVERIFIABLE`（§3.2.2）。
   上游 `runtime_status_pid_is_live()` 在 `start_time` 缺失时返回 `True`，直接照抄会漏报。
3. **`source_format` 闩锁从「24 小时墙钟」改成「绑定 gateway 代际、代际内永不过期」**（§4.1.2）。
   墙钟闩锁在 adapter 回滚和 health loop 卡死两个方向上都会错。
4. **账本与告警彻底分家**（§10.2）：账本只用**已确认**的状态转换切段，
   `link_down` / unknown 证据一律删失，未证明在线的区间不进中位数。
5. **迁移门槛区分 pilot 闸门与统计结论**（§10.6）：迁移后允许「零失败 + 足够暴露时间」
   的右删失判定，不再要求必须攒够 10 次掉线。

## 0. 一句话结论

在 `corlinman-prod` 上新建一个**只读、无恢复能力**的 systemd oneshot + timer，名为
**`hermes-qq-sentinel`**。它每 60 秒读一次 Hermes 自己写的 `gateway_state.json`
（**常态与非常态都不新开任何到 NapCat 的连接**），把结果同时喂给两个消费者：**Telegram 告警**和
**会话时长账本**。它不做自动重启、不做自动重登、不碰 NapCat WebUI。

这三条禁令的**强制层**是 systemd 沙箱（内核）＋ 单一运行时封装（代码里唯一的对外出口）；
仓库内那条静态扫描测试是**防回归检查，不是权限边界**（§8.5 说明了为什么不能把它当边界）。

原任务把「告警」和 T6「会话时长采集」当成两件事。它们的观测面完全相同，分开做必然导致
两条 OneBot 探针连到同一个 NapCat —— 这正是本项目一直在躲的「第二个生命周期管理器」隐患。
所以本设计是**一个观测器，两个消费者**。

---

## 1. 前提事实（2026-08-31 主线程只读实测，覆盖 T3.md 的过时描述）

| 层 | 主机 | 事实 |
|---|---|---|
| Hermes | `corlinman-prod` = 43.133.12.98 | `hermes.service`；`HERMES_HOME=/opt/hermes/data`；`api.telegram.org` **直连可达（302）** |
| Telegram 出网 | `corlinman-prod` | **不需要 SOCKS**。该机上**不存在** `tg-socks-relay.service`。`TELEGRAM_BOT_TOKEN` 在 `/opt/hermes/data/.env` |
| QQ / NapCat | `cornna-cn` = 8.134.33.19 | `napcat-native.service`（**原生 AppImage，不是 Docker**，`User=napcat-native`，进程树 `dbus-run-session → AppRun → qq → qq`），直接监听 `127.0.0.1:3011`（OneBot v11 WS）与 `127.0.0.1:6099`（WebUI） |
| Telegram 出网 | `cornna-cn` | **不可达（curl 000）** |
| 隧道 | `corlinman-prod:onebot-tunnel.service` | 本机监听 `127.0.0.1:3011` → `cornna-cn:127.0.0.1:3011`；**与 `hermes.service` 是两个独立 unit**，Hermes 挂了它还在 |
| 已有只读探针 | `cornna-cn` | `napcat-native-health.timer`（enabled，**每 5 分钟**）→ oneshot → `/usr/local/libexec/napcat-native-health`；`STATE_PATH=/run/napcat-native-health/state.json`（**tmpfs，重启即失**）；`TIMEOUT_SECONDS=3`；**完全不告警**（grep `telegram\|curl\|notify\|alert\|sendMessage\|chat_id\|webhook\|mail` 命中 0） |
| 已下线的生命周期管理器 | `cornna-cn` | `napcat-watch.service`、`qq-watchdog.service/.timer` 均为 **masked**（有意关停，H1 §8.4） |

由此得到本设计最硬的两条边界：

1. **告警器只能住在 `corlinman-prod`** —— 只有那台能把消息发出去。
2. **不许有任何自动恢复行为** —— `napcat-watch.py` 因为是「第二个会消耗登录尝试的生命周期
   管理器」被 stop/disable（H1 §8.4）。反复自动重启/密码登录会让腾讯风控加码，被踢后密码登录
   还会阻塞在短信验证上。本组件重蹈覆辙即为设计失败。

### 1.1 一条不能忽略的部署前置

`H1-napcat-hardened.md` §7.4 / §10 记录：生产 `/opt/hermes/repo` 停在 commit
`8911e2e0edf750b104edbdc106d63d6cdac88524`，其中的 OneBot adapter **不含**本地工作区的
account-state 加固（`ACCOUNT_AUTH_REQUIRED_MARKER` / `account_probe_secs` /
独立 account-state 追踪，含 `_publish_account_runtime_status`）。

**没有这批加固，`gateway_state.json` 里根本不会出现 `auth_required`，本告警器的核心信号就是空的。**
所以 **T1（加固部署清单）必须先落地，T3 才有意义**。本设计对此的处置见 §6.4「数据源自检」——
不是假设它已部署，而是启动时主动验证并在不满足时**发一条明确的降级告警**。

### 1.2 实测基线（2026-08-31，主线程只读探测）

上面那句「T1 必须先落地」在设计初稿里还是推理。**2026-08-31 的只读实测把它坐实了，
而且顺带证明了本组件要解决的问题此刻正在发生。**

时间线（全部为只读观测，未做任何写操作、未重启任何服务、未发送任何 QQ 消息）：

| 时刻 (UTC) | 观测点 | 结果 |
|---|---|---|
| `2026-08-30T18:36:57.553362Z` | `corlinman-prod:/opt/hermes/data/gateway_state.json` | `platforms.onebot.updated_at` 停在此刻，**此后 15 小时未再变动** |
| `2026-08-31T07:30:12Z` | `cornna-cn: journalctl -u napcat-native.service` | `[KickedOffLine] [下线通知]` —— 账号被踢 |
| `2026-08-31T09:46Z` | 短生命周期 OneBot 只读探针（经 `onebot-tunnel.service`，只调 `get_status`） | `WS_LINK=true` / `ACCOUNT_ONLINE=False` / `ACCOUNT_GOOD=True` / `UIN=2952532060` |
| `2026-08-31T09:46Z` | `corlinman-prod: systemctl is-active hermes.service` | `active`（全程未中断） |

同一时刻 `gateway_state.json` 里 `platforms.onebot` 的实际内容：

```json
"onebot": {"state": "connected", "error_code": null, "error_message": null,
           "updated_at": "2026-08-30T18:36:57.553362+00:00"}
"gateway_state": "running"
```

远端 `adapter.py` 对 `_publish_account_runtime_status|qq_account_auth_required|account_auth_required`
的 grep 命中数为 **0**。

**这四行数据同时说明了三件事：**

1. **问题真实且正在发生。** 账号在 `07:30:12Z` 被踢，到 `09:46Z` 已经离线 **2 小时 16 分钟**，
   没有任何人被通知。这是 H1 §3 记录的 9.7 h / 11.7 h 空窗的第三次复现 ——
   本组件不是预防性建设，是在补一个正在漏水的洞。
2. **Hermes 此刻在撒谎。** 进程 active，文件写着 `state: "connected"`，而账号实际离线。
   任何只看 `state` 字段、不做格式自检的消费者都会得出「一切正常」。
3. **旧 adapter 的写入语义与新 adapter 不同**，这直接推翻了设计初稿 §3.2 的一个假设 ——
   详见 §3.2 的修正。

将来若有人质疑这个组件是否值得做，答案就是上面这张表。

---

## 2. 组件形态：一个观测器，两个消费者

```
                    每 60s（systemd timer）
                             │
                             ▼
        ┌────────────────────────────────────────┐
        │  observe.sample()  →  Observation      │
        │  数据源①  /opt/hermes/data/gateway_state.json   （唯一常规数据源）
        │  数据源①附  /opt/hermes/data/gateway.pid           （gateway 存活第二物证）
        │  数据源①附  /opt/hermes/data/gateway.lock          （gateway 存活第三物证）
        └────────────────────────────────────────┘
                             │
                    confirm.py 去抖（唯一一份）
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
   ┌────────────────────┐        ┌────────────────────────┐
   │ decide.py 状态机   │        │ ledger.py 会话账本     │
   │ 冷却 / 分类 / 预算 │        │ 追加 sessions.jsonl    │
   └─────────┬──────────┘        └───────────┬────────────┘
             ▼                                ▼
   notify.py → Telegram            report 子命令 → 中位在线时长
```

`run` 路径上**没有任何出网到 NapCat 的分支** —— OneBot 短探针只存在于一个手动子命令里（§3.3）。

两个消费者共享同一次采样，也共享同一份去抖判定（`confirm.py`）：
初稿让告警走去抖、账本走原始采样，结果是「告警吸收了抖动，账本却把抖动记成了真实会话」（审计 F-06）。
**告警视图和测量视图可以有不同的输出，但不能有不同的事实。**

---

## 3. 数据源：为什么是文件，而不是新开 WebSocket

### 3.1 「复用 live client」这条约束的确切含义

`plugins/platforms/onebot/client.py:830-908` 的 `_LIVE_CLIENT` / `_LIVE_ADAPTER` 是
**模块级全局变量**，由 `adapter.py:2154/2161` 在 `connect()` 里 `set_live_client(...)` 写入。
`tools/onebot_client.py:154` 的复用路径也只是 `from ... import get_live_client` 后直接读这个全局。

**结论：这个注册表是进程内的。** 另一个进程 import 同一个模块拿到的是一份全新的、值为 `None`
的全局。所以「在外部进程里复用 Hermes 的 live client」不是不推荐，而是**物理上不可能**。

因此这条约束的可实现形式只有一个：**不要在 Hermes 已经握着一条 WS 的时候再开第二条。**
本设计用「读 Hermes 自己写的状态文件」满足它，**常态下新增连接数为 0**。

这也正是 `tools/onebot_client.py` 模块 docstring 里写的理由：
「Reusing it avoids burning a second connection slot on the backend and avoids two clients
racing on the same account.」本设计把同一条理由推到了极致 —— 连短连接都不开。

### 3.2 数据源①：`gateway_state.json`（常态唯一数据源）

- 路径：`${HERMES_HOME}/gateway_state.json`，生产上是 `/opt/hermes/data/gateway_state.json`。
  （`gateway/status.py:231` `_get_runtime_status_path()`；`_RUNTIME_STATUS_FILE = "gateway_state.json"`）
- 写入者：`plugins/platforms/onebot/adapter.py:1968` `_publish_account_runtime_status()`
  → `gateway/platforms/base.py:3449` `_write_runtime_status_safe()`
  → `gateway/status.py:1038` `write_runtime_status(platform="onebot", ...)`。
- 平台键固定为 `"onebot"`（`plugin.yaml` 顶部注释：`_platform_name_from_manifest` 由
  `name: onebot-platform` 去掉 `-platform` 得到）。

#### 3.2.1 刷新频率有两种，取决于生产上跑的是哪版 adapter ⚠️

**这是设计初稿被实测推翻的一处假设，实现者必须按修正后的版本做。**

| | **新 adapter（本地工作区，T1 待部署）** | **旧 adapter（`8911e2e0` 生产在跑）** |
|---|---|---|
| 谁写 `platforms.onebot` | `adapter.py:1968 _publish_account_runtime_status()`，由 `adapter.py:2668 _health_loop()` 在**每个 tick 末尾无条件调用** | 只有 `gateway/platforms/base.py:3434` 与 `gateway/run.py` 的连接/重连事件 |
| 写入时机 | **周期性**：每 `HEALTH_PROBE_SECS = 30.0` 秒一次，状态没变也刷新 `updated_at` | **仅状态变化时**（connect / retry / fatal） |
| `error_message` | 恒为四个已知前缀之一（§6.4） | 恒为 `null`（或偶发的异常字符串） |
| 账号真相 | `auth_required` / `qq_account_auth_required` 会出现 | **永远不会出现** |
| 「文件不动」的含义 | **Hermes 不在写了** | **完全正常** —— §1.2 实测：文件冻结 15 小时而 `hermes.service` 全程 active |

初稿写的「即使状态没变也会刷新 `updated_at`，这使『文件不动』成为一个可靠的
『Hermes 不在了』信号」**只对新 adapter 成立**。在旧 adapter 上它是错的，而且错得很危险：
照初稿实现的哨兵一装上生产就会立刻误报 `OBSERVER_DOWN`。

**因此本设计不再把「文件陈旧」当作 Hermes 死亡的唯一判据。**
新增一个与格式无关、在两种 adapter 下都成立的判据 —— **写者进程存活性**。
它的准确定义见 §3.2.2；**不要**按下面这行初稿伪码实现（审计 F-04 已证明它有漏报）：

```
# ⛔ 初稿写法，已废弃：
# writer_alive = pid 存在 and start_time 匹配
```

`pid` / `start_time` / `kind` / `argv` 由 `write_runtime_status()` 在**每一次**写入时无条件刷新
（`gateway/status.py:1073-1077`，取自 `_build_pid_record()`，`gateway/status.py:627-638`），
而 gateway 启动时必定写一次（`gateway_state="starting"`），所以即使在旧 adapter 的稀疏写入下，
这四个字段也始终指向**当前**那个 gateway 进程。

需要读的字段（`kind` / `argv` 初稿漏了，它们是免费的身份证据，必须用上）：

```jsonc
{
  "pid": 12345,                     // 进程存活判定
  "start_time": 987654,             // PID 复用防护（Linux: /proc/<pid>/stat 字段 22）
  "kind": "hermes-gateway",         // 身份校验，必须等于此字面量
  "argv": ["...", "gateway", "run"],// 身份校验，见 §3.2.2
  "gateway_state": "running",       // "stopping"/"stopped" ⇒ 主动停服
  "exit_reason": null,              // 非 null ⇒ 主动停服
  "updated_at": "2026-08-31T04:00:00Z",
  "platforms": {
    "onebot": {
      "state": "connected" | "auth_required" | "disconnected" | "retrying" | "connecting" | "fatal" | "paused" | "disabled",
      "error_code": null | "qq_account_auth_required" | "onebot_link_offline",
      "error_message": "QQ account is online; source=...; heartbeat_age=...s; link_online=True",
      "updated_at": "2026-08-31T04:00:00Z",
      "writer_pid": 12345,           // 条目级写者身份，可能缺失，见下
      "writer_start_time": 987654    // 同上
    }
  }
}
```

> **`writer_pid` / `writer_start_time` 是可选字段，实现必须容忍缺失。**
> 它们由 `gateway/status.py:1122-1123` 写入，注释说明用途正是「区分本条目是当前活进程写的、
> 还是从上一个进程继承下来的」—— 这正好是 §4.1.2 代际闩锁需要的键。
> 但 §1.2 的生产实测样本里**没有这两个键**（生产停在 `8911e2e0`）。
> 所以：**有就用条目级的，没有就回落顶层 `pid` / `start_time`**，并在账本里记下用了哪一种
> （`generation_source: "entry" | "toplevel"`）。把缺失当成 0 或当成匹配都是错的。

#### 3.2.2 `writer_state`：三值，不是布尔值（审计 F-04）

初稿说「语义与实现均对齐 `runtime_status_pid_is_live()`」。**这句话本身就是缺陷**，因为上游那个
函数在这里的语义是错的（对我们的用途而言）：

```python
# gateway/status.py:1168-1188 —— 实际实现
recorded_start = (record or {}).get("start_time")
current_start = _get_process_start_time(pid)
if (recorded_start is not None and current_start is not None
        and current_start != recorded_start):
    return False
return True          # ← 任一为 None ⇒ 任何活着的 PID 都算「还活着」
```

对 gateway 自己（做 `--replace` 接管决策）这个偏向是安全的：宁可认为老进程还在、也不要误杀。
**对哨兵它是反的**：`start_time` 读不到 + PID 被复用给别的进程 ⇒ 哨兵认定 writer 还活着 ⇒
Hermes 真死了也不发 `A3_OBSERVER_DOWN`。这是漏报，不是保守。

而且 `_get_process_start_time()` 返回 `None` 在生产上**不是罕见分支**：
`gateway/status.py:346-378` 读 `/proc/<pid>/stat`，`PermissionError` / `FileNotFoundError` 全部吞掉返回 `None`。
哨兵以专用非 root 用户运行，而 **§8.3 初稿的 unit 里写了 `ProtectProc=invisible`** ——
那正好会让 `/proc/<hermes pid>` 对哨兵不可见，于是 `start_time` **永远**是 `None`。
初稿等于亲手把自己的 PID 复用防护关掉了。§8.3 已相应修正。

因此本设计用**三值**判定，在 `ops/qq_sentinel/observe.py` 里自行实现（不 import `gateway.status`，理由见 §10.1）：

```python
class WriterState(enum.Enum):
    LIVE = "live"                    # 能证明 gateway 进程还在
    DEAD = "dead"                    # 能证明它不在了
    UNVERIFIABLE = "unverifiable"    # 证明不了任何一边 —— 这是一种要说出来的状态
```

完整 truth table（自上而下，先命中先返回）：

| # | 条件 | `writer_state` | 备注 |
|---|---|---|---|
| 1 | 文件缺失 / 不可读 / 非法 JSON | `DEAD` | 连记录都没有 |
| 2 | `pid` 缺失 / 非整数 / ≤ 0 | `DEAD` | 记录不完整 |
| 3 | `kind != "hermes-gateway"` | `DEAD` | 不是 gateway 写的文件 |
| 4 | `argv` 缺失，或拼成的命令行不像 `gateway run` / `gateway restart` | `DEAD` | 语义对齐 `looks_like_gateway_runtime_command_line()`（`gateway/status.py:507-518`） |
| 5 | PID 不存在（`os.kill(pid, 0)` → `ProcessLookupError`） | `DEAD` | |
| 6 | PID 存在，且实际 `start_time` 可读，且与记录**不等** | `DEAD` | PID 复用，确定是另一个进程 |
| 7 | PID 存在，`start_time` 可读且相等，且 `/proc/<pid>/cmdline` 可读且像 gateway | `LIVE` | 唯一的强肯定 |
| 8 | PID 存在，但 `start_time`（记录侧或实际侧）读不到 | `UNVERIFIABLE` | ⛔ **不得**像上游那样 `return True` |
| 9 | PID 存在、`start_time` 相等，但 `cmdline` 读不到 | `UNVERIFIABLE` | `ProtectProc` / 权限问题 |

实现注意：`os.kill(pid, 0)` 对**别的用户**的进程返回 `EPERM`（`PermissionError`）——
这表示「进程存在但我无权发信号」，**必须当成存在**，不能当成不存在。哨兵和 Hermes 是不同用户，
这条路径是常态而非边缘情形。（本设计不依赖 psutil：`ops/qq_sentinel/` 只用标准库，见 §10.1。）

`UNVERIFIABLE` 怎么用，见 §4.1 的判定表 —— 一句话：**它既不当活也不当死，它自己会发一条告警**
（`A4_WRITER_UNVERIFIABLE`，§4.2）。把「我看不见」静默地折进「活着」或「死了」，
都是 §11.6 定义的设计失败。

#### 3.2.3 `updated_at` 的年龄是**有符号**的（审计 F-04b）

`gateway/status.py:1716-1722` 的 `_marker_is_stale()`：

```python
age = (datetime.now(timezone.utc) - written_dt).total_seconds()
return age > ttl_s
```

**未来时间戳的 age 是负数，`age > ttl_s` 为假 ⇒ 判定为「新鲜」。** 卡死的 gateway
（或时钟跳变、或 NTP 校正后写入的文件）留下一个未来的 `updated_at`，就能让哨兵永远认为它在正常刷新。

本设计的 `state_age_secs()` 返回**有符号**秒数，并做三分类：

| 有符号 age | 分类 | 处理 |
|---|---|---|
| `age < -CLOCK_SKEW_TOLERANCE_SECS`（默认 −120） | `IMPLAUSIBLE_FUTURE` | **按 `STALE` 处理**，并触发一次 `A4`（时钟或写者异常，必须让人知道） |
| `-120 ≤ age ≤ STATE_STALE_SECS`（180） | `FRESH` | 正常 |
| `age > 180` | `STALE` | 陈旧 |
| 时间戳缺失 / 解析失败 | `STALE` | 与 `gateway/status.py` 对缺失时间戳的保守取向一致 |

`CLOCK_SKEW_TOLERANCE_SECS = 120` 的理由：容忍 NTP 小步校正与写入/读取之间的正常时钟抖动，
但不容忍「文件时间戳比现在晚好几分钟」这种只可能来自故障的情形。
§9 的 T2 必须有一条 `updated_at = now + 1h` 的负例，断言结果是 `STALE` 而不是 `FRESH`。

### 3.3 数据源②：OneBot `get_status` 短连接 —— **自动触发已删除，改为手动子命令**

#### 3.3.1 初稿错在哪（审计 F-01，致命）

初稿的触发条件是「`gateway_state.json` 陈旧 **且** `pid` 对应进程已不存在」，并据此声称
「此时 Hermes 持有的 WS 连接数为 0，本探针不是第二条连接，而是唯一一条」。

**这个推理有一个挡不住的失败时序：**

```
t0    旧 Hermes 崩溃退出。gateway_state.json 仍留着它的 pid/start_time。
t1    systemd Restart= 拉起新 Hermes。新进程正在 import、读配置、连 NapCat。
t2    新 Hermes 已经建立了自己的持久 OneBot WS，但**还没写第一次 runtime status**
      （或者写失败了：权限、磁盘满、HERMES_HOME 指错）。
t3    哨兵这一拍读文件：updated_at 陈旧 ✓，记录的 pid 已死 ✓  ⇒ 判定「Hermes 已死」
      ⇒ 开短连接。**此刻后端上同时存在两条 WS。**
```

`PROBE_MIN_INTERVAL_SECS = 300` 只限制**重复**频率，对**第一次**重叠毫无作用。
文件缺失、JSON 暂时读到半截、旧 PID 尚未被覆盖，都会落进同一个洞。
T4 的审计独立发现了同一类错误（`audits/T4.md` F16：Hermes 起来之后还每分钟跑一次独立探针）——
两份设计各自犯了同一个错，说明「Hermes 活着时不许开第二条连接」这条铁律不能停在口头约定。

#### 3.3.2 为什么选择删掉自动探针，而不是把判据修窄

先说结论：**T3-A 的 `run` 路径里不存在任何 OneBot 出网分支。** 理由有两层。

**第一层：判据修不到「零竞态」。** 能加的都加上（三份物证 + 启动宽限 + 连续多拍确认，见 §3.3.3），
剩下的仍是一个 TOCTOU 窗口：哨兵在 t 时刻证明「此刻没有 gateway」，systemd 可以在 t+1ms 拉起一个。
把这个窗口从「几乎必然」压到「极小」是可能的，压到零是不可能的。

**第二层——这层才是决定性的：探针买到的东西比初稿声称的少得多。**
初稿说「探针让我们能把区间标成 `end_censored=true` 而不是伪造一个 `kicked`」。
**这句话是错的。** 去看 §10.2 的判定：`end_censored` 是由 `OBSERVER_DOWN` 这个观测态**本身**决定的，
根本不需要问 NapCat 任何问题。探针的返回值不进入任何一个统计字段，它只能让告警文案
从「Hermes 挂了，QQ 状态未知」变成「Hermes 挂了，QQ 当时还在线」。

**为了一句更好听的告警文案，去冒违反本设计最硬那条边界的风险，是坏交易。**
这也正是审计给的兜底选项：「若构造不出不与新 Hermes 竞态的判据，就删掉自动短探针 ——
宁可把该区间记为删失」。采纳。

被放弃的方案及其错处：

| 放弃的方案 | 错在哪 |
|---|---|
| 保留自动探针，把判据收窄到「三份物证都说没有 gateway + 10 拍确认」 | 仍有 TOCTOU；且为了一个纯诊断字段，让 `run` 路径永久保留一条出网代码分支 —— 这条分支一旦存在，未来的人就会往里加东西（§8.5 讲的就是这个失败模式） |
| 保留自动探针，但先抢 `gateway.lock` 证明没人持有 | `is_gateway_runtime_lock_active()`（`gateway/status.py:977-1010`）是 `open(path,"a+")` + 抢 `LOCK_EX`：①需要写权限，而 unit 里 `/opt/hermes/data` 是 `ReadOnlyPaths`；②抢锁本身会和正在启动的 gateway 竞争，可能把新 Hermes 挤掉。**一个只读观测器去抢生产互斥锁，本身就是越界。** |
| 干脆连手动探针也删掉 | 运维在「Hermes 已停、准备迁移」时确实需要一个只读的 `get_status`；T4 runbook 的正确形态就是**在启动 Hermes 之前**跑它（`audits/T4.md` F16 的修法）。删干净会把这个正当用途也砍掉 |

#### 3.3.3 手动子命令 `probe`：唯一允许接触 OneBot 的入口

```bash
python3 -m ops.qq_sentinel probe --confirm-hermes-down
```

- **只能由人在终端里跑**，`run` / timer 路径永远不会调到它。
- `--confirm-hermes-down` 是必填开关，不给就直接拒绝并打印用法。
  它的作用不是「授权」，而是**留痕**：运维必须显式声明自己知道这条命令会建一条 WS。
- 命令**自己再验一遍**，人的声明不被信任。**三份物证任一显示 gateway 可能存在，即拒绝：**

  | 物证 | 路径 | 拒绝条件 |
  |---|---|---|
  | ① 运行状态 | `${HERMES_HOME}/gateway_state.json` | `writer_state != DEAD`（含 `UNVERIFIABLE`，§3.2.2） |
  | ② PID 文件 | `${HERMES_HOME}/gateway.pid` | 文件存在且其 `pid` 记录不是 `DEAD`（同一张 truth table） |
  | ③ 运行锁 | `${HERMES_HOME}/gateway.lock` | 文件存在且其 `pid` 记录不是 `DEAD`。**只读解析 JSON 内容，绝不 flock** |

  另加两条时间闸：
  - `PROBE_GATEWAY_ABSENT_GRACE_SECS = 600` —— 三份物证最近一次显示「有 gateway」的时刻起，
    至少要过 10 分钟。挡的正是 §3.3.1 那个「新进程已起、状态未写」的窗口
    （systemd `Restart=` 的重启是秒级，10 分钟远大于它）。
  - `PROBE_MIN_INTERVAL_SECS = 300` —— 两次手动探针之间的最小间隔，防手抖连点。

- 拒绝时**退出码 3**，并打印哪一份物证挡住了它，例如：
  `refused: gateway.lock still names a live PID (pid=41233, start_time=884219) — 拒绝建立第二条 WS`。
- 通过时只做**一次** `get_status`，打印结果，退出。不写账本、不发告警、不改任何状态
  （除了 `${STATE_DIR}/state.json` 里的 `last_probe_at`，用于上面的最小间隔）。

**写成可测试断言**（§9 的 T3，这是本次修订最重要的一条测试）：

```python
# tests/ops/test_qq_sentinel_probe_gate.py
def test_run_path_never_probes(monkeypatch, tmp_path):
    """审计 F-01：陈旧状态文件 + 新 Hermes 已启动未刷新 ⇒ 探针调用次数必须为 0。"""
    calls = []
    monkeypatch.setattr(observe, "_onebot_get_status", lambda *a, **k: calls.append(1))
    write_state_json(tmp_path, pid=DEAD_PID, updated_at=hours_ago(3))   # 旧进程已死
    write_pid_file(tmp_path, pid=os.getpid())                          # 新进程刚起，还没写状态
    for _ in range(20):
        run_once(home=tmp_path)
    assert calls == []          # ← run 路径不许有任何 OneBot 调用，一次都不行
```

再加一条更强的：`run` 模块的依赖图里**根本不存在** OneBot 传输符号 ——
`tests/ops/test_qq_sentinel_boundary.py` 断言 `ops.qq_sentinel.run` 及其传递闭包里
不出现 `onebot_call` / `websockets` 的引用（`probe.py` 除外）。

#### 3.3.4 探针的硬限制（`probe` 子命令内部，写进常量并做**运行时**断言）

| 项 | 值 |
|---|---|
| 唯一封装 | `probe.probe_get_status()` —— 全仓库**不得**在别处调用 `onebot_call()`（§8.2） |
| 动作白名单 | `PROBE_ACTION_ALLOWLIST = ("get_status",)`，且 action 是**硬编码字面量**，不接受参数 |
| 超时 | `PROBE_TIMEOUT_SECS = 10` |
| 最小间隔 | `PROBE_MIN_INTERVAL_SECS = 300` |
| 失败语义 | 探针失败 ⇒ `probe_unavailable`，**绝不等于 `offline`** |
| 端点校验 | 见 §8.2.1，`scheme/host/port` 逐项运行时校验，`ONEBOT_HTTP_URL` 非空即拒绝 |

### 3.4 明确否决的数据源

| 否决项 | 理由 |
|---|---|
| 常态开一条自己的 OneBot WS | 与 Hermes 的连接竞争同一账号，违反 §3.1 的约束；文件已经给出全部所需字段 |
| NapCat WebUI（`:6099`）任何接口 | 读接口和写接口共用同一把 Credential，没有作用域更小的 token。持有它就等于持有 `/api/Process/Restart` 和 `PasswordLogin` 的能力，权限边界会退化成「靠我们自己代码里的 if 判断」，不可审计。见 §7 |
| `cornna-cn:/run/napcat-native-health/state.json` | 在另一台机上；`/run` 是 tmpfs 重启即失；且那台机发不出 Telegram |
| 解析 `journalctl -u hermes.service` | 日志格式不是契约，会随日志级别和轮转变化 |

---

## 4. 问题 1 —— 触发条件

### 4.1 观测态：五值枚举 `AccountView`

每次采样把上面那堆字段折成**一个**枚举值。判定顺序自上而下，**先命中先返回**：

先算三个与枚举无关的前置量：

- `writer_state ∈ {LIVE, DEAD, UNVERIFIABLE}` —— §3.2.2 的 truth table。
- `state_age ∈ {FRESH, STALE, IMPLAUSIBLE_FUTURE}` —— §3.2.3 的有符号年龄。
- `source_format ∈ {modern, unknown, legacy}` —— §4.1.2 的**代际**闩锁判定。
  注意是**三值**：初稿只有 `modern` / `legacy`，把「还不知道」强行归进 `legacy`，
  这正是审计 F-05 那两个失败模式的根。

然后判定顺序自上而下，**先命中先返回**：

| # | `AccountView` | 判据（对 `platforms.onebot`，除非注明） | 语义 |
|---|---|---|---|
| 1 | `OBSERVER_DOWN` | 文件缺失 / 不可读 / 非法 JSON / 无 `platforms.onebot`；**或** `writer_state == DEAD`；**或**（`writer_state ∈ {LIVE, UNVERIFIABLE}` **且** `source_format == modern` **且** `state_age != FRESH`） | Hermes 不在了 |
| 2 | `AUTH_REQUIRED` | `error_code == "qq_account_auth_required"` **或** `state == "auth_required"` | QQ 账号被踢，**需要人**。**注意它排在 `SOURCE_INCOMPATIBLE` 前面**，理由见 §4.1.1 第 3 点 |
| 3 | `SOURCE_INCOMPATIBLE` | `source_format == legacy`（已确认，见 §4.1.2） | 旧 adapter，账号真相不在文件里（T1 未部署） |
| 4 | `LINK_DOWN` | `error_code == "onebot_link_offline"` **或** `state ∈ {"disconnected","retrying","connecting","fatal"}` | WS 链路静默，通常会自愈 |
| 5 | `ONLINE` | 其余（`state == "connected"` 且 `error_code` 为空） | **「未被证明离线」**，不等于「已证明在线」。**它不等于「可计入会话时长」**，测量口径见 §10.2.2 |

`writer_state == UNVERIFIABLE` 的完整处理（这是初稿完全没有的分支）：

| `source_format` | `state_age` | 判定 | 为什么 |
|---|---|---|---|
| `modern` | `FRESH` | 落到第 2~5 行正常判定 | 有人在按 30 秒周期写文件，这本身就是 writer 活着的证据 |
| `modern` | `STALE` / `IMPLAUSIBLE_FUTURE` | `OBSERVER_DOWN` | 看不见进程 **且** 没人在写，证据够了 |
| `unknown` / `legacy` | 任意 | **不升级为 `OBSERVER_DOWN`**，落到第 2~5 行 | legacy 天然稀疏写入，陈旧是正常的；此时「看不见进程」是我们**唯一**的死亡判据，而它恰好失效了 —— 只能诚实承认判断不了 |

**这三种情况一律额外触发 `A4_WRITER_UNVERIFIABLE`（§4.2），并把本拍的账本记录标记为
`verification_degraded=true`（§10.2.3）。**

最后那一格是必须说出来的坏消息：`writer_state == UNVERIFIABLE` + legacy adapter 时，
**哨兵连「Hermes 挂了」都会漏报**。这不是可以静默接受的降级（§11.6），
所以 A4 的文案直接写「我现在看不到 Hermes 进程，在 T1 部署完成之前，连它死了我都可能不告诉你」，
并给出修法（§8.3 的 `ProtectProc` 与 DAC 权限检查）。
`UNVERIFIABLE` 在正确部署的机器上应该**一次都不出现** —— 它出现就是部署有问题。

#### 4.1.1 为什么这样排，以及它挡住了哪个假阳性

主线程点名要求确认的形态是：**「文件陈旧 + `state: connected` + `error_code: null` +
Hermes 进程活着」在旧 adapter 下是正常的，不能判成 `OBSERVER_DOWN`。**

上表挡住它的是**两道**闸，不是一道：

1. **陈旧判据被 `source_format == modern` 限定。** 旧 adapter 天然稀疏写入（§1.2 实测：
   冻结 15 小时仍属正常），所以在 legacy / unknown 格式下**根本不评估 `updated_at`**。
2. **`writer_state == LIVE`。** 文件里的 `pid` / `start_time` / `kind` / `argv`
   指向那个还活着的 gateway 进程。

初稿把 `SOURCE_INCOMPATIBLE` 排在第 1 位、`OBSERVER_DOWN` 排在第 2 位，也能挡住这个
假阳性（legacy 会先命中规则 1 而短路）—— 但那样会引入一个**更糟的漏报**：
在旧 adapter 上，只要 Hermes 真的死了，规则 1 依然抢先命中，哨兵会一直报「格式不兼容」
而**永远说不出「Hermes 挂了」**。而 T1 上线前的这段窗口恰恰是最需要它说话的时候。

**rev-1 的第三点：`AUTH_REQUIRED` 现在排在 `SOURCE_INCOMPATIBLE` 之前。**
理由是证据的方向性 —— `error_code == "qq_account_auth_required"` 是**正证据**：
只有新 adapter 才写得出这个值（`adapter.py:1979-1981`，`ACCOUNT_AUTH_REQUIRED_REASON`），
旧路径无论如何都产生不了它。所以看见它就等于同时证明了「格式是 modern」和「账号真的被踢了」。
让「我还没确定格式」挡住一条确凿的被踢告警，是纯亏损。
（观察到它时同步把该代际闩锁为 `modern`，见 §4.1.2。）

完整场景表（初稿只有 6 行，审计指出证明不完整；下面补齐了新旧进程重叠、
`start_time` 缺失、闩锁、未来时间戳这四类漏掉的情形）：

| # | 场景 | `source_format` | `writer_state` | `state_age` | 判定 | 对不对 |
|---|---|---|---|---|---|---|
| 1 | §1.2 实测的当前生产 | legacy | `LIVE` | 15 小时前 | `SOURCE_INCOMPATIBLE` | ✅ 不误报 |
| 2 | 旧 adapter + Hermes 崩了 | legacy | `DEAD` | 任意 | `OBSERVER_DOWN` | ✅ 不漏报 |
| 3 | 新 adapter + Hermes 崩了 | modern | `DEAD` | 陈旧 | `OBSERVER_DOWN` | ✅ |
| 4 | 新 adapter + gateway 卡死（进程在、循环不转） | modern | `LIVE` | `STALE` | `OBSERVER_DOWN` | ✅ §7.3 说的「插件方案报告不了自己的死亡」 |
| 5 | 新 adapter + 账号被踢 | modern | `LIVE` | `FRESH` | `AUTH_REQUIRED` | ✅ |
| 6 | PID 被回收给别的进程 | 任意 | `DEAD`（`start_time` 不等） | — | `OBSERVER_DOWN` | ✅ 且被 3 拍确认吸收 |
| 7 | **旧进程已死、新进程刚起还没写状态** | 上一代际的值 | `DEAD` | 陈旧 | `OBSERVER_DOWN` | ⚠️ 会在启动窗口内报一次「Hermes 挂了」——**可接受**（3 拍 ≈ 5 分钟确认足以跨过正常启动），且 §3.3 已保证**这条路径不会开探针** |
| 8 | **`/proc` 不可读（`ProtectProc` 配错、DAC 权限）** | modern | `UNVERIFIABLE` | `FRESH` | 落 2~5 行 + `A4` | ✅ 不静默 |
| 9 | 同上但文件也陈旧 | modern | `UNVERIFIABLE` | `STALE` | `OBSERVER_DOWN` + `A4` | ✅ |
| 10 | 同上但 legacy | legacy | `UNVERIFIABLE` | 任意 | 落 2~5 行 + `A4` | ⚠️ 此时**无法**判断 Hermes 死活，A4 明说 |
| 11 | **新 gateway 代际刚启动，格式尚未定** | `unknown` | `LIVE` | `FRESH` | 落 2~5 行；不评估陈旧；不发 A0 | ✅ 见 §4.1.2 |
| 12 | 新 gateway 代际 + 确实是 legacy（`unknown` 宽限期满） | `legacy` | `LIVE` | 陈旧 | `SOURCE_INCOMPATIBLE`（再经 5 拍确认） | ✅ |
| 13 | **`updated_at` 是未来时间戳**（卡死 / 时钟跳变） | modern | `LIVE` | `IMPLAUSIBLE_FUTURE` | `OBSERVER_DOWN` + `A4` | ✅ 审计 F-04b；初稿会判成「新鲜」并永远沉默 |
| 14 | 回滚到旧 adapter（modern → legacy） | 新代际重新判定，不继承旧代际的 modern | `LIVE` | 陈旧 | `SOURCE_INCOMPATIBLE` | ✅ 审计 F-05 场景 1；初稿会误报 `OBSERVER_DOWN` |
| 15 | modern 的 health loop 卡死超过 24 小时 | 代际内 modern **永不过期** | `LIVE` | `STALE` | `OBSERVER_DOWN` | ✅ 审计 F-05 场景 2；初稿会在闩锁过期后改判 legacy 并**关掉掉线检测** |

#### 4.1.2 `source_format` 的闩锁必须绑定 gateway **代际**，不是绑定墙钟（审计 F-05）

朴素判法是「本次采样的 `error_message` 是否以 §6.4 四个已知前缀之一开头」。
**单看一次采样会误判**：`gateway/run.py:12723-12733` 在连接成功时会写
`platform_state="connected", error_code=None, error_message=None` —— 新 adapter 上
也会短暂出现 `error_message == null`，最长存活到下一个 30 秒 health tick。
所以必须闩锁。**初稿闩锁的形式错了。**

**初稿：24 小时墙钟闩锁。两个方向都会坏，而且坏得刚好相反：**

| 失败模式 | 初稿的行为 | 后果 |
|---|---|---|
| modern 跑过之后**回滚到旧 adapter** | 24 小时内旧 adapter 的稀疏文件仍被判 modern ⇒ 陈旧判据生效 | 180 秒后**误报** `OBSERVER_DOWN`，而 Hermes 好好活着 |
| modern 的 health loop **卡死超过 24 小时** | 闩锁过期 ⇒ 无前缀 ⇒ 改判 legacy ⇒ 进入降级模式 | `A1_AUTH_REQUIRED` 被**关掉**。哨兵在最需要说话的时候闭嘴 —— 这是本设计最不能出现的失败 |

根因：**格式是进程的属性，不是时间的属性。** 同一个 gateway 进程不会在运行中途把自己的
adapter 从新版换成旧版；而换了进程之后，旧进程的证据一秒钟都不该被继承。
时间在这两件事里都不是正确的键。

**rev-1：闩锁的键是 gateway 代际。**

```python
generation = (writer_pid, writer_start_time)     # 优先取 platforms.onebot.writer_pid/writer_start_time
                                                 # 缺失则回落顶层 pid/start_time（§3.2 的注）
```

持久化在 `${STATE_DIR}/state.json`（只保留当前代际 + 上一代际，防止无限增长）：

```jsonc
"source_format_by_generation": {
  "12345:987654": {
    "first_seen_at": "2026-09-01T03:00:00Z",
    "modern_proof_at": "2026-09-01T03:00:31Z",   // null = 尚未拿到 modern 证据
    "verdict": "modern"                          // "modern" | "unknown" | "legacy"
  }
}
```

判定规则（对**当前**代际）：

1. 本拍命中 §6.4.1 四个前缀之一，**或**看到 `error_code == "qq_account_auth_required"`
   / `state == "auth_required"` ⇒ `verdict = modern`，写 `modern_proof_at`。
   **代际内一旦 modern，永不回退、永不过期。** 这直接消灭失败模式 2。
2. 尚无 modern 证据，且 `now - first_seen_at < SOURCE_UNKNOWN_GRACE_SECS`（900 秒）
   ⇒ `verdict = unknown`。
3. 尚无 modern 证据，且宽限期已过，且**看到了 legacy 指纹**
   （`error_message is null` 且 `state_age == STALE` 且 `writer_state != DEAD`，即 §6.4.2 的三合一）
   ⇒ `verdict = legacy`。
4. 尚无 modern 证据、宽限期已过，但也没看到 legacy 指纹（例如文件一直很新鲜但 `error_message`
   一直是 `null`）⇒ 保持 `unknown`，并计入 A4 的诊断计数。**不要猜。**
5. 代际变化（`generation` 与 state.json 里记录的不同）⇒ **开新条目，`verdict` 重置为 `unknown`**，
   旧代际的判定一个字节都不继承。这直接消灭失败模式 1（回滚）。

`unknown` 的行为（这是初稿完全缺失的第三种状态，也是 F-05 修法的关键）：

| 能力 | `unknown` 期间 | 为什么 |
|---|---|---|
| `A1_AUTH_REQUIRED` | **启用** | `auth_required` 是正证据，legacy 产生不了它（§4.1.1 第 3 点）。禁用它等于为了一个格式问题放弃核心告警 |
| `A2_LINK_DOWN` | **启用** | `state ∈ {disconnected,retrying,fatal}` 新旧路径都会写，两边都代表真实的链路问题 |
| 基于 `updated_at` 陈旧的 `OBSERVER_DOWN` | **停用** | legacy 的稀疏写入会让它误报（§1.2 实测：冻结 15 小时属正常） |
| 基于 `writer_state == DEAD` 的 `OBSERVER_DOWN` | **启用** | 与格式无关 |
| `A0_SOURCE_INCOMPATIBLE` | **停用** | 还没确定是 legacy，先别喊 |

一句话：**`modern` 是一个绑定在某个 gateway 进程上、观测到即永久成立的事实；
换了进程就从头证明；证不出来就诚实地说「还不知道」，而不是默认它是 legacy。**

#### 4.1.3 `A0` 也需要连续确认拍数

初稿 §4.3 声称「任何类别都要连续 N 拍确认」，但常量表里只有 auth / link / observer / recover 四个，
**`A0_SOURCE_INCOMPATIBLE` 没有确认拍数** —— fresh install 或代际切换后单次
`error_message=None` 就能触发一条 A0。补上：

```
SOURCE_INCOMPATIBLE_CONFIRM_TICKS = 5      # 5 分钟连续判定为 legacy 才发 A0
```

5 拍（而不是 3 拍）的理由：A0 是「部署有问题」类告警，不是「QQ 掉线了」类告警，
晚 2 分钟没有代价；而它一旦发出就会同时把 A1/A2 降级停用，误发的代价比晚发大得多。

**`ONLINE` 这个命名有坑，必须理解后再实现。** `adapter.py:1968`
`_publish_account_runtime_status()` 对 `account_online is True`（真在线）和
`account_online is None`（未知，启动后还没收到心跳/状态）**写出完全相同的
`state="connected"` + `error_code=None`**，只有 `error_message` 前缀不同。

这正是任务要求的行为：**`account_online` 为 `None` 绝不能当离线**，否则会误报到爆。
所以本设计**故意**把「在线」和「未知」折进同一个 `ONLINE`，让「未知」天然走向「不告警」。

```python
account_evidence = "online"  if error_message.startswith("QQ account is online") else \
                   "unknown" if error_message.startswith("QQ account status is unknown") else None
```

**但这个折叠只对告警成立，对测量不成立（审计 F-07）。** 初稿写「`account_evidence` 只写进账本，
不参与任何告警判定」——前半句把它降格成了诊断装饰，于是账本按 `AccountView == ONLINE` 切段，
把「从未证明过账号在线」的时段也算成了会话时长。后果很具体：
哨兵启动后长时间只有 `account_online=None`（Hermes 刚起、心跳还没来），随后一次 auth-required，
就会产出一段**非删失的 `kicked` 区间** —— 一段从未证明账号在线过的「在线时长」直接进中位数。

**rev-1 把两个视图明确分开：**

| | 告警视图（`decide.py`） | 测量视图（`ledger.py`） |
|---|---|---|
| 输入 | `AccountView`（unknown 折进 `ONLINE`） | `AccountView` **＋** `account_evidence` |
| `account_evidence == "unknown"` | 不告警（保持初稿行为，正确） | **不能开启一段可计入统计的区间**；已开启的区间被标记降级 |
| `account_evidence is None`（legacy / 无 `error_message`） | 不告警 | 同上，且整段 `end_censored=true`（§6.4.3） |

准确表述：**`account_evidence` 不参与任何告警判定，但它是测量口径的一等公民。**
具体规则见 §10.2.2。

### 4.2 告警类别

| 类别 | 触发观测态 | 确认拍数 | 是否需要人 | 说明 |
|---|---|---|---|---|
| `A0_SOURCE_INCOMPATIBLE` | `SOURCE_INCOMPATIBLE` | `SOURCE_INCOMPATIBLE_CONFIRM_TICKS = 5` | 是（部署问题） | 见 §6.4，每 24 小时最多一条 |
| `A1_AUTH_REQUIRED` | `AUTH_REQUIRED` | 3 | **是** | 核心告警。文案按 §6 的登录方式分支 |
| `A2_LINK_DOWN` | `LINK_DOWN` | 10 | 视时长 | 瞬时故障会自愈，去抖窗口开到 10 分钟（§5） |
| `A3_OBSERVER_DOWN` | `OBSERVER_DOWN` | 3 | 是 | Hermes 死了。**文案不再包含探针结论**（§3.3 已删除自动探针），改为「QQ 侧状态未知」 |
| `A4_WRITER_UNVERIFIABLE` | `writer_state == UNVERIFIABLE`（横切）**或** `state_age == IMPLAUSIBLE_FUTURE` | 5 | 是（部署/时钟问题） | rev-1 新增。每 24 小时最多一条。见 §4.1、§3.2.2、§3.2.3 |
| `A5_FLAPPING` | 一小时内 ≥ `EPISODE_FLAP_THRESHOLD` 次「掉线→恢复」往返 | — | 是 | rev-1 新增。**取代**该小时内的逐次 A1/A2/恢复通知，见 §5.5 |

`A4` / `A5` 都是 rev-1 按审计新增的：前者堵住「我判断不了却不说」（F-04），
后者堵住「抖动时把人吵到关掉告警」（F-11）。两条都服从 §11.6：判断不了要说出来，
但**说一次**，不是每分钟说一次。

### 4.3 明确**不**告警的情况

- `ONLINE`（含 `account_evidence == "unknown"`）—— 任务的硬要求。
- 单次采样异常：**任何类别都要连续 N 拍确认**，拍数见上表（rev-1 补齐了 A0/A4，
  初稿这句话是空头支票：常量表里只有四个类别有拍数）。
- 静音窗口内（`mute_until` 文件，§5.4）。
- `A5_FLAPPING` 生效期间的单次 A1/A2 与恢复通知（被合并成一条，§5.5）。
- `state` 被 `gateway/run.py` 的重连机器短暂改写：`gateway/run.py:12624/12732/14051` 等处
  会对同一个 `platforms.onebot` 键写 `connecting`/`connected`/`retrying`，**多写者共享同一字段**。
  adapter 的 `_health_loop` 每 30 秒会把账号真相重新盖回去，所以错误值最多存活 30 秒，
  被去抖窗口吸收。**这是去抖窗口必须 ≥ 3 拍的直接理由之一。**
  ⚠️ 同一个抖动对**账本**同样有害（会伪造短会话），所以账本也必须用确认后的转换切段 —— 见 §10.2.1。

---

## 5. 问题 2 —— 去抖与冷却

### 5.1 节拍

`TICK_SECS = 60`（systemd timer `OnUnitActiveSec=60s`，`AccuracySec=5s`）。

理由：写者（adapter `_health_loop`）周期是 30 秒，账号探针周期
`ACCOUNT_PROBE_SECS = 60.0`。60 秒的采样节拍恰好是写者的整数倍，且不比账号探针更快 ——
比数据源刷新还快的采样只会读到重复值，白白抬高噪声敏感度。

### 5.2 确认拍数（N 拍连续同态才算数）

| 常量 | 值 | 首次告警延迟 | 理由 |
|---|---|---|---|
| `AUTH_REQUIRED_CONFIRM_TICKS` | **3** | ~2–3 分钟 | 3 拍 = 180 s，跨过一个完整的 `ACCOUNT_PROBE_SECS=60` 探针周期加余量，也吸收 §4.3 那个 ≤30 s 的多写者抖动。被踢是**粘性**状态（`_set_account_status` 只接受显式证据，`None` 永远不会把状态改成离线），不需要更长的确认；再长就是在浪费历史上 9.7 h / 11.7 h 的空窗本来要省下的时间 |
| `LINK_DOWN_CONFIRM_TICKS` | **10** | ~10 分钟 | adapter 自己要 `HEALTH_LOST_SECS = 120` 秒无帧才宣告链路静默；再叠 10 分钟连续静默，意味着重连阶梯（1→2→5→10→30 s，末档重复）已经失败 20 次以上，不再是「瞬时」。10 分钟也稳稳覆盖 NapCat 进程内部重初始化窗口（H1 §8.7，秒级）和 autossh 隧道重建（`RestartSec=30s` + `ConnectTimeout=10s`，H1 §8.2） |
| `OBSERVER_DOWN_CONFIRM_TICKS` | **3** | ~5 分钟 | 文件在 180 s 后才算陈旧（= 6 个漏掉的 30 s 刷新，且大于 `gateway/status.py` 自己的 `_RUNTIME_STATUS_STALE_TTL_S = 120`），再要 3 拍连续 ⇒ Hermes 静默约 5 分钟后告警。正常 deploy 重启是秒级，不会触发 |
| `SOURCE_INCOMPATIBLE_CONFIRM_TICKS` | **5** | ~5 分钟 | rev-1 新增（审计 F-05）。初稿漏了它，导致单次 `error_message=None` 就能触发 A0 并顺手把 A1/A2 停用。理由见 §4.1.3 |
| `A4_CONFIRM_TICKS` | **5** | ~5 分钟 | rev-1 新增。`UNVERIFIABLE` 可能来自一次瞬时的 `/proc` 读失败，不要单拍就喊 |
| `RECOVER_CONFIRM_TICKS` | **10** | ~10 分钟 | rev-1 从 3 改为 10（审计核验点 11：3 拍只能过滤 < 3 分钟的抖动）。恢复通知是「事情结束了，你可以不管了」的信号，**它发早了比发晚了危险** —— 人会据此停止关注，然后 4 分钟后又掉了。与 `LINK_DOWN_CONFIRM_TICKS` 对齐 |
| `RECOVER_CONFIRM_TICKS_AFTER_FLAP` | **30** | ~30 分钟 | rev-1 新增。当本 episode 在过去 1 小时内已经有 ≥2 次往返时，恢复确认要求提高到 30 拍。抖动越厉害，「它真的好了」这句话越贵 |

### 5.3 冷却与降频

同一告警类别在条件持续未解除期间的重发节奏：

| 常量 | 值 |
|---|---|
| `ALERT_COOLDOWN_SECS` | **3600**（1 小时） |
| `COOLDOWN_ESCALATE_AFTER` | **3** 个冷却窗口（rev-1 从 6 下调） |
| `ALERT_COOLDOWN_LONG_SECS` | **21600**（6 小时） |
| `GLOBAL_HOURLY_CAP` | **6** 条/滚动小时（跨全部类别，**含汇总消息与恢复通知**） |

**计数定义（初稿在这里有歧义，审计 F-11 要求写死）：**

> **首次告警是第 1 个冷却窗口的开始。** 即 `window_index` 从 **1** 开始计数，
> 第 `k` 条告警发生在 `t0 + (k-1) × ALERT_COOLDOWN_SECS`（`k ≤ COOLDOWN_ESCALATE_AFTER`）。
> 达到 `COOLDOWN_ESCALATE_AFTER` 之后，冷却切换为 `ALERT_COOLDOWN_LONG_SECS`。

**逐条时间线（把审计要求的 9.7 h / 11.7 h 场景算完，这就是 golden test 的期望值）：**

| 事件 | t（从掉线起） | 第几条 | 说明 |
|---|---|---|---|
| 掉线发生 | 0:00 | — | |
| 3 拍确认完成 | 0:03 | **#1** | A1 首发 |
| 第 2 个冷却窗口 | 1:03 | **#2** | |
| 第 3 个冷却窗口 | 2:03 | **#3** | 到达 `COOLDOWN_ESCALATE_AFTER = 3`，切长冷却 |
| 长冷却 6 小时 | 8:03 | **#4** | |
| H1 §3 的 9.7 h 场景在此结束 | 9:42 | — | |
| 恢复确认（10 拍） | 9:52 | **#5** | 恢复通知，带中断时长 |
| **9.7 h 场景合计** | | **5 条** | 初稿算法是 8 条 |
| 11.7 h 场景：第 5 个窗口 | 14:03 | — | 未到，11.7 h 场景在 #4 之后即恢复 |
| **11.7 h 场景合计** | | **5 条** | |

`GLOBAL_HOURLY_CAP` 是防呆闸：任何 bug 造成的抖动，一小时最多打扰 6 次，超出部分合并成
一条 `⚠️ 告警被限流：过去一小时抑制了 N 条` 的汇总。
**这条汇总消息本身也计入 `GLOBAL_HOURLY_CAP`**（初稿没说，于是上限实际是 7 条/小时 —— 审计 F-11）。
上限用满之后**完全静默直到下一个滚动小时**，但静默期间抑制的条数会累加到下一条汇总里。

### 5.4 运维静音

`${STATE_DIR}/mute_until` 文件，内容一行 RFC3339 时间戳（例：`2026-09-01T18:00:00Z`）。

- 当 `now < mute_until` 时**不发送任何告警**，但状态机与账本**照常运行**。
- 静音期结束后的第一条告警必须带一行 `（静音期内曾发生：<类别> × N）`，
  否则静音就变成了另一个 9.7 小时空窗。
- 该文件由运维手写，组件只读不写。用途：计划内维护、B3 的生产变更窗口、家宽迁移当天。

### 5.5 episode 级退避与 `A5_FLAPPING`（rev-1 新增，审计 F-11）

上面的冷却只管「同一条件持续未解除」。它管不住另一种形态：
**反复掉线 / 反复恢复**（例：NapCat 每 10 分钟掉一次、3 分钟后自己回来）。
每一轮都是一个新 episode，每个 episode 都能合法地发一条 A2 + 一条恢复通知
⇒ 一小时 6 条，天天如此，人会关掉告警，然后连真正的掉线一起错过。

```
EPISODE_FLAP_WINDOW_SECS   = 3600      # 观察窗口
EPISODE_FLAP_THRESHOLD     = 3         # 窗口内 ≥3 次「确认掉线→确认恢复」往返
FLAPPING_ALERT_COOLDOWN_SECS = 21600   # 抖动期内每 6 小时一条
```

进入 flapping 模式后：

- **停发**逐次的 A1/A2 与逐次的恢复通知；
- 改发一条 `A5_FLAPPING`，内容是「过去 N 分钟内 QQ 链路往返了 M 次，最长在线 X 分钟，
  最短 Y 分钟；我不再逐次通知，每 6 小时汇报一次；要看细节跑 `report --recent`」；
- 每 `FLAPPING_ALERT_COOLDOWN_SECS` 重发一次，直到窗口内往返次数回落到阈值以下；
- 退出 flapping 模式时发一条收尾（「已连续稳定 30 分钟」，用 `RECOVER_CONFIRM_TICKS_AFTER_FLAP`）。

**账本不受 flapping 模式影响**：抖动出来的每一段仍照实记录，但按 §10.2 全部是 `end_censored=true`
（`link_down` 结束的区间一律删失），所以它们不会污染中位数，也不会伪造出「大量独立样本」。

---

## 6. 问题 3 & 4 —— 恢复通知与短信验证

### 6.1 问题 3：恢复通知 —— **发，但有条件**

**结论：发。** 判据与规则：

1. **只有真的发过该类别的告警，才发对应的恢复通知。** 从不发「QQ 恢复了」而之前没人被吵过。
2. 需要 `RECOVER_CONFIRM_TICKS = 10` 拍连续 `ONLINE` 才算恢复（rev-1 从 3 提到 10）；
   若本 episode 在过去 1 小时内已有 ≥2 次往返，提高到 `RECOVER_CONFIRM_TICKS_AFTER_FLAP = 30` 拍。
   **恢复通知不需要与升起对称**——这是初稿的一个错误直觉：升起晚了人晚知道 3 分钟，
   恢复发早了人会**停止关注**然后错过 4 分钟后的再次掉线。两个方向的代价不对称，参数就不该对称。
3. 恢复通知**不受 `ALERT_COOLDOWN_SECS` 约束**（它是一次性的收尾），但仍计入 `GLOBAL_HOURLY_CAP`。
4. 恢复通知**必须带本次中断时长**（首次确认 → 首次恢复），并注明该时长的下界性质
   （账本里对应区间若被删失，文案要说「至少 X 分钟」而不是「X 分钟」）。
5. flapping 模式下不发逐次恢复通知（§5.5）。

**为什么必须发**：H1 §6.1 是一次真实事故 —— 用户报告「已经登录」，但目标实例根本没有产生
账号就绪证据，二维码还在每分钟刷新。人做完手工动作后**没有任何反馈渠道确认动作生效**。
恢复通知就是那条反馈。它把「我扫完码了吧？」变成一个有据可查的事实。

顺带：中断时长这个数字正是本方案要证伪核心假设的原料（§10），恢复通知等于把它顺手播报一次。

### 6.2 问题 4：短信验证必须被单独说出来

被踢后 NapCat 日志会出现
`https://ti.qq.com/safe/tools/captcha/sms-verify-login?...`。这时人要做的事和扫码
**完全不同**（要拿手机收短信，扫码解决不了），所以告警必须点名说清楚。

**登录方式分类 `login_hint`，取值与优先级（高→低）：**

| `login_hint` | 判据（对 NapCat 日志窗口做大小写不敏感子串匹配） | 人要做什么 |
|---|---|---|
| `sms_verify` | `ti.qq.com/safe/tools/captcha/sms-verify-login` | **拿手机过短信验证** |
| `duplicate_login_guard` | `duplicate_login_guard` 或 `账号已经登录` | 命中 NapCat 的重复登录 guard（H1 §8.6），**只能人工重启 `napcat-native.service`**；本组件不会做 |
| `qr_scan` | `qrcode` / `二维码` / `getQRCodePicture` | 扫一次当前二维码 |
| `kicked` | `KickedOffLine` / `你的账号当前登录已失效` | 被踢，登录方式待定 |
| `unknown` | 上面都没命中，或 hint 缺失/过期 | 打开 WebUI 自行确认 |

优先级按「人的下一步动作差异有多大」排序，不按日志出现顺序。

**不转发验证链接。** `sms-verify-login` URL 与二维码同级敏感（H1：不要把二维码、WebUI token
或 OneBot token 发到聊天里）。告警只说「需要短信验证」并指出链接在哪台机的哪条 journal 里。

### 6.3 hint 从哪来 —— 拆成 T3-B，且 T3-A 不依赖它

问题在于：NapCat 日志在 `cornna-cn`，告警器在 `corlinman-prod`，而 OneBot 协议里没有任何
动作能透出「为什么离线」（H1 §8.5 实测：被踢后 `get_status` 只给 `online=false, good=true`）。

因此分两步交付：

**T3-A（先上，自足）** —— 告警器不依赖 hint 也完整可用。hint 缺失或过期时
`login_hint = "unknown"`，文案里带一行明确的「登录方式未确定」，**绝不静默降级**。

**T3-B（后上，闭合问题 4）** —— 三件小东西：

1. `cornna-cn`：**扩展已有的** `/usr/local/libexec/napcat-native-health`，
   新增一段对 `journalctl -u napcat-native.service --since -30min` 的固定标记扫描，
   把结果写进**持久化**路径 `/var/lib/napcat-native-health/login_hint.json`
   （**不是 `/run`** —— tmpfs 重启即失，对跨机消费毫无价值）。
   unit 需相应加 `ReadWritePaths=/var/lib/napcat-native-health`（它是 `ProtectSystem=strict`）。
   **该脚本仍然一条告警都不发**，职责只增加「分类」，不增加「通知」。
2. `cornna-cn`：新增只读打印器 `/usr/local/libexec/napcat-login-hint-print`
   （root:root 0755），内容只有 `cat /var/lib/napcat-native-health/login_hint.json`。
3. 一把**专用的、被 forced-command 锁死的** SSH key：
   `authorized_keys` 条目形如
   `command="/usr/local/libexec/napcat-login-hint-print",restrict ssh-ed25519 ...`。
   私钥放 `${STATE_DIR}/hint_id_ed25519`（0600，属主为 sentinel 用户）。

   `restrict` 关掉端口转发、agent 转发、pty、X11 —— **这把钥匙无法被用来隧道到 `:6099`，
   也无法拿到 shell**。约束由 `cornna-cn` 的 sshd 强制执行，不是由我们的代码 if 出来的，
   这是它比「代码里维护一张接口白名单」强得多的地方。

   `ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${STATE_DIR}/known_hosts -i ${STATE_DIR}/hint_id_ed25519 <user>@<cornna-cn>`，
   总超时 `HINT_SSH_TIMEOUT_SECS = 10` 秒。hint 超过 `HINT_MAX_AGE_SECS = 900` 秒未更新即视为过期。

#### 6.3.1 拉取时机：不是「只在首次确认时一次」（审计 F-13）

初稿写「调用只在 A1 事件首次确认时发生一次，不是每拍都拉」。**这条规则和 900 秒的 hint 有效期
组合起来会把 T3-B 变成摆设**，失败时序是确定的：

```
t+0     账号被踢。NapCat 日志此刻只有 [KickedOffLine]，还没有 sms-verify-login 行。
t+3min  A1 首次确认 ⇒ 拉一次 hint ⇒ 拿到 login_hint = "kicked"（登录方式待定）。
t+8min  NapCat 尝试自动重连失败，日志出现 ti.qq.com/.../sms-verify-login。
        cornna-cn 上的 login_hint.json 更新为 "sms_verify"。
t+18min 本地缓存的 hint 超过 900 秒 ⇒ 过期 ⇒ login_hint 退化为 "unknown"。
t+1h03  第 2 条告警：「登录方式未确定」。
t+2h03  第 3 条告警：「登录方式未确定」。
```

⇒ **用户明明需要拿手机过短信验证，却只会一直收到泛化的「登录方式未确定」。**
问题 4（「短信验证必须被单独说出来」）根本没有被闭合。

**rev-1 的拉取时机**（三个触发点，共用一套限流）：

| 触发点 | 条件 |
|---|---|
| ① A1 首次确认 | 与初稿相同 |
| ② A1 episode 进行中且本地 hint 已过期或为 `unknown`/`kicked` | 每 `HINT_MIN_INTERVAL_SECS = 600` 秒最多一次 |
| ③ 每条 A1 重发（冷却窗口）之前 | 若 hint 过期则先刷新，确保发出去的文案是当前的 |

限流与上限：

```
HINT_MIN_INTERVAL_SECS       = 600     # 两次 ssh 之间的最小间隔，保留原有节流
HINT_MAX_FETCHES_PER_EPISODE = 73      # 首次 + 每 10 分钟一次，覆盖到 episode 第 12 小时
```

`73 = 1 + 12 × 3600 / 600`：首次拉取后，即使每次都恰好卡在最小间隔，也能在第 12 小时再刷新一次，
完整覆盖 H1 已观测到的 9.7 h / 11.7 h 空窗；上限仍然存在，不会把 episode 内拉取变成无限循环。

**hint 升级要主动播报**：当 `login_hint` 从 `unknown` / `kicked` 变成 `sms_verify`
或 `duplicate_login_guard` 时，**立即发一条不受冷却约束的更新告警**（每 episode 最多一条，
仍计入 `GLOBAL_HOURLY_CAP`）：

```
🔵 QQ 哨兵：补充信息 —— 这次不是扫码能解决的

刚才那条掉线告警里我说"登录方式未确定"。现在确定了：需要短信验证。
你要做的事：拿手机，去 cornna-cn 上按 journal 里的验证链接走短信流程。
（链接不转发到这里，见 §6.2；在 cornna-cn 上跑 journalctl -u napcat-native.service 自己看）
```

**人的下一步动作变了，就必须再说一次。** 这是问题 4 真正闭合的地方 ——
不是「有一个 hint 字段」，而是「hint 变了会告诉你」。

A1 episode 结束（恢复确认）后，本 episode 的拉取计数清零。
episode 之外**永不**拉取：不掉线的时候没有任何理由去 ssh 另一台机器。

**为什么不让 `cornna-cn` 那台自己发告警**（这是主线程点名要求论证的取舍）：
它 `api.telegram.org` 不可达（实测 curl 000），这是死结；它的状态在 `/run` tmpfs 上，
跨机不可见、重启即失；它也完全看不到 Hermes 的死活，而「Hermes 挂了」是本组件必须覆盖的告警之一。
所以 `napcat-native-health` **保持为纯本地只读探针**，只多产出一个字段，不承担通知职责。

### 6.4 数据源自检（`SOURCE_INCOMPATIBLE`）

#### 6.4.1 新格式的指纹：四个 `error_message` 前缀

新 adapter 写出的 `platforms.onebot.error_message` 必以下列四者之一开头
（字面量出自 `adapter.py:1979-1996` `_publish_account_runtime_status()`）：

```
"QQ account is offline/auth-required; manual login is required"
"OneBot WebSocket link is offline; reconnect is in progress"
"QQ account is online"
"QQ account status is unknown (awaiting heartbeat/probe)"
```

#### 6.4.2 旧格式的指纹（§1.2 实测得到，可精确识别）

```jsonc
"onebot": {"state": "connected", "error_code": null,
           "error_message": null,                       // ← 恒为 null，这是判别特征
           "updated_at": "2026-08-30T18:36:57.553362+00:00"}   // ← 可以陈旧到 15 小时
"gateway_state": "running"                                     // ← 且进程活着
```

即 **`error_message` 恒为 `null` ＋ `updated_at` 陈旧 ＋ 写者进程存活** 这三者同时成立，
就是「旧 adapter 在跑」的确定性指纹。旧路径上写 `platforms.onebot` 的只有
`gateway/platforms/base.py:3434` 和 `gateway/run.py`，它们传的是
`error_message=None` 或一个异常字符串，两者都不会命中 §6.4.1 的四个前缀。

判定按 §4.1.2 的闩锁做，**不是逐次采样直接下结论**。

#### 6.4.3 触发时的行为

`AccountView == SOURCE_INCOMPATIBLE`（**经 `SOURCE_INCOMPATIBLE_CONFIRM_TICKS = 5` 拍确认**，§4.1.3）时：

1. 发 `A0_SOURCE_INCOMPATIBLE` 告警，**每 24 小时最多一条**，直到修好；
2. 进入**降级模式**：`A1_AUTH_REQUIRED` / `A2_LINK_DOWN` 停用
   （它们的信号源确实是空的，发出来只会是假的），
   **`A3_OBSERVER_DOWN` 保持启用** —— 它在 legacy 下靠 `writer_state == DEAD` 判定，不依赖格式（§4.1.1）。
   ⚠️ 例外：`writer_state == UNVERIFIABLE` + legacy 时连 A3 都不可靠，此时由 `A4` 负责说明（§4.1）；
3. 账本照记，但该段区间标 `source_format="legacy"`、`end_censored=true`、`start_censored=true`、
   `account_evidence=null`，**不进入任何统计** —— 旧 adapter 下的「在线」不是账号在线，
   把它算进去会污染迁移对比。

**降级模式只在 `verdict == "legacy"` 时进入，`unknown` 不进入**（§4.1.2）。
初稿把「还没确定」和「确定是旧的」混为一谈，代价是每次 gateway 重启后的头几分钟里
`A1_AUTH_REQUIRED` 都可能被误关掉。

#### 6.4.4 告警文案必须说人话

⛔ 不要发「数据源不兼容」「schema mismatch」这类词。收告警的是要去干活的人，不是编译器。

```
🟠 QQ 哨兵：装上了，但现在判断不了 QQ 是否在线

原因：生产上跑的 Hermes 还是旧版 adapter，它不写账号状态，
      所以 gateway_state.json 里永远不会出现"账号被踢"这件事。

现在的真实处境：QQ 掉线了我也不会告诉你 —— 和没装哨兵一样。

要做的事：完成 T1 部署（account-state 加固推上生产），
          清单见 docs/orchestration/results/T1.md。

在那之前，我只保留一个能力：Hermes 进程死了会通知你。
本告警每 24 小时重复一次，直到 T1 部署完成。
```

**这是本设计里最重要的防呆。** 没有它，一个装好了但数据源是空的告警器会安静地什么都不做，
把人重新送回 9.7 小时空窗 —— 而且这次还带着「我装了告警」的错误安全感。
§1.2 的实测正是这个失败模式的实物样本：`hermes.service` active、文件写着 `connected`、
账号已经离线 2 小时 16 分钟。


## 7. 问题 5 —— 落点选择

### 7.1 结论

**`corlinman-prod` 上独立的 systemd oneshot + timer（`hermes-qq-sentinel`），只读。**
不做成 Hermes 插件内的后台循环，也不扩展 `cornna-cn` 上的 `napcat-native-health`。

### 7.2 四个候选的逐项对照

| 维度 | ①Hermes 插件后台循环 | ②**corlinman-prod systemd timer（选定）** | ③扩展 cornna-cn 的 `napcat-native-health` | ④hermes cron job |
|---|---|---|---|---|
| 能不能发出 Telegram | 能（本机直连可达） | **能** | **不能**（实测 curl 000，死结） | 能 |
| Hermes 挂了谁告警 | **没人。和被监控对象同生共死** | **systemd 还在，照常告警** | 看不到 Hermes | **没人**（cron 调度器在 gateway 进程里） |
| 会不会新开 WS 连接 | 不会（进程内直调 `health_snapshot()`） | **不会**（只读文件）。rev-1 已删除自动短探针，`run` 路径无任何 OneBot 出网分支（§3.3） | 不适用 | 不会 |
| 信号新鲜度 | 最好（毫秒级） | 30 秒粒度（写者周期） | 5 分钟 | 60 秒 |
| 沙箱可审计性 | 无（跟 gateway 同权限，能 import 全世界） | **`systemctl show` 可验；`napcat-native-health` 已是现成范本** | 好 | 无 |
| 状态持久化 | 需自建 | 文件，随机器 | **`/run` tmpfs，重启即失** | 需自建 |
| 对会话时长数据的影响 | **每次 Hermes 重启都会伪造一次「区间结束」，数据被污染** | 能把 Hermes 死亡标成 `end_censored`，数据干净 | 不适用 | 同① |
| 框架成本 | **高**：`VALID_HOOKS` 里没有 gateway 启动钩子；`ctx.spawn_task` 全仓库零生产调用者，且 `register()` 阶段没有 running loop | 无（不 import 任何 Hermes 框架代码） | 低 | 中 |

### 7.3 「Hermes 自己挂了谁来告警」—— 这是决定性的一条

必须把这条单拎出来，因为它不是一个边缘情形，而是**本项目反复出现的运行形态**：

**H1 记录了三次 `hermes.service` 处于 stop/disable/inactive 的运行窗口，
其中一次明确是账号掉线处置：**

| H1 出处 | 性质 | 是否账号掉线处置 |
|---|---|---|
| H1 §8.5（`:345-373`） | 账号出现真实掉线后，运维**主动 stop/disable `hermes.service`**，避免旧版 adapter 在账号离线时继续动作 | **是** |
| H1 §7.2（`:239-250`） | **切主操作**：切换主实例过程中 `cornna-cn:hermes.service` 被 stop/disable | 否（计划内变更） |
| H1 §6（`:137-153`） | **staged 实例观察期**：整个观察期内 `hermes.service` 保持 inactive，等待认证 | 否（部署阶段） |

> rev-1 修正（审计 F-14）：初稿把这三条一并概括成「在账号出问题的时候把 Hermes 停掉，
> 是这个项目的标准处置」。**这个概括超出了证据。** 只有 §8.5 是账号掉线处置，
> 另外两条是切主和 staged 部署。架构结论不受影响 —— 准确的表述是：
> **Hermes 有三个已记录的、时长可观的非运行窗口，其中一个恰好发生在账号掉线之后；
> 这足以证明告警器不能依附 Hermes 的生命周期。**
> 论证要靠得住，不是靠说得响。

一个住在 Hermes 里的告警器，会在这三种窗口里全部闭嘴，而其中一种恰恰是账号出事的时刻。

方案①还有一个更隐蔽的问题：它**无法报告自己的死亡**。gateway OOM、事件循环卡死、
OneBot 插件因为 `_platform_name_from_manifest` 那个「静默失败」的坑没加载上 —— 这些情况下
插件循环根本不存在，而外部看不出和「一切正常」有任何区别。方案②里，
「文件不再更新」本身就是一个明确的、有阈值的告警信号（`A3_OBSERVER_DOWN`）。

### 7.4 方案①能买到什么，以及为什么不够

方案①唯一的实质优势是可以直接调 `health_snapshot()`（`adapter.py:2019`），
拿到 `heartbeat_age_seconds` / `last_heartbeat_at_ms` 等 `gateway_state.json` 里没有的数值字段。

但本设计的两个消费者都不需要这些字段：告警只需要五值枚举；账本按**自己的采样时刻**记录
区间边界（60 秒分辨率对一段约 2 小时的区间是 0.8% 误差，见 §10.3）。所以这个优势换不到东西。

若将来真的需要结构化字段，正确做法是让 adapter 多写一个字段，而不是把告警器搬进 Hermes ——
`AGENTS.md:876-881` 明令插件不得改核心文件，`gateway/status.py` 的 `write_runtime_status`
也只接受固定 kwargs，加字段是核心改动，要单独评审。**本设计不需要它。**

### 7.5 诚实交代的残余风险

**`corlinman-prod` 整机死掉（宕机、systemd 挂掉、网络全断）时，没有任何东西会告警。**
这是本设计覆盖不了的盲区，机上任何方案都覆盖不了。

半价缓解措施（已纳入设计，默认开启）：**每日心跳**。
每天 `HEARTBEAT_LOCAL_TIME = "09:00"`（`Asia/Shanghai`，与 D1/D2 定下的
`HERMES_TIMEZONE=Asia/Shanghai` 约定一致）发一条日报，含过去 24 小时的在线率、
区间数、最长/最短区间。**沉默本身因此变成可察觉的信号** —— 早上没收到日报就是出事了。
这不是真正的 dead man's switch（那需要一个机外的监控服务），但成本是零，且覆盖了整机失联。

#### 7.5.1 心跳的可执行契约（rev-1 新增，审计 F-12）

初稿只给了一个常量 `HEARTBEAT_LOCAL_TIME = "09:00"`，没定义调度方式、错过窗口怎么办、
发送失败怎么办、会不会重复发。这不是契约，是一个愿望。补齐：

**调度**：不使用第二个 systemd timer（多一个 unit 就多一个会漂的东西）。
在每一拍的 `run` 里判断，状态持久化在 `state.json`：

```jsonc
"heartbeat": {
  "last_sent_local_date": "2026-09-01",   // Asia/Shanghai 的日历日
  "attempts_local_date": "2026-09-01",    // attempts_today 属于哪一个本地日
  "attempts_today": 0                     // 当日发送尝试次数
}
```

规则：

1. 每一拍先取当前 `Asia/Shanghai` 本地日期。若 `attempts_local_date != today`（含旧状态缺字段），
   **先**写 `attempts_local_date = today` 并把 `attempts_today = 0`；这一步不受 09:00 发送窗口限制。
2. 若 `local_time >= 09:00` 且 `last_sent_local_date != today`
   且 `attempts_today < HEARTBEAT_MAX_ATTEMPTS = 3` ⇒ 发送。
3. 发送**成功**后写 `last_sent_local_date = today`；发送**失败**只 `attempts_today += 1`，
   下一拍重试（最多 3 次，之后放弃当日心跳并在日志里留痕 —— 不为一条心跳耗尽消息预算）。
   到下一个 `Asia/Shanghai` 本地日时，第 1 条规则重新开放 3 次尝试，不能继承前一天的失败计数。
4. **错过窗口照发**：机器 09:00 时是关的、14:00 才开机 ⇒ 14:00 那一拍照发，
   文案里加一行「本条日报延迟发送（计划 09:00，实际 14:03）」。
   **延迟本身就是信息** —— 它说明机器有一段时间不在。
5. **不补发历史**：跳过的日期不追发，日报只描述过去 24 小时。
6. 心跳计入 `GLOBAL_HOURLY_CAP`。

**夏令时**：`Asia/Shanghai` **自 1991 年起不实行夏令时**，本设计不需要处理 DST 跳变。
（实现仍用 `zoneinfo.ZoneInfo("Asia/Shanghai")`；若系统缺 tzdata 导致构造失败，
回落到固定 UTC+8 并在日报里加一行「时区数据库缺失，已按固定 UTC+8 计算」——
静默回落是 §11.6 禁止的。）

---

## 8. 问题 6 —— 权限边界（可审计形式）

### 8.1 硬约束的正面表述

> **`hermes-qq-sentinel` 不得拥有任何重启容器/服务、触发登录、或调用 NapCat WebUI 写接口的能力。**

下面把它翻译成**可被外部命令逐条验证**的形式。四节的**证据强度不同，rev-1 明确标出**
（初稿把它们并称「四层权限边界」，审计 F-03 指出这个说法不成立）：

| 节 | 内容 | 性质 |
|---|---|---|
| §8.2 | 四张 allowlist 常量 | **声明**（文档 + 防回归断言）|
| §8.2.1 | `probe_get_status()` 单一封装 | **强制**（代码层唯一出口 + 运行时校验）|
| §8.3 | systemd 沙箱 | **强制**（内核）|
| §8.4 | 三条禁令的审计命令 | 复核手段（部分内核强制、部分只是 grep）|
| §8.5 | 静态扫描测试 | **防回归**，不是边界 |

**真正挡住事情发生的是 §8.2.1 和 §8.3。** 其余三节是让人能验证、能发现回归。
把这两类混为一谈，会得到一种「我们有四层防护」的错觉 —— 而其中两层一推就倒。

### 8.2 接口白名单 —— **常量是声明，`probe_get_status()` 才是强制**（审计 F-02）

初稿把这四张常量表称作「第一层强制」。**它们不强制任何东西**，审计的核对是对的：

- `PROBE_ACTION_ALLOWLIST` 既没传进 `onebot_call()`，也没有调用前后的校验；
  `onebot_call()` 接受任意 action 字符串。
- `NET_EGRESS_ALLOWLIST` 只是一个字面量元组，不限制任何实际目的地。
- 最要命的一条：**设计声称探针只走 `ws://127.0.0.1:3011`，但 `onebot_base_url()`
  优先读 `ONEBOT_HTTP_URL`**（`tools/onebot_client.py:57-65`，docstring 原文
  「Prefers ``ONEBOT_HTTP_URL`` and falls back to ``ONEBOT_WS_URL``」）。
  环境里只要存在这个变量，所谓「WS-only 到本地隧道」的探针就变成了一个
  **POST 到别处的 HTTP 请求** —— 而 `.env` 里恰好可能有它。

rev-1 的处理分两半：**这四张表降级为「文档 + 防回归断言」**（保留，它们有价值），
**真正的强制是 §8.2.1 的单一运行时封装 + §8.3 的内核沙箱**。

```python
# ops/qq_sentinel/constants.py —— 这四张表是"允许的对外接触面"的**声明**，
# 不是强制层。强制层见 §8.2.1（运行时封装）与 §8.3（systemd 沙箱）。
READ_PATHS_ALLOWLIST = (
    "${HERMES_HOME}/gateway_state.json",
    "${HERMES_HOME}/.env",              # 只取 TELEGRAM_BOT_TOKEN / ONEBOT_WS_URL / ONEBOT_ACCESS_TOKEN
    "${STATE_DIR}/state.json",
    "${STATE_DIR}/mute_until",
    "${STATE_DIR}/sessions.jsonl",
    "${STATE_DIR}/login_hint.json",     # T3-B
)
WRITE_PATHS_ALLOWLIST = ("${STATE_DIR}/",)          # 仅此一个目录
NET_EGRESS_ALLOWLIST = (
    "https://api.telegram.org/bot<token>/sendMessage",   # POST，唯一 HTTP 目的地
    "ws://127.0.0.1:3011",                              # OneBot，经已有 onebot-tunnel.service
)
PROBE_ACTION_ALLOWLIST = ("get_status",)            # OneBot 动作，**只有一个**
SUBPROCESS_ALLOWLIST = ()                           # T3-A：空。T3-B：唯一一条冻结 argv（§6.3）
```

`PROBE_ACTION_ALLOWLIST` 只有 `get_status` 这一项是关键：没有 `send_msg`（不会替 Hermes 说话）、
没有任何 `set_*`（不会改后端配置）。而且 **OneBot v11 协议本身就没有任何能让 QQ 账号登录的动作** ——
「触发登录」这件事在这条通道上不存在可用的原语。

#### 8.2.1 唯一出口：`probe.probe_get_status()`

全仓库**只有这一个函数**可以调用 `onebot_call()`；`ops/qq_sentinel/` 里任何其他模块
出现 `onebot_call` 即测试失败（§8.5）。它的契约：

```python
def probe_get_status() -> dict:
    """The ONE place this component may talk to OneBot.  Manual `probe`
    subcommand only — never reachable from `run` (see §3.3).

    Hard-wired, in order:
      1. action is the literal "get_status" — not a parameter, not configurable.
      2. ONEBOT_HTTP_URL must be empty/absent, else refuse.  onebot_base_url()
         PREFERS it over the WS URL (tools/onebot_client.py:57-65), so a stray
         value silently turns this "WS-only local probe" into an HTTP POST to
         somewhere else.
      3. The resolved URL must parse as scheme=ws, host in {127.0.0.1, ::1},
         port == 3011.  Validated with urllib.parse.urlsplit, not a substring
         match.
      4. prefer_live=False.  prefer_live=True makes onebot_call() import
         plugins.platforms.onebot.client (tools/onebot_client.py:146-155),
         which violates the "no Hermes imports in production code" rule (§10.1)
         and would silently reintroduce a Hermes dependency into the one
         component that must survive Hermes being broken.
      5. Post-call assertion: onebot_base_url() still resolves to the exact
         validated URL (guards against anything mutating os.environ mid-call).
    """
```

**运行时断言，不是常量字面值断言**（审计原话）。测试必须包含这些负例：

| 负例 | 期望 |
|---|---|
| `ONEBOT_HTTP_URL=http://evil.example/` + 合法 `ONEBOT_WS_URL` | **拒绝**，且 `onebot_call` 调用次数为 0 |
| `ONEBOT_WS_URL=ws://10.0.0.5:3011` | 拒绝（主机不是 loopback） |
| `ONEBOT_WS_URL=ws://127.0.0.1:6099` | 拒绝（端口不是 3011 —— 这正好是 WebUI 端口，必须挡） |
| `ONEBOT_WS_URL=wss://127.0.0.1:3011` | 拒绝（scheme 必须是 `ws`；隧道是本地明文） |
| `ONEBOT_WS_URL=ws://127.0.0.1:3011/../../x` | 拒绝（path 非空即拒） |
| 尝试用非 `get_status` 的 action 调用封装 | 封装**没有 action 参数**，这条在类型层面就不可表达 |

**环境变量的处理**（初稿没写，是个真坑）：哨兵从 `${HERMES_HOME}/.env` 读配置，
但 `onebot_call()` 是从 `os.environ` 读的。所以封装内部用一个 contextmanager：
调用前把校验过的 `ONEBOT_WS_URL` / `ONEBOT_ACCESS_TOKEN` 写进 `os.environ` 并
**删除** `ONEBOT_HTTP_URL`，调用后原样恢复。
⛔ 不要把 `.env` 整个 `load_dotenv` 进 `os.environ` —— 那等于把 `.env` 里任何一个
`ONEBOT_HTTP_URL` 直接交给传输层。

#### 8.2.2 出网收窄从「可选推荐」升级为**必选部署条件**

初稿把 `IPAddressDeny/Allow` 写成「可选但推荐」。审计要求提升为必选，采纳：
它是唯一能在**内核层**兜住「代码写错了目的地」的东西，而上面那个 `ONEBOT_HTTP_URL` 的洞
恰好证明了代码层的声明是会错的。具体配置见 §8.3，**验收清单里它是硬项**（§9 的 T11）。

### 8.3 systemd 沙箱（**内核强制**，`napcat-native-health.service` 是现成范本）

`cornna-cn` 上那个只读探针的写法直接照抄并加严：

```ini
[Service]
Type=oneshot
User=hermes-qq-sentinel
Group=hermes-qq-sentinel
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
# ⚠️ rev-1：初稿这里是 ProtectProc=invisible —— 那会让 /proc/<hermes pid> 对本 unit 不可见，
# 于是 §3.2.2 的 start_time / cmdline 永远读不到，PID 复用防护和身份校验全部失效
# （writer_state 恒为 UNVERIFIABLE）。哨兵的核心判据不能被自己的沙箱挡掉。
# default = 保持内核默认的 /proc 可见性；哨兵仍然无权 ptrace 或发信号给别人的进程。
ProtectProc=default
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @mount @debug @module @reboot @swap @obsolete
ReadWritePaths=/var/lib/hermes-qq-sentinel
ReadOnlyPaths=/opt/hermes/data /opt/hermes/repo
TimeoutStartSec=45

# rev-1：出网收窄从"可选推荐"升级为必选（§8.2.2）。
IPAddressDeny=any
IPAddressAllow=localhost
IPAddressAllow=149.154.160.0/20
IPAddressAllow=91.108.4.0/22
# 外加 /etc/resolv.conf 里的 nameserver（若不在 127.0.0.0/8）
```

**部署硬条件（rev-1，缺一不可，`selftest` 逐条验证）：**

| 条件 | 验证命令 | 期望 |
|---|---|---|
| `IPAddressDeny=any` 已生效 | `systemctl show hermes-qq-sentinel.service -p IPAddressDeny` | 非空 |
| `/proc/<hermes pid>/stat` 可读 | `sudo -u hermes-qq-sentinel test -r /proc/$(systemctl show -p MainPID --value hermes.service)/stat` | 退出 0 |
| `ProtectProc` 不是 invisible/noaccess | `systemctl show hermes-qq-sentinel.service -p ProtectProc` | `ProtectProc=default` |
| `gateway_state.json` / `gateway.pid` / `gateway.lock` 可读 | `sudo -u hermes-qq-sentinel head -c1 /opt/hermes/data/gateway.lock` | 退出 0 |

第 2、3、4 行是 rev-1 新增的：`writer_state`（§3.2.2）**完全依赖**这三样东西可读。
读不到不会崩，会退化成 `UNVERIFIABLE` 然后每天发一条 A4 —— 那是设计好的行为，
但它意味着组件的核心判据是瘸的，所以部署时必须验一遍。

启用 `IPAddress*` 前先跑一次 `--force-test-alert` 确认没把 DNS 挡死。

### 8.4 三条禁令各自的验证命令（复核手段）

| 禁令 | 为什么结构上做不到 | 审计命令（期望输出） |
|---|---|---|
| **不得重启容器/服务** | 非 root 专用用户；无 sudoers 条目；不在任何特权组；`NoNewPrivileges`；`SystemCallFilter=~@reboot`；`ProtectSystem=strict` 使 unit 文件不可写；`/run/systemd/private` 是 root-only；本机根本没有 docker | `sudo -l -U hermes-qq-sentinel` → *not allowed*；`id hermes-qq-sentinel` → 只有自身组；`grep -rn 'systemctl\|subprocess\|docker\|Popen\|os.system' ops/qq_sentinel/` → **0 命中**（T3-B 除外，见下） |
| **不得触发登录** | OneBot 动作白名单只有 `get_status`，且协议无登录原语；不接触 WebUI | `grep -rn 'PasswordLogin\|QuickLogin\|RefreshQRcode\|quick_login' ops/qq_sentinel/` → **0 命中** |
| **不得调用 NapCat WebUI 写接口** | 进程环境里**根本没有** WebUI token，也没有到 `:6099` 的路由（本机不监听 6099，隧道只转发 3011） | `systemctl show hermes-qq-sentinel.service -p Environment -p EnvironmentFiles` 中无 `NAPCAT_*` / `WEBUI_*`；`grep -rn '6099\|webui\|WebUI\|Process/Restart' ops/qq_sentinel/` → **0 命中**；`ss -ltn | grep 6099` 在 corlinman-prod 上为空 |

T3-B 引入唯一一条 subprocess，因此 `SUBPROCESS_ALLOWLIST` 从空变成**一条冻结的 argv 列表**，
且必须包含 `-o BatchMode=yes` 与 `-i ${STATE_DIR}/hint_id_ed25519`；对端由 forced-command
锁死（§6.3）。测试断言这条 argv 逐元素相等，且它只能出现在 `hint.py` 的 `_fetch_login_hint()`
里（§8.5 第 1 条）。

> **注意这三行「为什么结构上做不到」的证据强度不一样，别混着读**（审计核验点 1/3）：
> 「非 root + 无 sudoers + `SystemCallFilter`」是**内核强制**；
> 「grep 命中 0」只是**防回归**（§8.5）。
> 第三行的「进程环境里没有 WebUI token、本机不监听 6099」是**结构性**的（真的拿不到），
> 这条最硬。表格里的 grep 列是给审计人看的复核手段，不是边界本身。

### 8.5 静态扫描 —— **防回归检查，不是权限强制层**（审计 F-03）

> **rev-1 措辞修正。** 初稿把这条测试列为「四层权限边界」之一。它不是边界。
> 审计给出的绕过示例全部成立：`getattr(__import__("os"), "sy"+"stem")(...)`、
> `__import__("sub"+"process").run(...)`、`importlib.import_module()`、拆分字符串、
> 改大小写、`os.kill` / `signal` / `ctypes` / 裸 socket。
> **一个大小写敏感的源码子串扫描挡不住任何一个想绕过它的人。**
>
> 但这不是它的用途。**它面对的是未来的我们，不是攻击者。** 半年后有人在告警器里顺手加一句
> 「检测到掉线就重启一下 NapCat」，他不会去做字符串拼接绕过 —— 他会直接写 `subprocess.run`，
> 然后 CI 当场红掉，并把 H1 §8.4 的教训甩到他脸上（`napcat-watch.py` 就是这么诞生又这么被
> stop/disable 的）。**这个价值是真实的，只是它的名字应该叫防回归检查。**
>
> 真正的强制层是两个，都不在我们的代码里：
> **§8.2.1 的单一运行时封装**（唯一出口，做运行时校验）和 **§8.3 的 systemd 沙箱**（内核）。

`tests/ops/test_qq_sentinel_boundary.py`：

```python
FORBIDDEN_TOKENS = (
    "systemctl", "subprocess", "os.system", "Popen", "docker",
    "restart", "reboot", "PasswordLogin", "QuickLogin", "RefreshQRcode",
    "send_msg", "send_private_msg", "send_group_msg", "set_group", "set_friend",
    "delete_msg", "6099", "webui", "sudo",
    # rev-1 追加：审计点名的其他动作面
    "os.kill", "ctypes", "importlib", "__import__", "socket.socket", "eval(", "exec(",
)
```

rev-1 对扫描规则的三处收紧（按审计的「修」段）：

1. **不再豁免整行。** 初稿允许「`SUBPROCESS_ALLOWLIST` 所在的那一行」整行豁免 ——
   那等于开了一个只要把代码写在同一行就能钻的口子。
   改为：**只允许 `ops/qq_sentinel/hint.py` 这一个文件里的一个专用函数** `_fetch_login_hint()`
   使用 subprocess，且该函数的 argv 必须逐元素等于冻结常量（§8.4），
   函数体行数上限 40 行。扫描器对该文件做 AST 检查而不是子串豁免。
2. **扫描范围扩大**：`ops/qq_sentinel/**/*.py` **＋** `ops/qq_sentinel/systemd/*.{service,timer}`
   （unit 文件里出现 `ExecStartPre=` / `ExecStopPost=` / `systemctl` 同样失败）。
3. **被 import 的本地代码纳入范围**：断言 `ops.qq_sentinel` 的**传递 import 闭包**
   除标准库外只包含 `ops.qq_sentinel.*` 与 `tools.onebot_client`（后者仅允许出现在
   `probe.py` 的闭包里）。用 `ast` 遍历本地文件收集 import，不执行代码。
   这条同时兜住 §3.3 的「`run` 路径不许有 OneBot 传输符号」。

测试注释里必须写明 `napcat-watch.py` 那段来由，以及**这一节的标题**：
「防回归检查，不是权限强制层」。别让下一个人误以为有它就安全了。

---

## 9. 问题 7 —— 测试方式

### 9.0 先把「一拍」的时间预算和持久化顺序定死（rev-1 新增，审计 F-12）

初稿只规定了探针超时，**没有规定 Telegram 请求的超时**。`urllib.request.urlopen()`
不传 `timeout` 就用全局默认（可能是无限）。`TimeoutStartSec=45` 是 systemd 杀进程的上限，
不是 HTTP 超时 —— 一旦 Telegram API 或 DNS 卡住，oneshot 会占着 timer 直到被 SIGTERM，
**告警既没发出去、也没记下来「本该发」这件事**。这是一次完整的静默失败，
而且它恰好发生在需要告警的时刻。

```
NOTIFY_TIMEOUT_SECS        = 8      # 单次 Telegram HTTP 请求（urlopen(timeout=...)，必传）
NOTIFY_RETRIES             = 1      # 失败后重试 1 次
NOTIFY_TOTAL_BUDGET_SECS   = 20     # 一拍内所有发送合计的硬上限，超了就留到下一拍
HINT_SSH_TIMEOUT_SECS      = 10     # §6.3，只在 A1 episode 内可能发生
TICK_BUDGET_SECS           = 40     # 一拍总预算，必须 < TimeoutStartSec(45) < TICK_SECS(60)
```

**最坏情况核算**（必须能算得出来，否则就是没定义）：

| 阶段 | 最坏耗时 |
|---|---|
| 读 3 个状态文件 + 解析 | < 0.1 s |
| 读 `/proc` 做 `writer_state` | < 0.1 s |
| hint ssh（仅 A1 episode，且受 §6.3.1 限流） | 10 s |
| Telegram 发送（8 s × 2 次尝试，最多 1 条/拍） | 16 s |
| 写 state.json + 追加 sessions.jsonl | < 0.1 s |
| **合计** | **≈ 26 s < 40 s 预算 < 45 s systemd 上限 < 60 s 节拍** |

**一拍内最多发送 1 条消息。** 多条待发时排队到后续拍（队列在 `state.json` 里，
上限 `PENDING_ALERT_QUEUE_MAX = 10`）。持续故障使队列溢出时仍丢弃最旧项，并把数量累加到
`pending_alerts_dropped`；**下一条成功发送的消息**必须注明「持续故障期间已丢弃 N 条旧告警」，
发送成功后才清零该计数。这样保留有界队列策略，但告警丢失不会静默。
这条规则同时保证了上面的预算成立，也天然给 `GLOBAL_HOURLY_CAP` 上了第二道保险。

**持久化顺序（这是 F-12 真正的要害）：**

```
1. 采样、判定                       ← 纯计算
2. 写 state.json（含"本拍决定要发 X，幂等键 K"）   ← 先落盘
3. 追加 sessions.jsonl（如有区间闭合）
4. 才去发 Telegram
5. 发送成功 → 写 state.json 标记 K 已发送
   发送失败/超时 → K 留在队列里，下一拍重试；连续失败 3 次记 last_send_error 并降级为下一拍再试
```

**先落盘再发送**，因为 systemd 可以在任何一步 SIGTERM 我们。
被杀在第 4 步 ⇒ 下一拍从 state.json 看到 K 未发送 ⇒ 重发。
被杀在第 2 步之前 ⇒ 什么都没发生，下一拍重新判定。
在队列未溢出时，这个顺序使崩溃窗口唯一可能的错是重复发送，而不是丢失；
**持续故障把队列压过 10 条时会按上述有界策略丢弃最旧告警**，并由下一条成功消息披露累计丢弃数。
这两个场景不能混称为“绝不丢失”。

幂等键 `K = (episode_id, alert_class, window_index)`，
`episode_id` 是 episode 首次确认时刻的 RFC3339 字符串。

### 9.1 测试清单

**绝对不许为了测试把 QQ 弄掉线。** 全部验证都在打桩、临时目录、死端口上完成。
这沿用仓库既有做法（`tests/gateway/test_onebot_transport.py` 通篇用 `ws://127.0.0.1:1` 这个死端口）。

| # | 层次 | 做法 | 断言要点 |
|---|---|---|---|
| T1 | 纯状态机单测 `test_qq_sentinel_decide.py` | 表驱动：喂一串 `(AccountView, account_evidence, 假时钟)`，比对**发送决策序列** | 主体工作量在这。逐条覆盖 §5.2 的**六个** `*_CONFIRM_TICKS`、冷却、降频、`GLOBAL_HOURLY_CAP`（含汇总消息计入）、静音窗口、静音后补报。**rev-1 新增 golden 时间线**：§5.3 那张表的 9.7 h 与 11.7 h 场景**逐条断言消息序列与时刻**（各 5 条）；「10 分钟掉线 / 3 分钟在线」重复 6 轮 ⇒ 断言进入 `A5_FLAPPING` 且每 6 小时只有 1 条 |
| T2 | 采样层 `test_qq_sentinel_observe.py` | 往 `tmp_path` 写真实形状的 `gateway_state.json` / `gateway.pid` / `gateway.lock`，`monkeypatch.setenv("HERMES_HOME", tmp_path)` | 五值枚举全覆盖；**必测负例**：文件缺失 / 空文件 / 截断 JSON / 二进制垃圾 / 缺 `platforms` 键 / `state="connected"` 且 `error_message` 是 unknown 前缀（**必须判 `ONLINE` 不告警**）/ `updated_at` 陈旧 / `error_message` 前缀不认识。**rev-1 新增**：§3.2.2 truth table **九行逐行**（尤其 `start_time` 缺失 ⇒ `UNVERIFIABLE` 而**不是** `LIVE`）；`updated_at = now + 1h` ⇒ `IMPLAUSIBLE_FUTURE`（F-04b）；`kind` 被改成别的值 ⇒ `DEAD`；PID 存在但 `argv` 不像 gateway ⇒ `DEAD` |
| T2b | 代际闩锁 `test_qq_sentinel_source_format.py` | 合成多代际序列（`writer_pid`/`writer_start_time` 变化） | rev-1 新增（F-05）：新代际 ⇒ `unknown` 且不继承；代际内 modern 证实后**跑满 48 小时仍是 modern**；modern→回滚到 legacy 新代际 ⇒ 不误报 `OBSERVER_DOWN`；`unknown` 期间 `auth_required` **仍能触发 A1** |
| T3 | **探针门禁 + 探针负例** `test_qq_sentinel_probe_gate.py` | ①`run` 路径调用计数；②把 `ONEBOT_WS_URL` 指向 `ws://127.0.0.1:1`（仓库既有死端口惯用法） | **rev-1 最重要的一条（F-01）**：旧状态文件 PID 已死 + 新 Hermes 已启动未刷新，跑 20 拍 ⇒ **探针调用次数 == 0**（§3.3.3 的代码）。手动 `probe` 的三份物证拒绝路径各一条，断言退出码 3。§8.2.1 的六条 URL/action 负例。以及原有的：**探针失败必须归类为 `probe_unavailable`，绝不能变成 `offline`** —— 搞反了就会在 NapCat 一切正常时把人吵醒 |
| T4 | 发送层 `test_qq_sentinel_notify.py` | 发送函数以 `send: Callable[[str], None]` 注入，测试传一个 append 到 list 的假函数 | 永不触网。对 §6.2 五种 `login_hint` 的文案做 golden string 断言，特别是 `sms_verify` 必须出现「短信验证」四个字且**不含** `ti.qq.com` 链接。**rev-1 新增**：注入一个 sleep 12 秒的假发送 ⇒ 断言在 `NOTIFY_TIMEOUT_SECS` 处被中断且**状态已先落盘**；同一幂等键重启后不重复发送；hint 从 `kicked` 升级为 `sms_verify` ⇒ 断言发出一条 episode 内唯一的更新告警（F-13）；每日心跳的五种情形（准点 / 延迟 / 当日已发 / 发送失败重试 / `Asia/Shanghai` 跨日后 `attempts_today` 清零并重新允许 3 次尝试）|
| T5 | 契约测试 `test_qq_sentinel_contract.py` | **只在测试里** `import gateway.status` 与 `plugins.platforms.onebot.adapter`，断言字段名/常量没漂 | 断言 `_RUNTIME_STATUS_FILE == "gateway_state.json"`、`PLATFORM_NAME == "onebot"`、`ACCOUNT_AUTH_REQUIRED_REASON == "qq_account_auth_required"`、`HEALTH_PROBE_SECS == 30.0`、`HEALTH_LOST_SECS == 120.0`，以及 §6.4 那四个 `error_message` 前缀字面量仍在 `adapter.py` 里。**rev-1 新增两条「故意不一致」断言**：①`runtime_status_pid_is_live()` 在 `start_time=None` 时仍返回 `True`（上游行为），②我们的 `writer_state()` 在同一输入下返回 `UNVERIFIABLE`。**这两条一起写，是为了让未来"顺手对齐上游"的人看见这是故意的**（§3.2.2）|
| T6 | 边界测试 | §8.5 的静态扫描 + AST import 闭包检查 | rev-1 扩范围：`systemd/*.{service,timer}`；`ops.qq_sentinel.run` 闭包内无 `onebot_call`/`websockets`；subprocess 只允许出现在 `hint.py::_fetch_login_hint()` |
| T7 | 账本与统计 `test_qq_sentinel_ledger.py` | 合成一串观测序列，检查落盘的 session 记录与 `report` 输出 | 区间切分、`end_reason` 分类、`start_censored`/`end_censored`、中位数/IQR、`n < 10` 时 `median/p25/p75/min/max` 必须全部打印「样本不足，不下结论」。**rev-1 新增**：①单拍 `connecting` 抖动**不得**切段（F-06）；②`link_down` 结束的区间必须 `end_censored=true`；③`account_evidence="unknown"` 不得单独开区间，已开区间混入 unknown 后不得进中位数（F-07）；④采样缺口 > `MAX_SAMPLE_GAP_SECS` ⇒ 旧区间删失闭合；当前拍 online 时只能经统一函数开 `start_censored=true` 的新区间，非 online 时不得产生新区间（F-08）；⑤epoch 切换 ⇒ 区间不得跨 epoch；当前拍 online 时只能经统一函数开 `start_censored=true` 的新区间，非 online 时不得产生新区间；⑥`report --epoch` 单侧 `n<10` 也必须拒绝下结论（F-10）；⑦零失败 + 足够暴露时间 ⇒ 走 §10.6 的右删失判定而不是「数据不足」，并断言 `exposure_h` 与 `failures` 都从同一 `rate_rows` 总体派生（F-09）|
| T8 | 生产干跑（不发送） | `python3 -m ops.qq_sentinel run --once --dry-run` | 打印本次判定与**将要发送的原文**，一个字节都不发。QQ 完全不参与 |
| T9 | 生产链路验证（唯一一次真实发送） | `python3 -m ops.qq_sentinel notify --force-test-alert` | 发一条带 `[TEST]` 前缀的消息，验证 token/chat_id/出网通。**不触碰 QQ，不读账号状态** |
| T10 | 全链路彩排（真发送，QQ 仍不参与） | `HERMES_HOME=/tmp/fake-home python3 -m ops.qq_sentinel run --once`，其中 `/tmp/fake-home/gateway_state.json` 是**手工把 `state` 改成 `auth_required` 的副本** | 用真二进制、真 Telegram 走完 A1 全流程。这就是仓库既有的「状态文件指临时目录 + 发送函数/端点打桩」做法，是**被批准的唯一掉线彩排方式** |
| T11 | unit 冒烟 + **沙箱硬条件** | `systemd-analyze verify ops/qq_sentinel/systemd/*.{service,timer}`；沙箱演练照 H1 §8.3，只用 `/run/systemd/system` 下的临时 test unit | 不碰生产 endpoint。**rev-1 硬项**：§8.3 那张表四条逐条验（`IPAddressDeny` 生效 / `ProtectProc=default` / `/proc/<hermes pid>/stat` 可读 / 三个 gateway 文件可读）。任一不过即不许启用 timer |
| T12 | 自检 | `python3 -m ops.qq_sentinel selftest` | 验证能读到 `gateway_state.json` / `gateway.pid` / `gateway.lock`、能解析、`error_message` 前缀已知、**`writer_state` 不是 `UNVERIFIABLE`**、`STATE_DIR` 可写、Telegram 配置齐全（**只校验存在性，不打印值**）、**`ONEBOT_HTTP_URL` 未设置**。不发送、不探针 |

上线顺序：T1–T7（含 T2b）在本机 CI 跑绿 → T11 → 部署 → T12 → T8 → T9 → T10 → 启用 timer。

**验收门槛（rev-1）**：T3 的 `test_run_path_never_probes`（F-01）、T2 的 `UNVERIFIABLE` 与
未来时间戳两条（F-04/F-04b）、T2b 的四条（F-05）、T7 的七条（F-06~F-10）
**属于阻断项** —— 它们对应审计里的致命/高危 finding，红了就不许上线。

---

## 10. 会话时长采集（原 T6，已合并为第二个消费者）

原计划把「会话时长采集器」作为独立任务 T6。**合并进本组件**，因为它的观测面与告警
100% 重合，分开做必然产生第二条到 NapCat 的探针 —— 正是本项目一直在躲的东西。

它服务的是 **D-QQ-02**（出口 IP 从机房迁到家宽）：迁移的全部理由建立在
「H1 §3 实测的 ~2 小时固定会话寿命是低信任账号在机房 IP 上的症状，不是常数」这个假设上。
**没有迁移前的基线，迁移后就无法判断做对了没有。** 而 H1 §3 的样本量是 **n = 2**，
那是线索，不是结论。

### 10.1 为什么账本必须和告警在同一个进程、且在 Hermes 之外

- 与告警共用一次采样 ⇒ **零额外连接**。
- 在 Hermes 之外 ⇒ **Hermes 每次重启不会被记成一次「区间结束」**。
  若采集器住在插件里，`hermes.service` 的每次 deploy 重启、每次 OOM、
  以及 H1 §8.5 记录的那次「账号出事就把 Hermes 停掉」的处置，都会伪造一个区间边界，
  中位数直接报废。这是 §7.2 表格里「对会话时长数据的影响」那一行的具体含义。

同样的独立性理由适用于依赖：**`ops/qq_sentinel/` 的生产代码不 import 任何 Hermes 模块**
（`gateway.*`、`plugins.*`、`agent.*` 一律不 import），只用标准库；
唯一例外是 §3.3.3 的**手动** `probe` 子命令复用 `tools.onebot_client.onebot_call`
（模块顶层只有标准库，`websockets` 是按需 import），且用 try/except 包住，
import 失败即 `probe_unavailable`。
**`run` 路径的 import 闭包里连这一个例外都没有**（§8.5 的 AST 检查钉死）。
⚠️ 调用时必须 `prefer_live=False`：`prefer_live=True` 会让 `onebot_call()`
`from plugins.platforms.onebot.client import get_live_client`（`tools/onebot_client.py:146-155`），
把这条规则从后门破掉（§8.2.1）。

理由：这个组件的全部价值在于**别的东西都坏了的时候它还能说话**。
一次把 Hermes 依赖树弄坏的部署，不能顺手把报警器一起弄哑。
§3.2.2 那段 `writer_state` 判断宁可重写一遍，也不 import `gateway.status` ——
何况我们**故意**要和上游的语义不同（上游 `start_time` 缺失时返回 `True`，我们返回 `UNVERIFIABLE`），
import 反而会让这个差异变成一个随时会被"顺手对齐"掉的隐患。
漂移风险由 §9 的 T5 契约测试兜住（测试里 import，生产代码里不 import），
其中包含两条**「故意不一致」**断言。

### 10.2 记什么：`${STATE_DIR}/sessions.jsonl`（append-only，一行一段区间）

```jsonc
{
  "schema": 2,                        // rev-1：字段有增加，schema 从 1 提到 2
  "session_id": "2026-09-01T03:12:00Z",
  "started_at": "2026-09-01T03:12:00Z",
  "ended_at":   "2026-09-01T05:19:00Z",
  "duration_s": 7620,
  "end_reason": "kicked",
  "start_censored": false,
  "end_censored":   false,
  "samples": 127,
  "max_gap_s": 62,                    // rev-1：实测最大缺口，不是常量
  "account_evidence": "online",       // rev-1：全段口径，见 §10.2.2
  "evidence_online_samples": 127,     // rev-1 新增
  "evidence_unknown_samples": 0,      // rev-1 新增
  "verification_degraded": false,     // rev-1 新增，见 §10.2.3
  "source_format": "modern",
  "generation_source": "entry",       // rev-1 新增，"entry" | "toplevel"（§3.2）
  "epoch": "datacenter",
  "self_id": "2952532060",
  "raw_transitions": 3                // rev-1 新增：本区间内被去抖吸收掉的原始翻转次数
}
```

**字段兼容性**：rev-1 只**新增**字段并把 `schema` 从 1 提到 2，
**没有重命名或删除**任何既有字段，`epoch` 取值仍是 `datacenter` / `home_broadband`，
`duration_s` / `schema` / `epoch` 含义不变 —— 与 T3-rev 修订单锁定的契约一致。
读取端必须按 `schema` 分支：schema 1 的旧行（若存在）缺少新字段，一律按最保守方式处理
（`account_evidence` 缺失 ⇒ 当作未确认；`verification_degraded` 缺失 ⇒ 当作 `true`）。

#### 10.2.1 区间边界必须用**已确认**的状态转换（审计 F-06）

**初稿的区间定义是「连续采样中 `AccountView == ONLINE` 的极大连续段」——用的是原始单拍。**
而 §5 的 10 拍 / 3 拍去抖只用于发送。这个不一致有三个具体后果：

1. §4.3 已经写明 `gateway/run.py` 的重连机器会短暂把 `state` 改写成
   `connecting`/`retrying`（最多存活 30 秒）。**告警把它吸收了，账本却把它记成一次区间结束 +
   一次新区间开始** —— 凭空造出两段假的短会话。
2. 反复抖动会伪造出大量「独立样本」，直接把 `n ≥ 10` 这个门槛喂饱，
   然后用一堆 30 秒的假区间去和迁移前的真区间比中位数。
3. `end_reason=link_down` 被当成**非删失**的结束。但 OneBot 链路断开时 **QQ 账号很可能仍然在线**
   （隧道重连、NapCat 内部重初始化都会断链路而不掉账号）。
   把它当成会话真实结束 ⇒ **系统性低估会话时长** ⇒ 迁移前后的对比整体失真。

**rev-1 的规则：**

- 账本的状态转换来自 `confirm.py`，与告警**同一份**去抖判定（§2 的图）。
  区间只在**确认后**的状态变化处切分。
- **时间戳用触发确认的那一串采样中的第一拍**，不是确认完成的时刻。
  否则每段区间都会被人为拉长 `CONFIRM_TICKS × 60` 秒（A2 是 10 分钟，不是小数点误差）。
- 被去抖吸收掉的原始翻转次数记进 `raw_transitions`，**不丢失**审计能力。
- 原始采样另存 `${STATE_DIR}/raw_transitions.jsonl`（append-only，
  只在**原始** `AccountView` 变化时写一行，外加每天一行 keepalive）。
  它是排查用的证据链，不参与任何统计。体积估算：抖动最凶的日子也在千行量级/天，
  按 `RAW_LOG_MAX_BYTES = 64 MiB` 做一次性截断保护（超限时停止写入并在日报里说明，**不轮转、不删除**——
  append-only 的文件不该被本组件改写）。

**`end_reason` 判据改为「确认后的下一个稳定态」：**

| 值 | 判据 | `end_censored` |
|---|---|---|
| `kicked` | **连续确认的** `AUTH_REQUIRED`（3 拍） | **false** ← 唯一的非删失结束 |
| `link_down` | 确认的 `LINK_DOWN`（10 拍） | **true**（rev-1 改）|
| `planned_stop` | 确认的 `OBSERVER_DOWN` 且 `gateway_state ∈ {stopping,stopped}` 或 `exit_reason` 非 null | true |
| `observer_down` | 确认的 `OBSERVER_DOWN` 且非优雅停机 | true |
| `sample_gap` | 采样缺口超限（§10.2.4） | true |
| `epoch_change` | epoch 切换事务（§10.4） | true |
| `unknown` | 其余，含 `SOURCE_INCOMPATIBLE`、`verification_degraded` | true |

> **只有确认的 `AUTH_REQUIRED` 才是非删失结束。** 这是本节的一句话总结。
> 理由：`auth_required` 是**账号层**的显式证据（`adapter.py:1979-1981`），
> 它确实意味着「这一段在线到此为止」。其余所有结束原因都只证明**我们看不见了**，
> 而不是**账号下线了**。把「我看不见」当成「它结束了」，是这份账本最容易犯也最贵的错。

`sample_gap` / `epoch_change` 是 rev-1 新增的两个值（审计 F-08）。它们都是删失的，
不影响 `duration_s` / `epoch` / `schema` 这套被锁定的契约字段。

#### 10.2.2 测量视图：只有**全程证明在线**的区间才进统计（审计 F-07）

初稿把 `account_online is None`（unknown）折进 `AccountView.ONLINE`（对告警是**正确**的，§4.1），
然后账本又按 `== ONLINE` 开区间，报告只过滤 `start_censored` / `end_censored`。
后果：**一段从未证明账号在线过的时间，可以变成一条非删失的 `kicked` 记录进中位数。**
最典型的产生方式：哨兵启动后 Hermes 长时间只写「QQ account status is unknown」，
随后一次 auth-required —— 中间那段「未知」被整段算成了在线时长。

**rev-2 先把“能否开区间”收口为一个函数；所有分支都只能调用它：**

```python
def maybe_open_session(state, observation, *, start_censored: bool):
    if state.current_session is not None:
        return None
    if observation.confirmed_view != AccountView.ONLINE:
        return None
    if observation.account_evidence != "online":
        return None
    session = start_new_session(
        started_at=observation.sampled_at,
        start_censored=start_censored,
    )
    state.current_session = session
    return session
```

`start_new_session(...)` 只允许出现在这个函数体内；正常转换、采样缺口、epoch 切换都不得绕过它。
规则因此只有一条可判定前提：**当前确认态必须是 `ONLINE`，且当前采样必须有
`account_evidence == "online"`，否则返回 `None`，不产生区间。**

正常转换分支在没有当前区间时调用：

```python
maybe_open_session(
    state,
    observation,
    start_censored=(previous_confirmed_view != AccountView.AUTH_REQUIRED),
)
```

前一确认态为 `AUTH_REQUIRED` 时，账号离线有明确证据，恢复后的首个 online 采样可作为非删失起点；
首次运行，或从 `LINK_DOWN` / `OBSERVER_DOWN` / `SOURCE_INCOMPATIBLE` / 只有 unknown 证据的阶段回来时，
真实起点不可知，故传 `start_censored=True`。`unknown` 期间本身不开区间；随后第一次出现 online 证据时，
`started_at` 就取该次采样，并按上述规则置 `start_censored=true`。这两句话不再互相冲突。

区间已经开启后，中途出现 `unknown`（例如 Hermes 重启后短暂丢失账号状态）时不切断
（那会造假短会话），但累计 `evidence_unknown_samples`；若 `evidence_unknown_samples > 0`
⇒ `account_evidence = "mixed"`，该区间不进中位数，也不进 §10.5 的 rule-of-three 判定总体。

`account_evidence` 的取值仍是全段口径：`"online"`（全程 online）/ `"mixed"`（有 unknown）/
`"unknown"`（全程 unknown，按统一函数不会单独产生）/ `null`（legacy，没有证据字段）。
报告的完整区间过滤条件仍是 **「非删失 **且** `account_evidence == "online"` 且
`verification_degraded == false`」**。

**告警视图不受影响**：unknown 仍然不告警（那是任务的硬要求，§11.2）。
改的是**测量**口径。一句话：**不告警的理由是「不确定」，而不确定的东西同样不能进中位数。**

#### 10.2.3 `verification_degraded`

区间内任一采样出现 `writer_state == UNVERIFIABLE`（§3.2.2）或 `state_age == IMPLAUSIBLE_FUTURE`
（§3.2.3）⇒ 整段 `verification_degraded = true` ⇒ **不进中位数，也不进入 §10.5 的
rule-of-three 判定暴露时间**（但仍原样记录）。
理由：那段时间里我们连「Hermes 是不是还活着」都不确定，不能把它算成“没有失败的暴露时间”。
现有账本只有区间级布尔值、没有每个采样的可验证时长，无法可靠拆分降级区间；因此整段保守排除。

#### 10.2.4 采样缺口与哨兵自身停机（审计 F-08）

初稿把未闭合区间放 `state.json`，但**没定义哨兵停机数小时后首次采样怎么办**。
后果很直接：哨兵停了 6 小时，回来读到 `ONLINE` 就接着往下记 ⇒ **那 6 小时被算成在线时长**，
而那段时间里 QQ 可能掉了又回来。`Persistent=true` 只补一次触发，**不补齐停机期的采样**。

**rev-1：每一拍都持久化 `last_sample_at`，并在每一拍开头做缺口检查。**

```
MAX_SAMPLE_GAP_SECS = 180        # 3 拍。允许 systemd AccuracySec 抖动和一次漏拍
```

```
gap = now - state["last_sample_at"]
if gap > MAX_SAMPLE_GAP_SECS:
    # 关闭旧区间：结束时刻取 last_sample_at（我们最后一次真的看见），不是 now
    close_session(ended_at=last_sample_at, end_reason="sample_gap", end_censored=True)
    # 只有当前拍重新证明 online 才开；offline/AUTH_REQUIRED/LINK_DOWN 等均返回 None
    maybe_open_session(state, observation, start_censored=True)
```

- `max_gap_s` 字段记录**区间内实际观测到的最大相邻采样间隔**（初稿写死 60 是错的：
  它是常量而不是观测值，永远无法暴露漏拍）。
- `state.json` 缺失 / 首次运行 ⇒ 第一段区间 `start_censored = true`（初稿已有此条，保留）。
- 时钟倒退导致 `gap < 0` ⇒ 按缺口处理（同 §3.2.3 的取向：负数一律不当"正常"）。
- §9 的 T7 必须有一条「停机 6 小时后重启」的用例。

### 10.3 分辨率与精度

采样节拍 60 秒 ⇒ 区间边界分辨率 ±60 秒。对一段约 7200 秒的区间是 **0.8%** 误差。
测量目标是**中位数的量级变化**（2 小时 → 8 小时？还是仍然 2 小时？），
0.8% 的边界误差在这个问题面前可以忽略。

**因此账本记录的是哨兵自己的采样时刻，不去解析 adapter 内部的
`last_heartbeat_at_ms` / `heartbeat_age_seconds`。** 那些字段不在 `gateway_state.json` 里
（只在进程内的 `health_snapshot()` 里），为了 0.8% 的精度把整个组件搬进 Hermes 是坏交易 ——
这就是 §7.4 的结论。

### 10.4 `epoch`：迁移前后的分组键

`HERMES_QQ_SENTINEL_EPOCH` 环境变量，取值 `datacenter`（默认）/ `home_broadband`。
家宽迁移当天由运维改这一个值并 `systemctl restart`。

用显式 epoch 而不是按时间戳切分，因为迁移不是一个瞬间 —— 会有扫码、观察、
可能的回滚。哪些区间属于哪一边，应该是人明确声明的事实，不是从时间戳猜出来的。
（`report` 仍额外支持 `--since` / `--until` 做交叉检查。）

#### 10.4.1 epoch 切换是一个显式事务，区间**永不跨 epoch**（审计 F-08）

初稿只说「改一个环境变量并 restart」，没定义**正在进行中的那段区间**怎么办。
单条记录只有一个 `epoch` 字段，而一段跨越切换时刻的区间既不属于 `datacenter` 也不属于
`home_broadband` —— 按哪一边归类都会污染对比，而迁移当天恰恰是最不能出错的一天。

`state.json` 里持久化 `epoch`。每一拍开头（在缺口检查之后）比对环境变量：

```
if env_epoch != state["epoch"]:
    close_session(ended_at=last_sample_at, end_reason="epoch_change", end_censored=True)
    state["epoch"] = env_epoch
    # 新 epoch 只有当前拍重新证明 online 才开；非 online 切换不制造虚假区间
    maybe_open_session(state, observation, start_censored=True)
```

- 关闭时刻用 `last_sample_at`，理由同 §10.2.4：那是我们最后一次确知处于旧 epoch。
- 若切换当拍满足统一开区间前提，新 epoch 的新区间 `start_censored = true`；若当拍非 online，
  `maybe_open_session(...)` 返回 `None`，不预写占位区间，后续只在新的有效 online 采样上重新判定。
- **`epoch` 的取值集合不变**（`datacenter` / `home_broadband`），字段名不变。
  改的只是「切换时刻怎么处理」这个初稿留空的地方。
- 迁移当天建议同时写一个 `mute_until`（§5.4）覆盖扫码窗口，避免扫码过程中的
  A1/A2 把人吵一遍 —— 但**账本照记**，静音只影响发送。

### 10.5 报告

```bash
python3 -m ops.qq_sentinel report --compare
python3 -m ops.qq_sentinel report --epoch datacenter [--since ISO] [--until ISO] [--include-censored]
```

命令形态不变（修订单锁定：`report --compare` 与 `report --epoch ...`）。

输出**每个 epoch 都必须包含这六项**（rev-1：初稿只报「完成区间的中位数」，
那正是 F-09 说的选择偏差 —— 只看失败事件的统计量）：

| 项 | 含义 |
|---|---|
| `exposure_h` | **rule-of-three 判定暴露时间**：该 epoch 的 `rate_rows` 已观测时长之和；含其中的删失区间与当前未闭合区间 |
| `failures` | **同一 `rate_rows` 总体内的失败事件数**：`end_reason == "kicked"` 且 `end_censored == false` |
| `censored` | 真正的删失区间数：`start_censored == true` 或 `end_censored == true`，按 `end_reason` 分列 |
| `n_complete` | `rate_rows` 中同时非起点删失、非终点删失的区间数 |
| `median / p25 / p75 / min / max` | 仅基于 `n_complete` 那批 |
| `longest_ongoing_h` | 当前未闭合区间已持续时长（右删失的最强单点证据）|

rule-of-three 只使用下面这个单一判定总体；分子与分母共享完全相同的总体资格筛选，
分子只在该总体内再判断是否出现 `kicked` 失败端点：

```python
rate_rows = [r for r in epoch_rows
             if r.get("account_evidence") == "online"
             and not r.get("verification_degraded", True)]
exposure_h = sum(r["duration_s"] for r in rate_rows) / 3600.0
failures = sum(1 for r in rate_rows
               if r.get("end_reason") == "kicked"
               and not r.get("end_censored"))
```

`failures` 明确是 `rate_rows` 的子集；删失区间与当前未闭合区间只贡献其中**已经观测且全程可验证**的
person-time，不凭空贡献失败事件。`account_evidence == "mixed"` / `null` 与
`verification_degraded == true` 的整段均排除：schema 2 只有区间级汇总，无法可靠拆出其中可验证的
子时长，整段排除是不会把不可验证时段误算成“零失败暴露”的保守口径。
本报告不另保留“所有区间总时长”这个宽口径字段，避免它与判定用的 `exposure_h` 同名或被误用于结论。

#### 10.5.1 `n < 10` 的防呆必须覆盖**所有**报告入口（审计 F-10）

初稿只在 `--compare` 上写了防呆，然后**紧接着给出一段会对任意非空样本直接打印 median
的无 CLI 片段** —— 操作员用它拿到 `n=1` 的中位数，正好重演 H1 §3 用 n=2 宣称
「~2 小时固定会话寿命」的错误。单 epoch 的 `report` 也没禁止在 `n<10` 时展示统计量。

**rev-1：所有入口共用同一个 `summarize(rows) -> Summary` 函数。**
防呆写在 `summarize()` 里，而不是写在某一条 CLI 分支里 —— 写在分支里的规则，
下一个分支就会忘了带上。

```
0 ≤ n_complete < MIN_SESSIONS_FOR_COMPARE(10)
    ⇒ 所有位置统计量（median/p25/p75/min/max）一律输出字符串
      "样本不足（n=X），不下结论"
      而不是数字。exposure_h / failures / censored 照常输出（它们不是结论，是事实）。
```

适用于：`report --compare`、`report --epoch`、每日心跳日报（§7.5）、
恢复通知里的时长播报（那是单次事实，不受此限但必须标注是否删失）。

`--include-censored` 仍然可用，但它输出的每一行都会**强制加前缀** `[含删失，不可用于结论]`。

无 CLI 时的等价一次性算法（`jq` 未必装）。
**rev-1 修正了初稿这段片段的两个错**：它对 `n=1` 也会打印 median，且没有过滤
`account_evidence`：

```bash
python3 - <<'EOF'
import json, statistics
MIN_N = 10                      # 与 MIN_SESSIONS_FOR_COMPARE 保持一致
rows = [json.loads(l) for l in open('/var/lib/hermes-qq-sentinel/sessions.jsonl')]
for ep in ('datacenter', 'home_broadband'):
    all_ = [r for r in rows if r.get('epoch') == ep]
    rate_rows = [r for r in all_
                 if r.get('account_evidence') == 'online'
                 and not r.get('verification_degraded', True)]  # 缺字段=保守排除
    ok = [r for r in rate_rows
          if not r.get('start_censored') and not r.get('end_censored')]
    exposure_h = sum(r.get('duration_s', 0) for r in rate_rows) / 3600.0
    failures = sum(1 for r in rate_rows
                   if r.get('end_reason') == 'kicked'
                   and not r.get('end_censored'))
    censored = sum(1 for r in all_
                   if r.get('start_censored') or r.get('end_censored'))
    d = [r['duration_s'] for r in ok]
    verdict = ('median=%.0fs' % statistics.median(d)) if len(d) >= MIN_N \
              else '样本不足（n=%d），不下结论' % len(d)
    print('%-16s n=%-3d exposure=%.1fh failures=%d censored=%d  %s'
          % (ep, len(d), exposure_h, failures, censored, verdict))
EOF
```

### 10.6 采集必须先于迁移跑起来

#### 10.6.1 迁移**前**的闸门（pilot gate）—— 不变

**放行家宽迁移（D-QQ-02）的前置条件：**

| 条件 | 阈值 |
|---|---|
| 哨兵连续运行 | ≥ **7 天** |
| `epoch=datacenter` 的**非删失且证据完整**区间数（§10.2.2 口径） | ≥ **10** |
| `A0_SOURCE_INCOMPATIBLE` | 已清除（即 T1 已部署） |
| `A4_WRITER_UNVERIFIABLE` | 已清除（§8.3 的沙箱硬条件全过） |

不满足就迁，等于把唯一一次「做完永不再迁」的机会（D-QQ-02）花在一个无法验证的赌注上。
这个方向没问题，**因为迁移前的失败事件是自然发生的**（H1 §3 记录的 9.7 h / 11.7 h 就是），
攒够 10 段不需要等待任何坏事额外发生。

#### 10.6.2 迁移**后**的判定 —— 初稿在这里有一个自毁式的错误（审计 F-09）

初稿写「迁移后同样收满 ≥7 天且 ≥10 段再做 `--compare`」。**这句话在成功时无法满足：**

> 如果家宽迁移确实管用、账号连续在线超过 7 天，那么这 7 天里**一段完成区间都不会产生**。
> 于是「迁移成功」这个结果，恰恰无法通过验收。

三个具体后果：

1. **只能继续等真实掉线。** 为了凑够 10 段而等待，等于**主动等待失败发生**。
2. **样本有选择偏差。** 迁移后能凑出的 10 段，天然全是失败事件周围的区间，
   不能代表稳定运行的常态。
3. **误读风险。** 「长时间没掉线」会被这套规则报成「数据不足」，
   最坏的情况是诱使运维去人为制造掉线来"补样本"。

**rev-1：把 pilot 闸门和统计结论彻底分开，迁移后允许右删失判定。**

```
POST_MIGRATION_MIN_DAYS       = 7      # 最短观察期
POST_MIGRATION_MAX_WAIT_DAYS  = 30     # 最长等待期，到点必须出结论（哪怕结论是"没有事件"）
```

观察期满 7 天后，按下表**先命中先返回**出结论（`report --compare` 直接打印结论行）：

| # | 条件 | 结论 |
|---|---|---|
| 1 | 两侧 `n_complete ≥ 10` | 走原路：并排中位数 + 差值。**这是最强的结论** |
| 2 | `failures == 0` 且 `exposure_h` 足够（见下） | **「显著改善」**：用零事件的置信上界判定 |
| 3 | `failures > 0` 但 `n_complete < 10`，且已达 `POST_MIGRATION_MAX_WAIT_DAYS` | 输出「证据不足以做中位数比较」+ 完整的暴露/失败/删失三元组，**由人决定** |
| 4 | 其余（未满 7 天，或未到 30 天且样本仍在积累） | 「观察中」+ 当前三元组 + 距离各条判据还差多少 |

**第 2 行的判据（零事件时怎么下结论）：**

本节的 `failures` 与 `exposure_h` 均严格使用 §10.5 的同一 `rate_rows` 总体；不得改回
“所有区间做分母、完整区间做分子”的混合口径。
基线给出掉线率 `λ₀ = failures_datacenter / exposure_h_datacenter`（次/小时）。
迁移后观察到 `k = 0` 次失败、暴露时间 `T` 小时。
泊松零事件的 95% 单侧上界是经典的 **rule of three**：`λ_upper = 3 / T`。

```
若  3 / T  <  λ₀ / 2      ⇒  结论："显著改善（掉线率至少降低一半，95% 单侧）"
否则                       ⇒  结论："尚无失败事件，但暴露时间不足以排除原有掉线率"
                              并打印还需要多少小时（T_needed = 6 / λ₀）
```

举个数：若基线是「平均每 2 小时掉一次」（`λ₀ = 0.5`），
则 `T_needed = 6 / 0.5 = 12` 小时 —— 家宽下**连续在线 12 小时零掉线**就足以在 95% 置信下
排除「掉线率没变」。这远比「等 10 次掉线」现实，而且它衡量的是**我们真正关心的那件事**。

> 这里放弃的方案是「用 Kaplan–Meier 做完整生存分析」。
> 它在统计上更完备，但需要在一个只用标准库、跑在 oneshot 里的组件中实现生存曲线与置信带，
> 复杂度换来的精度对「2 小时还是 8 小时」这个量级问题没有实际帮助。
> **rule of three 只需要两个数（暴露时间、事件数），可以手算复核，也不会悄悄算错。**
> 判断依据的可复核性，在这里比统计效率重要。

**无论走哪一行，报告都必须打印暴露时间、失败数、删失数**（§10.5），
而不是只报一个「完成区间的中位数」。审计原话：不得强行等到 10 次失败。

**超期兜底**：到达 `POST_MIGRATION_MAX_WAIT_DAYS` 仍未满足第 1/2 行，
输出「无足够事件，但也没有观测到失败」并结束等待。**不再继续等**。
这条是为了防止「等到有结论为止」变成「等到出事为止」。

---

## 11. 实现者须知

按「最容易做错」排序。前六条每一条都对应一个**会静默失败**的坑。

### 11.1 ⛔ 这个组件永远不做的三件事

**不重启、不重登、不写 NapCat 配置。** 在代码里加一句
「顺手把 NapCat 重启一下」不是优化，是把 `/usr/local/sbin/napcat-watch.py` 又造了一遍 ——
那个脚本因为是「第二个会消耗登录尝试的生命周期管理器」在 2026-08-30 被有意 stop/disable
（H1 §8.4）。反复自动重启 / 密码登录会让腾讯风控进一步加码，被踢后密码登录还会阻塞在
短信验证上。**§8.5 的静态扫描会在 CI 里拦住未来的你**（它是防回归检查，不是权限边界 ——
真正拦不住的东西由 §8.3 的内核沙箱兜底）。写那个测试时，把这段来由抄进注释。

**rev-1 追加第四件事：不在 `run` 路径上建任何 OneBot 连接。**
「Hermes 好像死了，那我探一下更准」——这句话就是审计 F-01 的入口。
探针只存在于手动子命令里，且有三份物证 + 两条时间闸把守（§3.3）。

### 11.2 `account_online is None` 绝不是离线

`adapter.py:1843` `_set_account_status()` 对 `None` 的处理是**直接 return** ——
「连上但还没报状态」是 *unknown*，不是 offline。文件层面这体现为：
真在线和未知**写出完全相同的 `state="connected"` + `error_code=None`**，只有
`error_message` 前缀不同（§4.1）。

所以 `AccountView.ONLINE` 的语义是「**未被证明离线**」，不是「已证明在线」。
这个折叠是**故意的**，别去「修」它。把 unknown 单独拉出来当离线，会在每次 Hermes 启动的
头几秒都误报一次。

**但折叠只对告警成立（rev-1，审计 F-07）。** `account_evidence` **不参与任何告警判定，
但它决定一段区间能不能进统计**：区间只能从 `account_evidence == "online"` 开始，
中途混入 unknown 的区间标 `"mixed"` 并排除出中位数（§10.2.2）。
初稿那句「只写账本、不参与判定」把它降格成了诊断装饰，代价是
「从未证明在线过的时间」可以变成一条非删失的 `kicked` 记录。

### 11.3 「文件不动」在旧 adapter 上是正常的

设计初稿在这里错过一次（§3.2.1）。**别用 `updated_at` 陈旧当作 Hermes 死亡的通用判据。**
实测：生产文件冻结 15 小时而 `hermes.service` 全程 active（§1.2）。
通用判据是 `writer_state`（§3.2.2 的**三值** truth table，不是布尔）；`updated_at` 陈旧只在
`source_format == modern` 时才是证据，在 `unknown` / `legacy` 下**根本不评估**。
判定顺序见 §4.1 那张表，**顺序本身是设计的一部分**，调换会引入漏报（§4.1.1 已论证）。

**rev-1 的两条补充，都不许绕过：**
1. **别去"对齐上游"** `runtime_status_pid_is_live()`。它在 `start_time` 缺失时返回 `True`，
   我们返回 `UNVERIFIABLE`，**这是故意的**（§3.2.2），T5 有两条断言把差异钉住。
2. `updated_at` 的年龄是**有符号**的。未来时间戳的负 age 必须当 `STALE`，
   不能像 `_marker_is_stale()` 那样当成"新鲜"（§3.2.3，审计 F-04b）。

### 11.4 `platforms.onebot.state` 是多写者共享字段

除了 adapter，`gateway/run.py:12624/12732/14051/14115/14148` 等处的重连机器也往同一个键写
`connecting` / `connected` / `retrying` / `fatal`，并会把 `error_code` / `error_message`
清成 `None`。新 adapter 的 30 秒 health tick 会把账号真相盖回去，所以错误值最多存活 30 秒。

后果有**三**个（rev-1 补了第三个），都已在设计里处理，实现时别绕过：
1. 告警必须 **≥3 拍确认**（§5.2）才能吸收这个抖动；
2. `source_format` 必须**带代际闩锁**（§4.1.2），否则一次 `error_message=None` 会把
   modern 误判成 legacy，整个哨兵瞬间掉进降级模式；
3. **账本也必须用确认后的转换切段**（§10.2.1）。初稿只让告警去抖、账本吃原始采样，
   结果是同一个 30 秒抖动被告警吸收、却在账本里变成两段假会话（审计 F-06）。
   去抖判定只有一份，两个消费者共用。

### 11.5 探针失败 ≠ 账号离线

`probe_get_status()` 超时、连接被拒、`websockets` 没装 —— 全部归类为
`probe_unavailable`，**绝不可以退化成 `offline`**。搞反了就会在 NapCat 一切正常的时候
（比如隧道刚好在 `RestartSec=30s` 的重连窗口里）把人半夜叫醒。
§9 的 T3 用死端口 `ws://127.0.0.1:1`（仓库既有惯用法）专门钉这条。

**rev-1：探针不再有任何自动触发路径**（§3.3，审计 F-01）。
初稿的「只在 Hermes 已死时才开」挡不住「新 Hermes 已起、状态未写」的竞态。
现在它是手动子命令 + 三份物证 + 两条时间闸。
任何「顺手每拍都探一下更准」或「把 probe 挂回 run 里」的改动，
都会变成第二条与 Hermes 竞争同一账号的连接 —— T3 的 `test_run_path_never_probes` 会红。

### 11.6 静默降级 = 设计失败

本组件的每一种「我判断不了」都必须**说出来**：
数据源是旧格式（§6.4）、格式尚未确定（§4.1.2 的 `unknown`）、
**看不见 Hermes 进程或时间戳不可信（§4.2 的 `A4`，rev-1 新增）**、
hint 过期（§6.3）、探针不可用（§11.5）、静音期结束后的补报（§5.4）、
tzdata 缺失导致心跳按固定 UTC+8 计算（§7.5.1）。
一个装好了但什么都不做的告警器，比没装更危险 —— 它附赠一份错误的安全感。
§1.2 的实测就是这个失败模式的实物样本。

**告警文案写人话。** 收件人是要去干活的人：告诉他**现在什么坏了**、**他要做什么**、
**这个组件不会替他做什么**。不要出现 "schema mismatch"、"数据源不兼容"、"state transition" 这类词。

### 11.7 秘密处理

- `TELEGRAM_BOT_TOKEN` / `ONEBOT_ACCESS_TOKEN` 从 `/opt/hermes/data/.env` 进程内读取。
- **不进命令行**（`ps` 可见）、不进日志、不进 journal、不进告警正文、不进 `state.json`。
- `selftest` 只校验存在性，**只打印键名不打印值**。
- 不转发 `ti.qq.com/.../sms-verify-login` 链接、不转发二维码、不转发 WebUI token（§6.2）。
- Telegram 出错时打印 HTTP 状态码即可，**不要回显请求 URL**（token 在 path 里）。

### 11.8 部署顺序（有硬依赖）

```
T1 部署（account-state 加固上生产）
   └→ T3-A 上线（哨兵：告警 + 账本）
        └→ selftest 通过、A0 与 A4 告警都消失   ← 这一步是 T1 与沙箱配置是否真的生效的验收
             └→ 收满 ≥7 天 / ≥10 段基线（§10.6.1）
                  └→ D-QQ-02 家宽迁移（改 epoch=home_broadband，走 §10.4.1 的切换事务）
                       └→ 观察 ≥7 天，按 §10.6.2 的四行判定表出结论
                          （零失败 + 足够暴露时间即可下结论，**不必等够 10 次掉线**）
                       └→ 最长等到 POST_MIGRATION_MAX_WAIT_DAYS = 30 天，到点必须出结论
                            └→ report --compare
T3-B（短信验证 hint）可在 T3-A 之后任意时点插入，不阻塞任何环节
```

**T3-A 可以先于 T1 部署** —— 它会立刻发一条 A0 告警说「我还判断不了 QQ 状态」。
这不是缺陷，这是让 T1 的部署缺口第一次变得可见。

### 11.9 其他易错点

- **平台键是 `"onebot"`**，不是 `"onebot-platform"`，也不是 `"qqbot"`（后者是另一个平台，
  走腾讯官方 QQ Bot API）。来源见 `plugins/platforms/onebot/plugin.yaml` 顶部注释。
- **`HERMES_HOME` 必须显式设置**为 `/opt/hermes/data`。不设会 fallback 到 `~/.hermes`
  （`hermes_constants.py:53`），而 unit 里 `ProtectHome=yes` ⇒ 读到的是一个不存在的路径，
  哨兵会永久报 `OBSERVER_DOWN`。
- **时间戳解析**：`updated_at` 可能带微秒和 `+00:00`（§1.2 实测样本就是
  `2026-08-30T18:36:57.553362+00:00`），也可能是 `Z` 结尾。两种都要吃得下，
  解析失败按「陈旧」处理（与 `gateway/status.py` 对缺失时间戳的保守取向一致）。
- **`sessions.jsonl` 只追加不重写**，每行一个完整 JSON 对象，带 `schema` 字段（rev-1 起为 `2`）。
  未闭合的当前区间放在 `state.json` 里，不要先写一行再回头改。
  读取端按 `schema` 分支；缺字段一律按最保守方式处理（§10.2）。
- **先落盘再发送**（§9.0）。被 systemd 杀在发送中途时，唯一可能的错误是重复发一条告警，
  而不是丢失一条告警。这个方向是故意选的，别为了"避免重复"把顺序倒过来。
- **`ONEBOT_HTTP_URL` 必须为空。** `onebot_base_url()` 优先读它（`tools/onebot_client.py:57-65`），
  设了它就会把「WS-only 到本地隧道」变成 HTTP POST 到别处。`selftest` 会检查这一条。
- **不要修改 `plugins/` 下任何文件。** 哨兵是纯消费者。若发现确实需要 adapter 多写一个字段，
  那是另一个任务（要过 `AGENTS.md:876-881` 的核心改动评审），不要顺手改。

---

## 12. 附录：交付物清单

### 12.1 文件

| 路径 | 内容 |
|---|---|
| `ops/qq_sentinel/__init__.py` | — |
| `ops/qq_sentinel/__main__.py` | CLI：`run` / `report` / `selftest` / `notify` / **`probe`**（rev-1 新增，§3.3.3）|
| `ops/qq_sentinel/constants.py` | 全部阈值与四张 allowlist（§5、§8.2）。**allowlist 是声明，不是强制层** |
| `ops/qq_sentinel/observe.py` | 读三个文件 + `writer_state`（§3.2.2）+ `state_age`（§3.2.3）+ 代际闩锁（§4.1.2）→ `Observation`。**无任何网络代码** |
| `ops/qq_sentinel/confirm.py` | **rev-1 新增**：唯一一份去抖/确认逻辑，告警与账本共用（§2、§10.2.1）|
| `ops/qq_sentinel/decide.py` | **纯函数状态机**，无 I/O，无时钟（时钟注入）—— 测试主战场。含冷却/预算/flapping（§5）|
| `ops/qq_sentinel/ledger.py` | `sessions.jsonl` 追加 + `raw_transitions.jsonl` + `summarize()`（§10.5.1 的唯一统计入口）|
| `ops/qq_sentinel/notify.py` | Telegram 发送（标准库 `urllib.request`，**必传 timeout**），发送函数可注入 |
| `ops/qq_sentinel/hint.py` | **rev-1 拆出**：T3-B 的 SSH hint 拉取。**全组件唯一允许出现 subprocess 的文件**（§8.5）|
| `ops/qq_sentinel/probe.py` | **rev-1 新增**：`probe_get_status()` —— 全组件唯一允许接触 OneBot 的文件（§8.2.1）。`run` 路径的 import 闭包里不得出现它 |
| `ops/qq_sentinel/systemd/hermes-qq-sentinel.service` | §8.3 的沙箱（`ProtectProc=default` + 必选 `IPAddress*`）|
| `ops/qq_sentinel/systemd/hermes-qq-sentinel.timer` | `OnUnitActiveSec=60s` / `AccuracySec=5s` / `Persistent=true` |
| `ops/qq_sentinel/README.md` | 运维视角：装/停/静音/看报告/审计命令/**手动 probe 的使用与拒绝语义** |
| `tests/ops/test_qq_sentinel_{decide,observe,source_format,probe_gate,notify,ledger,contract,boundary}.py` | §9 的 T1–T7（rev-1 增加 `source_format` 与 `probe_gate` 两个文件）|

### 12.2 常量总表

**rev-1 变更用 ⚠️ 标出；新增用 ＋ 标出。**

| 常量 | 值 | 出处 |
|---|---|---|
| `TICK_SECS` | 60 | §5.1 |
| `STATE_STALE_SECS` | 180 | §4.1 / §5.2 |
| ＋`CLOCK_SKEW_TOLERANCE_SECS` | 120 | §3.2.3（未来时间戳容忍窗口，F-04b）|
| ⚠️`SOURCE_MODERN_LATCH_SECS` | **删除** | §4.1.2 —— 墙钟闩锁被代际闩锁取代（F-05）|
| ＋`SOURCE_UNKNOWN_GRACE_SECS` | 900 | §4.1.2（新代际的"格式未知"宽限期）|
| `AUTH_REQUIRED_CONFIRM_TICKS` | 3 | §5.2 |
| `LINK_DOWN_CONFIRM_TICKS` | 10 | §5.2 |
| `OBSERVER_DOWN_CONFIRM_TICKS` | 3 | §5.2 |
| ＋`SOURCE_INCOMPATIBLE_CONFIRM_TICKS` | 5 | §4.1.3 / §5.2（F-05）|
| ＋`A4_CONFIRM_TICKS` | 5 | §5.2 |
| ⚠️`RECOVER_CONFIRM_TICKS` | **10**（初稿 3） | §5.2 / §6.1（F-11）|
| ＋`RECOVER_CONFIRM_TICKS_AFTER_FLAP` | 30 | §5.2 / §5.5 |
| `ALERT_COOLDOWN_SECS` | 3600 | §5.3 |
| ⚠️`COOLDOWN_ESCALATE_AFTER` | **3**（初稿 6） | §5.3（F-11）|
| `ALERT_COOLDOWN_LONG_SECS` | 21600 | §5.3 |
| `GLOBAL_HOURLY_CAP` | 6（**含汇总消息与恢复通知**） | §5.3（F-11）|
| ＋`EPISODE_FLAP_WINDOW_SECS` | 3600 | §5.5 |
| ＋`EPISODE_FLAP_THRESHOLD` | 3 | §5.5 |
| ＋`FLAPPING_ALERT_COOLDOWN_SECS` | 21600 | §5.5 |
| ＋`PENDING_ALERT_QUEUE_MAX` | 10 | §9.0 |
| ＋`NOTIFY_TIMEOUT_SECS` | 8 | §9.0（F-12）|
| ＋`NOTIFY_RETRIES` | 1 | §9.0 |
| ＋`NOTIFY_TOTAL_BUDGET_SECS` | 20 | §9.0 |
| ＋`TICK_BUDGET_SECS` | 40 | §9.0（< `TimeoutStartSec`=45 < `TICK_SECS`=60）|
| `PROBE_TIMEOUT_SECS` | 10 | §3.3.4 |
| `PROBE_MIN_INTERVAL_SECS` | 300 | §3.3.3 |
| ＋`PROBE_GATEWAY_ABSENT_GRACE_SECS` | 600 | §3.3.3（F-01）|
| `PROBE_ACTION_ALLOWLIST` | `("get_status",)` | §3.3.4 / §8.2 |
| `HINT_MAX_AGE_SECS` | 900 | §6.3 |
| ＋`HINT_MIN_INTERVAL_SECS` | 600 | §6.3.1（F-13）|
| ＋`HINT_MAX_FETCHES_PER_EPISODE` | 73（覆盖至少 12 小时） | §6.3.1 |
| ＋`HINT_SSH_TIMEOUT_SECS` | 10 | §6.3 / §9.0 |
| `SOURCE_INCOMPATIBLE_REPEAT_SECS` | 86400 | §6.4.3 |
| ＋`A4_REPEAT_SECS` | 86400 | §4.2 |
| `HEARTBEAT_LOCAL_TIME` | `"09:00"` `Asia/Shanghai` | §7.5 |
| ＋`HEARTBEAT_MAX_ATTEMPTS` | 3 | §7.5.1（F-12）|
| ＋`MAX_SAMPLE_GAP_SECS` | 180 | §10.2.4（F-08）|
| ＋`RAW_LOG_MAX_BYTES` | 64 MiB | §10.2.1 |
| `MIN_SESSIONS_FOR_COMPARE` | 10 | §10.5.1 / §10.6 |
| `MIN_BASELINE_DAYS` | 7 | §10.6.1 |
| ＋`POST_MIGRATION_MIN_DAYS` | 7 | §10.6.2（F-09）|
| ＋`POST_MIGRATION_MAX_WAIT_DAYS` | 30 | §10.6.2（F-09）|

### 12.3 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HERMES_HOME` | 必填 | 生产 `/opt/hermes/data`（§11.9） |
| `HERMES_QQ_SENTINEL_STATE_DIR` | `/var/lib/hermes-qq-sentinel` | 唯一可写目录 |
| `HERMES_QQ_SENTINEL_EPOCH` | `datacenter` | §10.4 |
| `TELEGRAM_BOT_TOKEN` | 取自 `${HERMES_HOME}/.env` | 值不入日志/文档 |
| `TELEGRAM_ALERT_CHAT_ID` | `1114483029` | 收件人 |
| `ONEBOT_WS_URL` | 取自 `${HERMES_HOME}/.env` | **仅手动 `probe` 用**，经 `onebot-tunnel.service`。必须是 `ws://127.0.0.1:3011`（§8.2.1 运行时校验）|
| `ONEBOT_ACCESS_TOKEN` | 取自 `${HERMES_HOME}/.env` | 仅手动 `probe` 用 |
| `ONEBOT_HTTP_URL` | **必须不存在/为空** | rev-1：`onebot_base_url()` 优先读它（`tools/onebot_client.py:57-65`），设了它探针就会 POST 到别处。`selftest` 检查，`probe_get_status()` 拒绝（§8.2.1）|
| `HERMES_QQ_SENTINEL_HEARTBEAT` | `1` | 每日心跳开关（§7.5） |

### 12.4 rev-1 未改动的契约（T4 依赖，本次修订刻意保持）

按 `tasks/T3-rev.md` 的「契约锁定」段，以下四项**一个字都没改**：

| 契约 | 状态 |
|---|---|
| `epoch` 取值 `datacenter` / `home_broadband` | 未改（只补了「切换时刻怎么处理」，§10.4.1）|
| session 记录含 `schema` / `duration_s` / `epoch` | 未改（`schema` 值从 1 提到 2，字段名与语义不变）|
| CLI 形态 `report --compare` / `report --epoch ...` | 未改（新增的 `probe` 是**另一个**子命令，不影响 report）|
| `sessions.jsonl` append-only | 未改（新增的 `raw_transitions.jsonl` 同样 append-only）|

新增的字段（`evidence_*` / `verification_degraded` / `generation_source` / `raw_transitions`）
与新增的 `end_reason` 取值（`sample_gap` / `epoch_change`）都是**增量**，
没有重命名或删除任何既有字段。
