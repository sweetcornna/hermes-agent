# Orchestration Plan — 个人 QQ 接入稳定性与抗风控

来源计划：`~/.claude/plans/purring-shimmying-leaf.md`（2026-08-31 用户已批准）

## 结论（一句话）

**协议端不换。** NapCat 是唯一健在且无外部鉴权依赖的真客户端方案；所有替代品要么已归档
（Lagrange.Core 2025-10-12、go-cqhttp）、要么依赖第三方签名服务（LLBot/PMHQ/Lagrange）、
要么本项目已实测排除（SnowLuma）。真正的杠杆在**出口网络、登录尝试次数、已写好但未上线的代码**。

## 批次与状态

| 批 | 任务 | 轨道/档位 | 状态 |
|---|---|---|---|
| B0 | agent 定义 + 骨架 + 冒烟 | 主线程 | 进行中 |
| B1 | T1 account-state 加固核对 | gpt-luna-exec | **通过**（第 2 轮） |
| B1 | T2 限流参数建议表 | gpt-luna-exec | **通过** |
| B1 | T3 告警 + 会话时长采集设计 | opus | **通过**（第 2 轮，1034 行） |
| B2 | T4 家宽迁移 runbook | opus | **待审计**（2300 行） |
| B2 | T5 审计 T4 / T5b 审计 T3 | gpt-auditor ×2 | 运行中 |
| B3 | T7 主窗口豁免入站限流 | 待定 | 待派（见 D-QQ-06） |
| B3 | 生产变更（需用户逐步放行） | 主线程 + 用户 | 阻塞于 B1/B2 |
| B4 | ~~T6 会话时长采集器~~ **已并入 T3** | — | 见 D-ORCH-02 |

## 并行隔离

- T1 / T2 / T3 无文件交集：T1 只读 + 写 `results/T1.md`；T2 写 `results/T2.md` +
  `docs/migration-corlinman/B3-hermes-config.yaml.example`；T3 只写 `tasks/T3-design.md`。可并发。
- T4 依赖无；T5 依赖 T4 完成。

## 实测修正（2026-08-31，见 decisions.md D-QQ-04 / D-QQ-05）

- **拓扑漂移**：线上 NapCat 已从 Docker 改为原生 AppImage（`napcat-native.service`），
  H1 §4 的拓扑表不再是现状。已把事实发给在跑的 T3/T4。
- **L2 已达成**：活链路上没有密码登录，只剩一个惰性残留文件待清理。B3 第 1 步降级为卫生清理。
- **L3 确认仍成立**：`corlinman-prod:/opt/hermes/repo` 实测停在 `8911e2e0`，account-state 加固确实没上线。
- **L5 确认缺口真实**：`napcat-native-health` 对告警关键词 grep 命中数为 0，且它所在的 `cornna-cn`
  发不出 Telegram（curl 000），而 `corlinman-prod` 可以（302）。

## 最终依赖链（见 D-QQ-09）

```
T1（整包部署，14 个文件）
  └─> T3（哨兵上线，把发现延迟从 9.7h 压到分钟级）
        └─> baseline 采样（≥7 天 且 ≥10 段非删失区间）
              └─> T4（家宽迁移，一次性不可逆）
                    └─> 两周对比中位会话时长 → 判定住宅 IP 是否有效
```

**L1 是最大的杠杆，但有 3 周以上前置期**——因为它不可逆且只有一次机会，
必须先有能证伪它的观测能力。

不被阻塞、随时可做：L4 的三个真·群向开关；T7（主窗口豁免入站限流）。
