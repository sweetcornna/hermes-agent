# Search MCP 修复与亚戈账号绑定

2026-09-06 15:11:44 UTC，生产 `cornna-cn/hermes-cloud` 完成镜像切换。
`free-search-mcp 0.11.0` 是当天核对的 [PyPI 最新版本](https://pypi.org/project/free-search-mcp/0.11.0/)，
未把没有更新号的现有包伪称为升级到更高版本。修复的是实际缺失的运行依赖与启动路径。

原镜像缺少 MCP SDK 和 uv，配置存在但没有可用工具。新镜像固定
`mcp==2.1.1`、`uv==0.12.10`，以约束文件保留现有 Python 依赖。
free-search 由 uv 独立环境运行，缓存预热到持久目录。

首次切换失败后自动回退原镜像：`uv --no-project` 仍读取工作目录中的
`exclude-newer="14 days"`，因此无法选择刚发布的 0.11.0。最终通过原有
MCP `cwd: ${PLUGIN_DATA}` 配置隔离启动目录，未修改 Hermes 仓库的依赖策略。
第二次切换成功；见 [deployment-retry.txt](deployment-retry.txt)。
原容器保留为 `hermes-cloud-before-search-20260906`，QQ 客户端未重启。

生产实测默认搜索引擎部分被拦截，Bing 曾返回不相关结果。当前以 Baidu、
AnySearch、DuckDuckGo、Mojeek、Google News 组合运行。
[production-search.txt](production-search.txt) 通过真实插件发现、SDK 握手、
工具注册和 registry dispatch 完成两次查询，返回 Python 和骑士学院官网。
每次约 11 秒，只暴露一个 `search` 工具，结果数限制 1–5。
其中一个查询也返回了无关条目；搜索连通并不保证所有结果准确，仍需核对来源。

QQ **2104743984 在当前角色互动中是亚戈**，与格兰继续同寝，也是格兰暗恋的对象。
按发送者真实账号识别，昵称与自称不改变身份。不推定双方已确立恋人关系，
不把 QQ 映射或所选情感走向伪装成官方资料。人格与频道注入两个文件均同步到源码和安装目录。
群聊发送者 ID 位于当前消息上下文，固定人格与实时状态仍分开。

相关测试 178 项通过（[combined-tests.txt](combined-tests.txt) 177 项加
[revision-tests.txt](revision-tests.txt) 1 项）。模型身份、冒名、原作事实与专业任务
探测见 [persona-model-final.jsonl](persona-model-final.jsonl)。生成的二分查找代码
经 3234 个输入验证。新建六条会话分支承接有效历史，旧会话与缓存前缀保持原样。
最终文件哈希见 [manifest-final.json](manifest-final.json)；最初部署清单保留不覆盖。

随后已部署 [自然回复与 3 秒等待调整](../conversation-20260906/README.md)，
其中记录最新人格版本及当前 QQ 原生桥的卡片、图片限制。
