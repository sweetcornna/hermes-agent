# 格兰特利语料蒸馏轮（2026-09-14）

**状态：诊断、实现、采样、机械门禁、匿名盲评全部完成（生产同款 `gemini-3.8-flash-high`）。
候选 = voice.md v3。**已于 2026-09-14 部署到 p2**（见文末「部署记录」）。**

**一句话结论**：候选对**已验收版**是决定性胜出（34–10–4，voice+nat +1.40，facts/task 不降）。
而已验收版**就是 p2 线上在跑的那一版**（见下方订正），所以这一对正是部署决策本身。
另一臂 `p2_current`（C6）事后查明取自宿主上一棵**从未被读取**的陈旧副本，
其对照结果只作存档，不构成部署选项。

本轮做的是 `docs/migration-corlinman/C1-grantley-port-notes.md` §4 自己记账、
**至今从未跑过**的那条蒸馏管线：「grantley 的约 1928 行对话历史…蒸馏管线从未跑过…
这一步没有可复用的实现，需要单独排期」。

## 结论先说

上一轮（`grantley-humanizer-20260907`）把 AI 腔标记清干净了——`本大爷` 25.1% → 0%、
`哈？` 23.8% → 0.8%、词汇层口语度已与真人齐平。**这个成果不被本轮推翻。**

但同一批数据显示它同时把**聊天质感**清掉了。三条实测轴，现验收版都比前任版本更远离
他自己那个群里的真人：

| 轴 | 真人（他的绑定群） | 前任版本 | 现验收版 |
|---|---|---|---|
| 句尾无标点率 | **90.9%** | 0.1% | **0.0%** |
| 语气词 / 100 字 | **1.138**（对他说话的人 1.92） | 0.949 | **0.343** |
| ≥2 气泡占比 | —（真人一条就是一条，但只有 8 字） | 91.5% | 20.0% |
| 每气泡字数中位 | **8** | 18 | 49 |

`z123-cloud/humanizer` 给这个形态起了名字——**「AI 假装正常人」**：句式工整、语气克制、
永远中立，读起来没毛病但没有记忆点，与满篇套话在同一维度上失败。

**为什么此前没人发现**：`grantley-humanizer-20260907/grade.py:37` 明确指示盲评评委
「`[MSG_BREAK]` 是分气泡控制符…不据此扣自然度分」，评委也从未被要求看标点或语气词分布。
所以"holdout 4 胜 2 平"与"质感塌缩"可以同时成立，不矛盾。

## 生产机已换到 p2（2026-09-14 核实）

生产机现为 **`cornna-p2` = 149.118.61.165**（aarch64，Docker：`hermes_cli.main gateway run`
+ dashboard，`/opt/hermes/repo` + `/opt/hermes/data`，有 `Dockerfile.p2`，容器起于 Sep 08/09）。

- ⚠️ **ssh config 里的 `cornna-p2-root` 走的跳板 `workbench-host`（34.146.77.27）已不可达**；
  直连 `root@149.118.61.165` 可用。`cornna` 用户在 sudo 组但 sudo 需要密码，而
  `/opt/hermes/data` 是 `drwx------ fwupd-refresh:docker`，所以采样必须以 root 身份跑。
- ⚠️ **容器外没装 `python-dotenv`**，`grantley-evaluation/remote_runner.py` 会 ImportError。
  本轮的 `remote_sampler.py` 改用 stdlib 解析 `.env`，输出 schema 不变。
- 配置（按字段白名单读取，未整份输出）：`model.default = gemini-3.8-flash-high`
  （实返 `gemini-3.8-flash`）、`model.provider = cornna`、`base_url = https://api.cornna.xyz/v1`、
  `memory.provider = grantley`、`inject_identity_section = True`、`inject_voice_section` 未配、
  六个频道白名单齐全、`platforms.onebot.enabled = True`、`prefill_messages_file` 未配。

### ⚠️ 订正：线上跑的一直是已验收版（2026-09-14 部署前查明）

本轮采样期间我判定"p2 现役是 C6 版 `22ba6a36…`"，**这是错的**。根因是宿主上有两棵同名树：

| 宿主路径 | 容器内 | 角色 |
|---|---|---|
| `/opt/hermes-cloud/{repo,data,home}` | bind mount 到 `/opt/hermes/{repo,data,home}` | **真正生效** |
| `/opt/hermes/{repo,data}` | 未挂载 | 8 月遗留的陈旧安装副本 |

`persona.py` 的 `ASSETS_DIR` 是包相对路径，容器内解析为
`/opt/hermes/repo/plugins/grantley/assets/` = 宿主 `/opt/hermes-cloud/repo/...`，
那里的 `grantley.md` 是 **`279adcee…`（已验收版，mtime Sep 7 10:38）**。
我当初读的 `/opt/hermes/data/plugins/grantley/assets/grantley.md`（`22ba6a36…`，Aug 21）
在那棵未挂载的树里，**从未被任何代码路径读取**。活进程的 INFO 审计行也印证了这一点：
`Session plugin prompt section: id=grantley.identity … chars=3130`——3130 是已验收版的长度，
不是 C6 的。

后果：`p2_current` 那一臂测的是一份**从未上线的文件**。`before/grantley.md.p2-deployed`
保留为该陈旧副本的存档，文件名有误导性但内容真实。**部署决策落回「候选 vs 已验收」这一对，
候选在该对上 34–10–4 胜出。** config.yaml 同理在 `/opt/hermes-cloud/data/`。

## 本轮改了什么

| 文件 | 改动 | 理由 |
|---|---|---|
| `plugins/grantley/assets/voice.md` | **新增**，1386 字符 | 示范层。散文规则已被证明无效——同一条气泡规则在身份段和 `adapter.py:4931-4944` 的 `PLATFORM_HINT` 里各写了一遍，holdout 仍然 36/36 单气泡。第三遍散文没有理由生效，所以改成示范形态 |
| `plugins/grantley/assets/grantley.md` | 3321 → 3216 字符（**净 −105**） | 三处最小改动，见下 |
| `plugins/grantley/persona.py` | 加 `VOICE_ASSET_PATH` / `load_voice_document()` | 声音资产没有 volatile 段，是纯缓存前缀载荷；发现占位符直接抛 `PersonaAssetError` |
| `plugins/grantley/__init__.py` | 抽出 `_register_lane_section`，注册两段 | 两段共用同一个 `is_grantley_onebot_lane` 门禁，非绑定车道两半都拿不到 |
| `plugins/grantley/plugin.yaml` | 加 `inject_voice_section`（默认 false） | 与身份开关互相独立 |
| `tests/plugins/grantley/test_persona_rendering.py` | +9 个测试 | 预算、门禁、缓存安全三件结构性的事 |

### grantley.md 的三处改动

1. **禁令表里的靶子换成实测项。** 原文点名 `哈？`／`本大爷`／`随时叫我`——其中
   **`随时叫我` 在 1695 条聊天语料里命中 0 次**，是凭想象立的。换成实测排名靠前的
   `啧`(13.4%)、`嗯`(15.5%)、`查了一下`(8.3%)、`省得你`(5.1%)。
2. **「闲聊通常一条」改掉。** 这句是单气泡塌缩的直接成因（"通常一条"是默认值，模型照做了），
   且与声音段冲突。改为「多是两条，靠每条更短还原碎感而不是靠条数，上限三条」——
   后半句沿用 `xiaohuihui.md:250-253` 已验证的写法，理由在通道（`max_bubbles_per_reply = 3`，
   超了会被折成合并转发卡）而不在人格。
3. **七条散文示意迁出为示范形态。** 实测它们的表层形态一条都没被继承。迁出后
   身份段净减 105 字符，且争执／认错／拒绝三个场景在声音段里补齐了。

**不动**：volatile 段标题（`persona.py:69` 逐字匹配，改名即 fail loud）、8 个
`{{persona.*}}` 占位符、`life_seeds/grantley.yaml`。

### 预算

单段上限 4000、聚合上限 8000（`hermes_cli/plugins.py:497-501`），**超限是整段静默丢弃，
不是截断**。预算要按**渲染后**长度算（`format_system_prompt_section` 会加段头尾），
在 p2 容器内实测：

| 段 | 原文 | 渲染后 |
|---|---|---|
| `grantley.identity` | 3073 | 3153 |
| `grantley.voice` | 1775 | 1852 |
| 聚合（含 START/END 包裹与分隔符） | — | **5081 / 8000，余 2919** |

⚠️ 改了 `grantley.md` 就使 `grantley-humanizer-20260907/acceptance.json` 的
`persona_sha256 = 279adcee…` 不再匹配；线上现为 `7847c8fa…`。原件在 `before/`
（sha256 见 `before/SHA256SUMS.txt`，身份辨析见 `before/NOTE.md`）。
**本轮不签发新的 `acceptance.json`**：24 条用例是开发集且 v1→v2→v3 就是在其上迭代的，
签一份 `passed: true` 会把开发集数字伪装成独立验收。

## 产物

| 文件 | 内容 |
|---|---|
| `FINGERPRINT.md` | 指纹报告。§0.1 用途切分、§A2 长度与气泡、§A3 句尾标点、§A4 语气词（CCL 2023 锚点）、§A5 log-odds 双基线、§A6 真实触发分布、§A7 反模式台账 |
| `RESEARCH-HUMANIZER.md` | 去 AI 味外部方法调研（H01–H07），含**明确拒绝**的三条外部规则 |
| `fingerprint.py` | 纯测量模块。只读仓库内既有文件，**不发任何 API 请求**。`verify` 比对 FINGERPRINT.md 内嵌数字 |
| `gate.py` | 机械门禁。三级纪律，见下 |
| `thresholds.json` | 冻结的一级漂移带，来源 = 全部 120 条已验收样本 |
| `baseline-accepted.jsonl` | 已验收版本的 120 条输出（模型混合 `gpt-6-astra` + `gemini-3.7-flash-tiered`） |
| `probe-old-corpus.jsonl` | 前任模型聊天切片 1697 条，用于验证门禁有牙 |
| `provenance.json` / `fingerprint.json` | 切分计数与完整测量 |
| `before/` | 改动前的三份资产 + sha256 |

## 机械门禁的三级纪律

- **二级 · 绝对门（blocking）** M4 禁用式命中 = 0、M5 示范逐字泄漏 = 0、M3 `[LONG_REPLY]`
  用对。阈值**不是人拍也不是语料量出来的**，而是人格文档自己已声明的规则——门禁只是把
  散文规格变成可执行断言。语料的贡献仅限于给出 M4 的候选词排序。
  ⚠️ **现任自己也过不了这道门**（120 条里 `句首哈？` 1 条、`闲聊Markdown标题` 4 条）。
  这记在 `thresholds.json` 的 `incumbent_tier2_violations` 里，**不因此放宽门禁**。
- **一级 · 漂移带（blocking，单向棘轮）** 阈值 = 现任在 120 条采样上的实测分布 + 容差
  （p90 不许超 1.5×、≥2 气泡占比不许再降 15 个点、语气词密度不许再低、句号收尾率不许再升）。
  它是漂移探测器，不主张审美理想，**也不可能让定义它的那一版失败**。
  ⚠️ **已知盲点**：某条轴若已触底（如 `final-dev` 的 ≥2 气泡本来就是 0%），棘轮给不出信号。
  这种轴上的改善只能从三级的"距真人多远"读，最终由盲评判。
- **三级 · 只报告（永不 blocking）** 人类锚点、对格兰说话的真人、前任模型分布并列打印。
  M7 签名词命中率**刻意不设下限**——给签名词设下限就是奖励复读，正是老语料 403 次
  「哈？」的成因（`humanizer-chinese` 的"防过度纠偏"独立支持这一条）。

### 为什么这不是退回子串快照老坑

`tests/plugins/grantley/test_persona_rendering.py:104-105` 的教训是「Substring snapshots
cannot prove how a model behaves」，被删掉的是对**提示词字节**的哈希/子串断言。区别是
结构性的：本设计断言的是**采样出的模型输出分布**，作者合法改一句话不会红，只有行为
分布变了才红。落地边界同样关键——`gate.py` **不进 pytest**；pytest 只测尺子本身
（给定人造文本，计数器返回 N）。

已验证门禁有牙：拿前任模型的 1697 条当候选 → `FAIL M4 {本大爷: 427, 句首哈？: 403,
动作旁白: 7}`。拿现任自己当候选 → 一级 PASS（棘轮不能让定义它的那一版失败）。

## 三臂采样结果（生产同款模型 `gemini-3.8-flash-high`）

三条臂唯一的变量是身份/声音资产；频道段、实时状态（固定）、刺激、温度 0.4、
max_tokens 1200 全部相同。刺激集 24 条，从真实 user 侧按固定种子（20260914）抽取
3–20 字短闲聊，**文本不落盘**（`stimuli.jsonl` 未存 messages，只留每条 sha256 与集合 sha256，
见 `stimuli.meta.json`）。

| 臂 | 整条 p50/p90 | 每泡 p50 | ≥2 泡 | 尾。 | **无尾标点** | **语气词** | 该标未标 |
|---|---|---|---|---|---|---|---|
| `p2_current`（陈旧副本 C6，非线上） | 52/108 | 22 | 96% | 83% | 0% | 1.779 | 4% |
| `accepted`（已验收＝当时线上） | 21/122 | 20 | 4% | 75% | 0% | 0.385 | 4% |
| **`candidate`（本轮）** | 32/**77** | **10** | 96% | **4%** | **88%** | **1.061** | **0%** |
| *真人·他的绑定群* | *8/21* | — | — | *0.6%* | ***91%*** | ***1.138*** | — |
| *真人·对格兰说话* | *11/30* | — | — | *40%* | *49%* | *1.92* | — |

**候选在三条实测轴上都落到人类锚点上**：无尾标点 88%（真人 91%）、语气词 1.061
（真人 1.138）、每气泡 10 字（真人 8）。整条 p90 77 是三臂最短。

实际样例（同一条刺激）：

| 臂 | 输出 |
|---|---|
| `p2_current` | `哈？你想得美。[MSG_BREAK]我看起来像很有钱的样子吗？自己下半个月的伙食费都快见底了…` |
| `accepted` | `想得美，我兜里比脸还干净，自己下顿饭钱还得算着花呢。找别人要去。` |
| `candidate` | `想得美，我兜里比脸还干净[MSG_BREAK]真要有钱我早跑去加餐了，哪轮得到你` |

### 机械门禁判定

阈值重新冻结自 **`accepted` 臂**（同模型、同刺激、n=24），比原先混模型的 120 条基线严谨：
`thresholds-p2.json`。

| 臂 | 二级绝对门 | 一级漂移带 | 结论 |
|---|---|---|---|
| **`candidate`** | PASS | PASS | **PASS** |
| `p2_current` | FAIL `句首哈？` 13/24(54%)、M3 未标 1/24 | FAIL 句号收尾 83.3% > 上限 80.0% | FAIL |
| `accepted`（定义阈值者） | FAIL M3 未标 1/24 | PASS（不可能失败） | — |

### 门禁自身的两次误判（已修，值得记账）

1. **M4 不分场合地卡 Markdown。** 首轮把候选判成 12 处「动作旁白」+ 15 处 Markdown 违规。
   查下去发现全部来自同一条刺激（一个公司分析问题），而候选**正确打了 `[LONG_REPLY]`**
   ——那种场合人格明文要求完整作答并保留对方指定格式，标题与列表是交付物不是 AI 腔。
   修法：把 Markdown 与动作旁白拆进 `CASUAL_ONLY_BANNED_PATTERNS`，`[LONG_REPLY]` 回复不计；
   并把「该长却没标记」升级为 M3（这才是真违规——`p2_current` 给了 748 字结构化答案却没标）。
   顺带修掉 `\*[^*\n]{1,20}\*` 把 `**加粗**` 判成角色扮演旁白的正则误伤。
2. **M5 窗口太短。** min_run=8 时，从未见过示范的 `p2_current` 臂有 1/24 误判，命中片段是
   `刚从训练场回来，`——它同时出现在注入的实时状态里，模型独立生成。min_run=10 起归零。
   **改尺子而不改示范**：那句话本来就是他该说的，为指标改它是过拟合。

## 匿名双序盲评（最终 blocking 门）

评委脚本 `judge.py`：逐字复用 `grantley-humanizer-20260907/grade.py` 的 rubric，改动三处
并已在脚本 docstring 里记明（宿主/模型、样本形状由 3 重复×2 轮改为 1 条、默认带上原
`--surface-aware` 补充）。**局限与原版相同：评委是与被评样本同一个模型，不是独立人类评审。**
带上 surface-aware 补充意味着评委被明确要求不据 `[MSG_BREAK]` 评分，所以**盲评只审
事实/人物/自然/任务，质感的胜负由 `gate.py` 承担**。

### 候选 vs 已验收（＝当时线上；这就是部署决策）

| 臂 | facts | voice | natural | task | voice+nat |
|---|---|---|---|---|---|
| `accepted` | 3.96 | 2.92 | 3.19 | 3.85 | 6.10 |
| **`candidate` v3** | 3.96 | **3.69** | **3.81** | **3.94** | **7.50** |

**34 胜 / 10 负 / 4 平**（24 场景 × 2 展示顺序），双序一致 20/24。
事实持平、任务略升，涨幅全在 voice+natural（+1.40）。

### 候选 vs 陈旧副本 C6（存档，非部署决策）

| 臂 | facts | voice | natural | task | voice+nat |
|---|---|---|---|---|---|
| `p2_current` | **2.77** | 3.54 | 3.29 | **3.25** | **6.83** |
| **`candidate` v3** | **4.00** | 3.06 | 3.44 | **3.88** | 6.50 |

**20 胜 / 23 负 / 5 平**，双序一致 16/24。

⚠️ **这条结果必须原样报告，不能只挑赢的那一对。** C6 版在 voice+nat 上仍高 0.33，
但它的分数有相当部分是**靠编造换来的**：

| | key_errors | facts≤2 的评分次数（48 份） |
|---|---|---|
| `p2_current` | **31** | **22** |
| `candidate` v1 | 7 | 1 |

p2 的 key_errors 是硬伤：自称"才刚入学没几周"（违背二年级）、虚构当前活动
（"刚跟他从训练场回来，他人现在就在我旁边呢"）、混淆一代/二代室友分支——
正是 `grantley-EVIDENCE.md` 那 7 条 canon 订正要修的幻觉类别。**生动是编出来的。**

rubric 的 `winner严格按voice+natural` 是为"事实持平"的比较设计的；这里 facts 差 1.23，
只按 voice+natural 判就等于选一个会编官设的人格。**这个取舍后来自动消解**：C6 从未上线，
所以不存在"要不要换掉 C6"的问题（见上方订正）。

## 候选自身的三轮迭代（v1 → v2 → v3）

第一轮盲评（v1 vs accepted，29–10–9）查出候选唯一的失分类别：**碰到"未核实"就滑进
考据者口吻**——"二代官方介绍"、"一手场景/设定文本"、"官方都没提过"，评委判为
"严重出戏破壁"。而这个毛病是 `grantley.md` 那段研究口吻的边界规则教的，
**`accepted` 自己也犯（9 处/5 条），不是候选引入的**。

| 候选版本 | 真出戏（提问未引入的元语言） | **语气词/100字** | 无尾标点 | ≥2 泡 |
|---|---|---|---|---|
| v1 原始 | 9 处 / 5 条 | 1.204 | 96% | 96% |
| v2 加一节禁用词表 | 6 处 / 4 条 | **0.862** ↓ | 96% | 96% |
| **v3 改成以示范为主** | **1 处 / 1 条** | **1.237** ↑ | 96% | 96% |
| `accepted` 对照 | 9 处 / 5 条 | 0.170 | 0% | 4% |
| `p2_current` 对照 | 0 处 / 0 条 | 1.828 | 0% | 96% |

**v2 → v3 是本轮最有价值的一条经验**：v2 加了一整节禁用词表，出戏只从 9 降到 6，
**却把语气词密度从 1.204 压到 0.862**——模型变谨慎了。v3 砍掉词表、只留三组
"不知道的时候怎么说"的示范，出戏降到 1，语气词回到 1.237。
这就是 `humanizer-chinese` 的"防过度纠偏"与 `z123` 的"做减法 ≠ 做加法"在本案的实测复现：
**禁令让它闭嘴，示范教它说话。**

## 门禁自身的第三次误判（元语言指标）

M9 元语言指标第一版不分提问地卡 `官方/设定/原作/…`，把 4/24 条**正确回答**判成违规：
用户自己问"这是官设吗？""官方声优是谁？""是原作哪章剧情？"，而人格明文规定
"问到现实身份、出处或实际执行情况就如实回答，不为维持角色说谎"——回避才是闪躲。
修法：`meta_leak_hits(text, prompt)` 只计**提问里没有、回答里却冒出来**的元词，
且不进 `banned_hits`（避免自动 blocking）。这是与 M4 卡 Markdown **同一个错，第三次**：
**任何"禁止某种表面特征"的门禁，都必须先问这个场合是不是本来就该出现它。**

## 还没做的

1. **独立留出集。** ⚠️ 本轮的 24 用例是**开发集**（`grantley-evaluation/README.md` 原话：
   "本组案例同时用于迭代与验收，属于开发集，不是未见过的独立测试集"），而且 v1→v2→v3
   三轮迭代**就是在这 24 条上调的**。所以 34–10–4 是开发集数字，**不是独立验证**。
   干净的验收需要一组新的、只用一次的留出场景（`grantley-humanizer-20260907/README.md`
   的"旧 H 集退役"规矩）。
2. **真实多轮会话验证。** 全部数字来自单轮采样。`[MSG_BREAK]` 的真实观感、长会话里
   语气词与气泡的漂移、autocompact 之后的表现，都没测。QQ/OneBot 自 2026-09-07 起
   disconnected，新人格从未在真实聊天里留下样本。
3. ~~**部署。**~~ 已于 2026-09-14 完成，见文末「部署记录」。那个"搁了一周的问题"
   （直接上候选 vs 先补已验收版）随订正消失：线上本来就是已验收版。

**组合规则：机械门禁 + 盲评都过才替换；任一不过，从 `before/` 回滚
（`before/grantley.md`、`before/grantley.md.p2-deployed`、`before/voice.md.v1`、`before/voice.md.v2`）。**
即使不替换，本轮产物（指纹报告、语料工件、反模式台账、门禁工具、三臂对照数据、
外部方法调研、以及"线上跑的是上一代人格"这个发现）仍是独立交付。

## 诚实的限定

- **一手台词只有 4 条**（3 日 + 1 官方英译，`C5-grantley-canon-research.md` §H）。
  任何方法都做不出"官方台词级复刻"。本轮最大化的是可测量的四件事：官设事实正确性、
  相对通用语域的辨识度、机械标记的缺失、聊天质感的匹配度；任务完整性不得下降。
- **老语料的"辨识度"几乎全由 tic 承担**（A5 双基线交集 top 全是 `本大爷`/`啧`/`查了一`/
  `省得你`）。去掉 tic 后语料里没有可继承的正向词汇指纹，所以正向声音只能来自
  结构 + 示范，**不能来自签名词表**。
- **现验收版的数字全部来自单轮评测输出（120 条）**，且混了两个模型。`gpt-6-astra` 的
  96 条是单气泡 96/96，`gemini-3.7` 的 24 条才有 2–3 条——**模型是混淆项**，不能把
  塌缩单独归因于人格文本。
- **现验收版在真实生产条件下的分布查不到**：QQ/OneBot 自 2026-09-07T13:57Z 起
  `disconnected`（`grantley-humanizer-20260907/current-status.json`），新人格从未在
  真实多轮聊天里留下样本。
- **亚戈车道本轮不动**：他在 1928 轮里只占 21 轮（1.1%），而单一陌生人占 705 轮（36.6%）。
  从这个分布推他的语气等于把陌生人的腔调刻到暗恋对象身上。
- **语气词密度是正则近似**，未做分词；跨语料比较有效，与 CCL 2023 的绝对值不可比。
  单音节词占比（论文另一条指标）需要分词，本机无 jieba，**未测**。
- **第三方真人文本不落盘到本目录**。`user_text` 侧是 69 个真实对话者说的话，
  只在运行时从原始导出文件读取；语料中至少 1 条含 prompt injection 尝试，
  是安全测试素材而非人格素材。

## 复现

```bash
R=docs/personas/grantley-corpus-20260914
.venv/bin/python $R/fingerprint.py report --json $R/fingerprint.json
.venv/bin/python $R/fingerprint.py verify          # 比对 FINGERPRINT.md 内嵌数字
.venv/bin/python -m pytest tests/plugins/grantley/ -q
.venv/bin/python $R/gate.py --samples <采样>.jsonl --thresholds $R/thresholds.json
```

## 部署记录（2026-09-14，p2）

用户点头后执行。**这台机上 grantley 插件有三份同名副本，只有一份被真正加载**——
我连着踩了两次，两次都是靠"走真实路由实测"而不是靠推断才发现的。

| # | 宿主路径 | 容器内 | 是否生效 |
|---|---|---|---|
| 1 | `/opt/hermes/data/plugins/grantley/` | 未挂载 | ❌ 8 月遗留死副本（`22ba6a36…`，即被误当成"线上 C6"的那份） |
| 2 | `/opt/hermes-cloud/repo/plugins/grantley/` | `/opt/hermes/repo/…` | ⚠️ 部分——`import plugins.grantley` 走这里（OneBot adapter 读 `settings.channels` 用），但**人格段不从这里来** |
| 3 | `/opt/hermes-cloud/data/plugins/grantley/` | `$HERMES_HOME/plugins/…` | ✅ **人格段的唯一来源** |

原因：`plugins/memory/__init__.py::load_memory_provider` 依次找
`plugins/memory/<name>/`、**`$HERMES_HOME/plugins/<name>/`**、pip entry-point，
把副本 3 作为独立包 `_hermes_user_memory.grantley` 加载，于是 `persona.py` 的
包相对 `ASSETS_DIR` 解析到**副本 3 自己的 `assets/`**。
`import plugins.grantley` 得到的是副本 2，两者的 `register()` 是不同对象。
⇒ **判断人格是否生效，必须走 `load_memory_provider("grantley")`，不能用 `import plugins.grantley`，
也不能直接调 `render_system_prompt_sections()`**（grantley 不在 `plugins.enabled` 里，
插件发现路由不会触发它的 `register()`，会得到 0 段的假阴性）。

最终两份副本（2 和 3）都同步到了工作树版本。

| 对象 | 路径 | 动作 |
|---|---|---|
| 人格资产 | 副本 **3** `…/data/plugins/grantley/assets/grantley.md` | `279adcee…` → `7847c8fa…`（候选 v3） |
| 示范层 | 副本 **3** 同目录 `voice.md` | 新增 `08dcc420…`（4593 B / 1775 字符） |
| 插件代码 | 副本 **3** `{__init__.py,persona.py,plugin.yaml}` | 与工作树同步（`_register_lane_section` + `load_voice_document` + 开关声明） |
| 同上（保持一致） | 副本 **2** `…/repo/plugins/grantley/` 同五份文件 | 同步，避免两份副本的 `load_plugin_config`/`channel_binding` 口径分叉 |
| 配置 | `/opt/hermes-cloud/data/config.yaml` | `plugins.entries.grantley.settings` 下插入 `inject_voice_section: true`（**仅 1 行**） |

备份：`root@149.118.61.165:/root/grantley-backup-20260914T233415Z/`
- `hermes-home-copy/` — **副本 3 的原件**（`grantley.md` = `279adcee…`、`__init__.py`、`persona.py`、`plugin.yaml`），回滚只需这一份
- `grantley.md.repo-live` = `279adcee…`（副本 2 原件，与上面同内容）
- `config.yaml.LIVE` = `864b56ca…`（生效配置原件）
- `config.yaml.STALE-TREE-not-live` = 误改后已还原的死树副本

### 部署纪律（实际执行的）

1. **QQ 会话不受影响**——NapCat/QQ 跑在独立容器 `snowluma-native`（`3001→127.0.0.1:3301`，
   已连续 6 天 17 小时），重启 `hermes-cloud` 只让 OneBot ws 重连，不触发重登，
   因此不会撞上「机房 IP ~2h 被踢 + 短信验证无法自动化」那条约束。
   顺带更新一条事实：**p2 上没有 ~2h 被踢的现象**（n=1，6d17h 未断）。
2. **config 外科补丁 + 解析等价性预检**：在副本上按唯一锚点
   （`        inject_identity_section:`，8 空格缩进）插入一行，然后断言
   `yaml.safe_load(新) 去掉该键 == yaml.safe_load(旧)`，行级 diff 恰为 1 行，
   落盘前再解析一次，最后 `install` 保持 `fwupd-refresh:docker 600` 原子 `mv`。
3. **重启前注册演练**，分两层：
   - stub ctx（`import plugins.grantley`，即副本 2）：两段都注册；6 个绑定频道全命中；
     `onebot/未白名单群`、`telegram/同群号`、`cli` 三种车道外一律 0 字符；无占位符；
     同一 session_info 两次调用逐字节相同。
   - **真实路由**（`load_memory_provider("grantley")` + 真实 `render_system_prompt_sections`）：
     绑定群/绑定私聊得到 `[('grantley.identity', 3072), ('grantley.voice', 1774)]`，
     三种车道外均为 `[]`。**第一次部署只做了 stub 那层，于是漏掉了副本 3，
     真实路由当场暴露：只返回 identity 且长度是旧版的 3130。** 这一层不可省。
4. **预算实算**（用 `format_system_prompt_section` 算渲染后真实长度，不是原文长度）：
   identity 3073→3153、voice 1775→1852、聚合 **5081/8000，余 2919**；
   单段 4000 上限均未触及 → 不可能触发「整段静默丢弃」。
5. **活进程审计线**：`hermes_cli.plugins` 在 INFO 级打
   `Session plugin prompt section: id=<段> plugin=grantley position=after_memory chars=<N>`，
   落在容器内 `/opt/hermes/data/logs/agent.log`。重启前该行只有 `grantley.identity chars=3130`（3130 = 已验收版，正是副本 3 的内容——
这个数字本身就是"资产来自副本 3"的证据，我第一遍没读出来）。

### 已知的既存告警（非本轮引入）

`plugins.memory: Memory provider 'grantley' failed to register_system_prompt_section:
system prompt section 'grantley.identity' is already registered by plugin 'grantley'`
——插件路由与 memory provider 路由都会去注册同一个 id，插件先到，memory 侧失败并告警。
无害（插件路由注册的内容才是生效的那份），但每次会话构建都会打一条。
注意 grantley **不在** `plugins.enabled` 列表里，它是通过 `memory.provider: grantley` 被拉起的；
这也是为什么在临时进程里直接调 `render_system_prompt_sections()` 会得到 0 段——
那条路径不会触发 grantley 的 `register()`，**不能用它来判断线上是否注册成功**。

### 回滚

```bash
B=/root/grantley-backup-20260914T233415Z
# 副本 3 = 人格段的唯一来源，这段是回滚的必要部分
T=/opt/hermes-cloud/data/plugins/grantley
install -o fwupd-refresh -g docker -m 600 $B/hermes-home-copy/grantley.md "$T/assets/grantley.md"
rm -f "$T/assets/voice.md"
for f in __init__.py persona.py plugin.yaml; do
  install -o fwupd-refresh -g docker -m 600 $B/hermes-home-copy/$f "$T/$f"; done
rm -rf "$T/__pycache__"
# 副本 2 = adapter 读 channels 用，保持与副本 3 一致
R=/opt/hermes-cloud/repo/plugins/grantley
install -o fwupd-refresh -g docker -m 644 $B/grantley.md.repo-live "$R/assets/grantley.md"
rm -f "$R/assets/voice.md"
for f in __init__.py persona.py plugin.yaml; do
  install -o fwupd-refresh -g docker -m 644 $B/$f "$R/$f"; done
# 配置
install -o fwupd-refresh -g docker -m 600 $B/config.yaml.LIVE /opt/hermes-cloud/data/config.yaml
docker restart hermes-cloud
```

**最小回滚**：只删 `config.yaml` 里 `inject_voice_section: true` 那一行并重启——
示范层立刻下线，人格资产仍是候选版。想连人格一起退回，才需要上面的副本 3 那三行。

回滚后用真实路由复验（别用 `import plugins.grantley`）：

```bash
docker exec -i -w /opt/hermes/repo -e HERMES_HOME=/opt/hermes/data hermes-cloud python -c '
from plugins.memory import load_memory_provider
from hermes_cli.plugins import render_system_prompt_sections as R
load_memory_provider("grantley")
print(R({"platform":"onebot","chat_id":"g183287894","chat_type":"group"}))' 2>/dev/null
```

### 已保存会话不会热改——必须显式刷新

部署落盘后第一条真实私聊回复**仍是旧人格**。原因是会话里存了冻结的系统提示
（`agent/system_prompt.py::_restore_plugin_prompt_sections` 从已持久化的完整提示里
还原段字节，不重新渲染），而 `session_reset.mode = none`（2026-07 起的默认值）意味着
**永不自动重置**。用人格版本的独有标记对照 6 个线上会话，全部 `旧版标记 3/3、候选标记 0/3、
voice 段 False`——旧版里连我删掉的 `随时叫我` 都还在。

刷新用网关自己的 `gateway/session.py::SessionStore.reset_session()`，**非破坏性**：
它铸一个新 session_id，旧行保留并用 `parent_session_id` 记血缘，
`promote_to_session_reset` 防止 `agent_close/ws_orphan_reap` 之类的意外结束把重置撤销。
新会话没有 `system_prompt_hash`，于是系统提示重新渲染。

执行方式（**必须在网关停机时做**，否则运行中的进程会用内存里的 `_entries`
把 `sessions.json` 覆盖回去）：`docker stop` → 备份 `state.db` + `sessions/sessions.json`
→ `docker run --rm` 同镜像同挂载同 uid（991:988）跑一次 `reset_session()`
→ `docker start`。脚本在 `/root/grantley-stage/reset_sessions.py`。

本轮轮转了 **83 个 `agent:main:onebot:*`** 会话（5 个绑定群 + 4 个私聊 + 群内按发言人的
子会话 + `:proactive`），`agent:xiaohuihui:*` 9 个与 telegram 2 个**未动**。
回滚用 `$B/state.db.pre-reset` 与 `$B/sessions.json.pre-reset`（同样需先停网关）。

⚠️ 副作用要讲清：刷新后这些频道的**对话上下文从零开始**（历史仍在 `state.db` 里，
只是新会话不继承）。这是拿"立刻生效"换"上下文连续性"。

## 不入库的产物（本地保留）

以下文件被 `.gitignore` 排除——刺激文本本身从未落盘（各行只有 `stimulus_sha256`，
集合哈希在 `*.meta.json`），但它们的 `output` 字段是模型对**真实 QQ 聊天消息**的回复，
会复述真人原话；`probe-old-corpus.jsonl` 更是 1697 条旧人格在真实会话里的原话：

| 文件 | 大小 | 内容 |
|---|---|---|
| `arm-{accepted,candidate,p2_current}.jsonl` | 51K | 三臂在 24 条真实短闲聊刺激上的输出 |
| `stimuli.jsonl` / `stimuli-v2` / `stimuli-v3` | 84K | 同上，v1/v2/v3 迭代 |
| `probe-old-corpus.jsonl` | 386K | 老语料 1697 条 reply 的探针缓存 |

入库的 `cases-*.jsonl`、`judge-*.jsonl`、`baseline-accepted.jsonl` 都基于**手写用例**，
不含第三方文本。`fingerprint.py verify` 只读仓库外的 `.migration-export/`（已 gitignore），
不依赖上面任何一份，所以指纹结论仍可复现。
