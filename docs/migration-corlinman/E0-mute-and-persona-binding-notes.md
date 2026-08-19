# E0 — 两个切换阻塞项：出站静音门控（D44）与人格按频道绑定接线

> 分支 `feat/corlinman-migration`。两项合并在一个任务里，是因为它们都改
> `plugins/platforms/onebot/adapter.py`，分开派会撞车；除此之外两者不相关，
> 因此**分成两个提交**。

| 提交 | 内容 |
|---|---|
| `3937e3041` | `onebot: make group_replies_enabled a two-way gate (D44)` |
| `4b5cb7938` | `onebot: wire the per-channel persona binding on both lanes` |

对上游的改动：**零**。全分支 `git diff --diff-filter=MDRT --stat $(git merge-base main HEAD)..HEAD`
仍只有 `.gitignore` +4 行（C4 遗留，非代码）。

---

## 1. 任务 A —— `send()` 遵守 `group_replies_enabled`

### 1.1 修的是什么

`group_replies_enabled` 在源系统里是「紧急静音**所有**群发言」，连 monitor 摘要都掐死
——生产里 `qunjlu` 从未送达正因如此。本移植中它只在**入站**被消费
（`router.py` 的回复门控）以及 `proactive.py` 自己的 gate ladder。
**从另一个方向来的一切照发不误**：投递到 `onebot:g<id>` 的 cron 任务、
模型调用 `send_message`、媒体上传。

事故语义因此是反的：运维按下静音，看到回复停了，而定时输出还在往同一批群里发。
QQ 侧整个安全论证（D17 双发风险）依赖「静音可信」，所以这是切换阻塞项而不是优化项。

### 1.2 门控落在哪些出口

一条群消息能离开本适配器的路径全部堵上：

| 出口 | 位置 | 说明 |
|---|---|---|
| `send()` | `adapter.py` | 文本、分块、`[MSG_BREAK]` 气泡、合并转发卡 |
| `_send_attachment()` | `adapter.py` | 文件上传 + 内联图片/语音 + caption |
| `send_image()` URL 分支 | `adapter.py` | 该分支不走 `_send_attachment` |
| `_send_segments()` | `adapter.py` | **纵深防御**：所有文本/内联媒体发送的唯一收口，后来新增的路径绕不过去 |
| `_standalone_send()` | `adapter.py` | **cron 跑在网关进程之外时走的就是它**（`tools/send_message_tool.py` 在 in-process adapter weakref 为 None 时回退到这里）。只堵活适配器等于把 D44 描述的洞原样留在隔壁进程 |

`_deliver_forward()`（合并转发卡）与文件上传分支不单独加门，因为它们只能从已加门的
`send()` / `_send_attachment()` 进入。

**私聊完全不受影响**，所有出口都是。静音管的是「在一屋子人面前说话」。

### 1.3 被拦时返回什么（验收要点 4）

```python
SendResult(
    success=False,
    error="OneBot: group speech is muted (group_replies_enabled=false); "
          "nothing was sent to group 183287894",
    error_kind="unknown",
    retryable=False,
    raw_response={"onebot_group_muted": True,
                  "reason": "group_replies_enabled=false",
                  "group_id": 183287894},
)
```

`_standalone_send()` 返回同构的 dict（`{"error": ..., "onebot_group_muted": True, ...}`），
因为它的契约是 dict 不是 `SendResult`。

调用方区分「被静音」与「发送失败」的方式：

```python
from plugins.platforms.onebot.adapter import is_muted_send_result
is_muted_send_result(result)   # SendResult 和 dict 都吃
```

不是靠匹配错误文案。两者都是 `success=False`（**不假装成功**），也都**不抛异常**。

#### 为什么 `error_kind` 不是 `forbidden` / `not_found`

这两个在语义上最贴切，也正是 `gateway/dead_targets.py::_DEAD_ERROR_KINDS` 的全部内容。
返回它们会让投递层把该群记入**永久不可达**名单
（`delivery.py:376-385`，且 `is_dead()` 会在后续每次投递前短路跳过）。
静音是运维随手会拨回去的开关，把目标标死会**活得比静音本身更久**，
把可逆开关变成粘滞开关——这正是本次迁移已经踩过多次的那类陷阱的镜像。

`unknown` 是词汇表里剩下的诚实选项：这是**本地策略拒绝**，不是平台错误，
而 `SEND_ERROR_KINDS` 里没有这个词，且我不能改 `gateway/platforms/base.py`（上游文件）。

**同样重要**：投递层的死目标判定其实是从**抛出的异常文本**再分类一次
（`_classify_dead_from_error_text`），不看 `error_kind` 字段。所以错误文案被刻意写成
不含 `classify_send_error` 匹配的任何子串（`forbidden` / `not a member` /
`chat not found` / `flood` / `rate limit` / `retry after` / `too long` /
`_RETRYABLE_ERROR_PATTERNS`）。测试直接钉住这条：
`classify_send_error(None, res.error) == "unknown"` 且
`DeadTargetRegistry.is_dead_error_kind(res.error_kind) is False`。

`retryable=False` 且 `retry_after=None`，所以重试阶梯不会把一条不该发的消息重试出去。

### 1.4 三条通道叠加的行为矩阵（验收要点 5）

判定式没有变，仍是 B4 那个更严格的形式，只是现在**三条通道读同一个函数**：

```
router_flag = adapter.router.group_replies_enabled        # 构造时的值，入站回复实际遵守的那个
live_flag   = extra["group_replies_enabled"] if present else router_flag   # 热生效
muted       = not (router_flag and live_flag)
```

| router_flag | live_flag | 入站回复门控（B2） | 主动发言门控（B4） | 出站 `send()`（D44） | 群里实际收到 |
|:---:|:---:|:---:|:---:|:---:|:---:|
| F | F | 丢弃 | 静音 | 拒发 | **无** |
| F | T | 丢弃 | 静音 | 拒发 | **无** |
| T | F | **放行**（router 持旧值） | 静音 | **拒发** | **无** |
| T | T | 放行 | 不静音 | 放行 | 正常发言 |

私聊在四行里都不受影响。

第三行是运维上最要紧的一行，也是这次改动新增的能力：**热按静音**时 router 还持有构造时的旧值，
入站仍会起一个 turn，但**答案在门口被拒**，不会进群。代价是白烧一次模型调用；
收益是「按下静音后群里立刻没有任何输出」这句话变成真的。让 router 也读 live 值属于 B2 的范围，
本次**没有**动（见 §4 遗留）。

实测（`_send_with_retry` 全链路，非推导）：热静音下一次群回复产生
1 条适配器 WARNING + 网关的 plain-text fallback 再撞一次门（第 2 条 WARNING + 1 条 ERROR），
**wire 上 0 个 action**，最终 `SendResult.success=False`、`is_muted_send_result=True`。
即 fallback 路径不会把消息漏出去，只是多两行日志。

### 1.5 D45：D2 的结构性抑制保留，未改回读标志

`qunjlu` 仍靠 `deliver="local"` + `enabled_toolsets=()`→`["no_mcp"]` 抑制，本次一行未动。
两者叠加，不是二选一——结构性抑制不依赖任何运行时配置读取正确。测试里钉住了这一点。

**顺带查实并记录**：`sanhu` / `jlu` 的 `deliver` 是 `onebot:2104743984`，
即**私聊**（裸 uin，不是 `g<id>`）。所以 D44 这道闸**不覆盖它们**——
这符合「静音管的是群发言」的设计，但意味着**这个标志不是这两个任务的 kill switch**，
不应有人以为它是。已写进 README 与测试（`test_the_two_qq_digests_deliver_to_a_dm_which_the_mute_never_touches`）。

---

## 2. 任务 B —— 人格按频道绑定接线

### 2.1 按实际签名接的证据（验收要点 6）

先读代码再接，`channel_binding.py` 的实际公开面：

```python
@dataclass(frozen=True)
class PersonaChannelBinding:
    persona_id: str = PERSONA_ID
    chat_id: str = ""
    channel_owner_id: str | None = None
    is_group: bool = False
    display_name: str = ""

def bindings_from_config(raw: Mapping | None) -> dict[str, PersonaChannelBinding]
def resolve_channel_prompt(binding, *, on=None, data_dir=None) -> str | None
```

接入方式与文档描述的两处差别，均以**代码**为准：

1. docstring 的示例是在适配器里**手工构造** `PersonaChannelBinding(...)`；
   实际用的是 `bindings_from_config(raw)` 再按 `str(chat_id)` 取——
   这才是它自己提供的、能吞掉配置笔误的入口（畸形条目跳过而不抛）。
2. docstring 没提 `on=`；实际签名有，且它是本次能加缓存的**前提**（见 §2.3）。

测试 `test_it_calls_the_real_channel_binding_signature` 用 `inspect.signature` 把
参数名、`on` 的 keyword-only 属性、以及 dataclass 字段集一并钉死，
所以任何一侧改签名都会在这里响，而不是在生产里静默退化成「没有 owner 框定」。

### 2.2 两条通道一致性怎么验证的（验收要点 6）

**结构上一致**：解析只有一份，放在新模块
`plugins/platforms/onebot/persona_binding.py::channel_prompt(extra, *, chat_id, is_group)`。
两条通道各自只有一行调用：

| 通道 | 调用点 |
|---|---|
| 入站回复 | `adapter.py::_build_message_event` → `channel_prompt(_gh_live_extra(self), chat_id=群号或对端uin, is_group=…)` |
| 主动发言 | `proactive.py::generate` → `channel_prompt(live_extra(adapter), chat_id=group_id, is_group=True)` |

B4 当初刻意不单独接主动发言那条腿（否则人格框架在「回复」与「主动发言」之间不一致）
——共享解析器就是这个判断的落地形式：一致性由构造保证，不靠两个文件保持约定。

**测试上也验证**：`test_both_lanes_frame_the_channel_identically` 对同一个群
分别跑 `_build_message_event` 与 `PR.generate`，断言两者的 `event.channel_prompt`
**相等且非 None**；`test_both_lanes_degrade_together` 断言未绑定时两者**同时**为 `None`。

### 2.3 配置来源与缓存

来源优先级：

1. 适配器 `extra["persona_channels"]`（**存在即采纳，空 dict 表示「没有绑定」而不是回退**）；
2. 否则 `plugins.entries.grantley.settings.channels` —— C1 §4.1 写给运维的那条路径。

第 2 条通过 `plugins.grantley.load_plugin_config()` 读取。该函数原名 `_load_plugin_config`，
本次改为公开并保留旧名别名——目的是**不在适配器里重新推导配置路径**，
否则路径一改就是两处要同步。测试 `test_the_documented_config_path_is_the_one_that_is_read`
钉住 `("plugins","entries","grantley","settings") in G._CONFIG_PATHS`。

缓存按 `(persona_id, chat_id, is_group, 日期)` 记忆渲染结果，并在每次未命中时清掉非今日条目
（上界 = 实际说话的频道数，不随 uptime 增长）。**这是合法的**：`channel_binding` 自己声明
输出是「daily frozen snapshot，是 `(binding, date)` 的纯函数」，本模块显式传 `on=today`，
所以记忆化不可能改变模型看到的任何一个字节。不缓存的话，每条入站群消息都会走一次
`life.resolve_seed_library()` 的 YAML 磁盘读（它自己没有缓存）。

`bindings_from_config` 的解析**不缓存**（纯 dict 遍历），这样运维改 `extra` 能像本适配器
其它键一样热生效。`plugins.entries.…` 那一路则是每进程读一次（见 §4 遗留）。

### 2.4 降级

任何一种「没有绑定」都止步于 `channel_prompt = None`，**不抛异常、不注入空框架**：

- 人格包不存在（`plugins.grantley` 与部署名 `_hermes_user_memory.grantley` 都试过）；
- 没有任何 `channels` 配置；
- chat_id 未登记；
- 条目畸形（`{"183287894": "not-a-mapping"}`）或整张表不是 mapping；
- 解析器本身抛异常 —— `channel_prompt` 外层有兜底 `try/except`。

最后一条是写测试时才补上的：原实现把 `bindings_from_extra` 放在 try 之外，
一个人为注入的异常会顺着 `_build_message_event` 冒出去。已修正并有测试
（`test_a_broken_resolver_costs_neither_lane_its_message`）。

### 2.5 一处刻意的偏离

配置写 `group: false` 而事件是群消息时，**以事件为准**（`dataclasses.replace` 改写 `is_group`，
并记一条 INFO）。理由：配置笔误不该让人格在群里以为自己在私聊；而且这也正是让两条通道
对同一频道保持一致的东西（主动发言恒为群）。测试
`test_the_event_wins_when_the_config_disagrees_about_group` 钉住。

---

## 3. 交付物与测试

### 3.1 文件

| 文件 | 行数 | 说明 |
|---|---:|---|
| `plugins/platforms/onebot/persona_binding.py` | 265 | **新增** —— 两条通道共用的人格框架解析 |
| `plugins/platforms/onebot/adapter.py` | 2263 | 静音判定 + 5 处出站门控 + 入站 `channel_prompt`（+153/-1） |
| `plugins/platforms/onebot/proactive.py` | 825 | 静音判定改为委派 + 主动发言 `channel_prompt`（+16/-8） |
| `plugins/platforms/onebot/README.md` | 496 | 双向闸说明、返回契约、`persona_channels` 键（+57/-0） |
| `plugins/grantley/__init__.py` | 109 | `_load_plugin_config` → `load_plugin_config`（保留别名）（+12/-4） |
| `tests/gateway/test_onebot_group_mute.py` | 465 | **新增** —— 任务 A，31 例 |
| `tests/gateway/test_onebot_persona_binding.py` | 335 | **新增** —— 任务 B，20 例 |
| `tests/gateway/test_onebot_plugin.py` | 1051 | 8 个群目标发送用例显式开启群回复（+13/-9） |

### 3.2 测试结果（与基线逐项对比）

| 套件 | 基线 | 本次 |
|---|---|---|
| `tests/gateway/test_onebot_group_history.py` | 81 passed | **81 passed** |
| `corlinman_jobs + qzone + grantley + cron` | 1452 passed, 1 skipped | **1452 passed, 1 skipped** |
| onebot 七件套 + `test_onebot_client.py` + `corlinman_jobs` | 757 passed | **757 passed**（不含新文件）／**808 passed**（含新增 51 例） |
| `tests/gateway/test_onebot_group_mute.py` | — | **31 passed** |
| `tests/gateway/test_onebot_persona_binding.py` | — | **20 passed** |

全部离线：不连 SSH、不打真实 HTTP、不碰真实 QQ、不触发任何真实发送。

### 3.3 为什么改了 8 个既有测试

它们向群发送时用的是默认配置，而默认配置是**静音**的——也就是说在旧行为下，
这些用例断言的是「往一个已静音的群里发消息成功」。新行为下它们必须显式
`group_replies_enabled: True`。断言内容一行未改，只补了配置。这不是放宽，
而是这些用例原本依赖的正是本次要修的缺陷。

---

## 4. 我没做的事 / 已知缺陷 / 遗留风险

1. **入站门控仍读构造时的 router flag，不读 live 值。** 热按静音时入站仍会起一次 turn
   （答案被出站门拦下，群里收不到，但模型调用已经花掉了）。让 router 读 live 属于 B2 的范围，
   本次没动。**影响**：静音期间群里被 @ 会白烧上游账号池调用——而 §18 已证实该池很紧。
   如果切换窗口要长期挂静音，建议单独立项让 router 也读 live。
2. **`sanhu` / `jlu` 投递到私聊，D44 覆盖不到。** 见 §1.5。它们目前靠 `install_enabled=False`
   （未启用）而非本闸抑制。
3. **`plugins.entries.grantley.settings.channels` 每进程只读一次。** 改这张表要重启才生效；
   `extra["persona_channels"]` 那一路是热生效的。绑定数据（群主 uid）是静态部署数据，
   判断可接受，但记在这里以免日后有人以为它热生效。
4. **`_deliver_forward()` 与文件上传分支没有各自的门。** 它们只能从已加门的入口进入，
   现在是安全的；如果将来有人直接调用它们，`_send_segments` 的纵深防御**不覆盖**这两条
   （它们走 `_call`）。这是已知的、有意的边界。
5. **人格框架未在真实模型调用里验证过。** 测试只验证 `event.channel_prompt` 被正确设置且
   两条通道一致；「网关确实把它并进 `combined_ephemeral`」这一步依赖
   `gateway/run.py:5211-5213` 的既有行为，**本次没有跑通端到端**（那需要真实 agent turn，
   而 D42 的身份句尚未落地，跑了只会烧池）。**建议列入切换窗口验证项**。
6. **缓存的时钟是 `datetime.now(timezone.utc).astimezone()`**，与 `life.now_dt()` 的表达式
   逐字相同，但这是**复制而非共用**。若 `life.now_dt()` 日后改用别的时区锚点，缓存键会与
   `resolve_channel_prompt` 内部的日期错开一天（表现为跨日那一刻框架不刷新）。
   本次显式传 `on=` 已经把 `resolve_channel_prompt` 的日期钉在我算出的那个值上，
   所以实际不会不一致——但「两处各写一遍 now」这件事本身值得记一笔。
7. **没有在生产机上验证任何东西**（按约束，生产机只读且不得改动）。目标机 SQLite / 时区
   相关的 D48 结论不受本次改动影响。
8. **`error_kind="unknown"` 是权衡后的次优解。** 最贴切的 `forbidden` 会导致死目标标记，
   而扩充 `SEND_ERROR_KINDS` 需要改上游文件。若日后允许改上游，正确做法是加一个
   `policy_blocked` 之类的 kind 并让 `dead_targets` 忽略它。
9. **没有触碰 `gateway/` `cron/` `tools/` `hermes_cli/`**，也没有启用任何任务、
   没有让 hermes 对外发出任何消息。
