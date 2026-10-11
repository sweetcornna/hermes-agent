# QQ 空间每日图文发布：当前约定

2026-09-09 更新，目标是当前 P2 的 `hermes-cloud`，不是已退役广州云。

## 用户要求

- 每次三张独立图片；三个不同日常活动与地点，不是同一场景换镜头。
- 每张必须有格兰本人。其他同学根据当天情节选择对应立绘。
- 格兰是橙色虎兽人；脸型、虎纹、毛色、体型、角色身份和画风保持一致。
- 不强制每张照搬立绘制服。每组三张至少两张采用更近、更有冲击力的运动写真构图，允许赤膊、敞开上衣、腹肌与胸肩肌肉展示；下身可穿短裤、泳裤或简洁运动服，只要隐私部位完全遮挡，不涉及性或色情。
- 表情按场景变化（兴奋、专注、放松等），头部与颈部、肩部保持连续的毛发/光影和正确比例，禁止贴图感、断颈或漂浮头。
- 姿态、透视、光影与构图有冲击感；每日变化，不能把示例活动当固定日程。
- 生图模型使用 `gpt-image-2.5-sunburst`。

## 实现

- `plugins/corlinman_jobs/prompts.py` 是任务提示的源文件，要求先规划三个不同片段，再写正文与三条画面描述。
- `plugins/qzone/publish.py` 的每日分支为显式画面描述和自动补图都添加角色与画面约束。显式描述必须有三条。
- `reference_characters` 支持每次选择不同同学，daily 路径始终在首位补入 `grantley`，最多再选两位。显式参考图缺失时停止，不能换成无参考生成。
- 日常任务图片审核拒绝时不降级成纯文字发布；其他普通发布调用保留原有语义。
- Cornna provider 的参考图指令保留脸型、虎纹、体型约束，但允许场景明确要求的服装变化。
- 生产任务以 CLI 的结构化 argv 更新，提示包含真实换行；此前写入的字面 `\\n` 已清除。

## 生产状态与验证

- SSH：`ssh -o ProxyJump=none cornna-p2-root`。
- 网关代码/数据：宿主 `/opt/hermes-cloud/repo`、`/opt/hermes-cloud/data`；容器对应 `/opt/hermes/repo`、`/opt/hermes/data`。
- 任务 `hermes.qzone_daily`，ID `4e19ad70a46c`，北京时间每天 22:21，启用。文本模型仍为 `gemini-3.8-flash-high`。
- `image_gen.model` 和 `image_gen.cornna.model` 均为 `gpt-image-2.5-sunburst`；QZone 默认参考角色仅 `grantley`，同学通过每次参数选择。
- 代码、配置与任务备份：`/opt/hermes-cloud/data/.deployments/qzone-art-direction-v2-20260909/backup/`。
- 相关测试：`tests/plugins/qzone`、`tests/plugins/corlinman_jobs/test_specs.py`、`tests/plugins/image_gen/test_cornna_provider.py` 共 **512 passed**。
- 新方向做了三次真实参考图生成，使用格兰/保尔/提奥立绘，三张均为 1672×941，耗时约 66.3/78.9/71.2 秒。已逐张看图：训练、河边活动、庭院聚餐，格兰均在且肌肉自然可见。
- 预览与结果：容器 `/opt/hermes/data/cache/qzone-previews/art-v2-20260909/`；本机 `/Users/cornna/Documents/Codex/2026-09-09/qzone-art-preview/`。
- 这些新方向预览未发布到 QQ。修改前最后一次真实三图发布的工具回执是 `success=true, images=3`，tid `5c10fcaf87cca06aff1f0900`；不能把该旧回执当作新方向的发布验收。
- 参考约束是生成输入与资产验证，不是对未来每次出图相似度的保证。多日差异仍需实际运行观察。
