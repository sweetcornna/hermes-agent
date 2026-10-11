# 格兰（格兰特利·贝尔）人格重新蒸馏：内部资料盘点

盘点日期 2026-10-04。只读：没有改动仓库任何文件，没有 SSH 到 P1（P1 上的现状一律标「未核实」）。
仓库 `/Users/cornna/project/hermes-agent`，分支 `feat/corlinman-migration`，HEAD `f9b29c49b6`。
`plugins/grantley/**`、`tests/plugins/grantley/**`、`docs/personas/grantley-*` 在工作区**无未提交改动**；
但 `agent/system_prompt.py`、`plugins/platforms/onebot/{adapter,persona_binding,sticker}.py` 有未提交改动，
其中两处直接影响人格（见 §1.6 与 §2）。

证据标记：【码】读了代码；【文】文档/记忆笔记记载；【算】本次盘点自己重算；【未核实】需要在 P1 上确认。

---

## 1. 硬约束清单

### 1.1 文件与它们各自的角色

| 路径 | 现值 | 角色 |
|---|---|---|
| `plugins/grantley/assets/grantley.md` | 3263 字符 / 8897 B | 身份文档。按 `## 此刻的我（实时状态）` 切成 stable（进系统提示）与 volatile（每轮附在用户消息后） |
| `plugins/grantley/assets/voice.md` | 1775 字符 / 4593 B | 示范层（怎么说话），纯缓存前缀载荷，不许含占位符 |
| `plugins/grantley/assets/life_seeds/grantley.yaml` | 71 行 | 每日生活节拍种子（mission 5 / travel 3 / academy_scene 8 / companion 7 / mentor 2 / tension 5 / weather 4 / mood 7 / duration 4 / season 1） |
| `plugins/grantley/assets/stickers/*.jpg` | 9 张，23–29 KB | 表情包图片；**描述文字不在此目录**，在 `plugins/platforms/onebot/sticker.py:92-146`（该目录没有 `catalog.json`） |
| `plugins/grantley/plugin.yaml` | `inject_identity_section`/`inject_voice_section` 默认都是 `false` | 开关。线上两者都开（P1 `config.yaml`，2026-09-19 记录【文】） |
| `plugins/grantley/channel_binding.py` | 渲染 `## 这个频道` | 频道层文本 + 车道闸 `is_grantley_onebot_lane()` |
| `plugins/platforms/onebot/persona_binding.py` | 把频道层与表情菜单拼成 `channel_prompt` | 两条车道（回复、主动发言）共用 |
| `plugins/platforms/onebot/adapter.py:5222` | `PLATFORM_HINT`（797 字符，**工作区版本≠HEAD 版本**） | 平台提示 |
| `plugins/platforms/onebot/sticker.py` | 菜单 467 字符，概率 0.18 | 表情包菜单（每轮掷骰） |

生产里**实际被读取的是哪一份资产**要核实：2026-09-14 在 p2 上踩过「三份同名副本、只有 `$HERMES_HOME/plugins/grantley/` 那份生效」
（`grantley-corpus-20260914/README.md` 文末「部署记录」）。P1 是原生部署、没有两棵 `/opt/hermes-cloud` 树，
但 `$HERMES_HOME/plugins/grantley/`（`/opt/hermes/data/plugins/grantley/`）仍可能与 repo 副本并存。
**判断办法**：看 journald 里 `Session plugin prompt section: id=grantley.identity ... chars=N`，N 应为 3072；
或用 `load_memory_provider("grantley")` 走真实路由（不能 `import plugins.grantley`，也不能直接调
`render_system_prompt_sections()`，且 session_info 必须带 `platform/chat_id/chat_type`，否则得到 0 段假阴性）。

### 1.2 必须逐字保留的东西

| 对象 | 精确值 | 位置 | 改了会怎样 |
|---|---|---|---|
| volatile 段标题 | `## 此刻的我（实时状态）`（全角括号；整行 `strip()` 后相等） | `persona.py:69`；匹配在 `persona.py:166` | 找不到标题 → `split_persona_document` 抛 `PersonaAssetError`（fail loud，`memory_provider.is_available()` 变 False） |
| volatile 段边界 | 从该标题到**下一个以 `## ` 开头的行**或文末 | `persona.py:170-175` | volatile 段内部若出现 `## `，段会被提前截断，后半截变成 stable（把占位符漏进缓存前缀；`assert_cache_safe` 会抛错） |
| 8 个占位符 | `{{persona.mood}}` `{{persona.fatigue}}` `{{persona.recent_topics}}` `{{persona.life_activity}}` `{{persona.life_location}}` `{{persona.life_companions}}` `{{persona.life_state}}` `{{persona.life_story_arc}}` | `grantley.md` volatile 段；集合被 `test_persona_rendering.py:84-93` 钉死 | 增删任何一个 → 测试红；新键会去 `state_json` 取，取不到静默渲染成空 |
| 占位符只能在 volatile 段 | stable 与 voice 中出现 `{{persona.*}}` 即抛错 | `persona.py:129-141`（`assert_cache_safe`）、`persona.py:208-218`（voice） | 同上 |
| 段 ID | `grantley.identity`、`grantley.voice`；渲染顺序 = ID 字典序（identity 先、voice 后） | `__init__.py:121-131`；`plugins.py:5396` | 系统提示里标题是 `## Plugin Context: grantley.identity`（`plugins.py:501`），多个测试数这个字符串 |
| 状态块标签 | `<persona-state>` … `</persona-state>`，**不许自带 `<memory-context>`** | `persona.py:103,277` | 管理器会再包一层，预包装会触发 warning 并被剥离 |
| 控制符 | `[MSG_BREAK]`（气泡分隔）、`[LONG_REPLY]`（必须在回复**最开头**才触发合并转发卡）、`[STICKER:slug]` | `adapter.py:167-168`；`send()` 里 `lstrip().startswith(LONG_REPLY_MARKER)` | `sticker.py:154` 的正则 `\[\s*STICKER\s*:\s*([A-Za-z0-9_-]+)\s*\]`；未知 slug 静默丢弃；主动发言的 `SKIP` 见 `proactive.py:240` |
| 亚戈绑定句 | ``QQ `2104743984` 就是亚戈``、`舍友和暗恋对象` | `channel_binding.py:65,267`；被 `test_life_and_events.py:859-870` 钉死 | 见 §1.5 |
| 同伴名 | 亚戈、奥斯卡、提奥、尤利乌斯、赫尔曼、保尔 六个名字必须**出现在 stable 文本里** | `test_life_and_events.py:55-58` | 任何一个从身份段里删掉 → 测试红 |

### 1.3 预算（当前值，字符，不是字节）

规则在 `hermes_cli/plugins.py`（HEAD 已提交）：单段 `4_000`、聚合 `8_000`、最多 32 段；
**超限是整段静默丢弃（只在日志打 warning），不是截断**。判定对象是 `strip()` 后的 `len(text)`，聚合要加上段头尾。

| 项 | 现值 | 上限 | 余量 | 出处 |
|---|---:|---:|---:|---|
| `grantley.identity` 内容 | 3072 | 4000 | **928** | `plugins.py:496-497,5438` |
| `grantley.voice` 内容 | 1774 | 4000 | **2226** | 同上 |
| identity 渲染后（含 `## Plugin Context: …` 头与 `<!-- …chars:N -->` 行） | 3152 | — | | `plugins.py:511-517`【算】 |
| voice 渲染后 | 1851 | — | | 【算】 |
| 聚合（含 `<!-- hermes-plugin-sections:start/end -->` 与分隔符） | **5079** | 8000 | **2921** | `plugins.py:5395-5455`【算】 |
| 文档记载的线上数字 | 5081 / 8000，余 2919 | | | 9-14 README；差 2 是 strip |
| 渲染开销 | ≈233（头尾 74 + 每段 ≈80 + 分隔 2） | | | 【算】 |

**测试低估了开销**：`test_persona_rendering.py:219-222` 用**原始** `len(stable)+len(voice)=4848 ≤ 8000` 判断，
没有算 ≈233 的渲染开销，所以真实可用内容上限是 ≈7767 而不是 8000；测试能在真实聚合已超限时仍然通过。
另外 `test_persona_rendering.py:111-112` 的 `len(stable) ≤ 4000` 量的是**未 strip** 的 stable（3073），`:215-216` 量的是未 strip 的 voice（1775）。
其他插件的段也计入 8000（线上有没有别的段【未核实】，看 journald 里 `Session plugin prompt section:` 行）。
超限没有任何运行期告警之外的自动保护，建议把「journald 里两段都出现且 chars 符合」写进部署检查单。

volatile 段（每轮 sidecar）当前 381 字符，含 8 条占位行；`event_top_n=5` 的事件行与生活节奏提醒另加。
它随每条**历史用户消息**一起持久化为 `api_content` 并在之后每次请求里重放（`agent/turn_context.py` 的
`compose_user_api_content` 注释），所以这里每多一个字，成本 = 字数 × 对话轮数。

其他长度常量（影响「气泡/卡片」形状，不影响提示词预算）：`MAX_MESSAGE_LENGTH=3800`（`adapter.py:119`）、
`FORWARD_TEXT_THRESHOLD=1000`（`:149`）、`DEFAULT_MAX_BUBBLES_PER_REPLY=3`（`:177`）。
判定在 `send()` 里对**整条回复**做：`needs_card = cards_enabled and (force_card or too_long or (too_many_bubbles and not merge_overflow))`
（`adapter.py:4126-4141`）。即第 4 个气泡一出现整条就变合并转发卡；`bubble_overflow: merge` 的逐频道表没配时默认走卡片。
表情贴纸挂在**最后一个气泡**里，不占条数（`test_onebot_stickers.py:558-585`）。

### 1.4 缓存安全要求

1. stable 半边必须是资产的纯函数：没有占位符、没有时钟、没有环境变量（`test_cache_safety.py:62-98`）。
2. 记忆 provider 的 `system_prompt_block()` 必须恒为空串（`test_cache_safety.py:104-111`）；
   `system_prompt_stable_across_compression()` 返回 True（`memory_provider.py:229`）。
3. 易变内容只许走用户消息 sidecar（`prefetch` → `<memory-context>` → `api_content`）；
   `sync_turn()` 刻意不做事（`test_cache_safety.py:124-127`）。
4. 资产修订只对**新建会话**生效；已存会话从 SQLite 还原冻结的系统提示字节
   （`test_persona_rendering.py:139-179`）。`session_reset.mode = none`，所以老会话永远是老人格——
   9-14 的做法是停网关后用 `SessionStore.reset_session()` 非破坏性换新 session_id（83 个会话）。
   工程化版本是 `docs/personas/grantley-humanizer-20260907/revise_sessions.py`（带回滚、继承历史），
   被 `tests/plugins/grantley/test_humanizer_revision.py:11` 与 `test_argo_revision.py:12`
   （指向 `docs/migration-corlinman/search-repair-20260906/revise_sessions.py`）按**路径**加载——**不要移动/改名这两个脚本**。
5. 频道层（ephemeral）约定「同一对话内字节稳定」（`channel_binding.py` 文件头）。
   **例外**：表情菜单每轮掷骰（p=0.18）追加在 ephemeral 末尾，一旦出现/消失就改变系统消息字节
   （`persona_binding.py:215-262` 文档承认这是刻意的）。对 Anthropic 端点的缓存代价【未测】。

### 1.5 测试钉住的断言（改人格时必须同步或保持）

**结构性（人格文本必须满足）**

| 文件:行 | 断言 | 对改写的含义 |
|---|---|---|
| `test_persona_rendering.py:80-97` | `doc.volatile` 以标题开头；占位符集合恰为上述 8 个；`stable == 标题前 + LIVE_STATE_POINTER + 标题后` | 标题、8 占位符、`LIVE_STATE_POINTER` 逐字 |
| `:100-104` | `"{{persona." not in stable` | |
| `:111-112` | `len(stable) ≤ 4000`（原始） | 最多再加 ≈927 字符（stable 原始 3073） |
| `:115-127` | 绑定车道里 `stable in prompt`，`## Plugin Context: grantley.identity` 出现 1 次，`stable` 出现 1 次 | 身份段不能被拆成两段注册，也不能在别处重复 |
| `:182-205` | telegram / cli / 未绑定群 → 没有身份段 | 车道闸 |
| `:215-216,219-227` | `len(voice) ≤ 4000`；`stable+voice ≤ 8000`；voice 无占位符 | 最多再加 ≈2225（voice）；两段合计最多再加 ≈3150（原始口径，真实口径少 ≈233） |
| `:230-234` | voice 含占位符 → `PersonaAssetError` | |
| `:289-297` | voice 段是资产的纯函数，两次渲染逐字节相同 | |
| `:300-302` | 无标题的文档 → 抛错 | |
| `test_life_and_events.py:55-58` | `companion` 种子里每个名字（独自一人除外）都在 `stable` 里 | 6 个同伴名不可删 |
| `test_life_and_events.py:57`（附近） | `mentor` 与 `companion` 不相交 | 种子文件 |
| `test_autocompact.py:613,635,720,786` | 绑定车道系统提示含 `## Plugin Context: grantley.identity`，非绑定车道不含；绑定车道恰好 6 个 `persona_life_*` 工具 | |

**状态块/频道层（文本被钉死）**

| 文件:行 | 断言 |
|---|---|
| `test_persona_rendering.py:305-324` | 疲劳桶标签是**英文** `rested/fresh/mild fatigue/tired`（0.15、0.4、0.75 为含下界），且绝不渲染浮点 |
| `:327-337` | `recent_topics` 新→旧、最多 5 条、`", "` 连接（`g, f, e, d, c`）；空 → 空串 |
| `:362-375` | 渲染后无 `{{`，含 `兴奋`、`tired`、`剑术场训练` |
| `:378-397` | 块以 `<persona-state>` 开头、`</persona-state>` 结尾；不含 `<memory-context>` |
| `test_cache_safety.py:133-155` | `prefetch` 含 `兴奋`、`tired`、`剑术场训练`，**不含** `0.9`；`api_content` 以原文开头并含 `<memory-context>` |
| `:196-210` | 生活节奏提醒文本含 `生活节奏提醒`，且不在 stable 里 |
| `test_life_and_events.py:848-857` | 群频道提示含 owner uid 与 `群聊` |
| `:859-870` | 亚戈绑定：``QQ `2104743984` 就是亚戈`` 与 `舍友和暗恋对象` 在群与私聊都出现；无关频道不含 uid 与该句 |
| `:873-887,890-899` | 频道提示不随日期/种子/情绪变化；owner 只改 uid |
| `:902-908` | 私聊提示含 `私聊`、不含 `群主` |
| `tests/gateway/test_onebot_persona_binding.py:221-228` | 频道提示**不含** `[MSG_BREAK]`；`persona_binding` 不得有 `_BREVITY_REMINDER`（2026-08-20 删除，防止气泡规则第三次重复） |
| `tests/gateway/test_onebot_stickers.py:254-255,283-298,306-307` | 目录里 `STICKER_CATALOG` 恰好 9 条且文件都存在；菜单含 `## 可用表情` 与 `[STICKER:`；默认概率 ∈ (0, 0.25] |

**部署工具类（不钉人格文字，但钉住「改人格后如何接入老会话」）**

`test_humanizer_revision.py`、`test_argo_revision.py`：`revise(home, gateway_config, channels, rollback=…)`、`without_row_ids()` 的签名与行为；
只分支绑定群与亚戈私聊；旧会话行与历史逐项不变；`system_prompt is None` 让新会话重建提示。

**没有被钉的东西**（2026-09 起刻意移除）：整文件 sha256、固定措辞子串。当时的理由是「子串快照证明不了模型行为」
（`test_persona_rendering.py:107-108`）；行为判定改由 `gate.py` 的输出分布门禁与盲评承担（§4）。

### 1.6 静默失效清单（不会报错，但人格整段消失/失真）

1. **车道闸依赖未提交的核心改动**：`agent/system_prompt.py::_plugin_session_info` 在工作区新增了 `chat_id`/`chat_type`
   （+6 行）；HEAD 版本只给 `session_id/model/provider/platform/profile_name/cwd`。
   没有这两个键，`is_grantley_onebot_lane()` 恒 False，**身份段与声音段都不注入**，且不报错。
   P1 靠 rsync 工作树所以生效；若有人只部署 HEAD 提交，人格会整段消失。【码，已用 `git show HEAD:` 核对】
2. 段超限 → 整段丢弃，只留 warning（§1.3）。
3. 车道闸要求 `platform=onebot` 且 `chat_type ∈ {group,dm,private}` 且 `channels` 里**有且仅有一条**同 id 且 `persona: grantley` 的显式条目；
   群还要求 `group: true` 且 id 是 `g<数字>` 的规范形（`channel_binding.py:68-130`）。
   所以：**未绑定的 QQ 私聊、cron/空间作业、Telegram、CLI 都拿不到身份段与声音段**。
   线上绑定的是 5 个群（1082225370、183287894、894800697、149881991、667528618）+ 亚戈私聊 2104743984【文，B3 示例与 9-06 记录】。
   其他私聊的格兰人格只来自 `SOUL.md`（见 §2）。
4. `plugins.entries.grantley.settings.channels` 每进程只读一次，改后要重启（E0 §4.3）。
5. 身份段第 `说话像在聊天` 末尾写着「具体形状与示范见声音段」；声音段开关默认 `false`，关掉时这是悬空引用。
6. 疲劳桶、`neutral`、`at_academy` 等英文/机器值会直接出现在 sidecar 里（§2.5）。

---

## 2. 系统提示的全貌：一次请求里格兰实际拿到什么

### 2.1 组装顺序（`agent/system_prompt.py::build_system_prompt_parts`，QQ 车道、无编码工作区）

系统消息 = `stable` + `context` + `volatile` + 请求时追加的 `ephemeral`；之后是消息历史，最后一条用户消息带 sidecar。

| # | 层 | 内容要点 | 字符数 | 谁生成 | 是否缓存冻结 |
|---|---|---|---:|---|---|
| 0 | **SOUL.md**（身份槽 #1；没有则回落 `DEFAULT_AGENT_IDENTITY`） | 切换窗口 D42 定稿的 3 句第一人称自述：「我是格兰特利·贝尔，弗罗汀人，骑士学院的学生，虎兽人，186公分…读不进书、也坐不住。脾气直，看不惯的事说不干就不干；但认定的朋友，我掏心掏肺地护着。」（`E-CUTOVER-RUNBOOK.md:34-39`，当时 404 B） | ≈135 字 | 手工文件 `$HERMES_HOME/SOUL.md` | 是。**P1 现状【未核实】**——P1 的配置/密钥是从 corlinman-prod 的 `/opt/hermes/data` 恢复的，SOUL.md 可能随之而来也可能没有；没有则第一段是 513 字符的英文「You are Hermes Agent … created by Nous Research」 |
| 1 | `HERMES_AGENT_HELP_GUIDANCE` | 英文：「You run on Hermes Agent (by Nous Research)…」 | 560 | 核心 | 是 |
| 2 | 任务完成/并行调用/steer/记忆/会话检索/技能 等通用英文指引 | `TASK_COMPLETION_GUIDANCE` 769、`PARALLEL_TOOL_CALL_GUIDANCE` 618、`STEER_CHANNEL_NOTE` 1091、`MEMORY_GUIDANCE` 1426、`SESSION_SEARCH_GUIDANCE` 186、`SKILLS_GUIDANCE` 906（按工具是否加载） | ≈5000 | 核心 | 是 |
| 3 | 「tool-use enforcement」 | 只对模型名含 `gpt/codex/gemini/gemma/grok/glm/qwen/deepseek` 的注入（`prompt_builder.TOOL_USE_ENFORCEMENT_MODELS`）。**`claude-opus-5-5` 不命中**，而 `gemini-3.8-flash-high` 命中（还会再带 860 字的 Google 操作指引）。换模型后这一层消失，历史评测都是在有这一层的条件下做的 | 0 或 824+860 | 核心 | 是 |
| 4 | **`PLATFORM_HINT`**（OneBot，插件注册，被 `agent.platform_hints.onebot` 覆盖则以覆盖为准【未核实】） | 见 §2.3 冲突 C1 | 797 | `adapter.py:5222` | 是 |
| 5 | 技能索引 | 所有可用技能（含从 corlinman-prod 恢复的 ≈数百个文件） | 未知，很可能上万字符 | | 是（放 volatile 前部） |
| 6 | MEMORY.md / USER.md（内置记忆） | 内容【未核实】；与 grantley 状态库无关 | 未知 | | 是 |
| 7 | **`## Plugin Context: grantley.identity`** | `grantley.md` 的 stable 半边：我是谁 / 认识眼前的人 / 脾气和反应 / 实时状态指针 / 说话像在聊天 / 查证、办事与生活的边界 | 3072 | `__init__.py` + `persona.py` | 是 |
| 8 | **`## Plugin Context: grantley.voice`** | `voice.md`：三条实测目标 / 示范（14 组） / 不知道的时候用他的嘴说（3 组） / 别装人味 / 发出前一问 | 1774 | | 是 |
| 9 | `Conversation started: <日期>`、`Model: claude-opus-5-5`、`Provider: cornna-claude`、`Platform: onebot` | 日期只到天；模型名会进系统提示 | ≈100 | | 是 |
| 10 | **ephemeral·会话上下文** | `## Current Session Context`：Source、「Multi-user session — messages are prefixed with [sender name]」、Connected Platforms、Home Channels、定时任务投递选项 | ≈600–900 | `gateway/session.py:482-745` | 请求时追加，不进冻结提示 |
| 11 | **ephemeral·`## 这个频道`** | 群：「这里是群聊。按对话自然接话，坦率友善…所有人的事实问题与执行任务都同样认真处理。」+ channel_owner 句 + 亚戈绑定句；私聊：「这里是私聊，只有你们两个人。」（亚戈私聊再加绑定句，**该句末尾仍带「群聊中依据当前消息的真实 QQ 发送者 ID…」**，在私聊里是多余的）。其他私聊只有 23 字 | 23–240 | `channel_binding.py:245-271` | 同上，日内字节稳定 |
| 12 | **ephemeral·`## 可用表情`**（p=0.18 才出现） | 9 张图的 slug + 画面 + 「用于：…」+ 「单独起一行写 `[STICKER:名字]`…一条回复最多用一张」 | 467 | `sticker.py:362` | 每轮掷骰，见 §1.4-5 |
| 13 | （可选）`channel_overrides.system_prompt` / `channel_prompts` | 配置里若有，追加在 ephemeral 后【未核实】 | | `gateway/run.py:5224-5236` | |
| 14 | 工具 schema | 含 6 个 `persona_life_*`（仅绑定车道）、`qq_send_voice` 等 | | | |
| 15 | **用户消息前缀** | `[Recent group messages]` + 最近 ≤12 行 `[昵称] 文本` + `[Current QQ sender ID: N]` + `[New message]` + 原文（`reply_context.py:140-170`；群内 5s 固定窗口把整个房间的发言收进同一轮） | | 适配器 | 不冻结 |
| 16 | **用户消息后缀（sidecar）** | `<memory-context>` + 英文 system note「…authoritative reference data — … should inform all responses」+ `<persona-state>`（381 字的实时状态块 + 可选「[生活节奏提醒 · level]」+ 「最近还留着印象的事（越靠前越鲜明）」最多 5 行带 `[0.92]` 权重） | ≈400–800 | `memory_provider.py:238-281` | 随每条历史用户消息持久化、重放 |

人格文本（7+8）= 4846 字符，估计不到整个系统提示的一半；它排在 volatile 层靠后、位于技能索引与记忆**之后**，
在 `Conversation started:` 行之前，前面是 ≈6k 字符的英文通用指引（+ 可能的 SOUL.md）。
**【未核实】** 真实占比应取 P1 `state.db` 里某个绑定会话的 `system_prompt` 实测（9-14 与 10-03 的记忆笔记都强调：离线预览格兰发言必须用真实系统提示，
把 `grantley.md` 原文当 system 走辅助通道会让 Opus 续写文档）。

### 2.2 其他会让「格兰」说话的出口（不走上面这条链）

| 出口 | 人格从哪来 | 备注 |
|---|---|---|
| 主动发言 | 同一个 `persona_binding.channel_prompt()`（`proactive.py:1112`），提示词是 `DEFAULT_PROMPT`「…发一条简短自然的群聊消息（一两句话即可）」+ 群聊记录 + `SKIP_HINT` | 另有 Sonnet 5.5 预判闸（`proactive_judge.py`，其系统提示里有一句配置里的 `proactive_judge_persona`「他是谁」，文本在 P1 `config.yaml`【未核实】）和隐式点名判定 `addressee.py`；3 个文本入口，**没有一个受本仓库测试约束** |
| QQ 空间/定时作业 | **只有 SOUL.md**（`corlinman_jobs/prompts.py:127-130` 明说「persona 来自 profile（SOUL.md / grantley memory provider）」）。cron 会话 `platform≠onebot`，车道闸挡掉身份段、声音段、状态 sidecar、persona_life 工具 | 空间说说（≤200 字）、评论回复、生图描述全部在 3 句 SOUL.md 的人格下完成；`voice.md` 的「句尾无标点」「两条气泡」对它们不成立 |
| 出站闸 | `agent_gate.py`（Sonnet/辅助模型审草稿，fail-open）→ `plugins/qzone/policy*.py`（关键词表，fail-closed） | 命中时静默替换/拦截，人格文本不知情；涉政/违法/色情/暴力/平台规避/翻墙推广 |
| 发送整形 | `send()`：去 Markdown、按 `[MSG_BREAK]` 切气泡、超 1000 字或 >3 泡 → 合并转发卡、`[LONG_REPLY]` 强制卡、`[STICKER:…]` 转图并挂最后一泡 | 人格文字里不能指望的控制：气泡 >3 的后果是卡片，不是被截断 |

### 2.3 谁覆盖谁；互相重复与冲突的规则

文本里**明文写了的优先级**只有三条：声音段「与身份段冲突时以身份段为准」（`voice.md` 第 3 行）；身份段「任务完整性、准确性及指定格式高于风格」；
sidecar「以最新的一个为准」。其余层之间没有声明优先级，实际靠位置（越靠后越近）和模型自己的调和。

| # | 冲突/重复 | 具体文本 | 影响 |
|---|---|---|---|
| **C1** | **条数默认值互相打架** | 工作区 `PLATFORM_HINT`：「For everyday chat, **usually send one concise message**; choose two or three only when the thought naturally needs separate bubbles.」vs `grantley.md`：「闲聊**多是两条**…上限三条」、`voice.md`：「闲聊多是两条…典型形状是：第一条直接接住…第二条把话抛回去」。HEAD 版 `PLATFORM_HINT` 又是第三种：「several short messages read better than one long one」（`git show HEAD:…adapter.py`）。9-14 README 已记录「通常一条」是单气泡塌缩的直接成因，而它又被写回了工作区版本（未提交，是否已上 P1【未核实】） | 现状更糟的方向不是单泡，而是**模板化的两泡**（§3.3：21/24、22/24 恰为 2 条） |
| **C2** | **声音段示范教「装保密」，身份段明令禁止** | `voice.md`：「我家的事我不爱提」「这个我不想细说[MSG_BREAK]反正现在能用，够了吧」vs `grantley.md`：「不装保密，不从未找到推出从未存在…」。humanizer 轮的 RESEARCH.md 曾写「不知道与不愿透露是两回事；不能用『秘密』『懒得说』掩盖资料缺口」 | 这是 v2→v3 为了去掉「考据者口吻」换上的示范，同时把「不知道」改写成了「不想说」 |
| **C3** | 示范里对骑术的否定倾向 | `voice.md`：「还没轮上正经露一手的机会[MSG_BREAK]真上了场再跟你吹」vs 评委 rubric 与 RESEARCH.md V07：骑术获奖线索存在但未证实，**不应肯定也不应绝对否定**。该示范在 `cases-v3` 的 case 8 输出里被逐字复用（「露一手的机会」） | 轻度；但示范是最强的复制源 |
| **C4** | sidecar 包装与内容自相矛盾 | 包装：「**authoritative reference data — … should inform all responses**」（`memory_manager.py:356-360`，核心代码，非 grantley）vs `<persona-state>` 首句：「**只在话题相关时采用**最新状态，空字段保持未知」 | 包装语逼模型把状态织进每一句 |
| **C5** | sidecar 泄漏机器值 | `精神状态：mild fatigue`（英文桶标签，被测试钉死）、`状态：at_academy`、默认 `心情：neutral`、`[0.92]` 事件权重、`[生活节奏提醒 · high] …先调用 persona_life_event_seed…` 的祈使句（会把模型引向工具调用而不是接话）。记忆笔记 `project_xiaohuihui_persona`：模型会把摘要/分布字面照抄 | 改标签要同步 `test_persona_rendering.py:305-324,374` 与 `test_cache_safety.py:147` |
| C6 | 同一规则写了 2–3 遍 | 「所有人同样认真」：身份段「认识眼前的人」、「查证、办事…」+ 频道层群聊句；「channel_owner 不等于亚戈」：身份段 + 频道层；「QQ 2104743984 就是亚戈」：身份段 + 频道层「固定人物绑定」 | 浪费预算；改一处漏另一处就冲突（9-14 的结论：同一规则写第三遍没有理由生效） |
| C7 | 一份声音段，两种语域 | 目标按**绑定群**真人量（句尾无标点 90.9%、语气词 1.138/100 字、中位 8 字）；FINGERPRINT §A3 自己指出「对格兰说话的真人」只有 49.4% 无尾标点，「目标区间必须按 group/dm 分层」。声音段不分群/私聊，亚戈私聊车道从未单独调过（「该车道本轮不动」，亚戈只占语料 21/1928 轮） | |
| C8 | SOUL.md 与身份段的口吻 | SOUL.md：「看不惯的事说不干就不干」「读不进书、也坐不住」；身份段：「不因讨厌学习或对方不是群主而少做、推脱」。若 P1 仍是这版 SOUL.md，第一段话就在暗示拒绝/犟 | 【未核实】先读 P1 的 SOUL.md |
| C9 | 群聊里没有「怎么称呼多人」的指引 | 身份段「称对方『你』」；群里是多人发言，用户消息里是 `[昵称] 文本`。无规则管 @ 谁、是否叫名字、转述 | 缺口而非冲突 |
| C10 | 表情包与气泡 | 声音段/身份段对表情只字不提；菜单说「单独起一行写 `[STICKER:名字]`」，适配器把它挂在最后一泡不占条数。xiaohuihui 文档里写的「表情占掉 1–3 条之一」在格兰这里**不成立** | 缺口 |
| C11 | 悬空引用 | 身份段指向「声音段」；开关默认 off | 见 §1.6-5 |

---

## 3. 历次迭代的结论

### 3.1 时间线

| 日期 | 轮次（目录/提交） | 改了什么 | 为什么 | 验证与数字 |
|---|---|---|---|---|
| 08-18 | **C1 移植** `674b9b7b31` | corlinman 的 3601 字符 openclaw 视角文档逐字移植；按 volatile 标题切 stable/volatile；sidecar 走 `prefetch`；D12–D20 若干机制修复（生活节拍永远出不了门的死分支等） | 缓存违规：占位符在系统提示里 = 每轮缓存失效 | 单测；`C1-grantley-port-notes.md`。原文有「本大爷」「嘴硬×心软二重账本」「隐形学霸装傻」「骑士底线」「H9 暗恋掩饰」，A2 里有逐字原文 |
| 08-18 | **C5/C6 官设调研与订正** `37f0ed7483` `67d8901905` | 删隐形学霸（与官设「嫌い：勉強」「裏表のない実直」相反）、删「本大爷」（官方只有「オレ」）、骑士道德锚降级、加官方档案；SOUL.md 定稿 3 句 | 调研只有 **4 条一手台词**（3 日 + 1 英译） | `C5-grantley-canon-research.md`；「官设矛盾」三处，之后又被 EVIDENCE.md 订正 7 条（讨厌学习 ≠ 低智力；「未核实」≠「官方不存在」；图片月份路径） |
| 08-20 | **task-first** `0b9863d9da` `f54389cd89` `ebd291613f` | 新增「任务优先级（高于表达风格）」；**删掉** `persona_binding._BREVITY_REMINDER`（频道层里的「1–3 条」提醒）；当时加了整文件 sha256 与子串钉子（之后在 9-06/9-07 移除） | 日常闲聊持续超量气泡；任务被角色扮演稀释 | 测试；这是气泡规则第一次被迫从三处缩到两处 |
| 09-06 | **KC2 二代校准** `docs/personas/grantley-evaluation` + `grantley-EVIDENCE.md` | 弗罗汀=国家；戴德里克/西利欧是教官；亚戈绑定 QQ 2104743984（同寝+暗恋但未交往，运行选择）；稳定段 3072 字符；频道层去掉日程注入；生活种子补全非空 | 旧版把 Frontail 当城市、把未核实说成「官方从未写过」 | 24 例单轮、`gemini-3.7-flash-tiered`、每例每版 3 轮共 144 次响应：**22 通过 / 2 部分通过**（第 13、22 项范围说得过满）；主观评分由实施者给出，**开发集同时是验收集** |
| 09-06 | **长会话** `grantley-longlife` | 每群一个共享会话；自动压缩 256000 token 触发、保留尾部 32000；图片原生；`session_reset=none`；导入 69 个旧会话/1331 条消息 | 用户选择 | 179 项测试；压缩后真实回答两次遇 429，**未端到端验收**；后续 10-03 又改成阈值 128000 |
| 09-07 | **humanizer** `grantley-humanizer-20260907` | 重写成现在的骨架（我是谁/认识眼前的人/脾气和反应/…）；合并重复禁令；加具体情境反应；加「讲解方向核对」；删旧「心智模型」等 | 去 AI 腔；7 个 canon 缺口 | `gpt-6-astra`、192 条回复（回归 48 + 开发 72 + 留出 72）。**留出 F01–F06：新版 4 胜 2 平**，表达分（辨识度+自然，满分 8，两种展示顺序平均）：F01 7→8、F02 6→8、F03 6→7.5、F04 6.5→6.5、F05 8→8、F06 6→6.5。24 回归通过；二分查找每版 1260 组合全对。评委=同一模型，非独立人类；稳定段 3131 字符；sha256 `279adcee…` |
| 09-07 | 修复 `g<数字>` 群号 | `is_grantley_onebot_lane` 解码规范群号 | 验收只用了纯数字群号，真实群会话**从未加载过人格**（直到 13:54 UTC） | 278 测试；教训：真实适配器产生的 id 形态要进测试 |
| 09-14 | **语料蒸馏** `grantley-corpus-20260914` | 新增 `voice.md`（示范层）；`grantley.md` −105 字符：禁令靶子换成实测项（`啧 13.4%`、`嗯 15.5%`、`查了一下 8.3%`、`省得你 5.1%`；`随时叫我` 实测 0 次是想象出来的）、「闲聊通常一条」→「多是两条…上限三条」、7 条散文示意迁出为示范；`persona.py` 加 `load_voice_document`；`__init__.py` 抽 `_register_lane_section` | 指纹显示 humanizer 轮把「聊天质感」清掉了：句尾无标点 0% vs 真人 90.9%；语气词 0.343 vs 1.138（0.18×）；单气泡塌缩 | 三臂（生产同款模型）+ 门禁 + 双序盲评；**候选 vs 已验收 34 胜 10 负 4 平**，voice+natural 6.10→**7.50**（facts 3.96=3.96，task 3.85→3.94），双序一致 20/24。部署到 p2 并轮转 83 个会话 |
| 09-19 | p2 整机下线 → **P1** | 原生 systemd 重建；`inject_voice_section: true`；渲染 `grantley.identity 3072` + `grantley.voice 1774` | | 配置/密钥取自 corlinman-prod；9-03 后 p2 上新增的记忆/会话永久丢失 |
| 10-03 | **模型换 `claude-opus-5-5` medium**（`providers.cornna-claude`，`anthropic_messages`）；每群一个共享窗口 + 固定 5s 房间窗口；群图回看；主动发言预判闸；隐式点名；表情包识别 | | | **人格文本与评测均未随模型重做**。此前所有分布数字（门禁阈值、34–10–4、4–2）都来自 gemini-3.7/3.8-flash 与 gpt-6-astra |

### 3.2 已证实的效果（数字）

- 词汇层 AI 腔：`本大爷` 25.1%→0%，句首`哈？` 23.8%→0.8%，口语词占比 97.5%（真人 95.9%）——humanizer 轮治好了，9-14 不重做。
- 质感三轴（stimuli，24 条真实短闲聊，`gemini-3.8-flash-high`，温度 0.4）：

| 臂 | 整条 p50/p90 | 每泡 p50 | ≥2 泡 | 句号收尾 | 无尾标点 | 语气词/100 字 |
|---|---|---|---|---|---|---|
| 已验收（9-07 版） | 21/122 | 20 | 4% | 75% | 0% | 0.385 |
| 候选 v3（现网） | 32/77 | 10 | 96% | 4% | 88% | 1.061 |
| 真人·绑定群 183287894 | 8/21 | 8 | — | 0.6% | **90.9%** | 1.138 |
| 真人·对格兰说话 | 11/30 | 11 | — | 40% | 49.4% | 1.92 |

- v1→v2→v3（出戏=提问未引入的元语言）：v1 9 处/5 条、语气词 1.204；v2 加禁用词表 6 处/4 条、语气词 **0.862**；v3 改成示范为主 **1 处/1 条**、语气词 **1.237**。

### 3.3 已被证明无效或有副作用的写法

1. **散文规则管不住气泡形状。** 同一条气泡规则在身份段、`PLATFORM_HINT`（以及曾经的频道层）里各写一遍，留出集仍 36/36 单气泡
   （注意该批 96 条来自 `gpt-6-astra`，模型是混淆项）。第三遍散文没有理由生效 → 改示范。
2. **「通常一条」是默认值，模型照做**；改成「多是两条」后**又固化成两条**：【算】`stimuli-v3` 候选臂 21/24 恰为 2 条、`cases-v3` 22/24 恰为 2 条
   （均 1–3 条的极窄带）。真人在绑定群 183287894 的同发言人连发（间隔 ≤30s，4583 条）是：**1 条 63%、2 条 23%、3 条 8%、4 条 3%、5+ 3%**【算，本次新算】。
   即：当前候选把「两条」做成了模板，与身份段自己写的「不固定轮换」冲突；而 `gate.py` 只设下限（≥2 泡占比不许再降），**看不见这个问题**。
3. **禁用词表让它闭嘴不让它说话**（v2）：出戏只降 9→6，语气词降 28%。示范（v3）才有效。
4. **禁令靶子凭想象立**：`随时叫我` 命中 0/1695。靶子必须由语料排序给出。
5. **被模型字面照抄的东西**：
   - 表情摘要（`[emm]` `[难过]`）：线上真发过 `&#91;emm&#93;`（xiaohuihui 经验）；
   - 分布表（「4–6 条 17%」）：模型照分布发而不是照规则发；
   - 示范句：【算】`cases-v3` 里 3/24 条含 ≥6 字的示范片段，其中 case 20 几乎整句复用「晚点就晚点，不催你」，case 8 复用「露一手的机会」，case 3 复用「从训练场回来」（该句同时在固定状态里，M5 窗口因此从 8 调到 10 字）；
   - 表情菜单的「用于：…」词组、`voice.md` 里 `……` 的位置。
6. **示范与评测用例同源（泄漏）**：14 组示范里至少 8 组镜像了 24 个开发用例/D·H·F 场景（胸口疤→case 14、E.P. 觉醒→13、家里人/几岁→14/15、骑术奖→8、二分查找→23、撤回消息→24、争执认错→20、说我坐不住→D 集）。
   README 自己承认 24 例是开发集且 v1→v3 就在其上迭代，34–10–4 不是独立验证；示范同源进一步抬高了这个数。
7. **分数可以靠编造换来**：`p2_current`（C6 版）voice+nat 6.83 > 候选 6.50，但 key_errors 31 vs 7，facts 2.77 vs 4.00——「生动是编出来的」。只按 voice+natural 判等于选一个会编官设的人格；
   所以任何新蒸馏必须把 facts / key_errors 作为否决项，而不是并列项。
8. **盲评看不见质感**：`grade.py`/`judge.py` rubric 明令「`[MSG_BREAK]` 不据此扣自然度」，评委从未被要求看标点或语气词分布——「留出 4 胜 2 平」与「质感塌缩」可同时成立。
9. **门禁自己的三次误判**（均为「禁止某表面特征却没问这个场合是否本来就该出现它」）：M4 把 `[LONG_REPLY]` 里的 Markdown 判违规；M5 窗口 8→10；M9 元语言词不分提问（用户问「是官设吗」时答话里有「官设」是对的）。
10. **工程陷阱**（与文本无关但会让人格「没生效」）：同名多副本；`import plugins.grantley` 与 `load_memory_provider` 路由不同；老会话冻结；`g<数字>` 群号；
    线上 adapter 整份覆盖会掉线（「线上只能外科补丁」，2026-08-28 笔记）；聊天里让格兰改配置/源码会造出僵尸网关（`feedback_grantly_self_edit_risk`）。

### 3.4 已知诚实限定

一手台词只有 4 条，任何方法都做不出「官方台词级复刻」；老语料（1928 轮，`mk_observations.grantley.ndjson`）的「辨识度」几乎全由 tic 承担
（`本大爷/啧/查了一/省得你` 双基线交集），去 tic 后没有可继承的正向词汇指纹，所以正向声音只能来自**结构 + 示范**；
老语料是旧人格的**输出**，不是格兰的「真话」；亚戈在其中只有 21 轮（1.1%），不能据此推亚戈车道语气；
评委=被评模型；单轮采样；QQ 自 2026-09-07 起一度断连，新人格在真实多轮聊天里的分布 9-14 时仍无样本，且至今没有 Opus 下的真实样本。

---

## 4. 评测工具

### 4.1 目录与用途

| 路径 | 作用 | 输入 → 输出 | 依赖 |
|---|---|---|---|
| `grantley-corpus-20260914/fingerprint.py` | 纯测量模块 + 语料指纹报告（`report`/`verify`）。核心函数 `measure()` 给出 p50/p90、气泡数分布、句尾标点熵、语气词/100 字、`[LONG_REPLY]` 占比、禁用式、签名词、口语词占比等 | 读 `.migration-export/json/mk_observations.grantley.ndjson`（1928 行）与 `.migration-export/sqlite/qq_group_history.sqlite`（本机存在；**gitignored**）→ `fingerprint.json` | 仅标准库 |
| `…/sample.py` | 采样器。`--mode stimuli`（从真实 user 侧按种子 20260914 抽 3–20 字短闲聊，**只落 sha256**）或 `--mode cases`（读 `grantley-evaluation/cases.json` 24 例，可 `--keep-messages`）；三臂 `p2_current`/`accepted`/`candidate`（`arms()` 读 `before/` 与当前工作树）；`--arms`、`--replicates`、`--n` | → `*.jsonl` + `.meta.json`（臂的 stable sha256、刺激集 sha256） | `plugins.grantley`、本仓库 venv、SSH |
| `…/remote_sampler.py` | 宿主侧单轮采样器：读 `config.yaml`+`.env`，**OpenAI 兼容** `POST {base_url}/chat/completions`，Bearer；4 线程，失败重试 1 次；输出 schema 与 `remote_runner.py` 一致 | stdin JSON → stdout JSONL | 宿主上的 `python3` + **PyYAML**；凭据留在宿主 |
| `…/gate.py` | 机械门禁（不进 pytest）。二级绝对门：M4 禁用式=0、M5 示范逐字泄漏=0（≥10 字连续子串）、M3 长/结构化回复必须带 `[LONG_REPLY]`；一级单向棘轮：p90≤1.5×基线、≥2泡占比不许降 15 点、语气词不许再低、句号收尾率不许升 5 点；三级只报告（人类锚点、M6 追问率、M7 签名词） | `--samples x.jsonl [--variant] --thresholds thresholds-p2.json`（或 `--baseline` 现算、`--freeze` 冻结） | 读 `voice.md` 抽示范；退出码 0/1 |
| `…/judge.py` | 匿名双序成对盲评。**复用** `grantley-humanizer-20260907/grade.py` 的 rubric（facts/voice/natural/task 各 0–4，`winner` 按 voice+natural，`key_errors` 单列），带 surface-aware 补充（不据 `[MSG_BREAK]` 扣分）；每场景 × 2 顺序 | `--samples cases-x.jsonl --left accepted --right candidate --output judge-x.jsonl` → `.mapping.json` + 汇总 | 经 SSH 复用 `remote_sampler.py` |
| `…/compare.py` | 多臂并列表（含人类锚点） | 只读 jsonl | |
| `…/thresholds.json` / `thresholds-p2.json` | 冻结的一级阈值：前者来自混合模型 120 条（p90 132、≥2泡 0.2、语气词 0.343、句号收尾 0.9）；后者来自 `accepted` 臂同模型 24 条（p90 122、≥2泡 0.0417、语气词 0.385、句号收尾 0.75） | | **阈值是对「旧人格 × gemini」的相对量，换模型/换基线必须重冻** |
| `…/baseline-accepted.jsonl` 120 / `probe-old-corpus.jsonl` 1697（gitignored）/ `before/*` | 基线与回滚素材（`before/NOTE.md`：`grantley.md.p2-deployed` 文件名误导，实为 8 月 C6 陈旧副本 `22ba6a36…`） | | |
| `grantley-evaluation/{run.py,remote_runner.py,cases.json,baseline.json}` | 09-06 的配对评测：24 例单轮；**`remote_runner.py` 支持 `turns` 多轮续写**，但 `import dotenv` | → `samples/final-samples/reviewed-samples.jsonl` | 宿主需 python-dotenv、PyYAML |
| `grantley-humanizer-20260907/{run.py,grade.py,summarize.py,check_generated_code.py,real_tool_smoke.py,scenarios.json,final-scenarios.json,…}` | 09-07 验收：D/H/F 场景两轮对话 × 3 重复；回归 R01–R24；`grade.py` 3 重复一起双序评；`check_generated_code.py` 真跑生成的代码；`real_tool_smoke.py` 真 AIAgent + 文件/终端工具；`summarize.py` 生成 `acceptance.json`（含 persona sha256） | | 经 `ssh cornna-cn` + `docker exec hermes-cloud`（**已不存在**） |
| `grantley-humanizer-20260907/{deploy.py,prepare_deployment.py,maintenance.py,post_verify.py,revise_sessions.py}` 与 `grantley-longlife/*`、`grantley-deployment/*` | **一次性**部署/迁移脚本，README 明说不要当常规更新工具重复运行 | | 其中 `revise_sessions.py` 被 pytest 加载 |
| 工作区草稿 `scratchpad/distill/harness.py`（本会话另一线程创建，本报告未审其代码） | 在 P1 上以 hermes 用户重放真实 onebot 轮次，用**会话真实系统提示**换 `grantley.identity/voice` 两段，再盲评+量质感 | | 方向与 §3.3 的教训一致 |

### 4.2 用例集规模与覆盖

| 集合 | 条数 | 覆盖 | 缺口 |
|---|---:|---|---|
| `grantley-evaluation/cases.json`（= 回归 R01–R24） | 24 单轮 | 档案(1)、年级(2)、室友(3)、分支(4)、异译(5)、出身(6)、兴趣(7)、骑马(8)、学习≠低智力(9)、奥斯卡(10)、教官(11)、E.P.(12–13)、家世疤痕(14)、年龄声优(15)、朋友求助(16)、群主示好/越界(17–18，`speaker=owner`，**不是**真实数字 ID)、普通日常(19)、争执(20)、日常溯源(21)、旧记忆冲突(22)、专业输出(23)、现实执行(24) | 约 15/24 是「事实/边界陷阱」，只有 16/19/20 是真正的闲聊；无多轮、无群内多人 |
| `scenarios.json`（D01–D06 开发 + H01–H06 留出，**H 集已退役**） | 12，各 2 轮 | 兴奋后收住、调侃与边界、低落陪聊、误会后道歉、喜欢与分寸、陌生人空状态；H：生活笑点、挑衅转话题、乡下梗、玩笑转正事、冒名、官设缺口后恢复 | 其中 `speaker=2104743984` 的 9/18（D·H·F 合计）——**亚戈车道只在这里被测过** |
| `final-scenarios.json`（F01–F06，**只用过一次**） | 6，各 2 轮 | 生活玩笑、较劲后散步、口音边界、排错、冒名 Argo、未核实经历 | 同上 |
| `stimuli`（`sample.py --mode stimuli`） | 24，种子 20260914 | 真实 user 侧 3–20 字短闲聊，**文本不落盘**（只有 sha256，集合 sha256 `6af4ca6a…`）；每次需要从 `.migration-export` 重新生成 | 全是旧人格时代的陌生人对话；无图片、无引用、无群内多人上下文、无主动发言 |
| `grantley-corpus-20260914` 三臂 | stimuli 72 + cases 72 + judge 4×48 | | |
| `fingerprint` 语料 | `mk_observations` 1928 行（human_chat 1699 / proactive 148 / qzone 60 / unknown 21，69 个对话者；亚戈 21 轮；单一陌生人 705 轮）；群 183287894 4583 条/39 人（2026-08-15→08-18 共 3 天）；群 980927602 48066 条/723 人 | | 时间窗口很短；格兰「真人」基准其实是**群友的**语域 |
| **从未覆盖** | | 群里多人连发的 5s 房间窗口、`[Recent group messages]` 前缀、`[Current QQ sender ID: N]` 真实格式（`sample.py:121,143` 用的是「当前发送者 id=…。」）、`PLATFORM_HINT`、SOUL.md、技能索引/记忆、表情菜单、sidecar 的 `[0.92]`/节奏提醒、压缩之后、长会话漂移、主动发言/隐式点名、空间说说、图片/表情包输入 | |

**保真度缺口**：`sample.py` 的系统消息 = `stable + voice + channel`，没有 SOUL.md、通用英文指引、`PLATFORM_HINT`、技能、工具、会话上下文，也没有生产的用户消息前缀与 `<memory-context>` 包装语。
所以 C1（`PLATFORM_HINT` 与声音段的条数冲突）这类跨层问题**在现有工具里根本测不到**。

### 4.3 要在 P1（172.245.54.136，原生 systemd，Opus 5.5）上复用，至少要改的地方

事实：`/opt/hermes/{repo,venv,data}`；`HERMES_HOME=/opt/hermes/data`（与脚本默认相同）；`hermes` 用户 uid 996；
登录 `ssh -i ~/.ssh/id_cornna_p3 -J cornna-legacy-bastion root@172.245.54.136`（`~/.ssh/config` 里的 `cornna-p1` 是 `cornna` 用户，sudo 要密码）；
连接过多会被拒，用 ControlMaster（`ControlPath /tmp/cm-p1-%C`）；整机 1973 MB，hermes 是次要租户（`MemoryMax=768M`），采样器要保持标准库轻量；
模型 `claude-opus-5-5`，provider `cornna-claude`（`base_url https://api.cornna.xyz`，`api_mode anthropic_messages`，`key_env: CORNNA_API_KEY`，x-api-key 鉴权），
effort 由 `agent.reasoning_overrides: {claude-opus-5-5: medium}` 决定；`providers.cornna`（OpenAI 兼容 `/v1`，`gemini-3.8-flash-high`）仍在，给 cron 用。【文，project_hermes_deployment / model 笔记】

| 位置 | 现在 | 需要改成 |
|---|---|---|
| `sample.py:39` `SSH_HOST`、`judge.py` `--ssh-host` 默认 | `root@149.118.61.165`（p2，已下线） | P1 的跳板写法（`ssh` 需要 `-J`/`-i`；脚本只传一个 host 参数 → 需 `~/.ssh/config` 别名或加 `--ssh-arg`） |
| `sample.py:185`、`judge.py:97` | `python3 -c <remote_sampler.py>` | `/opt/hermes/venv/bin/python -c`（宿主系统 python3 是否有 PyYAML【未核实】；venv 里一定有） |
| `remote_sampler.py:37` | `provider_config.get("api_key_env") or f"{PROVIDER}_API_KEY"` | P1 配置里字段是 **`key_env`**；沿用旧代码会回落成 `CORNNA-CLAUDE_API_KEY` → `KeyError` |
| `remote_sampler.py:39,56-73` | OpenAI 兼容 `/chat/completions`，`Authorization: Bearer`，读 `choices[0].message.content`、`finish_reason=="length"` | Anthropic Messages：`POST {base_url}/v1/messages`，`x-api-key` + `anthropic-version`，`system` 单独字段，读 `content[].text`，`stop_reason=="max_tokens"`；并复刻线上思考/温度设置（带 thinking 时温度不能自由设）。更稳的是**直接用 hermes 自己的 provider/transport**（`hermes_cli.runtime_provider`、`agent` 的 anthropic 适配）以保证与线上同一条线路 |
| `remote_sampler.py` | 只做**单轮**（无 `turns`） | 多轮能力在 `grantley-evaluation/remote_runner.py:77-93`（每步续上该臂自己的上一条回复）；合并两者 |
| `judge.py:89` | `expected_model="gemini-3.8-flash-high"` | 若不改，守卫会因 `model.default=claude-opus-5-5` 直接 `SystemExit("Configured model changed…")`。建议评委用**不同家族**的模型（`providers.cornna` + `gemini-3.8-flash-high` 或辅助任务的 Sonnet 5.5），不要再「评委=被评模型」；需要给 sampler 加 `--provider/--model` 覆盖（它现在固定读 `model.default`） |
| `sample.py:79-87` `FIXED_STATE`、`:121,143` 用户消息格式 | 英文/机器值照旧；「当前发送者 id=…。」 | 与生产逐字一致：`[Recent group messages]…[Current QQ sender ID: N]\n\n[New message]\n…` + `<memory-context>` 包装语；或干脆不手拼，改用**会话真实系统提示 + 换段**（见 `harness.py` 方向） |
| `sample.py:56-76` `arms()` | 写死三臂与 `before/` 文件 | 新轮的臂：现网（HEAD 的 `grantley.md`+`voice.md`）、草稿 A、草稿 B；`compare.py` 的臂名排序也写死 |
| `thresholds*.json`、`gate.py` 里的 print 锚点、`compare.py` `ANCHORS` | 旧人格×gemini 的相对量 | 在 Opus 下重采「现网」臂后重冻；并**补上限**（见 §5-2）|
| `stimuli()` | 从 `.migration-export` 旧语料抽样 | 增加来源：P1 `state.db` 里 10-03 之后真实 Opus 时代的轮次（只读、`mode=ro`；刺激文本不落盘）；本机还有 Mac QQ 全量导出可作更长窗口语料【文，reference_mac_qq_corpus_extract】 |
| `judge.py` | 对 stimuli 臂没有题面（`cases.get(cid, {"id": cid})`） | stimuli 臂也要把原话带给评委（但原话属第三方文本，只在运行时传，不落盘） |
| 运行环境 | 本机 `.venv`（Python 3.13，已重建带 `.[dev,anthropic]`）；pytest 必须走 `scripts/run_tests.sh` | `fingerprint.py verify` 依赖 `.migration-export/`，其 WAL 文件我读取时 `-shm` mtime 被刷新（内容无变化） |

---

## 5. 开放问题与建议（按优先级）

1. **先定评测基座，再动文字。** 所有历史数字都是在 gemini-3.7/3.8-flash 或 gpt-6-astra 上、且系统消息手拼的条件下得到的；现网已是 Opus 5.5 medium，
   且 Opus 不命中 tool-use-enforcement，通用指引层也变了。**第一步是在现网条件下重采「现网」臂**（真实系统提示换段 + 真实用户消息格式 + 真实模型/思考设置），
   看 §3.3 里的各个病（两泡模板、句号收尾、示范复读）在 Opus 下是否还存在——Opus 对散文规则的服从度可能与 flash 完全不同，
   「散文规则管不住气泡形状」这条结论**只在旧模型上证实过**。

2. **把「条数/长度」当目标分布而不是下限。** 现在的门禁只有下限，看不见 92% 恰好两泡。绑定群真人是 1 条 63%/2 条 23%/3 条 8%/4+ 6%（【算】，需要再分群/私聊两层）；
   `gate.py` 应加上限与熵（条数分布与人类分布的距离，每泡字数分布），并按 group / dm 分层。同时要决定一个**唯一的条数规则来源**：
   `PLATFORM_HINT`（C1）与声音段不能再各说各话；9-14 的经验是「第三遍散文无效」，应只保留示范 + 一个数字化约束的位置。

3. **修掉声音段里自相矛盾的示范**（C2/C3）：「不爱提/不想细说」要改成真正的「不知道」口吻或承认没信息；骑术示范不得带否定倾向；
   示范与评测用例**去同源**（至少换一批场景），并在门禁里保留 M5。示范句不要含可被整句复用的 `……`/固定收尾（case 20 已出现近乎逐字复用）。

4. **重做一个全新的、只用一次的留出集，并覆盖真实使用面**：来自 P1 近 3 天 Opus 时代真实轮次（含群内多人 5s 窗口、引用、表情包/图片、亚戈私聊、非绑定私聊、主动发言、压缩后）。
   现有 24 例/F 集已被迭代污染；亚戈车道 9/18 场景用的是 `speaker=2104743984` 但从未单独看分布。facts/key_errors 做**否决项**（p2_current 的教训）。

5. **理清 SOUL.md 与「谁拿得到人格」**（先去 P1 读 `/opt/hermes/data/SOUL.md` 和 `config.yaml` 的 `platform_hints`/`channel_overrides`/`proactive_judge_persona`）：
   - 非绑定私聊、QQ 空间说说/评论/生图作业、主动发言判定只靠 SOUL.md 的 3 句话；是否需要把一份「小号身份 + 空间语域」放到可被 cron 车道读到的地方（身份段目前被车道闸挡在 onebot 之外）；
   - SOUL.md 与身份段的口吻要对齐（C8），或让 SOUL.md 退化为中性。

6. **sidecar 卫生**（不涉及缓存，但每轮都在读）：把英文桶标签/机器值/权重/祈使句节奏提醒改成自然中文或直接隐藏；
   处理 C4（核心包装语「authoritative … should inform all responses」与「只在话题相关时采用」的矛盾）——只能在 `<persona-state>` 内部写更强的措辞，核心不动。
   改桶标签必须同步 `test_persona_rendering.py:305-324,374`、`test_cache_safety.py:147`。每日一个全局节拍（8 个场景种子，不分时段、所有群和私聊共用同一个状态）也与「晚上说在食堂」这类穿帮同源，值得评估是否把活动字段改成可空。

7. **预算规划**：身份段只剩 928 字符、声音段 2226、聚合 2921（真实口径）。想加「真人化」内容要先回答：往哪里放。
   选项：(a) 压缩身份段里与频道层重复的三处（C6，约 300–400 字符可回收）；(b) 把「怎么说」整体改为更少、更不可复读的示范；(c) 放进 sidecar（每轮重放，成本 = 字数×轮数）；(d) 提升核心常量（违背「窄核心」，不建议）。
   同时把测试的聚合口径改为含渲染开销，并加运行期自检（journald 里两段都出现）。

8. **把 PLATFORM_HINT 与未提交改动落地**：HEAD 与工作区的 `PLATFORM_HINT` 不同；`agent/system_prompt.py` 的 `chat_id/chat_type` 改动是人格注入的**前置条件**却未提交（§1.6-1）。
   蒸馏结果上线前先把这两处提交并确认 P1 上跑的就是这版（P1 靠 rsync 工作树，且线上 adapter 过去因整份覆盖掉过线）。

9. **上线路径**：资产更新只影响新会话；绑定群 10-03 起是新的共享会话，但**亚戈私聊与其他老私聊的冻结提示可能仍是 9-14 或更早的版本**【未核实，用「旧版独有标记」逐会话对照，9-14 用过这个办法】。
   用 `revise_sessions.py`（保留历史、可回滚）或 `reset_session()`，停网关时执行；部署后用 `load_memory_provider` 路由而不是 `import plugins.grantley` 验证；同时核对 `$HERMES_HOME/plugins/grantley/` 与 repo 副本一致。

10. **目标到底是谁的嘴。** 格兰没有一手台词语料（4 条），老语料是旧人格的输出，语域基准取的是**绑定群群友**的语域。
    这意味着「像真人」≠「像格兰」：正向声音只能来自结构 + 示范 + 官设性格（直爽、好动、重朋友、嘴欠、粗鲁里带体贴）。重新蒸馏前要明确：是追求「像一个活跃的群友」还是「像游戏里的格兰」，
    以及亚戈私聊车道是否需要和群聊不同的语域（句尾标点、条数）。xiaohuihui 的做法可借鉴的是**结构**而不是内容：「观察档案」+ 表达 DNA（节奏用数字：单条中位 6 字、p90 17 字）+
    有明确出处的统计、`群方言≠签名`、反模式台账、诚实边界；但 xiaohuihui 是 21.9 KB 的 SOUL.md（profile 级），格兰的身份段只有 4000 字符预算，不能照搬体量。

---

## 附：未核实项（需要在 P1 上确认才能下结论）

- `/opt/hermes/data/SOUL.md` 的内容与大小；`config.yaml` 中 `agent.platform_hints`、`channel_overrides`/`channel_prompts`、`platforms.onebot.extra` 里的 `proactive_judge_persona`/`sticker_*`/`max_bubbles_per_reply`/`bubble_overflow`、`inject_*_section`、`auxiliary.*`。
- P1 上 `adapter.py` 的 `PLATFORM_HINT` 是 HEAD 版还是工作区版；`agent/system_prompt.py` 是否含 `chat_id/chat_type`；`$HERMES_HOME/plugins/grantley/` 是否存在及其 sha256。
- 各绑定会话冻结的系统提示里是否含当前 voice 段、`grantley.md` 的哪一版。
- 内置 MEMORY.md/USER.md 的内容是否包含与人格冲突的条目；技能索引的大小。
- 表情菜单在 Anthropic 缓存下的实际命中代价。
- 非绑定私聊是否是有意不挂人格（目前设计上 fail-closed）。

## 附：本次盘点的痕迹

- 未修改仓库任何文件；唯一写入是本报告。
- 为统计「真人连发条数」，把 `.migration-export/sqlite/qq_group_history.sqlite`（gitignored）拷到 scratchpad 的临时副本上用 `immutable=1` 查询，查完已删除副本；
  在此之前我曾用 `mode=ro` 直接打开过原库两次，导致同目录 `qq_group_history.sqlite-shm` 的 mtime 刷新（内容与主库文件无变化）。
- 第三方真人文本没有被复制进本报告；引用的只有数字与我们自己写的示范句。
