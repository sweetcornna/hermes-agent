# 格兰：人格校准、自然表达与部署

本轮接续任务《优化Grantly人设与表达》。人格沿用二代新学期、继续与亚戈同寝、暗恋而未交往的运行选择，中文采用直爽、有点嘴欠的尺度。最终稳定人格3131字符，低于现有4000字符预算；没有新增核心工具、配置项、数据库结构或逐条回复的第二次润色调用。

**人格与会话迁移已部署。** 网关于2026-09-07 02:38:08 UTC加载新版，6个会话承接269条有效历史，旧会话和冻结提示词逐项保持一致。02:59:46 UTC的[登录后核验](post-login.json)曾确认QQ和Telegram均为connected。后续只读检查发现QQ再次登录失效，以及真实群号格式使完整人格漏载的问题；补充修复见[群号接入记录](group-routing-repair/README.md)，最新连接情况以[当前状态](current-status.json)为准。

## 人格与研究

- [原作研究增补](RESEARCH.md)：来源、语言、作品与场景、证据等级和访问限制。
- [中文反应示意](VOICE.md)：邀请、被调侃、争执、关心、道歉、拒绝与认真办事，全部明确属于原创示意。
- [相对现用基线的人格差异](persona.diff)：合并重复禁令，补充具体情境反应和关系差异，保留事实与工具执行边界。
- [最终人格](../../../plugins/grantley/assets/grantley.md)：动态状态仍随当前用户消息注入，稳定身份只在新会话建立时加载。

新核对的官方宿舍画面支持“抓住对方的话调侃”的表达方式。家庭、E.P.完整机制、骑术获奖等仍存在一手证据缺口；二手转录只是线索，不当作已核实档案。完整来源保留在研究层，后期剧情没有写入开篇个人记忆。

## 验收结果

最终纳入192条回复：24项事实/任务回归各版1次；6组开发场景与6组最终留出场景各版3次，每次2轮。模型请求与实际返回均为`gpt-6-astra`；采样统一`temperature=0.4`、`max_tokens=1800`。所有输出保留，没有挑选最佳回复。

| 最终留出场景 | 基线表达分 | 新版表达分 | 结果 |
|---|---:|---:|---|
| F01 生活玩笑 | 7 | 8 | 新版更好 |
| F02 较劲后改为散步 | 6 | 8 | 新版更好 |
| F03 口音边界后换话题 | 6 | 7.5 | 新版更好 |
| F04 排序代码与解释 | 6.5 | 6.5 | 持平 |
| F05 冒名玩笑后正常相处 | 8 | 8 | 持平 |
| F06 未核实经历与偏好 | 6 | 6.5 | 新版略好 |

表达分是人物辨识度与自然接话两项之和，满分8分，再取匿名A/B两种展示顺序的平均。新版4/6组更高，达到三分之二的门槛；F06一次新版胜、一次持平，不能描述成两次一致胜出。24项回归最终均通过；基线R06缺少渔村背景，R19把空话题推断为没有在聊天。最终样本未发现新增关键事实、身份绑定、时期或技术错误。

这些结论来自实施Agent逐条审阅与同一模型的匿名评分，不是独立人类评审，也不是长期对话的统计显著性结论。双方在认真解释时仍有较整齐的教学/核验口吻，不能把这轮通过表述为杜绝幻觉或彻底消除模板感。

| 证据 | 文件 |
|---|---|
| 全部最终输入与输出，便于阅读 | [comparison.md](comparison.md) |
| 逐项评审与两种顺序的评分理由 | [review.json](review.json) |
| 最终验收与人格哈希 | [acceptance.json](acceptance.json) |
| 24项最终回归 | [final-regression.jsonl](final-regression.jsonl) |
| 6组最终开发样本 | [final-dev.jsonl](final-dev.jsonl) |
| 6组最终留出样本 | [final-holdout.jsonl](final-holdout.jsonl) |
| 匿名评分全部输出 | [final-review/judge-raw.jsonl](final-review/judge-raw.jsonl) |
| 工程测试：270项通过 | [resume-tests.txt](resume-tests.txt) |
| 真实群号格式补充回归：278项通过（包含原270项） | [group-routing-repair/tests.txt](group-routing-repair/tests.txt) |
| 生成代码实际运行 | [code-check.json](code-check.json) |
| 真正的插件加载、Agent组装与文件/终端工具调用 | [final-real-tool-raw.txt](final-real-tool-raw.txt) |
| 6个生产绑定会话的私有快照迁移/回滚演练 | [migration-preflight.txt](migration-preflight.txt) |

二分查找每版通过1260个数组/目标组合；排序场景两版全部12段回复里的代码已执行，确认原数组保留、新结果排序正确。真实工具测试在临时`HERMES_HOME`中创建并读取文件，核验实际字节数和SHA-256，消息工具与真实QQ出口未启动。

## 迭代与留出集边界

第一版事实回归发现渔村背景被删和空状态推断问题，修订后重跑。初始H01–H06留出集在H04发现一条把赋值/比较方向说反的措辞，虽然后续代码正确，仍判为验收失败，未部署。初始评分还有误罚传输标记、漏读已有来源的问题；原始样本与评分均保留在`*-v1`、`*-v2`和根目录`judge-raw.jsonl`。

修订只增加文字、代码、例子与改动方向应相互对应的通用核对要求。旧H集退役，最终人格冻结后使用新的F01–F06测试集，见[final-scenarios.json](final-scenarios.json)。评分明确消息分隔/卡片标记不对用户展示，合理建议和假设玩笑不属于伪造过去活动。没有根据F集输出继续改人格。

## 部署与恢复

目标为已核实的`cornna-cn`主机、`hermes-cloud`容器。部署仅替换仓库和安装目录中的人格资源；使用既有SessionStore建立新版会话分支，复制有效历史，保留旧会话及冻结前缀。备份保存在服务器`/opt/hermes-cloud/data/.deployments/grantley-humanizer-20260907/backup`，其中配置与数据库含私有数据，不复制到此公开研究目录。

部署前核对旧资源、配置与镜像哈希，等待网关连续空闲，再停止网关完成资源更换与路由切换。失败恢复人格，并从最新有效历史建立回退分支，不用旧数据库覆盖部署后的聊天。QQ客户端、登录环境和发送链路保持现状；未向真实联系人发送测试消息。

首次执行记录见[deployment.txt](deployment.txt)，当时运行与历史验证见[post-deployment.json](post-deployment.json)。后续群号接入修复记录单独保存在[group-routing-repair](group-routing-repair/README.md)。验收条件、安装清单与恢复脚本均保存在服务器部署目录中。

首次部署后只读核验区分部署正确性与通信就绪：当时`deployment_verified=true`，但`operational_ready=false`、`passed=false`，待用户手动恢复QQ登录。带Docker UTC时间戳的[状态日志](qq-status-timeline.json)记录，QQ在02:31:54已转为`auth_required`，早于本次02:38:08的部署。网关启动初期曾短暂报告connected，随后账号探测仍为登录失效，不能把启动时的连接标志当最终验收结果。用户完成登录后再次只读核验全部通过，历史失败状态保留在`post-deployment.json`，当次成功状态保留在[post-login.json](post-login.json)。这两个文件是当时快照；后续连接变化记录在[当前状态](current-status.json)。

因此没有回退已通过验收的人格，也没有自动重登QQ、重建客户端或修改发送链路。后验验证在禁网、生产挂载只读的临时容器中完成，数据库比较在私有副本内执行；没有通过放宽生产文件权限读取备份。

## 复核

本地工程检查始终使用仓库入口：

```bash
scripts/run_tests.sh tests/plugins/grantley tests/agent/test_memory_provider.py tests/gateway/test_onebot_persona_binding.py -q --tb=short
.venv/bin/python docs/personas/grantley-humanizer-20260907/check_generated_code.py
.venv/bin/python docs/personas/grantley-humanizer-20260907/summarize.py
```

采样脚本`run.py`要求使用新的输出文件，防止覆盖既有样本；输入快照保存完整提示、参数及人格哈希。`grade.py`支持在完全相同的输入下接续缺失评分，不重采已有结果。生产部署脚本是本次一次性迁移记录，不应直接作为常规更新工具重复运行。
