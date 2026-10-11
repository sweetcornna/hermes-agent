# T4 rev-2 修订单（第二次打回 → 升级到 Sol xhigh）

审计原文：`docs/orchestration/audits/T4-rev1.md`（含主线程逐条验证与一条追加发现）。

## 这次的任务不是「修 4 个 bug」，是「扫一类模式」

上一轮 F3 / F4 的实质是**失败开放**：检查失败被解释成安全状态。
rev-1 在被点名的位置修对了，却在**新写的代码里重新引入同一模式**。
同一个脚本内部就自相矛盾——`probe_pgrep` 和 docker 检查是失败关闭的（注释里甚至写明了原则：
「docker 存在但查不了 ⇒ 阻断，不当成没有容器」），紧挨着的两个端口检查却是失败开放的。

**所以：不要只改下面点名的那几行。把 runbook 里所有闸门脚本按这个类扫一遍。**

扫描清单（每一处都要能回答「这条检查失败时会发生什么」）：
- 所有 `X=$(cmd ...)` 形式的命令替换——`cmd` 失败时 `X` 是什么？后续判断会不会因此通过？
- 所有 `2>/dev/null`——它吞掉的是预期内的噪声，还是「命令不存在 / 无权限」这类必须阻断的错误？
- 所有 `|| true`、`|| echo <安全值>`、`|| :`
- 所有 `case` 语句的通过分支——有没有把 `unknown` / 空值 / 查询失败列进去？
- 所有 `exit 0` / `SystemExit(0)`——是「确认没问题」还是「没能确认」？
- 所有只判断 `rc == 0` 或只判断 `rc != 0` 的地方——中间那些返回码意味着什么？
- 所有 `dict.get(k)` 用于安全判断的地方——`k` 缺失时的默认值会不会导致放行？

在结果文件里给出**扫描结果表**：每个闸门脚本 × 每条检查 × 失败时的行为（阻断/放行）。
已经是失败关闭的也要列出来，证明你扫过了。

## 已证实的四条（致命/高，必修）

### R1 §3.5 端口检查失败开放 —— 致命（`T4:848-855`）
```bash
n3011=$(ss -ltnH 2>/dev/null | grep -c '127\.0\.0\.1:3011')
[ "$n3011" -eq 0 ] || r=1
```
`ss` 不存在/无权限/失败 ⇒ 空输入 ⇒ `grep -c` 打印 `0` ⇒ 判定通过。6099 同。
**这正是 F3 指出的原始缺陷——为修 F3 而新增的检查自己是失败开放的。**
参照同一脚本 3e（cgroup）的 `|| echo -1` 写法，那个是对的。

### R2 Hermes 探针守门 —— 致命（`T4:2723-2735`）
两个独立问题：
- **A**：只有 `rc == 0` 阻断。权限错误、D-Bus 故障、systemd 查询失败同样非零，
  **全被解释成「Hermes 没在跑」并放行建连**。
- **B（更严重）**：`FileNotFoundError` 分支注释自己写着「本机没有 systemctl（例如 home 上跑）⇒
  这条闸不适用」然后放行。**但 Hermes 跑在 `corlinman-prod`，不在 `home`。**
  从 `home` 运行探针时这条闸**永远** `not_applicable`，从不阻断。
  **守门检查的是本机的 `hermes.service`，而它要防的 Hermes 在另一台机器上。**

D-QQ-13 已把「Hermes 运行时不得开第二条 WS」定为必须写成**可测试断言**
（理由：口头约定已被证明无效，T3 与 T4 独立犯了同一个错）。现在断言写了，但断言是空的。
**修法必须让守门检查真正的 Hermes 宿主**，并且无法确认时**阻断**而不是放行。
补一条测试：模拟 systemctl 返回非 0 非 3 的错误码时，探针必须拒绝建连。

### R3 watchdog 闸把「查不出来」当成合格 —— 高（`T4:673-679`）
`case "$act" in inactive|failed|unknown) ;;` —— `unknown` 是「查不了」，不是「已停」。
`failed` 尚可辩（masked+failed 确实没在跑），**`unknown` 必须移出通过分支**。

### R4 baseline 闸漏了 `end_censored` 必填校验 —— 高（`T4:3372-3377`）
必填列表是 `("schema","duration_s","epoch","start_censored")`，**漏了 `end_censored`**，
下游却用 `not r.get("end_censored")` 判断删失。缺字段 ⇒ `None` ⇒ `not None` = True
⇒ **被计入非删失区间** ⇒ 虚增「≥10 段」⇒ baseline 闸在证据不完整时放行迁移。
而迁移是一次性、不可逆、需要人在场的动作。

## 审计员提出但主线程未独立验证的五条（一并核查）

- **R5**（`T4:1701-1708`）Docker archive 用 `|| true` / `|| echo` 吞掉备份采集失败，
  随后仍执行破坏性 `docker rm`（硬约束 11）。
- **R6**（`T4:2641-2659`）kicked 检查用 `wc -l` 判断日志存在性；`journalctl` 无日志时输出
  `-- No entries --` 会让行数 >0，随后 grep 计数 0 ⇒ `KICKED_CHECK=PASS`（F11 残留）。
- **R7**（`T4:3005-3007`）runtime state 不可读时 `SystemExit(0)`，外层采样循环继续且返回成功。
- **R8**（`T4:3396-3398, 3429-3435`）「哨兵连续运行 ≥7 天」实为账本跨度近似，旧记录也能贡献跨度，
  不能证明中途没断（文档自己承认了）。要么换成真能证明连续性的判据，要么把这条限制说清楚
  并降低它在闸门里的权重。
- **R9**（`T4:1588-1628`）Docker fallback unit 写 `@sha256:REPLACE_WITH_REPO_DIGEST_FROM_STEP_1_6`
  占位符，未确认存在强制替换步骤；且 §6/互斥/回滚/一页纸清单主要围绕 native 编写，
  两条路径可能不对称。

## 不许动的部分

F2（身份目录硬阻断）、F5（token 只输出存在性）、F7（T3 契约、marker 移出 `sessions.jsonl`）、
F9（删除 unit 内 `docker rm -f`）、F10（`napcat-native.service` 换算表）、F12、F13、F14、F15、F17
已验证方向正确。

**F1 保持现状**：`Restart=on-failure` + `RestartSec=45s` + `StartLimitBurst=1` 保留；
「不 enable 开机自启」的论证（`Burst=1` 下无人值守的开机尝试会**偷走人工的那一次机会**）成立，采纳。

文档里 `⛔ 上一版…已删除` / `⚠️ 上一版在这里…` 的说明块**保留**——
主线程已核实那些是反面记录，不是残留缺陷。不要为了让 grep 干净而删掉它们。

**⛔ 不要碰 §8.4 及任何依赖 T3 统计判定的内容**（rule-of-three、迁移后判定表、
`exposure`/`failures` 口径）。`T3-design.md` 正在被另一个 agent 并行修订，那部分稍后统一对账。
账本的**字段名与命令形态**（`epoch`、`schema`/`duration_s`、`report --compare`）仍要保持。

## 交付

- 修订前 `cp -a docs/orchestration/tasks/T4-home-migration-runbook.md docs/orchestration/tasks/T4-home-migration-runbook.md.bak-pre-rev2-$(date -u +%Y%m%dT%H%M%SZ)`
- 就地修订 runbook
- `docs/orchestration/results/T4-rev2.md`：R1–R9 逐条处置 + **失败开放扫描结果表**
- 不要动 `docs/orchestration/tasks/T3-design.md`
- 边写边落盘
