# 搜索状态与官方搜索接入

2026-09-07 04:32:06 UTC，`cornna-cn/hermes-cloud` 已启用原生 `web_search` 的官方搜索后端。它调用反代保留的 OpenAI/Codex Web Search 和 Gemini Google Search。复用 `providers.cornna` 与 `CORNNA_API_KEY`，主聊天模型、凭证文件和其他配置语义保持不变。

## 当前可用路径

| 服务 | 通过现有反代调用的协议 | 部署后实测 |
|---|---|---|
| GPT/Codex官方搜索 | `POST https://api.cornna.xyz/v1/responses`，`tools:[{type:web_search}]`，强制执行搜索 | 10.43秒；实际模型`gpt-6-astra`；1次原生搜索，3条来源 |
| Gemini Google Search | `POST https://api.cornna.xyz/antigravity/v1beta/models/gemini-3.8-flash-high:generateContent`，`tools:[{googleSearch:{}}]` | 21.36秒；返回模型`gemini-3.8-flash`；有`webSearchQueries`和3条来源 |
| Codex独立Alpha Search | `POST https://api.cornna.xyz/v1/alpha/search` | 返回429、`model_cooldown`，本次没有启用此路径 |

默认`web.search_backend: official-search`，GPT优先，在总超时预算内失败时回退Gemini。两条服务也可以分别通过`codex-search`、`gemini-search`选择。只接受官方搜索执行与来源元数据；HTTP 200或模型口头声称搜索过均不足以通过。模型生成的无依据链接不会作为搜索结果交付。

官方协议依据：[OpenAI Web Search](https://developers.openai.com/api/docs/guides/tools-web-search)、[Codex Alpha Search客户端](https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/endpoint/search.rs)、[Codex请求/响应结构](https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/search.rs)、[Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search)、[Gemini原生生成协议](https://ai.google.dev/api/generate-content)。公开Gemini接口文档不能独自证明某一Antigravity反代兼容；本次同时验证了该反代的实际`groundingMetadata`。

## 代码与配置

实现为独立插件：[本地源码](/Users/cornna/project/hermes-official-search/README.md)，生产位置`/opt/hermes-cloud/data/plugins/official-search/`。使用既有`WebSearchProvider`和`PluginContext.register_web_search_provider`，没有更改Hermes核心文件或增加模型工具。配置见[official-search-settings.json](official-search-settings.json)，不含密钥。

API域名和密钥从现有Hermes provider读取；请求路径固定在同一域名下，不跟随带认证的HTTP重定向。回退结果包含实际`provider`、`attempts`、`fallback_used`和`search_evidence`。上游提供明确额度恢复时间时，当前进程暂时跳过相同配置/凭证的冷却服务。Google来源跳转链接和搜索建议元数据保留，摘要明确是依据来源的归纳，不冒充网页原文。

## free-search-mcp状态

该通道保留。真实插件加载、MCP握手和原生registry dispatch均成功，两个新缓存查询各约11.4秒；英文查询命中Python官方文档，中文查询命中骑士学院官网，也混入了一条无关Salesforce结果。

关闭自动救援、使用新缓存逐个引擎测试两个中英文查询，结果如下：

| 引擎 | 本轮结果 |
|---|---|
| Baidu | 两次均返回结果，包含相关官方来源 |
| AnySearch | 两次均返回结果，包含相关来源 |
| DuckDuckGo | 两次均约8秒连接超时 |
| Mojeek | 两次均HTTP 403 |
| Google News | 两次均约8秒连接超时 |

因此free-search-mcp是部分可用，不能称五个引擎全部正常。现有适配层返回的`engines`表示请求配置，不代表每个引擎都成功；它未保留上游的逐引擎错误详情。本轮按用户后续要求接入官方搜索，没有修改free-search的配置与实现。

## 验证与部署

- 9项独立插件测试通过：真实Hermes加载/工具分发、本地HTTP请求、已有凭证、跨profile隔离、无搜索/无来源/截断的拒绝、回退、额度冷却和重定向保护。
- 1项SQLite迁移与回滚测试通过：历史字节、旧冻结前缀、其他profile及退休的群成员独立路由保持完整，新会话清除过时的活动标记。
- 临时profile中的两种实际官方搜索通过，随后安装目录中的两种实际调用也通过。
- 正式切换在`active_agents=0`时完成；11个可达默认profile会话承接776条有效历史。旧会话历史与系统前缀未变，其他profile/退休路由未变。
- QQ客户端未重启。后验时QQ和Telegram均connected，网关运行用户可正常读取数据库。没有向联系人发送测试消息。

首次等待网关空闲的连接中断，没有产生生产写入；随后重新核对状态后完成切换。旧群会话曾残留很早的`active_turn_token`，后续日志也显示其他实际请求仍在处理，因此不能仅凭该标记断定哪条发送请求卡住。最终没有强制中断正在运行的模型调用。

关键记录：

| 记录 | 文件 |
|---|---|
| 部署及后验 | [official-deployment.txt](official-deployment.txt)、[official-postcheck.json](official-postcheck.json) |
| 安装后的官方搜索 | [installed-provider-smoke.jsonl](installed-provider-smoke.jsonl) |
| 临时profile实际调用 | [live-provider-smoke.jsonl](live-provider-smoke.jsonl) |
| 官方检索证据 | [official-search-evidence.json](official-search-evidence.json) |
| 独立Alpha端点429 | [alpha-probe.json](alpha-probe.json) |
| free-search整体与逐引擎 | [mcp-smoke.txt](mcp-smoke.txt)、[engines.jsonl](engines.jsonl) |
| 测试 | [official-provider-tests.txt](official-provider-tests.txt)、[revision-tests.txt](revision-tests.txt) |
| 会话迁移演练 | [official-migration-preflight.txt](official-migration-preflight.txt) |

服务器私有备份：`/opt/hermes-cloud/data/.deployments/official-search-20260907/backup/`。失败恢复配置，并从最新有效历史建立回退分支，不用旧数据库覆盖后续聊天。旧API凭证从未写入插件、测试源码或本目录；私有配置和数据库备份留在服务器。

## 限制

本次启用的是`web_search`。原生`web_extract`此前实测无可用后端，本插件没有把模型摘要伪装成正文提取来填补它；如需正文提取，仍应单独配置相应能力。独立Alpha Search的429与free-search部分引擎失败也仍然存在。

测试是本轮真实调用与有限场景覆盖，不保证外部服务永不超时、搜索结果永远相关或账户配额始终可用。
