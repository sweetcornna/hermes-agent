# QQ 原生桥最小加固

2026-09-07，按用户最新要求保留现有原生桥，Hermes 继续使用原有 OneBot
平台插件、配置和发送链路。本次不实施此前的外部界面桥迁移方案。

## 实际改动

- 将现有 QQ 容器的 Docker 重启策略从 `unless-stopped` 改为 `no`，避免
  容器异常退出或宿主机重启后自动拉起 QQ。
- 在现有 `/etc/supervisord.conf` 的 `[program:qq]` 段设置
  `autostart=false`、`startretries=0`，保留原有 `autorestart=false`。
- 确认 Supervisor 管理的 QQ 槽位为 `EXITED` 后，只更新该槽位的配置，
  更新后为 `STOPPED`。当前实际运行的 QQ 为此前人工启动的进程；更新前后
  所有 QQ PID 与进程启动时间完全一致。
- 保留独立 OneBot 网关的自动恢复：它重连本地管道，不负责 QQ 登录。

QQ、Hermes 均未重启，未调用登录、发送消息或业务探测接口，未变动会话、
人格、群白名单、三秒合并输入、气泡设置及持久化发送记录。
结果见 [result.json](result.json)。

## Hermes 与原型清理

本轮新增的 `desktop_qq` 接线已经撤回。OneBot `adapter.py`、`client.py` 和
`native_backend.py` 与本轮开始前的精确副本逐一核对 SHA-256，完全一致；
用户原先未提交的改动保留。

四个相关测试文件（原生桥接口、后端身份、引用回复、发送入口保护）通过
仓库 `scripts/run_tests.sh` 运行，**59 passed，0 failed**。

未完成的 UI 原型已移入本地 `snowluma-diagnostics/shelved-qq-ui-20260907`，
标记禁止部署；云端本轮新增的桌面测试镜像和暂存目录已删除，没有遗留运行服务。

## 已核实的现有保护与边界

- QQ 进程为 UID 1000，`CapEff=0`、`NoNewPrivs=1`，未配置 `LD_PRELOAD`。
- 未配置额外 QQ HOME；旧 QQ watchdog 被 mask，旧 NapCat 服务 disabled。
- OneBot、WebUI、VNC 的宿主机端口绑定到 `127.0.0.1`。
- 当前 QQ 界面显示登录失效，Hermes 正确报告 `auth_required`；本次没有自动重登。
- 当前容器仍有 `CapAdd=[SYS_PTRACE]` 和 `seccomp=unconfined`。修改这些选项
  需要重建容器；它们**没有在本次去除**，也没有宣称 seccomp 已生效。
  下次计划内重建应先以默认 seccomp、无 `SYS_PTRACE` 做隔离验证，保留相同
  账号数据、设备身份和版本，验收后再应用。
- 现有桥仍通过修改 QQ 启动入口与 Hook 调用内部接口；这是非腾讯授权的
  接入。关闭自动重启/重试只减少可控的额外启动行为，不能保证消除外挂类风控。

## 恢复

服务器私有备份：
`/opt/hermes-cloud/data/.deployments/native-minimal-hardening-20260907/`。
包含原 Supervisor 配置、Docker 重启策略和容器身份。
仅在需要撤销本次加固时恢复这两项；恢复配置不会恢复已经失效的 QQ 登录。
备份不包含也不覆盖聊天数据库。
