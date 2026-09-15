# 格兰二代人格生产部署

已于2026-09-06部署到 `cornna-cn` 的 `hermes-cloud` 容器。

- 更新 `persona.py`、`channel_binding.py`、人格Markdown及生活种子，仓库和安装目录共8个目标文件。
- 四个文件均与本地验证版本一致；稳定人格3072字符。
- 默认模型已配置为 `gemini-3.8-flash-high`；3次真实API探测成功，返回的基础模型名为 `gemini-3.8-flash`。本次无需重写已有正确模型配置。
- 暂存和安装后的真实插件导入、身份作用域、跨日稳定与状态块验证均通过。
- 容器已重启，网关running，Telegram connected。QQ在部署前后均为auth_required，需重新登录才能验证QQ真实收发。
- 保留配置、SOUL和会话历史。既有会话的冻结身份保留，新会话采用新版。

备份：`/opt/hermes-cloud/data/.deployments/grantley-kc2-20260906T125614Z/backup`。

回滚仅恢复此次8个文件，可在主机执行该部署目录的 `rollback.py`，随后重启 `hermes-cloud` 容器。它不回写配置或历史。脚本、暂存检查、安装检查及3.8探测结果均在本目录留档。
