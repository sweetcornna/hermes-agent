# 格兰：2026-10-10 用两代剧本重新蒸馏，立绘和表情换成二代

这一轮用用户导出的游戏数据（`骑士学院1和2_图片与台词全集.zip`，17 GB，不进仓库）做了三件事：

- 用两代剧本原文重新蒸馏格兰的人格、说话方式和生活种子
- 角色立绘换成 KC2 的 14 张原版立绘（原来 11 张，新增菲力克斯、伽米尔、贝弗利特）
- 表情包换成 12 张 KC2 格兰脸部表情（原来 9 张）

图都直接取自游戏原图，没有生成或重绘任何一张：立绘逐字节复制，表情用游戏自己的头像框（IconRect）裁出脸部。

**已于 2026-10-11 09:08 UTC 部署到 P1（172.245.54.136）。** 网关停了约 21 秒。部署前在模拟 P1 上完整演练过一遍，包括回滚。仓库里的改动还没提交。

## 改了什么

| 范围 | 文件 | 变化 |
|---|---|---|
| 人格 | `plugins/grantley/assets/grantley.md` | 第一人称从头重写。稳定部分 2787 → 2380 字符，“此刻的我”八个占位符不变 |
| 说话 | `plugins/grantley/assets/voice.md` | 从头重写，1236 → 1894 字符。写他真实的说话门道，例子全部重写 |
| 生活种子 | `plugins/grantley/assets/life_seeds/grantley.yaml` | 改用 KC2 译名；场景、去处、心情、小麻烦都按剧本里他会干的事取 |
| 表情 | `plugins/grantley/assets/stickers/` | 9 张 JPG → 12 张 PNG（437×285，带透明） |
| 表情目录 | `plugins/platforms/onebot/sticker.py` | 12 条目录，每张的用途取自配这张脸说的台词；菜单开头改成他的口气；旧 slug 映射到最接近的新脸 |
| 立绘登记 | `plugins/image_gen/cornna/characters.py` | 11 → 14 个 key（新增 `felix`、`gamir`、`bayfleet`） |
| 空间日记 | `plugins/corlinman_jobs/prompts.py` | 角色名单改用 KC2 译名，加上新角色 |
| 立绘文件 | `$HERMES_HOME/characters/*.png` | 不在仓库里。原 11 个文件名照旧，内容换成 KC2 版，另加 3 张 |
| 测试 | `tests/gateway/test_onebot_*`、`tests/plugins/image_gen/test_cornna_provider.py` | 跟着改 slug 和 key，补了旧 slug 映射的测试 |

稳定部分加上声音段是 4274 字符，单段不超过 4000、合计不超过 8000 的预算都满足。改之前的三份文本和 SHA256 留在 [before/](before/)；旧表情图在 git 的 HEAD（2723df93d）里。

### 第二轮：逐句重读后重写（2026-10-11）

第一版人格事实对了，说话却不像他。旧的声音段把他写成了一个冷淡、话少、句尾不带标点的人（“就今天？”“谁是你宝宝”），可剧本里的格兰正相反。所以第二轮把两作里他的每一句台词连同前后文重新读了一遍：KC1 1,850 行、KC2 2,339 行。然后人格、声音、生活种子和表情菜单全部从头重写，没有沿用旧句子。

读下来，他说话有这几样特点（出处见 [CANON.md §E](CANON.md#e-说话方式)）：

- 先出声再说事，第一拍常是“啊？”“真假？”。
- 想到就提议，提议很具体，要紧的词喊两遍。
- 道理简单，多半落在吃、睡、身体、好不好玩上，说得理直气壮；规矩上爱钻空子，被戳穿就认。
- 被损当场顶回去，转头就忘；被夸先美，夸多了起疑。
- 心事被说中就否认、结巴或找借口溜；自己的在意常说成是对方的。
- 真心话用最普通的字，说完就换话题。
- 关心人靠动手，安慰是“没事的啦”加一个实在的理由。
- 弯弯绕绕的话他听不懂，会直说听不懂。

写法上：

- 身份段和声音段都改成第一人称的大白话，规矩尽量写成他自己会怎么做，少列条款。
- 以前线上出过问题而加的规矩都还在，只是换成了他的口气。具体是：认人只看发送者 ID、AI 身份一句话承认、不说“恶心”“滚”、没工具结果不说做完、`[LONG_REPLY]` 和 `[MSG_BREAK]`、不说“我这儿”、不汇报记忆等。
- 为了贴合 QQ 聊天，有三条取舍：一条消息仍然短，句尾不加句号，口头禅一条最多一个。感叹号放开了，他来劲的时候就用；剧本里一半的台词以感叹号结尾。

另外用 10-04 那组 26 条合成探针，让 Claude 分别按旧人格和新人格各回一遍，作粗看用，不算盲评，也不是生产模型。新版先出声、会用感叹号、回答里带出原作事实，比如爸妈是考古学家、抄奥斯卡的笔记；旧版遇到这些会躲开。

样本里还发现两处问题，已经改了：

- 承认 AI 之后会反问“哪句最人机”。这条旧规矩被我漏掉了，已补回。
- 例子里吃的太多，回复也跟着老往吃上拐，换掉了两个例子。

### 人格里改正和新增的事实

细节和出处见 [CANON.md](CANON.md)。主要几条：

- **补上之前没出处的事**：出身首都市中心；父母是常年在海外的考古学家，乳母带大；独生子；入学前读过寄宿学校。
- **订正错误**：
  - 剑术是年级第二，“战力第一”不对。
  - 讨厌的是热，不是洗澡。
  - 跟奥斯卡第一天就玩到一起，不是“合不来”。
  - 一年级第 4 章抽签分开以前，跟亚戈是室友。
- **E.P. 按原文写**：变透明、穿过门，牵着手的人一起。范围、时长、上限剧本没交代，人格里不编。
- **新增日常细节**：怕热、饭量大、想喝酒、打呼噜给室友备耳塞、晕船、怕瞬移、把拥抱当城里的礼节。
- **名册改用 KC2 简中译名**（亚戈、西奥、保罗、迪德里奇、希利欧、巴亭……），旧译名照样认。
  补上贝弗利特、伽米尔、菲力克斯。“本大爷”是巴亭和菲力克斯的自称。

### 刻意的取舍

- **时间点不变**：二年级新学期，重分寝室时亚戈选了格兰（剧本场景 `1-2/219_C_01`）。保罗的真相、第 6 章改写过去、后日谈都在这之后，格兰不知道，所以不进人格。
- **“暗恋亚戈”是这里选的走向，不是官设。** 剧本里心动的多是亚戈，格兰常没察觉。人格里明说了这一点。
- **语气词用大陆口语**：KC2 简中本是台湾口语译法（喔、耶、啰、的说），这里不照搬。
- **成人内容一律不读、不用**：KC2 的 `Hscenes`、`Epi_H` 整表排除。

### 表情：从哪来、怎么选

KC2 立绘的 pattern 码是五位数字：服装、嘴型、眉、眼、效果（详细解码见 [CANON.md §H](CANON.md#h-立绘的表情码)）。

- **选哪些脸**：统计格兰 640 句非成人台词各用什么表情，按使用次数挑，不是挑看着好看的。
- **怎么描述**：每张的 `when` 取自配这张脸说的台词。
- **怎么裁**：全部是制服、同一个裁剪框。框就是游戏给 `Grantly.asset` 定的对话框头像 IconRect（385, 100, 437, 285）。

| slug | pattern | 脸 |
|---|---|---|
| grin | 11050 | 眯眼咧嘴笑（05 是最常用的脸，162 句） |
| laughing | 13050 | 眯眼张大嘴笑 |
| smug | 10000 | 平常脸，嘴角带虎牙 |
| fired-up | 13200 | 竖眉张嘴喊 |
| angry | 12221 | 竖眉瞪眼、冒汗 |
| unimpressed | 10031 | 半眯眼、冒汗 |
| doubtful | 10131 | 挑眉半眯眼 |
| shocked | 12071 | 圆点眼、冒汗 |
| flustered | 12073 | 圆点眼、冒汗加脸红 |
| shy | 11052 | 眯眼笑、脸红 |
| sheepish | 10351 | 皱眉闭眼干笑、冒汗 |
| aggrieved | 12371 | 皱眉圆眼喊、冒汗 |

**旧 slug 的处理**：thumbs-up、shrug、heart-hug、thinking 四个旧 slug 映射到 smug、sheepish、shy、doubtful；angry、fired-up、flustered、laughing、unimpressed 五个名字没变。这样做是因为分叉后的会话还带着旧回复，模型照着写旧标记时，不会悄悄丢掉表情。映射只对内置这套生效，自带 `catalog.json` 的表情包不受影响。

### 立绘

14 张都是 KC2 的 `10000` 立绘（制服、平常脸），从 `完整立绘_全部差分` 逐字节复制，跟导出包自带的 `图片/校验清单.sha256` 对得上。短边都 ≥ 1032 px，旧验收里两张 1100×688 的官方角色卡例外不再需要。逐张验收见 [CHARACTER-ASSET-ACCEPTANCE.md](../../migration-corlinman/CHARACTER-ASSET-ACCEPTANCE.md#kc2-replacement-2026-10-10)。

## 怎么做的

1. **解包**：zip 里的文件名是 UTF-8 但没打 UTF-8 标记，按 cp437 解回来。台词用 `全语言原始台词.jsonl` 的简中列，按“同表、同说话人、同文本”去重。
2. **读剧本**：格兰出场或被提到的 228 个场景（约 20 万字）分七块完整读过，另有一路专门核对世界观和名册。每条事实都带 `[KC?:表:行]` 出处，笔记整理进 [CANON.md](CANON.md)。
3. **说话统计**：两代去重台词 573 + 617 句，统计长度、句尾、口头禅，见 CANON §E。
4. **出图**：[build_assets.py](build_assets.py) 直接从 zip 读，输出表情、立绘和 [assets-manifest.json](assets-manifest.json)。每个文件都记录了来源路径、CRC32、SHA256。重跑两次结果逐字节相同。

```bash
python -P docs/personas/grantley-kc2-20261010/build_assets.py 骑士学院1和2_图片与台词全集.zip --stickers plugins/grantley/assets/stickers --characters OUT/characters --manifest docs/personas/grantley-kc2-20261010/assets-manifest.json
```

## 验证

- **Python 测试**（WSL，`scripts/run_tests.sh`）：
  - grantley、image_gen、corlinman_jobs、qzone 全部通过。
  - OneBot 网关测试只有 1 个失败：`test_onebot_main_window.py::TestConfigPlumbing::test_yaml_home_channel_arms_the_rule`。HEAD 上就失败（`gateway/config.py` 里没有 `Platform.ONEBOT`），和这次改动无关。
- **模拟 P1 演练**：用 HEAD 的代码搭一套 `/opt/hermes` 目录结构，依次跑 `deploy.sh` 和 `rollback.sh`。
  - 基线被改过时，部署拒绝执行。
  - 正常部署后，两条绑定通道都拿到 identity 3210 + voice 1449，未绑定通道为空，12 张表情和 14 个立绘 key 都能解析。
  - 回滚后，文件逐字节回到 HEAD，旧的 9 张 JPG 和 11 张立绘恢复。

## 部署记录

| 步骤 | 结果 |
|---|---|
| 预检（网关运行中） | 上传的打包文件 `/opt/hermes/sim/kc2` 校验通过；要替换的 29 个文件和 HEAD 一致，没有漂移；没有任何定时器会拉起网关；`preflight` 在临时副本上通过，可重入，能回滚 |
| 停机 | 09:07:57 停 `hermes.service`。退出码 1，10-08 停机时也这样，和这次无关 |
| 备份 | `/opt/hermes/backups/persona-kc2-20261011T090803Z`：两份 grantley 目录、3 个代码文件、立绘目录、sessions.json、state.db、grantley data.db，附 SHA256SUMS |
| 安装 | 两份 grantley 目录换上新人格和 12 张 PNG 表情，删掉 9 张旧 JPG；repo 换上 sticker.py、characters.py、prompts.py；`/opt/hermes/data/characters` 换上 14 张立绘；逐字节核对一致 |
| 会话 | `revise`：4 个绑定频道分叉到 `grantley-kc2-20261010-*`，2 个等第一条消息时新建，5 个主动发言频道重置 |
| 核验 | 6 个绑定频道都拿到 identity 2379 + voice 1893 字符，未绑定频道为空；12 张表情、14 个立绘 key 都能解析 |
| 启动 | 09:08:10 启动，NRestarts=0，09:08:21 OneBot 重新连上 |

部署时主模型 claude-opus-5-5 正处在限流里，网关在用备用模型 gemini-3.8-flash-high 回复。这是部署前就有的状态，由 `hermes-fallback-recovery.timer` 定时探测、自动切回。

**09:17 UTC 追加：表情发送概率 0.18 → 0.5。** 10-04 起表情菜单每轮都在，由模型自己决定发不发，发送时再按 `sticker_probability` 掷一次。
10-04 到这次部署之间共 241 条回复，模型选了表情的有 25 条（约 10%），乘以 0.18，实际只发出 4～5 张，不到 2%。
现在改成 0.5，预计大约每 20 条发 1 张。只改了 `/opt/hermes/data/config.yaml` 这一行，改前的备份在 `/opt/hermes/backups/config-sticker-20261011T091719Z.yaml`。改完重启了网关，09:17:29 OneBot 重新连上。

### 当时的部署步骤

需要经 Mac 跳到 P1，并短暂停网关。

1. **本机打包**：

   ```bash
   python -P docs/personas/grantley-kc2-20261010/stage.py 骑士学院1和2_图片与台词全集.zip OUT
   ```

   打出 `release.tar`、14 张立绘、`BASE.sha256` 和脚本，全部带 `SHA256SUMS`。

2. **复制**：把 OUT 复制到 P1 的 `/opt/hermes/sim/kc2/`，hermes 用户要能读到。
3. **停网关并部署**：停 `hermes.service`，用 root 跑 `bash /opt/hermes/sim/kc2/deploy.sh`。脚本会依次：
   - 校验暂存文件
   - 检查没有自定义表情目录
   - 核对生产上的 6 个文本文件和 9 张旧表情和 HEAD 一致，不一致就中止
   - 备份到 `/opt/hermes/backups/persona-kc2-<时间>`（两份 grantley 目录、3 个代码文件、立绘目录、sessions.json、state.db、grantley data.db）
   - 安装并逐字节核对
   - 跑 [deploy_persona.py](deploy_persona.py) 的 `preflight`、`revise`、`verify`
4. **启动网关**，在群里看一次表情和人格。

**回滚**：停网关，跑 `bash rollback.sh /opt/hermes/backups/persona-kc2-<时间>`。它会还原文件、删掉 3 张新增立绘，再跑一次 `deploy_persona.py rollback`，让绑定通道重新分叉、用回旧人格。部署之后收到的消息都保留。

## 遗留

- 10-04 那套种子已经生成的生活事件里有旧译名（如“西利欧”）。人格认旧译名，所以没有清理。要清理的话，照 10-04 的做法把对应 `life_events` 的 salience 置 0。
- 剧本里没交代的（年龄、首都名、去世的朋友、E.P. 上限等）列在 CANON §I。人格按“不编”处理。
