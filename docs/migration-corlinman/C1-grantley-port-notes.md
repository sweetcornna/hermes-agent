# C1 — 格兰特利（Grantley）角色系统移植笔记

**批次**: 3（格兰系统）
**范围**: corlinman 的 persona 定义 / 生活机制 / 衰减机制 → hermes-agent 原生形态
**落地**: `plugins/grantley/`（插件包）+ `tests/plugins/grantley/`（89 个用例）
**依据**: `A3-hermes-extension-points.md` §6、Gaps G2/G3/G4；`AGENTS.md:19-23`、`:88-91`
**未触碰**: `plugins/platforms/onebot/`（由另一位工程师并行实现）；`/Users/cornna/project/corlinman`（只读引用）

---

## 0. 一句话结论

格兰的系统提示把**会变的状态**（心情 / 精神状态 / 现在在做 / 人在哪）直接写进了系统提示。
在 hermes 里这是缓存违规。本次移植的核心动作是**把文档沿缓存边界切开**：稳定身份留在系统提示层，
易变状态改走每轮用户消息的 sidecar。角色文本一个字节都没有改。

---

## 1. 缓存张力的解决方案（本次最重要的架构决策）

### 1.1 冲突是什么

源提示词里有这一段（`default_grantley.md:21-34`）：

```
## 此刻的我（实时状态）

- 心情：{{persona.mood}}
- 精神状态：{{persona.fatigue}}
- 最近在聊：{{persona.recent_topics}}
- 现在在做：{{persona.life_activity}}
...
```

`AGENTS.md:19-23` 的红线：

> **Per-conversation prompt caching is sacred.** A long-lived conversation reuses a cached
> prefix every turn. Anything that mutates past context, swaps toolsets, or rebuilds the
> system prompt mid-conversation invalidates that cache and multiplies the user's cost.
> We do not do it (the one exception is context compression).

`AGENTS.md:88-91` 进一步要求 "a system prompt that is **byte-stable for the life of a
conversation**"。

这些字段的变化周期（衰减任务每小时一跳、生活节拍每天一跳、模型调 `persona_life_set_state`
随时一跳）**远短于一次会话**。把它们插进系统提示，等于承诺"任意一跳落在会话中间就整段前缀
缓存击穿"。这不会让角色坏掉，只会让它变贵——所以测试抓不到，必须靠设计抓。

### 1.2 采用的方案：沿边界切分文档，而不是编辑文档

`plugins/grantley/persona.py::split_persona_document` 在 `## 此刻的我（实时状态）` 处把
byte-exact 的源文档切成两半：

| 半边 | 内容 | 去向 | 缓存性质 |
|---|---|---|---|
| **stable**（3458 字符） | 其余全部章节，实时状态段替换为一段固定指针文本 | `SOUL.md`（profile 身份槽位） | **不含任何占位符** ⇒ 构造上字节稳定 |
| **volatile**（328 字符） | 实时状态段本身，连同它自己的引导语 | `MemoryProvider.prefetch()` → `<memory-context>` → 用户消息 `api_content` | 每轮可变，**成本为零** |

关键点：切分是**机械的、可复现的**，磁盘上的资产与 corlinman 原文逐字节一致
（`test_prompt_asset_is_byte_exact` 用 sha256 钉死）。我们没有"改写"角色文本，只是在
装载时按标题切开。

唯一新写的中文是 `LIVE_STATE_POINTER`——一段固定的指针文本，告诉模型"你的实时状态在每轮
消息的 `<persona-state>` 块里"。它属于框架脚手架而非角色内容；没有它，模型不知道挂在用户
消息上的那个块描述的是它自己。它不含任何占位符，因此不影响字节稳定性。

### 1.3 为什么是 sidecar 而不是别的通道

hermes 有三条能承载人格文本的通道，只有一条能承载**每轮都变**的内容：

| 通道 | 能否按频道变 | 能否会话内每轮变 | 本次用途 |
|---|---|---|---|
| `SOUL.md` / `register_system_prompt_section` | 否 / 否（拿不到 `chat_id`） | **否**——stable 层，改就击穿 | 稳定身份 |
| `ephemeral_system_prompt` / `channel_prompt` | **是** | **否**——仍是系统消息的一部分，会话内必须字节稳定 | 按频道人格（每日冻结快照） |
| `MemoryProvider.prefetch()` → 用户消息 `api_content` | 是（provider 自己决定） | **是**——挂在最新一条用户消息上，落在缓存前缀之后 | 一切衰减内容 |

A3 G2 明确点名了反面做法，我们遵守了：

> **不要**为衰减内容实现 `system_prompt_block()`——prefetch 的产出落在缓存安全的用户消息
> sidecar，system_prompt_block 落在系统提示里会每轮击穿缓存。

`GrantleyMemoryProvider.system_prompt_block()` 因此**刻意返回空字符串**，并在 docstring 里
写明了原因，避免后人"顺手补上"。

### 1.4 代价与取舍（诚实记录）

这个方案不是免费的，三个已知代价：

1. **历史里会堆积过期的状态块。** 第 N 轮的 `<persona-state>` 会一直留在对话历史里，第 N+1 轮
   又挂一个新的。模型可能看到互相矛盾的状态。缓解：块用 `<persona-state>` 标签包裹，指针文本
   明确写了"**以最新的一个为准**"。彻底解决需要改写历史，那恰恰是 `AGENTS.md` 禁止的。
2. **每轮都要付 sidecar 的 token。** 状态块 ~330 字符 + 最多 5 条生活事件。这是设计意图——
   它换来的是前缀缓存不失效，净成本远低于每轮重算前缀。`event_top_n` 可调。
3. **`is_trivial_prompt` 会跳过 prefetch。** 对"在吗""哈喽"这类招呼，hermes 不调 prefetch，
   那一轮模型看不到实时状态。判断是可接受的（打招呼不需要生活状态），但这是**未经真实网关
   验证**的行为，记入风险。

### 1.5 被否决的备选方案

| 备选 | 否决理由 |
|---|---|
| 保留占位符在系统提示，仅在会话开始时解析一次 | 会话可以持续几天；那时"现在在做"已经是几天前的事，角色会说出明显过时的状态。而且每开一个新会话就是一个新前缀，多频道场景下缓存命中率反而更差 |
| 把状态变更批到会话边界，中途冻结 | 与上一条同病；且需要一个"会话边界"钩子来触发刷新，而 `on_session_start` 拿不到 `chat_id` |
| 接受会话开始时失效一次 | 这其实就是 `SOUL.md` 方案本身；对**稳定身份**我们正是这么做的。对易变状态不适用 |
| 用 `transform_llm_output` 钩子事后注入 | 改的是模型输出，不是输入；解决不了"模型需要知道自己现在的状态"这个问题 |

---

## 2. 移植了什么

### 2.1 文件树

```
plugins/grantley/
├── plugin.yaml                     kind: exclusive（memory provider 按名激活）
├── __init__.py                     register(ctx)
├── persona.py                      文档装载 / 缓存边界切分 / {{persona.*}} 解析
├── state.py                        PersonaState、疲劳分桶、话题格式化、dedup+cap
├── decay.py                        衰减数学（纯函数，双时钟）
├── life.py                         生活文档、种子库、生活节拍抽取、节奏信号
├── store.py                        sqlite：persona_state + 追加型 life_events
├── tools.py                        persona_life_* 六个模型工具
├── jobs.py                         无 LLM 的 decay / life_advance 任务
├── channel_binding.py              按频道人格（OneBot 适配器集成点）
├── assets/
│   ├── grantley.md                 系统提示正文（byte-exact）
│   └── life_seeds/grantley.yaml    世界观种子包（byte-exact）
└── scripts/grantley_job.py         cron 入口（no_agent）

tests/plugins/grantley/
├── test_persona_rendering.py       占位符 / 状态渲染 / 资产字节一致性
├── test_decay.py                   衰减数学（含话题时钟解耦）
├── test_life_and_events.py         种子包、节拍抽取确定性、事件衰减、频道绑定
└── test_cache_safety.py            缓存属性
```

### 2.2 逐字节保留的资产

| 资产 | 来源 | sha256 |
|---|---|---|
| `assets/grantley.md` | `corlinman-server/.../persona/default_grantley.md`（**仓库版**） | `db2efde6…81ec25` |
| `assets/life_seeds/grantley.yaml` | `corlinman-agent/.../persona/life_seeds/grantley.yaml` | `ce280754…fb26fb` |

两者都由测试用 sha256 钉死，任何"顺手改进"都会红。

关于 `web_search` vs `WebSearch`：生产副本与仓库副本仅此一行不同。**采用仓库版的
`web_search`**——hermes 里线上工具名是有意义的，且仓库副本是后修的那一版。
`test_prompt_uses_web_search_wire_name` 双向断言（含 `web_search`、不含 `WebSearch`）。

### 2.3 衰减机制（源：`corlinman_persona/decay.py`）

逐条保留：

- `fatigue`：`max(0.0, fatigue - hours * 0.1)`，仅在 `hours_elapsed > 0` 时应用
- `mood`：仅当原本是 `"tired"` **且**新 fatigue **严格小于** 0.3 时翻成 `"neutral"`；
  其他 mood 标签一律不动（有专门用例锁 `0.3` 边界是 `<` 而非 `<=`）
- `recent_topics`：丢弃 `floor(topic_hours/24) * 1` 条**最旧**的（列表头部），按长度钳制
- 双时钟：`topic_hours_elapsed` 与 `hours_elapsed` 解耦；**仅当两个时钟都 ≤ 0 时**才 no-op
- `recent_topics` 上限 20、dedup 保留最后一次出现的位置

**疲劳分桶**（源：`corlinman_persona/placeholders.py`）原样保留，边界下闭：

| fatigue | 标签 |
|---|---|
| ≥ 0.75 | `tired` |
| ≥ 0.4 | `mild fatigue` |
| ≥ 0.15 | `fresh` |
| ≥ 0.0 | `rested` |

**裸浮点数永远不进提示词**——`test_fatigue_never_renders_as_a_float` 逐档验证，
`test_prefetch_reflects_state_and_lands_in_api_content` 额外断言 `"0.9" not in block`。

`recent_topics` 渲染：取最新 5 条、反转成新→旧、逗号连接；空列表渲染成 `""` 而非 `"[]"`。

### 2.4 生活机制（源：`corlinman_agent/persona/life.py`）

- 生活文档结构 `{schema_version, current{state,location,activity,companions,mood,weather,
  since,until_estimate,story_arc}, history[]}` 原样
- `_mirror_placeholder_keys` → `mirror_placeholder_keys`：把 `current` 的字段镜像到扁平
  `state_json["life_*"]` 键，这是占位符层的承载点
- 种子库解析链 **generic ← bundled pack ← operator override** 原样；slug 守卫
  （挡 `..` / `/` / 非 ascii）原样
- `compute_life_signals` / `days_since_last_outing` / `pick_nudge` 逐条移植，
  阈值不变：`OUTING_OVERDUE_DAYS=13`、`SAME_STATE_STALE_DAYS=6`、`OUTING_TOO_LONG_DAYS=8`；
  三条 nudge 的中文文案逐字保留
- 上限原样：diary 200 条、history 100 条、单条 diary 4000 字符
- `_ALLOWED_STATES` 六个状态原样
- 六个工具的**线上名**原样：`persona_life_get` / `set_state` / `diary_add` /
  `event_seed` / `set_seeds` / `get_seeds`

**无 LLM 属性被当作契约来守**：`test_life_advance_makes_no_llm_call` 用 AST 解析
`jobs.py` 的**整个可达导入图**（jobs → life/decay/store → state），断言没有任何一环
能触达模型客户端（`openai`/`anthropic`/`httpx`/`agent`/`model_tools`/…）。用 AST 而不是
文本查找，是为了让 docstring 里提到 LLM 不会误判、也让别名导入藏不住。

### 2.5 带衰减的生活事件存储（A3 G2）

`store.py` 里的 `life_events` 表，schema 与 A3 G2 指定的一致：
`(id, persona_id, created_at, salience, text, kind)`，**追加型**，正常路径不 update 不 delete。

衰减公式照抄树内唯一实现 `plugins/memory/holographic/retrieval.py::_temporal_decay`：

```
weight = salience * 0.5 ** (age_days / half_life_days)
```

半衰期 0 = 关闭衰减（返回 1.0），与该模块 `temporal_decay_half_life: 0` 的约定一致。
默认半衰期 14 天。**衰减在读取时施加**，所以调半衰期不需要动任何一行存量数据——
`test_changing_half_life_changes_recall_without_touching_rows` 锁住了这个性质。

salience 分级：模型主动的 `set_state` = 1.0 > 日记 = 0.8 > 自动节拍 = 0.5。

### 2.6 按频道人格（A3 G3）

`channel_binding.py` 提供 `PersonaChannelBinding` + `resolve_channel_prompt()`。
真正按频道变的是**谁是这个频道的 channel_owner / 群主**——单相思那条动力学（H9）指向的对象
每个群不同，这没法写进共享身份提示。

缓存契约：ephemeral 允许跨会话变，但**会话内必须字节稳定**。所以快照是
**每日冻结**的——只读 `(persona_id, 日期)` 确定性推出的当日节拍，
**绝不读 fatigue / mood / recent_topics**。`test_channel_prompt_is_byte_stable_within_a_day`
和 `test_snapshot_contains_no_decaying_value` 分别锁这两条。

---

## 3. 有意改动了什么（及理由）

| # | 改动 | 理由 |
|---|---|---|
| **D18** | **生活节拍的类别抽取：严格优先级 → 非空类别中均匀随机** | 见下方专节。修的是继承来的死代码缺陷 |
| D11 | 系统提示切成 stable / volatile 两半 | 本文 §1。不切就是缓存违规 |
| D12 | 节拍抽取从 `random.Random()` 改为按 `(persona_id, 日历日)` 确定性播种 | G3 的"每日冻结快照"要求任何进程问"今天什么节拍"都得到同一答案；顺带让抽取可测。想要真随机的调用方（模型的 `event_seed` 灵感抽取）自带 rng |
| D13 | `PersonaState` 增加第二个时钟 `topics_aged_at_ms`，并新增 `carry_topic_clock()` | 源码的 `apply_decay` 签名里有 `topic_hours_elapsed`，但存储层只戳一个时间戳，导致所有调用方都退化回单时钟——**双时钟设计在源系统里是够不着的**。话题时钟只按"已消耗的整天"推进、保留余数，所以每小时跑一次也仍然精确地每天老化一条话题（`test_hourly_decay_still_ages_exactly_one_topic_per_day`） |
| D14 | `fatigue` 在构造时钳进 `[0,1]` | 源码只在渲染时钳。一次坏写入会持久化越界浮点，之后每次衰减都要往回走 |
| D15 | `hours_between()` 对未设置（`0`）或未来的时间戳返回 `0.0` | 朴素的 `now - 0` 会让一条新行"衰减 50 多年" |
| D16 | 存储路径改为注入式（`plugin_db` / 显式连接），脚本里三级兜底解析 | 直接针对生产事故：`persona.decay` 跑了 1260 次、失败 1260 次，全是 `data_dir_unavailable`——它从一个从未被填充的 app-state 属性上取数据目录，静默永久空转。现在解析不出数据库是**非零退出 + stdout 打原因** |
| D17 | `sync_turn()` 明确不做事（不自动摄取对话） | 自动把每轮对话灌进事件库，会用闲聊塞满衰减窗口，把真正要浮出来的生活节拍淹掉 |
| D19 | `set_seeds` 只写 override 层，永不改 bundled 包 | 格兰的 lore 不可再生，必须留在版本控制里 |
| D20 | 不移植 `persona_inject.py` 的整条注入链 | hermes 有自己的系统提示层；A5 §5 也建议不直接移植 |

### D18 —— 修掉"格兰永远出不了门"

**缺陷**：源实现 `persona_life_advance.py:108-150` 把三个类别当**严格优先级**走，命中第一个
非空就 `break`：

```
academy_scene → at_academy
mission_scenario → on_mission
travel_destination → traveling
```

两个后果互相咬死：

1. `travel_destination` 排最后 ⇒ 只有 `academy_scene` **和** `mission_scenario` **都为空**时
   才会被选中；
2. 但选中后那段"从 mission/academy 池重抽 activity"的逻辑，**要求那两个池至少一个非空**。

条件互斥 ⇒ **重抽分支永远执行不到**。而 `grantley.yaml` 同时填了 `academy_scene`（10 条）和
`mission_scenario`（10 条）⇒ **`traveling` 状态永远抽不到**，种子包里 10 个旅行目的地是死数据，
格兰在自动节拍里永远离不开学院。

**决策：修，不保。** 依据：`persona.life_advance` 在 corlinman 生产**从未启用**
（`[persona.life_advance] enabled = false`，运行历史零触发），不存在需要保持一致的线上行为，
修复零回归风险。反过来，保留一个明显非预期的死分支，会让角色永久丢失一整类生活场景。

**改法**：类别选择从"走优先级取第一个非空"改为"**在所有非空类别中均匀随机取一个**"。
其余抽取逻辑（companion / weather / mood、`SOLO_COMPANION` 处理、travel 的重抽）全部不变。
偏离及理由写在 `draw_life_beat` 的 docstring 里。

**验证**：
- `test_grantley_can_actually_leave_the_academy` —— 用真实种子包跑 60 个 seed，断言三种
  life_state 全部出现（源实现下这个集合永远是 `{"at_academy"}`）
- `test_both_categories_are_reachable_when_both_pools_exist` —— 只给 travel+mission 时两种结果都能出
- `test_travel_draw_redraws_activity_away_from_the_destination` —— 抽中 travel 时
  `location` 是目的地、`activity` 来自 mission 池（即重抽确实生效）
- `test_travel_only_pack_leaves_activity_as_the_destination` —— 退化情形（无可重抽的池）行为明确

---

## 4. 切换时必须携带的数据

> ⚠️ **路径陷阱（先读这条）**：这些数据在 **`/opt/corlinman/execution-state/`**，
> **不是** `/opt/corlinman/data/`。后者是 **2026-07-27 冻结的死快照**，已经骗过了三方审计
> ——照着它做迁移会静默丢掉近三周的真实产出。取数前先确认目录。

| # | 数据 | 位置 | 丢失后果 | 状态 |
|---|---|---|---|---|
| 1 | **19 篇真实发布的说说**，带真实 `tid` / `qzone_url`，2026-07-28 → 08-17 | `execution-state/qzone_post_log/grantley.json` | 防重复发帖失效；角色会重发已发过的内容，且丢失公开发言的连续性 | **必须携带** |
| 2 | 已回复评论去重标记 | `execution-state/qzone_seen_comments/` | 切换后**重复回复**历史评论 | **必须携带** |
| 3 | 好友评论去重标记 | `execution-state/qzone_friend_comments/` | 同上 | **必须携带** |
| 4 | grantley 的约 **1928 行对话历史** | 活库 `mk_observations` | 唯一的真实交互记录；蒸馏管线从未跑过，`mk_items` 是空的，所以这是**仅有的**记忆原料 | **必须携带** |
| 5 | channel_owner 绑定 uid `2104743984` | 通道配置 | 单相思动力学（H9）失去指向对象，角色对群主退化成对普通群友的语气——这是人格最显眼的一层 | **必须携带** |
| 6 | 主要对话对象 uid `536132102`（290 条） | `mk_observations` | 关系连续性 | 随 #4 一并 |

### 4.1 承接方式

- **#5** 写进 `plugins.entries.grantley.settings.channels`，形如：

  ```yaml
  plugins:
    entries:
      grantley:
        settings:
          channels:
            "183287894":
              persona: grantley
              channel_owner: "2104743984"
              group: true
  ```

  由 OneBot 适配器经 `channel_binding.resolve_channel_prompt()` 消费（见 §5）。

- **#4** 的 1928 行对话历史**不要**直接灌进 `life_events`。事件库是**情景生活日志**，
  不是对话存档；把 1928 行原始对话塞进去会让衰减检索每轮浮出闲聊而不是生活节拍
  （这正是 D17 拒绝 `sync_turn` 自动摄取的同一个理由）。建议路径：先离线蒸馏成
  少量高 salience 的关系/事件条目再 `append_event`，原始行归档到 hermes 的会话存储。
  **蒸馏管线在 corlinman 从未跑过，所以这一步没有可复用的实现，需要单独排期。**

- **#1 #2 #3** 属于 qzone 工具族（C3）的状态，本任务不实现 qzone 工具，但**必须在
  C3 落地前先把文件搬过去**，否则第一次跑 `qzone.reply_comments` 就会重复回复。
  A5 §3.12 记了它们的结构（post_log 30 条 × 500 字符；seen_comments
  `{"version":2,"seen":{tid:[...]}}`，200 identity/tid × 100 tid，LRU）。

### 4.2 不需要携带的

- **persona 运行态**（`mood` / `fatigue` / `recent_topics` / `state_json`）——生产是空的
  （`mood=neutral, fatigue=0.0, recent_topics=[], state_json={}`）。没有 life、没有 diary
  条目被写过。**从零开始即可，没有任何东西可迁移。**
- **立绘 / 表情资产**——格兰零资产。
- **`mk_items` 蒸馏记忆**——完全为空，蒸馏管线从未运行。
- **`persona.decay` 的运行历史**——1260 次全失败，没有可继承的状态。

---

## 5. 集成点：OneBot 适配器

**本任务不实现 QQ 通道绑定本身。** 契约如下，适配器侧零依赖：

```python
from plugins.grantley.channel_binding import (
    PersonaChannelBinding, bindings_from_config, resolve_channel_prompt,
)

# 启动时：从 settings.channels 读一次
bindings = bindings_from_config(settings.get("channels"))

# 每条入站消息：
binding = bindings.get(str(chat_id))
event.channel_prompt = resolve_channel_prompt(binding)
```

- `MessageEvent.channel_prompt` 定义在 `gateway/platforms/base.py:2363`；
  网关在 `gateway/run.py:5211-5213` 把它并进 `combined_ephemeral`。
- 这正是 Discord / Feishu / Telegram 适配器已有的做法（A3 G3 的"惯用路线"）。
- `resolve_channel_prompt` 返回 `None` 时适配器什么都不做即可——角色照常工作，只是少了
  按频道的 owner 框定。它**永不抛异常**（"persona is decorative; chat must keep working
  when it breaks"）。
- 适配器**不需要**了解 persona 的任何内部结构，也不需要 import 除这一个模块以外的东西。

---

## 6. 部署形态

插件在仓库里是为了评审和测试；**部署形态是把整个目录拷到 `$HERMES_HOME/plugins/grantley/`**。
`plugins/memory/__init__.py` 会把它作为"user-installed provider"扫到（该文件头部列出的
四个来源之二）。因此包内所有 import 都是**相对导入**，以便在 in-repo 的
`plugins.grantley` 和部署后的 `_hermes_user_memory.grantley` 两个命名空间下都能工作。

激活方式是**按名选中**，不是 `plugins.enabled`：

```yaml
memory:
  provider: grantley
```

稳定身份单独装：

```bash
python plugins/grantley/scripts/grantley_job.py install-profile /opt/hermes/data/profiles/grantley
```

它把 stable 半边写成该 profile 的 `SOUL.md`。**只在安装时跑，绝不在会话中途跑**——
会话中途改 `SOUL.md` 正是 `AGENTS.md:88-91` 禁止的模式。

两个 cron 任务（都 `no_agent: true`，零 LLM 开销，对应 A3 G4）：

```bash
python plugins/grantley/scripts/grantley_job.py decay     # 建议每小时
python plugins/grantley/scripts/grantley_job.py advance   # 建议每天一次
```

> **时区**：按 `00-PLAN.md` D8，迁移任务时区一律显式声明。生产机是 **JST (+0900)** 而非上海，
> 隐式回退会让标称时间整体平移一小时。

**零新增运行期依赖**：只用到 `sqlite3`（stdlib）与 `pyyaml`（`pyproject.toml` 基础依赖
`pyyaml==6.0.3`，非 extra）。

---

## 7. 测试

```
tests/plugins/grantley/  —  89 passed
```

覆盖任务要求的五个方面：

| 要求 | 文件 | 关键用例 |
|---|---|---|
| 占位符 / 状态渲染 | `test_persona_rendering.py` | 疲劳分桶八档、话题新→旧取 5、未知键返回 `""`、资产 sha256 |
| 衰减数学（含解耦的话题时钟） | `test_decay.py` | `0.3` 边界是 `<`、双时钟独立、每小时跑仍每天老化一条 |
| 节拍抽取的确定性 / 播种 | `test_life_and_events.py` | 同日同节拍、跨日不同、注入 rng 可复现、D18 三种状态全可达 |
| 设计的缓存属性 | `test_cache_safety.py` | 状态变更后 stable 前缀逐字节不变；`system_prompt_block()` 恒空 |
| （附加）事件衰减 | `test_life_and_events.py` | 半衰期公式对齐 holographic、改半衰期不动存量行 |

缓存属性的断言方式（`test_state_change_does_not_mutate_the_cached_prefix`）：
跑一次生活节拍 + 追加事件 + 改 mood/fatigue + 跑一次衰减，然后断言
`load_persona_document().stable` 与操作前**逐字节相等**；同时
`test_prefetch_changes_between_turns_while_the_prefix_does_not` 断言 sidecar 确实变了——
两条一起才证明"状态真的动了，而前缀真的没动"。

`test_cache_safety.py` 走的是**真实路径**（`agent.memory_manager.build_memory_context_block`、
`agent.turn_context.compose_user_api_content` 真实 import），不是 mock。

---

## 8. 遗留风险

见任务回报的 `## Risks and gaps`。要点：

1. **无真实网关 / 真实 LLM 验证**。`prefetch` 进入 `api_content` 是在真实函数上验证的，
   但"provider 被 `memory.provider: grantley` 真正激活并在活跃会话里被调用"这条链路
   没有跑过。
2. **`is_trivial_prompt` 跳过 prefetch** 的实际影响未观测。
3. **部署命名空间未验证**：`_hermes_user_memory.grantley` 下的相对导入按代码阅读应当可行，
   但没有在真实的 `$HERMES_HOME/plugins/` 安装里跑过。
4. **历史里堆积过期状态块**对长会话的实际影响未观测（§1.4 第 1 条）。
5. **1928 行对话历史的蒸馏没有实现**，也没有可复用的源实现（管线从未跑过）。
6. **qzone 状态文件（#1 #2 #3）本任务未搬运**，是 C3 的前置条件。
