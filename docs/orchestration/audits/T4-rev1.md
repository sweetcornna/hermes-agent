# T4 rev-1 聚焦复审

审计员：gpt-auditor（只读）。**该 agent 输出了分析与 9 条候选问题，但未产出最终 Q1–Q4 表格与判定。**
主线程独立验证了其中 4 条最关键的候选，全部证实，据此直接给出判定。

**总判定：打回。**

## 根因：上一轮修的是「被点名的那几行」，不是「失败开放这一类」

上一轮 F3 / F4 的实质是**失败开放**——检查失败被解释成安全状态。
rev-1 在被点名的位置修对了，却在**新写的代码里重新引入同一模式**。
证据是同一个脚本内部的自相矛盾：

```bash
# ✅ 失败关闭：显式处理 rc
probe_pgrep() { ... *) say "$label=UNCHECKABLE(pgrep rc=$rc)"; return 1 ;; esac }

# ✅ 失败关闭：注释里甚至写明了原则
# (3f) 无 napcat 容器在跑。docker 存在但查不了 ⇒ 阻断，不当成「没有容器」
if dps=$(sudo -n docker ps --format '{{.Names}}' 2>/dev/null); then

# ❌ 失败开放：同一个脚本，同一个闸门
n3011=$(ss -ltnH 2>/dev/null | grep -c '127\.0\.0\.1:3011')
[ "$n3011" -eq 0 ] || r=1
```

作者**知道**这条原则（3f 的注释就是它），却没有把它当成一个类来扫。

---

## 已证实的失败开放（主线程逐条验证）

### R1 §3.5 放行闸的两个端口检查失败开放 —— 致命
`T4:848-855`

```bash
n3011=$(ss -ltnH 2>/dev/null | grep -c '127\.0\.0\.1:3011')
[ "$n3011" -eq 0 ] || r=1
```

脚本用 `set -uo pipefail` 但**没有 `set -e`**，且未检查 `ss` 的退出状态。
`ss` 不存在 / 无权限 / 执行失败 ⇒ `2>/dev/null` 吞掉错误 ⇒ 空输入 ⇒ `grep -c` 打印 `0`
⇒ `n3011=0` ⇒ `[ 0 -eq 0 ]` **通过**。6099（3d）同一模式。

**这正是 F3 指出的原始缺陷**：一个「没有 `napcat-native` 用户进程但仍持有 3011/6099」的残留
可以让闸门看起来通过，随后新机启动就产生同号并行登录（硬约束 3）。
**为修 F3 而新增的检查，自己是失败开放的。**

对照：同一脚本的 3e（cgroup）用 `|| echo -1` 做到了失败关闭，是对的。只有 3c/3d 漏了。

### R2 Hermes 探针守门在状态未知时放行，且在最可能运行它的机器上结构性失效 —— 致命
`T4:2723-2735`

```python
rc = subprocess.run(["systemctl","is-active","--quiet","hermes.service"], timeout=10).returncode
...
if rc == 0: raise SystemExit(3)
print("PROBE_GUARD=ok(hermes_not_active)")
```

**问题 A**：只有 `rc == 0` 阻断。`systemctl is-active` 的非零返回并不只表示「未运行」——
权限错误、D-Bus 故障、systemd 查询失败同样非零，**全部被解释成「Hermes 没在跑」并放行建连**。

**问题 B（主线程追加，审计员未指出，比 A 更严重）**：
`FileNotFoundError` 分支的注释自己写着「本机没有 systemctl（**例如 home 上跑**）⇒ 这条闸不适用」，
然后 `return`（放行）。**但 Hermes 跑在 `corlinman-prod`，不在 `home`。**
⇒ 从 `home` 运行探针时，这条闸**永远** `not_applicable`，从不阻断。
守门检查的是本机的 `hermes.service`，而它要防的 Hermes 在另一台机器上。
**这条闸在最可能被使用的场景里结构性失效。**

D-QQ-13 已经把「Hermes 运行时不得开第二条 WS」定为**必须写成可测试断言**，
理由正是口头约定已被证明无效（T3 与 T4 独立犯了同一个错）。现在断言写了，但断言本身是空的。

### R3 watchdog 闸把「查不出来」当成合格 —— 高
`T4:673-679`

```bash
case "$act" in inactive|failed|unknown) ;; *) echo "  ^ NOT INACTIVE"; bad=1 ;; esac
```

`systemctl is-active` 在无法确定状态时返回 `unknown`。把它列进通过分支
= 把「查不了」当成「已停」。`failed` 尚可辩（masked+failed 确实没在跑），**`unknown` 不行**。

注：`act=$(systemctl is-active "$u" 2>&1)` 把 stderr 收进变量，权限错误信息会落到 `*)` 分支置 `bad=1`，
这部分是对的。问题只在 `unknown` 这个显式取值。

### R4 baseline 闸漏了 `end_censored` 必填校验 —— 高
`T4:3372-3377` 与下游 `not r.get("end_censored")`

```python
missing = [k for k in ("schema", "duration_s", "epoch", "start_censored") if k not in obj]
```

`end_censored` **不在必填列表里**，下游却用 `not r.get("end_censored")` 判断是否删失。
缺该字段 ⇒ `get` 返回 `None` ⇒ `not None` 为 `True` ⇒ **被计入非删失区间**
⇒ 虚增「≥10 段非删失」的计数 ⇒ **baseline 闸在证据不完整时放行迁移**。

而迁移是一次性、不可逆、需要人在场的动作（D-QQ-02）。

---

## 审计员提出、主线程未逐条验证的其余候选

以下 5 条来自审计员分析，主线程**未独立验证**，但模式与已证实的四条同类，修订时应一并核查：

| # | 位置 | 问题 |
|---|---|---|
| R5 | `T4:1701-1708` | Docker archive 用 `\|\| true` / `\|\| echo` 吞掉备份采集失败，随后仍执行破坏性 `docker rm`（硬约束 11） |
| R6 | `T4:2641-2659` | kicked 检查用 `wc -l` 判断日志是否存在；`journalctl` 无日志时输出 `-- No entries --` 会让行数 >0，随后 grep 计数 0 ⇒ `KICKED_CHECK=PASS`（F11 残留） |
| R7 | `T4:3005-3007` | runtime state 不可读时 `SystemExit(0)`，外层六次采样循环继续且返回成功 |
| R8 | `T4:3396-3398, 3429-3435` | 「哨兵连续运行 ≥7 天」实为最早 `started_at` 到当前的账本跨度，旧记录也能贡献跨度，不能证明中途没断（文档自己承认了） |
| R9 | `T4:1588-1628` | Docker fallback unit 写 `@sha256:REPLACE_WITH_REPO_DIGEST_FROM_STEP_1_6` 占位符，未确认存在强制替换步骤；且 §6/互斥/回滚/一页纸清单主要围绕 native 编写，两条路径可能不对称 |

## 已确认修对的部分（不要在修订中动它们）

F2（身份目录硬阻断）、F5（token 只输出存在性）、F7（采用 T3 契约、marker 移出 `sessions.jsonl`）、
F9（删除 unit 内 `docker rm -f`）、F10（`napcat-native.service` 换算表）、F12、F13、F14、F15、F17
均已验证方向正确。F1 按主线程指令保留 `Restart=on-failure` + `StartLimitBurst=1`，并新增了
「不 enable 开机自启」的论证（理由：`Burst=1` 下无人值守的开机尝试会**偷走人工的那一次机会**）——
这个论证成立，采纳。

主线程另核实：文档中大量 `⛔ 上一版…已删除` / `⚠️ 上一版在这里…` 说明块保留了错误示例文本
（被删的 token 命令、被否决的 schema、字面反斜杠管道）作为反面记录，**属正常，不是残留缺陷**。

## rev-1 的诚实声明（保留为已知边界）

作者明确声明：31 个嵌入代码块通过语法/编译检查、分支行为用假 `docker`/`pgrep`/`systemctl`/`curl`
注入测过，但**所有「预期输出」值仍是未经真机验证的断言**。
本次审计证明了这一声明的必要性：**语法通过与假注入测试都不能证明闸门失败关闭**——
R1/R2 的代码都能正常跑通，只是在失败路径上放行。
