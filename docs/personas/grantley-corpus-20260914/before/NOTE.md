# `before/` 里各文件的真实身份

⚠️ **`grantley.md.p2-deployed` 的文件名是错的**，但为保持与 `SHA256SUMS.txt`、
三臂采样产物（臂名 `p2_current`）以及 README 正文的可追溯性，**不改名**。

| 文件 | sha256 前缀 | 真实身份 |
|---|---|---|
| `grantley.md` | `279adcee…` | 2026-09-07 已验收版。**这才是 p2 线上在跑的那一版**（部署前原件，副本也存在宿主备份目录 `grantley.md.repo-live`） |
| `grantley.md.p2-deployed` | `22ba6a36…` | humanizer 之前的 C6 版，取自宿主 `/opt/hermes/data/plugins/grantley/assets/`——那是一棵**未挂载、从不被读取**的陈旧树。**从未在 p2 生效**。文件名应理解为「在 p2 宿主上找到的一份副本」 |
| `voice.md.v1` / `.v2` | — | 示范层前两轮；v2 用禁用词表，把语气词密度从 1.204 压到 0.862，已废弃 |
| `grantley.yaml` / `plugin.yaml` | — | 对应快照 |

为什么错：`persona.py` 的 `ASSETS_DIR` 是包相对路径，容器内解析到
`/opt/hermes/repo/plugins/grantley/assets/`（= 宿主 `/opt/hermes-cloud/repo/...`），
而不是 `$HERMES_HOME/plugins/grantley/assets/`。活进程的 INFO 审计行
`Session plugin prompt section: id=grantley.identity … chars=3130` 也印证了这一点：
3130 是已验收版 stable 的长度。
