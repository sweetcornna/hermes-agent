# T4 rev-2 修订结果

## 0. 范围与结论

- **修订对象**：`/Users/cornna/project/hermes-agent/docs/orchestration/tasks/T4-home-migration-runbook.md`
- **修订依据**：`/Users/cornna/project/hermes-agent/docs/orchestration/tasks/T4-rev2.md`、`/Users/cornna/project/hermes-agent/docs/orchestration/audits/T4-rev1.md`
- **修订前备份**：`/Users/cornna/project/hermes-agent/docs/orchestration/tasks/T4-home-migration-runbook.md.bak-pre-rev2-20260831T115119Z`
- **规模**：4077 行 / 230986 bytes → 4738 行 / 264484 bytes
- **最终 SHA-256**：`9905c45983ebb74898d048a836457db681643e6b6efcb5b14b10ecbc2ca53933`
- **备份 SHA-256**：`b31493f310e57c9b1bb47d5e088342d1004426df5741e956f8bad353c166a800`
- **生产边界**：未 SSH、未扫码、未启动/停止任何生产服务、未访问生产 endpoint。

结论：**R1–R9 全部处置；扫描另发现并修复 10 处 release-relevant 的额外失败开放。**

## 1. 实施计划（修订前已先落盘）

### 文件范围

| 文件 | 动作 | 理由 |
|---|---|---|
| `/Users/cornna/project/hermes-agent/docs/orchestration/tasks/T4-home-migration-runbook.md` | 就地外科修订 R1–R9，并扫描所有闸门脚本 | 唯一修订对象 |
| `/Users/cornna/project/hermes-agent/docs/orchestration/tasks/T4-home-migration-runbook.md.bak-pre-rev2-20260831T115119Z` | 修订前 `cp -a` | 可回退；满足硬约束 11 |
| `/Users/cornna/project/hermes-agent/docs/orchestration/results/T4-rev2.md` | 记录计划、处置、扫描矩阵、真实验证与未完成项 | 修订单指定交付物 |

明确不修改：

- `/Users/cornna/project/hermes-agent/docs/orchestration/tasks/T3-design.md`；
- `/Users/cornna/project/hermes-agent/docs/migration-corlinman/H1-napcat-hardened.md`；
- runbook §8.4 及 rule-of-three、迁移后判定表、`exposure`/`failures` 口径；
- F2/F5/F7/F12–F15/F17 已确认段及保留的“上一版已删除”反面记录。

### 实现顺序与风险控制

1. 先备份，再修 R1/R3/R4/R5/R6/R7/R8。
2. R2 使用真正 Hermes 宿主的受限 SSH 状态查询；只允许明确 rc=3 放行。
3. R9 把 Docker digest、互斥、启动、验收、回滚和清单补成对称路径。
4. 全量扫描命令替换、stderr 吞错、`|| true`/安全值、`case`、成功退出、返回码三态和 `dict.get`。
5. 最后提取全部 heredoc 脚本做语法/编译检查，并用 fake 命令覆盖失败分支。
6. 用 rev-2 备份逐段哈希比较保护范围，确保未碰 §8.4/F15 等禁止段。

主要风险及控制：

| 风险 | 控制 |
|---|---|
| R2 再次只查本机 | home 使用 `ssh-file:/etc/hermes-probe-guard/prod-host`；prod 使用 `local`；受限 key 只能执行 `systemctl is-active --quiet hermes.service` |
| 管道把生产者失败变成零匹配 | 先检查生产者 rc，再解释输出；grep/pgrep 明确区分 0/1/≥2 |
| Docker 只修 unit、正文仍只支持 native | 对 §3.1/§3.5/§4.5/§6/§7/§8.5/附录 B 逐处对账 |
| 误碰并行 T3 统计修订 | 不改 T3 文件；§8.4 与备份逐字节一致；R4/R8 仅收紧 T4 迁移前证据闸 |
| 假测试掩盖错误 | 所有 PASS 均附真实命令输出；中间失败也单列根因 |

## 2. R1–R9 逐条处置

| # | 处置 | 改动位置与理由 | 结果 |
|---|---|---|---|
| **R1** | 旧机 3011/6099 与 prod 3011 改为先检查 `ss` 成功，再判断捕获结果是否为空；覆盖所有 bind 地址 | §3.5 `probe_port_absent()` 与 prod REMOTE；`ss` 失败输出 `UNCHECKABLE` 并非零 | **PASS** |
| **R2** | home 经 forced-command SSH key 查询真正宿主 `corlinman-prod`；prod 查本机；只有 rc=3 允许建 WS，rc=0/其他 rc/SSH 255/超时/命令缺失全部退出 3 | §5.2.1、§6.3、§6.4、§6.6.0、§7.3.5；新增 rc=4 fake test | **PASS** |
| **R3** | watchdog 不再接受 `unknown`；一次 `systemctl show` 查询 `LoadState`/`ActiveState`/`UnitFileState`，只接受明确 absent 或 inactive/failed + masked | §3.3.4(a) | **PASS** |
| **R4** | `end_censored` 加入必填；同时要求 `account_evidence` 存在非空、两个删失字段必须是 bool；统计使用直接索引，缺字段不再变成 `None` 安全值 | §8.1.2 baseline gate | **PASS** |
| **R5** | inspect/logs/ps/config listing/SHA256/complete marker/sync 任一步失败都中止；只有 `ARCHIVE_COMPLETE` 落盘并 sync 后才可能 `docker rm` | §4.5.4 | **PASS** |
| **R6** | journal 改用 `-q -o json`；零真实记录为 0 行，`-- No entries --` 不再贡献行数；unit 必须存在且 active | §6.2 | **PASS** |
| **R7** | runtime state 不可读或结构缺失退出 1；外层采样检查 systemctl/ss/pgrep 返回码并累计 `overall`，后续正常样本不能覆盖早期失败 | §6.6.1 | **PASS** |
| **R8** | 连续性改用 timer 当前 active 代际的 `ActiveEnterTimestampMonotonic`；旧账本跨度仅诊断、不放行；查询失败/非 active/时间戳异常全部 BLOCK | §8.1.2 | **PASS** |
| **R9** | Docker 使用 root-owned `docker-image.env` + image guard，活动 unit 不含 digest 占位符；复用端口 preflight 与同一 flock；补启动、kicked unit 分支、互斥、回滚、巡检和清单 | §1.6、§3.1/§3.5、§4.5、§6、§7、§8.5、附录 B | **PASS** |

## 3. 扫描额外发现：10 处 release-relevant 失败开放

以下均不在 R1–R9 点名位置内，属于同类扫描所得；**10/10 已修**。

| # | 原问题 | 失败时原行为 | 修复 |
|---|---|---|---|
| X1 | §3.5 unit 用 `is-active`/`is-enabled` 输出文本判断、忽略查询 rc | 查询异常若碰巧产生安全文本可放行 | 改为检查 `systemctl show` rc 与结构化属性 |
| X2 | §3.5 容器 grep 未显式区分 rc=1 与 rc≥2 | matcher 错误可能被当成无匹配 | 0=有容器、1=无容器、≥2=UNCHECKABLE/BLOCK |
| X3 | §4.3.2 root preflight 的 `if ... grep -qi` | grep rc≥2 落入 false 分支并继续 OK | 显式三态 `case`；行为测试覆盖 grep rc=2 |
| X4 | §4.3.3 端口检查用 `tail | grep` 的条件真假 | tail/grep 失败被解释成无监听 | `ss -ltnH` 成功后直接判断捕获输出，无 matcher 管道 |
| X5 | §4.8 临时 token 检查 `test && echo true || echo false` | token 空时最后 `echo false` 仍返回 0 | false 分支显式 `false`，命令非零 |
| X6 | §5.2 `ssh-keyscan | tee` | keyscan 失败可被 tee 的 0 覆盖，生成空 known_hosts | `HOSTKEY_GATE` 分离 keyscan rc、非空检查与 install rc |
| X7 | §5.2 unit placeholder 检查 `grep ... || echo PLACEHOLDER_REPLACED` | 文件缺失/grep 错误也打印安全值 | 原子替换 + grep 0/1/≥2 三态 `RTUNNEL_UNIT_GATE` |
| X8 | §6.3 token reader 用 `dict.get` 回退到首个 server/空 token | 配置缺失时仍可能尝试无认证连接 | 必须有 enabled server 与非空 token，否则退出 2、建连前终止 |
| X9 | §6.5 `journalctl | grep -c connect_failed` | journal 失败可伪造 0 | `HERMES_RELEASE_GATE` 先确认 journal 非空，再解释所有 grep rc |
| X10 | §8.1.2 缺 `account_evidence` 时 `None != "unknown"` | 不完整记录可进入 ≥10 样本 | 字段必填且必须为非空字符串；缺失直接 BLOCK |

说明：F2 身份段、F15 permitlisten 段、F14 UFW 段按修订单禁止修改。它们与备份逐字节一致；其中的 `|| true`/人工输出只出现在诊断、回滚或已确认流程，不作为本轮 release gate 的新改动。身份的最终登录放行仍由 §3.5 独立 fail-closed 地重验目录。

## 4. 失败开放扫描矩阵

“失败行为”指**命令失败、权限错误、输出缺失或返回码未知时**的最终行为。

| 闸门 | 检查 | 明确通过条件 | 失败行为 |
|---|---|---|---|
| §2.4.3 `IDENTITY_GATE` | 路径已存在 | 无；任何存在形式均非法 | 无条件 BLOCK；诊断 `ls/stat/find` 即使失败也不会越过随后 `fail` |
| §2.4.3 | 父目录、用户、组 | 三项明确存在 | 任一失败 BLOCK |
| §2.4.3 | `install -d` | 命令成功 | 失败 BLOCK；最终 §3.5 再独立重验目录存在/为空/可读 |
| §3.3.4 `WATCHDOG_GATE` | `systemctl show` 查询 | rc=0 且属性齐全 | query failure/missing properties BLOCK |
| §3.3.4 | unit absent | `LoadState=not-found` | 这是唯一 absent PASS；不是从错误文本推断 |
| §3.3.4 | active state | `inactive` 或 `failed` | `active`/`unknown`/其他值 BLOCK |
| §3.3.4 | unit file state | `masked`/`masked-runtime` | 其他值 BLOCK |
| §3.5 旧机 REMOTE | SSH | rc=0 | rc=255 或其他非零 BLOCK |
| §3.5 旧机 REMOTE | unit 状态 | show rc=0、load loaded/masked、active inactive/failed、unitfile masked | 查询失败/属性不符 BLOCK |
| §3.5 旧机 REMOTE | 两组 pgrep | rc=1 | rc=0 有进程 BLOCK；rc≥2 UNCHECKABLE/BLOCK |
| §3.5 旧机 REMOTE | 3011/6099 | `ss` rc=0 且输出空 | `ss` 非零 BLOCK；任意监听 BLOCK |
| §3.5 旧机 REMOTE | cgroup | 文件不存在，或可读且 0 行 | `wc` 失败映射 -1 并 BLOCK；非 0 BLOCK |
| §3.5 旧机 REMOTE | Docker/Podman | runtime 不存在，或查询成功且 grep rc=1 | runtime 查询失败、grep≥2、命中 napcat 均 BLOCK |
| §3.5 prod REMOTE | SSH/3011/pgrep | SSH 成功；`ss` 成功且空；pgrep rc=1 | 任一失败/监听/进程 BLOCK |
| §3.5 home REMOTE | 身份目录 | 目录存在，`find|wc` pipeline rc=0 且 0 项 | missing、find/sudo/wc 失败、非空均 BLOCK |
| §3.5 home REMOTE | QQ 进程与容器 | pgrep rc=1；runtime absent 或明确无 napcat | 查询失败、命中、grep≥2 均 BLOCK |
| §4.3.2 root preflight | 依赖 | pgrep 存在 | 缺失 BLOCK |
| §4.3.2 | runtime producer | runtime absent，或 `ps` rc=0 | runtime 存在但 `ps` 失败 BLOCK |
| §4.3.2 | runtime matcher | grep rc=1 | rc=0 容器存在 BLOCK；rc≥2 BLOCK |
| §4.3.2 | 全机 QQ pgrep | rc=1 | rc=0/≥2 BLOCK |
| §4.3.3 user preflight | ss/pgrep/flock 依赖 | 全部存在 | 缺任一命令 BLOCK |
| §4.3.3 | 端口 | 每个 `ss -H` rc=0 且输出空 | `ss` 失败或有监听 BLOCK |
| §4.3.3 | 本用户 QQ pgrep | rc=1 | rc=0/≥2 BLOCK |
| §4.3.3 | flock | 可打开且可取得 | 锁被持有、权限/打开错误均 BLOCK |
| §4.3.4 launcher | lock fd/flock | fd 可开且锁可取 | 任一失败退出 1；`exec` 失败非零 |
| §1.6 `DOCKER_DIGEST_GATE` | image inspect | rc=0，格式为允许仓库 + 64 hex digest | inspect/格式失败 BLOCK |
| §1.6 | digest 文件 | 创建、写入、readback 均成功 | 任一步 BLOCK |
| §4.5.2 image guard | 文件结构 | 可读、恰好一行、精确 digest | missing/空/多行/tag/格式异常 BLOCK |
| §4.5.2 image guard | 本地镜像 | exact digest `docker image inspect` 成功 | 失败 BLOCK |
| §4.5.2 `DOCKER_UNIT_GATE` | `systemctl cat`/过滤 | 两者 rc=0 | 任一失败 BLOCK |
| §4.5.2 `DOCKER_UNIT_GATE` | 禁止/必需指令 | 无 active `docker rm`/placeholder；image guard/preflight/flock/image var/privileged stop 齐全 | 任一不符 BLOCK |
| §4.5.3 Docker guard | `docker ps -aq` | rc=0 且 ID 为空 | 查询失败 BLOCK；ID 非空时诊断即使失败仍无条件 BLOCK |
| §4.5.4 archive | 容器状态 | inspect 可读且 running=false | 不可确认或 running BLOCK |
| §4.5.4 archive | 四项证据 | inspect/logs/ps/config-listing 全成功 | 任一失败退出，绝不调用 rm |
| §4.5.4 archive | 完整性/耐久性 | 必需文件、SHA256SUMS、marker、sync 全成功 | 任一失败退出，绝不调用 rm |
| §5.2 `HOSTKEY_GATE` | host/keyscan | host 合法、keyscan rc=0 且非空 | invalid/scan failure/empty BLOCK |
| §5.2 `HOSTKEY_GATE` | known_hosts install | install rc=0 | 失败 BLOCK |
| §5.2.2 host patch | host/placeholder count/write | host 合法、placeholder 恰好 1、原子替换成功 | 任一失败 BLOCK |
| §5.2.2 `RTUNNEL_UNIT_GATE` | placeholder grep | grep rc=1 | rc=0 placeholder 尚在 BLOCK；rc≥2 BLOCK |
| §6.2 `KICKED_CHECK` | unit | 允许名、存在且 active | invalid/missing/inactive/query failure BLOCK |
| §6.2 | journal | `-q -o json` rc=0 且至少 1 条真实记录 | failure/0 条 BLOCK |
| §6.2 | grep | rc 0/1 可解释，计数 0 | rc≥2 或命中 kicked BLOCK |
| §6.3 probe guard | break-glass | 仅显式 `T4_PROBE_ALLOW_WITH_HERMES=1` | 非查询失败分支；迁移流程禁止使用 |
| §6.3 probe guard | 真宿主查询 | **仅 rc=3** | rc=0 active、rc=4/其他、SSH 255、超时、缺命令、host-file 错误均退出 3 |
| §6.3 token | 配置结构 | enabled server + 非空 token，或 env 非空 token | 缺字段/空值退出 2，建连前停止 |
| §6.3 probe result | WS/actions/响应/UIN | 全部布尔断言真且 UIN 符合 | 异常或任一断言不符返回 1 |
| §6.5 `HERMES_RELEASE_GATE` | unit/NRestarts | active、0 | 查询失败/非预期 BLOCK |
| §6.5 | journal 证据 | 非空且 adapter/3011/account 三类证据均存在、connect_failed=0 | journal/grep 错误、缺证据、计数非 0 BLOCK |
| §6.5 | gateway pgrep | rc=0 且 count=1 | rc=1 视为 0 并 BLOCK；rc≥2 BLOCK；非 1 BLOCK |
| §6.6.1 `STABILITY_GATE` | 每次 systemctl/ss/pgrep | 查询成功且 active/0/1/1 | 任一失败或值异常令该样本 BLOCK |
| §6.6.1 | runtime JSON | 可读、结构齐全、state=connected、online 不为 false | 不可读/缺结构/状态异常退出 1 |
| §6.6.1 | 六次聚合 | 六次均 PASS | 任何一次失败令最终非零；后续正常样本不覆盖 |
| §6.6.3 `SIDEEFFECT_CHECK` | journal/行数/grep | journal 成功、非空、grep rc 0/1、send count=0 | 任一失败/非 0 BLOCK |
| §8.1.1 `T3_DEPLOY_GATE` | 代码/unit/timer/selftest/ledger | 每项明确成功 | 任一 `chk` 失败使最终 BLOCK |
| §8.1.2 `BASELINE_GATE` | timer 连续性查询 | show rc=0、active、monotonic 时间合法 | 查询/状态/时间戳异常 BLOCK |
| §8.1.2 | ledger 行 | JSON object，必填字段齐全，删失字段 bool，evidence 非空 | 任一坏行使整个 gate BLOCK |
| §8.1.2 | 样本数 | datacenter 合格记录 ≥10 | 不足 BLOCK |
| §8.1.2 | 连续天数 | timer 当前 active 代际 ≥7 天 | 不足 BLOCK；旧账本跨度不贡献 |
| §8.3.1 egress logger | curl/采集 | curl 存在、rc=0、响应像 IPv4 | 缺 curl exit 2；采集/格式失败写 unknown 并 exit 1 |
| §8.3.1 egress logger | unchanged | 已成功采集且等于上一已知值 | `exit 0`，这是“已确认无变化”，不是“没能确认” |

## 5. 验收标准逐条自查

| 验收项 | 判定 | 证据 |
|---|---|---|
| 修订前 `cp -a` UTC 备份 | **PASS** | `BACKUP_PATH=...bak-pre-rev2-20260831T115119Z`；`BACKUP_CMP_RC=0` |
| R1–R9 必修项 | **PASS** | 第 2 节逐条处置；最终静态检查 `R1_R9_STATIC_FAILURES=0` |
| 全类扫描并列每个闸门失败行为 | **PASS** | 第 3 节 X1–X10；第 4 节扫描矩阵 |
| R2 非 0 非 3 必须拒绝 | **PASS** | `R2_CASE=unknown_rc4 observed=3 expected=3 PASS=True` |
| R5 采集失败不得 rm | **PASS** | `logs_failure ... RM_CALLS=0 COMPLETE_MARKERS=0` |
| R6 `-- No entries --` 不得算日志 | **PASS** | `noentries observed=1 expected=1`，reason=`empty_log_window` |
| R7 早期失败不得被后续成功覆盖 | **PASS** | `first_bad_then_good ... STABILITY_GATE=BLOCK` |
| R8 旧记录不得贡献连续性 | **PASS** | 账本跨度 30 天、timer 1 天仍 `timer_continuous_days<7` |
| Docker unit 无活动 digest placeholder | **PASS** | `R9_UNIT_NO_ACTIVE_DIGEST_PLACEHOLDER=true`；`R9_DOCKER_UNIT_GATE=placeholder ... expected=1` |
| §8.4 未修改 | **PASS** | old/new SHA-256 均 `30452530b623b59e714446bc7245e1ecfc51cee85470b977ac5e9242a75557d6` |
| F2/F5/F15/F7 §8.2–8.3/F17 保护段未修改 | **PASS** | `PROTECTED_*_IDENTICAL=true` 六项 |
| 不修改 T3、不碰生产 | **PASS** | 无写工具调用指向 T3；全部行为测试使用 `/tmp` fake 命令；未执行真实 SSH |

## 6. 真实验证输出

### V1 备份

命令：修订前 `cp -a` + `cmp -s`。

```text
BACKUP_PATH=/Users/cornna/project/hermes-agent/docs/orchestration/tasks/T4-home-migration-runbook.md.bak-pre-rev2-20260831T115119Z
BACKUP_MODE=-rw-r--r-- BACKUP_SIZE=230986 BACKUP_MTIME=2026-08-31T04:15:20Z
BACKUP_CMP_RC=0
```

### V2 最终脚本语法/编译/Markdown

```text
FINAL_EXTRACTED=40
FINAL_COUNTS={'GATE': 10, 'ENV': 1, 'SH': 7, 'UNIT': 6, 'PY': 10, 'AK': 2, 'SSHD': 1, 'CHK': 2, 'CONF': 1}
FINAL_SHELL_CHECKED=19
FINAL_PY_CHECKED=10
FINAL_FENCE_COUNT=202
FINAL_FENCE_BALANCED=1
FINAL_SYNTAX_FAILURES=0
```

### V3 R1 端口与 unit 失败关闭

```text
R1_OLD_PORT_CASE=empty observed=0 expected=0 PASS=true
R1_OLD_PORT_CASE=fail observed=1 expected=1 PASS=true
R1_OLD_PORT_CASE=present observed=1 expected=1 PASS=true
R1_PROD_PORT_CASE=empty observed=0 expected=0 PASS=true
R1_PROD_PORT_CASE=fail observed=1 expected=1 PASS=true
R1_PROD_PORT_CASE=present observed=1 expected=1 PASS=true
UNIT_STATE=UNCHECKABLE(query failed: bus-failed)
R1_UNIT_QUERY_FAIL observed=1 expected=1 PASS=true
R1_BEHAVIOR_FAILURES=0
```

### V4 R2 真宿主 guard

```text
R2_CASE=active_rc0 observed=3 expected=3 PASS=True
R2_CASE=inactive_rc3 observed=0 expected=0 PASS=True
R2_CASE=unknown_rc4 observed=3 expected=3 PASS=True
R2_CASE=ssh_failure_rc255 observed=3 expected=3 PASS=True
R2_CASE=timeout observed=3 expected=3 PASS=True
R2_CASE=command_missing observed=3 expected=3 PASS=True
R2_CASE=host_file_missing observed=3 expected=3 PASS=True
R2_CASE=invalid_target observed=2 expected=2 PASS=True
R2_ORDER_GUARD_BEFORE_TOKEN observed=3 expected=3 PASS=True
R2_EMPTY_TOKEN_REFUSED observed=2 expected=2 PASS=True
R2_BEHAVIOR_FAILURES=0
```

### V5 R3 watchdog

```text
R3_CASE=pass observed=0 expected=0 PASS=true
R3_CASE=failed_state observed=0 expected=0 PASS=true
R3_CASE=absent observed=0 expected=0 PASS=true
R3_CASE=unknown observed=1 expected=1 PASS=true
R3_CASE=queryfail observed=1 expected=1 PASS=true
R3_BEHAVIOR_FAILURES=0
```

### V6 R4/R8 baseline

```text
TIMER_CONTINUOUS_ACTIVE_DAYS=8.00 (need >=7)
LEDGER_SPAN_DAYS_DIAGNOSTIC=30.00 (not a gate)
BASELINE_GATE=PASS
BASELINE_CASE=pass observed=0 expected=0 PASS=true

TIMER_CONTINUOUS_ACTIVE_DAYS=1.00 (need >=7)
LEDGER_SPAN_DAYS_DIAGNOSTIC=30.00 (not a gate)
BASELINE_GATE=BLOCK reason=timer_continuous_days<7
BASELINE_CASE=old_ledger_cannot_fake_continuity observed=1 expected=1 PASS=true

line 1: missing:end_censored
BASELINE_CASE=missing_end_censored observed=1 expected=1 PASS=true
line 1: missing:account_evidence
BASELINE_CASE=missing_account_evidence observed=1 expected=1 PASS=true
line 1: invalid:censored_flags_must_be_bool
BASELINE_CASE=invalid_end_type observed=1 expected=1 PASS=true
BASELINE_CASE=timer_query_rc4 observed=1 expected=1 PASS=true
BASELINE_CASE=timer_inactive observed=1 expected=1 PASS=true
BASELINE_BEHAVIOR_FAILURES=0
```

### V7 R5 archive-before-remove

```text
archive: ABORT: logs capture failed (partial archive kept at ...)
R5_CASE=logs_failure observed=1 expected=1 RM_CALLS=0 COMPLETE_MARKERS=0 PASS=true
archive: saved complete evidence to ...
archive: removed container 'napcat' (evidence kept in ...)
R5_CASE=complete_archive observed=0 expected=0 RM_CALLS=1 COMPLETE_MARKERS=1 PASS=true
R5_BEHAVIOR_FAILURES=0
```

### V8 R6 kicked journal

```text
R6_CASE=clean observed=0 expected=0 PASS=true
KICKED_CHECK=BLOCK reason=empty_log_window (读不到日志 ≠ 没有 kicked)
R6_CASE=noentries observed=1 expected=1 PASS=true
KICKED_CHECK=BLOCK reason=journalctl_failed
R6_CASE=fail observed=1 expected=1 PASS=true
KICKED_CHECK=BLOCK reason=kicked_seen
R6_CASE=kicked observed=1 expected=1 PASS=true
R6_BEHAVIOR_FAILURES=0
```

### V9 R7 采样聚合

```text
R7_CASE=all_valid observed=0 expected=0 PASS=true
state=UNREADABLE(JSONDecodeError)
STABILITY_GATE=BLOCK
R7_CASE=unreadable_all observed=1 expected=1 PASS=true
state=UNREADABLE(SimulatedFirstSample)
SAMPLE_1=BLOCK
SAMPLE_2=PASS
STABILITY_GATE=BLOCK
R7_CASE=first_bad_then_good observed=1 expected=1 PASS=true
R7_BEHAVIOR_FAILURES=0
```

### V10 额外 preflight 失败开放

```text
EXTRA_ROOT_PREFLIGHT=clean observed=0 expected=0 PASS=true
EXTRA_ROOT_PREFLIGHT=grep_error observed=1 expected=1 PASS=true
EXTRA_ROOT_PREFLIGHT=container_present observed=1 expected=1 PASS=true
EXTRA_ROOT_PREFLIGHT=docker_query_fail observed=1 expected=1 PASS=true
EXTRA_PORT_PREFLIGHT=empty observed=0 expected=0 PASS=true
EXTRA_PORT_PREFLIGHT=ss_failure observed=1 expected=1 PASS=true
EXTRA_PORT_PREFLIGHT=listener_present observed=1 expected=1 PASS=true
PREFLIGHT_BEHAVIOR_FAILURES=0
```

### V11 R9 digest、unit 与路径对称

```text
R9_DIGEST_GATE=valid observed=0 expected=0 PASS=true
R9_DIGEST_GATE=malformed observed=1 expected=1 PASS=true
R9_DIGEST_GATE=fail observed=1 expected=1 PASS=true
R9_IMAGE_GUARD=valid observed=0 expected=0 PASS=true
R9_IMAGE_GUARD=mutable_tag observed=1 expected=1 PASS=true
R9_IMAGE_GUARD=missing observed=1 expected=1 PASS=true
R9_IMAGE_GUARD=inspect_failure observed=1 expected=1 PASS=true
R9_DIGEST_BEHAVIOR_FAILURES=0

R9_DOCKER_UNIT_GATE=clean observed=0 expected=0 PASS=true
R9_DOCKER_UNIT_GATE=placeholder observed=1 expected=1 PASS=true
R9_DOCKER_UNIT_GATE=missing_guard observed=1 expected=1 PASS=true
R9_DOCKER_UNIT_GATE=cat_failure observed=1 expected=1 PASS=true
R9_DOCKER_UNIT_GATE_FAILURES=0

R9_UNIT_NO_ACTIVE_DIGEST_PLACEHOLDER=true
R9_UNIT_EXACT_DIGEST_ENV=true
R9_UNIT_IMAGE_GUARD=true
R9_UNIT_PORT_PREFLIGHT=true
R9_UNIT_SHARED_FLOCK=true
R9_UNIT_IMAGE_VARIABLE=true
R9_UNIT_NO_ACTIVE_DOCKER_RM=true
R9_UNIT_RESTART_NO=true
UNIT_STRUCTURE_FAILURES=0

R9_SYMMETRY_STARTUP=1
R9_SYMMETRY_KICKED_UNIT_BRANCH=1
R9_SYMMETRY_FLOCK=2
R9_SYMMETRY_ROLLBACK=1
```

### V12 Hermes release 与 reverse-tunnel placeholder

```text
EXTRA_HERMES_RELEASE=clean observed=0 expected=0 PASS=true
EXTRA_HERMES_RELEASE=fail observed=1 expected=1 PASS=true
EXTRA_HERMES_RELEASE=empty observed=1 expected=1 PASS=true
EXTRA_HERMES_RELEASE=connect_failed observed=1 expected=1 PASS=true
HERMES_RELEASE_BEHAVIOR_FAILURES=0

EXTRA_RTUNNEL_PLACEHOLDER=replaced observed=0 expected=0 PASS=true
EXTRA_RTUNNEL_PLACEHOLDER=placeholder observed=1 expected=1 PASS=true
EXTRA_RTUNNEL_PLACEHOLDER=missing observed=1 expected=1 PASS=true
RTUNNEL_GATE_BEHAVIOR_FAILURES=0
```

### V13 最终 R1–R9 静态验收与保护范围

```text
R1_NO_OLD_SS_GREP_COUNT=true
R1_UNCHECKABLE_PORT_BRANCH=true
R2_TRUE_HOST_MODE=true
R2_NO_NOT_APPLICABLE=true
R2_ONLY_RC3_ALLOWS=true
R2_RC4_TEST_PRESENT=true
R3_UNKNOWN_NOT_PASS=true
R4_END_CENSORED_REQUIRED=true
R4_DIRECT_BOOLEAN_CHECK=true
R5_ARCHIVE_NO_SWALLOW=true
R5_COMPLETE_BEFORE_RM=true
R6_QUIET_JSON_JOURNAL=true
R7_UNREADABLE_NONZERO=true
R7_FINAL_AGGREGATE=true
R8_MONOTONIC_CONTINUITY=true
R8_LEDGER_SPAN_NOT_GATE=true
R9_NO_DOCKER_UNIT_PLACEHOLDER=true
R9_IMAGE_GUARD=true
R9_DOCKER_STARTUP=true
R9_DOCKER_ROLLBACK=true
PROBE_HOME_SHELL_GUARD_LINES=1
PROBE_LOCAL_SHELL_GUARD_LINES=3
SECTION_8_4_IDENTICAL=true
R1_R9_STATIC_FAILURES=0
```

保护段：

```text
PROTECTED_F2_identity_IDENTICAL=true
PROTECTED_F5_webui_IDENTICAL=true
PROTECTED_F15_permitlisten_IDENTICAL=true
PROTECTED_F7_8_2_8_3_IDENTICAL=true
PROTECTED_SECTION_8_4_IDENTICAL=true
PROTECTED_F17_APPENDIX_A_IDENTICAL=true
SECTION_8_4_OLD_SHA256=30452530b623b59e714446bc7245e1ecfc51cee85470b977ac5e9242a75557d6
SECTION_8_4_NEW_SHA256=30452530b623b59e714446bc7245e1ecfc51cee85470b977ac5e9242a75557d6
```

## 7. 中间失败与根因（未隐藏）

| 失败 | 根因 | 处置 |
|---|---|---|
| baseline 首轮“8 天 PASS”得到 `timer_timestamp_invalid` | 本机本次开机不足 8 天，夹具用真实 monotonic 时钟减 8 天得到负值；脚本正确阻断 | 固定 fake monotonic 为 40 天后重跑，7 个分支全过；未改业务逻辑 |
| R9 首轮 valid digest 失败：`unknown group root` | macOS 本地测试没有 Linux `root` 组名 | 仅测试夹具去掉 `-o root -g root`，目标 Linux 命令保留 |
| R9 首轮 image guard：`mapfile: command not found` | macOS Bash 3.2 无 `mapfile` | runbook 改为 fd + `read` 单行校验；重跑全过 |
| Hermes release 首轮全为 rc=127 | 章节移动后临时提取文件名从 `L3415` 变成 `L3419`，测试引用旧路径 | 修夹具路径，4 分支全过；未改 gate |
| 最终静态检查两次假 FAIL | 首次断言期待字面 `LISTEN_3011=UNCHECKABLE`，但代码是通用 `$label`；随后全文计数把 usage 行算作 probe 调用 | 改成函数体+调用点、shell 参数行断言；最终 `R1_R9_STATIC_FAILURES=0` |

## 8. 未完成项与验证边界

- **文档修订未完成项：0。**
- **生产验证未执行**：按任务禁止，不 SSH、不碰生产、不扫码。因此 runbook 中真实主机上的 systemd 状态、端口、扫码、OneBot 响应、timer 连续天数和 Docker digest 仍必须在实际执行时按闸门输出验证。
- **T3 实现/部署不在本任务范围**：`T3_DEPLOY_GATE`/`BASELINE_GATE` 仍会在实际 T3 未就绪时正确阻断迁移。
- **F15 与 §8.4 保持原样**：即使扫描时看到其中人工命令形态，也未越权修改；保护段哈希已给出。
