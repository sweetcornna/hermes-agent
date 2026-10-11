# F1 — QQ 语音消息与语音通话：入站转录修复、outbound speak() 收口、call-shaped 会话

> ## ⚠️ 先读这个
>
> **"语音通话"这个词在这里指的不是实时语音。** OneBot v11（NapCat 的实现）整个
> action 集合就是消息，外加群 AI 声聊三个动作
> （`send_group_ai_record` / `get_ai_record` / `get_ai_characters`）和
> `send_poke`——没有呼叫信令，没有 RTP，没有媒体通道。`voice_call.py` 交付的是
> **建立在语音消息之上、带通话语义的会话**：邀请/振铃、接听或拒绝、通话中、
> 挂断（任一方主动挂断，或响铃/空闲超时，或撞到时长/轮次上限）。双方交换的仍然
> 是一条条语音气泡；"通话"买的是**框架**（会话边界、状态、超时），不是更低延迟
> 的传输层。要去掉这条限制，需要 OneBot 后端提供一个今天不存在的原生呼叫
> action（见 §2）——这不是"正在做的一半"，是这个功能在当前协议下能做到的
> 全部。任何暗示未来版本会自然长出实时语音的说法都是错的，除非先有后端能力。

分支 `feat/corlinman-migration`。落地位置：`plugins/platforms/onebot/protocol.py`
（新增 5 个 action）、`tools/audio_container.py`（共享 magic-byte 嗅探器，入站
转录 bug 的发现与修复起点，此后未再变过）+ `tools/transcription_tools.py`（入
站转录的核心派发层）+ `agent/audio_decoder.py`/`agent/audio_decoder_registry.py`
（解码器 provider registry，新）+ `plugins/voice_codecs/`（SILK/AMR 解码器，原
生化重构后的落点——见 §3 与 §3.2）、`plugins/platforms/onebot/voice.py`（新，
outbound 语音唯一收口）、`plugins/platforms/onebot/voice_call.py`（新，纯状态
机）、`plugins/platforms/onebot/adapter.py`（19 个新 YAML 配置键、常驻
sweeper、通话接线）、`plugins/platforms/onebot/proactive.py`（主动语音与主动
通话两条 lane）、`plugins/platforms/onebot/client.py`（live-adapter
registry）、`plugins/qq_voice/`（新插件，两个模型可调用工具）。

---

## 1. 交付了什么

| 文件 | 改动 | 内容 |
|---|---|---|
| `plugins/platforms/onebot/protocol.py` | +147/− | `GetRecord`、`SendGroupAiRecord`、`GetAiRecord`、`GetAiCharacters`（NapCat-only，群限定）、`SendPoke` 五个新 action dataclass 及其 wire 序列化 |
| `tools/audio_container.py` | +28/− | 共享 magic-byte 探测器新增 AMR/SILK 识别（见 §3） |
| `tools/transcription_tools.py` | +166/−（当时的修复），后续原生化重构精简为纯派发 | 当时新增 `_preconversion_container`/`_convert_silk_to_wav`/`_convert_amr_to_wav`，把 `.amr`/`.silk` 直接塞进 `SUPPORTED_FORMATS`，转录前统一预转码为 WAV（见 §3）。**这一层解码逻辑后来被搬进插件**（见 §3.2）：`SUPPORTED_FORMATS` 现在退回只含内建格式，新增 `supported_formats()` 动态并入已注册解码器的扩展名；本文件只剩 `_sniff_source_container`/`_registered_decoder_extensions`/`_decoder_for_source`/`_prepare_audio_for_transcription`/`_unsupported_format_error` 这层派发。`_resolve_faster_whisper_load_params` 修复 Apple Silicon 上 `device`/`compute_type` 配置被无意回退（见 §3.1，未受本次重构影响）|
| `agent/audio_decoder.py` | 新 | `AudioDecoder` ABC（`name`/`container_ids`/`extensions`/`claims(*, container_id, suffix)`/`decode(file_path) -> DecodedAudio`）、`DecodedAudio`、`AudioDecodeError`——SILK/AMR 解码器现在实现的插件契约（见 §3.2）|
| `agent/audio_decoder_registry.py` | 新 | `register_decoder`/`list_decoders`/`get_decoder`/`find_decoder`/`decodable_extensions`/`snapshot_registration`/`restore_registration`——仓库里第七个同族 provider registry（其余六个：tts / transcription / image_gen / video_gen / web_search / browser），见 §3.2 |
| `hermes_cli/plugins.py` | +68/−，纯增量 | `PluginContext.register_audio_decoder`，把上面两个新模块接进既有插件加载流程 |
| `plugins/voice_codecs/` | 新插件，`kind: backend` | `SilkDecoder`（依赖可选包 `pilk`）+ `AmrDecoder`（依赖外部二进制 `ffmpeg`），不注册任何工具；`tools/transcription_tools.py` 里搬出来的解码逻辑的落点（见 §3.2）|
| `gateway/platforms/qqbot/adapter.py` | 后续合并（本次原生化重构未触碰此文件） | `_guess_ext_from_data`/`_looks_like_silk` 改为委托给共享嗅探器，去掉自己的一份 magic-byte 探测；转码阶梯（`_convert_silk_to_wav`/`_convert_ffmpeg_to_wav` 等，与上面 `plugins/voice_codecs/` 无关的另一套独立实现）未动（见 §3）|
| `plugins/qq_voice/quota.py` | 新，510 行 | `qq_send_voice`/`qq_ai_voice` 的文字长度上限与每日配额（见 §5 F1-8～F1-11） |
| `plugins/platforms/onebot/voice.py` | 新，649 行 | outbound 语音唯一收口：`speak()`（TTS 合成）与 `speak_ai_character()`（群 AI 声聊），恢复 `send_voice()`/`_send_attachment()` 绕过的三项保证（见 §4） |
| `plugins/platforms/onebot/voice_call.py` | 新，952 行 | 纯函数、无 I/O 的通话会话状态机：`invite`/`offer`/`accept`/`decline`/`hangup`/`sweep` |
| `plugins/platforms/onebot/adapter.py` | +627/− | `_PRIVATE_YAML_KEYS` 新增 19 个键（见下）、`_call_sweeper_loop` 常驻 sweeper、`_route_call_inbound`/`_speak_call_reply` 接线、`start_voice_call`/`end_voice_call` |
| `plugins/platforms/onebot/proactive.py` | +1016/− | `proactive_voice_*`（主动语音，modality 而非 gate）与 `proactive_call_*`（主动通话）两条新 lane 及其配置解析 |
| `plugins/platforms/onebot/client.py` | +71/− | `set_live_adapter`/`get_live_adapter`——`get_live_client` 的同层扩展，`qq_voice` 插件靠它找到活着的 gateway adapter |
| `plugins/qq_voice/` | 新插件 | `qq_send_voice`、`qq_ai_voice` 两个模型工具（`__init__.py`/`common.py`/`quota.py`/`send_voice.py`/`ai_voice.py`） |
| `plugins/platforms/onebot/README.md` | 扩写 | 记录上述全部（不在本文档重复配置表） |

19 个新 YAML-only 配置键（`adapter.py::_PRIVATE_YAML_KEYS`）：

```
voice_call_enabled, voice_call_ring_timeout_secs, voice_call_idle_timeout_secs,
voice_call_max_duration_secs, voice_call_max_turns, voice_call_allow_groups,
voice_call_allowed_users,
proactive_voice_enabled, proactive_voice_probability, proactive_voice_max_chars,
proactive_voice_provider,
proactive_call_enabled, proactive_call_targets, proactive_call_daily_max,
proactive_call_min_gap_minutes, proactive_call_active_start_hour,
proactive_call_active_end_hour, proactive_call_probability, proactive_call_prompt
```

默认值、含义、YAML 样例见 `plugins/platforms/onebot/README.md`"Voice — messages
and calls"与"Proactive speaking"两节；本文档不重复，只记工程决策与已知缺口。

---

## 2. 约束与由此而来的设计

**约束**：NapCat 暴露的 OneBot v11 action 集合里没有任何呼叫原语。没有
"incoming call" notice/event，没有 accept/reject/hangup action，没有媒体协商，
没有 RTP。这不是 NapCat 的实现缺口，是协议本身没有这层——OneBot v11 建模的是
IM 消息收发，不是通话信令。

**由此而来的设计**：既然唯一可用的原语是"发一条语音消息"，"通话"就只能建在
消息之上——把呼叫语义（振铃、接听/拒绝、通话中、挂断、超时）做成一层**纯状态
机**，状态机本身完全不知道传输层的存在。`voice_call.py` 的模块 docstring把这一
点说得很直接："Everything in here is pure: no asyncio, no I/O, no wall clock."
状态转移函数（`invite`/`accept`/`decline`/`hangup`/`on_inbound`/`sweep`）接收
`now: float` 和已解析的 `CallConfig`，返回一个 `CallDecision`（要不要发、发哪一
句、为什么）；真正把决定送上线的唯一地方是 `perform()`
（`voice_call.py:930-953`），它把"说这句话"和"戳一戳"作为**注入的可调用对象**
接收，从不自己 import 传输层。

**如果未来某个 OneBot 后端真的长出了原生呼叫 action**，需要提供什么，会话层
才能原样搭在上面而不用改：

1. 一个 notice/event 类型，通知"对方发起了呼叫"（目前 `offer()` 存在但没有
   任何东西调用它，见 §6，正是因为没有这个事件源）；
2. accept / reject / hangup 三个 action，语义对应 `voice_call.accept` /
   `decline` / `hangup`；
3. 呼叫状态变化的通知（对方挂断、对方接听），替代目前靠"下一条消息按话术分类"
   模拟 accept/decline/hangup 的做法（`classify_intent`，见 §5-3）；
4. 可选：一个媒体/流式 action，把 `perform()` 里的"说一句语音消息"换成"打开一
   条音频流"。

关键是第 1-3 点不需要改 `CallSession`/`CallState`/超时逻辑的任何一行——需要改
的只是 `OneBotAdapter._call_speak`/`_call_poke`/`_perform_call_decision` 这几
个"决定meets wire"的适配层函数，以及新增一个 `offer()` 的调用点。这正是把状态
机做成纯函数、把传输作为注入依赖的意义所在。

---

## 3. 入站转录 bug：发现与修复

**失败链**：QQ 语音消息在 NapCat 侧是 SILK 或 AMR 裸编码流（不是容器），但
`RecordSegment`/`get_record` 返回的文件名/后缀经常是猜的、经常是错的（一个
SILK payload 常被存成 `.amr`）。修复前：

1. `tools/audio_container.py::sniff_container` 完全不认识 AMR/SILK 的 magic
   bytes——只识别 `ftyp`/`OggS`/`fLaC`/`RIFF+WAVE`/`ID3`/ADTS/`webm` 这几种真
   正的容器格式；
2. `tools/transcription_tools.py::SUPPORTED_FORMATS` 里没有 `.amr`（只有到
   `.caf` 为止的一串常见格式）；
3. `transcribe_audio_local_fallback` 在做任何格式转换之前先跑
   `_validate_audio_file(file_path)`，而这个函数只按文件后缀查
   `SUPPORTED_FORMATS`——`.amr` 不在表里，直接返回
   `"Unsupported format: .amr"`；
4. 上层把这个失败渲染成对模型可见的占位符
   `"[voice message could not be transcribed automatically; ...]"`
   （`gateway/run.py:24427`/`:24436`，另有一处不带原因的
   `"[voice message could not be transcribed]"` 在 `gateway/run.py:24362`）。

净效果：**任何一条 QQ 语音消息，转录必然失败**——不是偶发，是这条路径上的每
一条都会踩到同一个格式白名单缺口。

**修复**：

* `tools/audio_container.py` 新增 `_AMR_MAGIC = b"#!AMR"`、
  `_SILK_MAGIC = b"#!SILK"`、腾讯自家的一字节 SILK 前缀
  `_SILK_TENCENT_HEADER = b"\x02!"`，`sniff_container` 据此返回 `"amr"` /
  `"silk"`；
* 当时 `tools/transcription_tools.py::SUPPORTED_FORMATS` 直接收录了
  `.amr`/`.silk`，新增 `_preconversion_container()`：**字节优先于文件名**——
  先读文件头交给 `sniff_container`，只有在探测不出结果时才退回看后缀；命中
  SILK/AMR 就先经 `_convert_silk_to_wav`（pilk）/`_convert_amr_to_wav`
  （ffmpeg）转成 WAV，再进正常的转录管线。**这一步后来被原生化重构搬出了
  核心**（见 §3.2）：今天的分层是核心嗅探（`tools/audio_container.py`，本节
  上面这条，未变）→ 核心派发（`tools/transcription_tools.py::_sniff_source_container`/
  `_decoder_for_source`/`_prepare_audio_for_transcription`，只认字节和 registry，
  不认具体编码）→ 插件解码（`plugins/voice_codecs/silk.py::SilkDecoder.decode`/
  `plugins/voice_codecs/amr.py::AmrDecoder.decode`，pilk/ffmpeg 调用现在只存在
  于这两个方法里；`_convert_silk_to_wav`/`_convert_amr_to_wav` 这两个函数名本
  身连同它们的旧实现已经不存在了，没有被原样搬走）。

**为什么修在核心层（`tools/`），不修在 onebot 插件里**：这个 bug 不是 onebot
专属的。当时 `tools/transcription_tools.py` 模块 docstring 明确写着支持格式包
含"the QQ/WeChat voice-note codecs amr and silk"——微信语音同样是这两种裸编码
之一，任何把语音丢给 `transcribe_audio` 的平台都会踩同一个坑。如果只在
`plugins/platforms/onebot/` 里加一层"先探测容器再转码"，等于在这个代码库里造出
**第二份**容器探测实现——而这不是假设：修这个 bug 的当时，
`gateway/platforms/qqbot/adapter.py`（官方 QQ 机器人 API 平台，与 onebot 完全独
立的另一套接入）已经有一份自己的 SILK 探测（当时的
`_guess_ext_from_data`/`_looks_like_silk`）和 pilk/ffmpeg 转码流水线，跟这次修
的东西做的是同一件事，却完全没有共享代码。（那句 docstring 后来随 §3.2 的原
生化重构一起改写了——现在说的是"plus every codec a registered audio-decoder
plugin claims, decoded to WAV before dispatch"，不再点名 amr/silk 这两个具体
编码，跟 §3.2 记的"核心自己不拼编码名字"是同一件事在文档层面的体现。）

**这份重复后来被合并了**，探测这一半——转码阶梯
（`_convert_audio_to_wav_file`/`_convert_silk_to_wav`/`_convert_ffmpeg_to_wav`：
`gateway/platforms/qqbot/adapter.py:1999`/`:2074`）没有动，动的只是识别字节形
状的两个函数：`_guess_ext_from_data`（`gateway/platforms/qqbot/adapter.py:2050`）
现在是 `return sniff_audio_ext(data, ".amr")`，`_looks_like_silk`
（`gateway/platforms/qqbot/adapter.py:2064`）现在是
`return sniff_container(data) == "silk"`，两个都直接委托给
`tools/audio_container.py`；顶部新增
`from tools.audio_container import sniff_audio_ext, sniff_container`
（`gateway/platforms/qqbot/adapter.py:75`）。一条 AST 解析的单测
（`tests/gateway/test_qqbot.py::test_detection_helpers_carry_no_magic_bytes`）
把"这两个函数体内不得再出现任何 `b"..."` 字节字面量"钉死，防止将来又漂回
第二份实现。

**合并顺带治好了一条活的 bug，不只是去重**：旧的 `_looks_like_silk` 只认
"`#!SILK`/`#!SILK_V3` 从第 0 字节开始"或"`\x02!` 这个截断头从第 0 字节开
始"，认不出"任意一字节前缀 + 完整 magic"这种形状——腾讯真实产出的
`\x02` + `#!SILK_V3` 就是这种形状，恰好落在旧代码两个分支的中间，被判定为
不是 SILK，走到 `.amr` 默认值，被送去 ffmpeg——**而 ffmpeg 解不了 SILK**。
共享嗅探器的 `sniff_container` 本来就在偏移 0 和偏移 1 两个位置都认
`#!SILK`（`tools/audio_container.py:96`：
`data.startswith(_SILK_MAGIC) or data[1:].startswith(_SILK_MAGIC)`），合并之
后这条路径自动通了——这不只是替 qqbot 省了一份实现，是修好了官方 QQ 机器人
适配器上一条本来会失败的语音转写路径。

顺带记两条行为变化，都是安全的收窄/扩张：合并后 qqbot 的探测能认出六种它以
前认不出、全部会塌成 `.amr` 默认值的容器（ID3 mp3、`\xff\xfa` 系 mp3、ADTS
aac、`ftyp` 的 m4a/mp4 分支、webm）；同时不再把裸 `RIFF`（无论 form type）
当 `.wav`——共享嗅探器要求 `RIFF` 之后第 8-11 字节确实是 `WAVE` 才认；也不
再把 ASF/WMA 的头（`\x30\x26\xb2\x75`）错当成 `OggS` 映射到 `.ogg`——旧
代码这条映射本来就是错的（那个字节序列是 ASF 的 GUID，不是 Ogg 的
`OggS`），所以这一条不算回退。这些改变是安全的，因为扩展名只是临时文件的
后缀：真正决定走 pilk 还是 ffmpeg 的判断是 `_looks_like_silk(audio_data)`
这个内容检查，ffmpeg 自己也会再嗅探一次内容，不依赖这个后缀猜对。

**新缺口，替换掉旧的"两份实现并存"那条**：ASF/WMA 现在完全不被
`tools/audio_container.py` 识别（不在 `CONTAINER_TO_EXT` 里，`sniff_container`
里也没有对应分支）。旧 qqbot 代码把它误判成 `.ogg`，这本来就是错的，所以这
不算功能回退；但如果要真正支持 ASF/WMA，需要在核心嗅探器里把它加成一个独立
的 container id，而不是靠某个调用方自己猜。列入 §6 已知缺口。

### 3.1 顺带修复：Apple Silicon 上 STT 的 `device`/`compute_type` 配置被无意回退

跟 AMR/SILK 那条并列的另一处"本次顺带发现并修掉的既有缺陷"，不是这次任务的
本职工作，但影响同一个转录管线，值得记进同一份文档。

**根因**：`884900ffd6`（"fix: avoid local STT crash on Apple Silicon"）晚于
`06fc6e0c29`（"fix(stt): respect device and compute_type from config.yaml"，
#8319，`git merge-base --is-ancestor 06fc6e0c29 884900ffd6` 可证前者是后者的
祖先），但**无意回退**了后者：#8319 让 `stt.local.device`/`compute_type` 这两
个配置项被真正尊重；Apple Silicon 崩溃修复给两个原本会被打破的 CUDA 相关测
试打了 `patch(..., return_value=False)`，唯独漏了 #8319 那个断言配置被尊重
的测试——`tests/tools/test_transcription_tools.py::test_config_device_and_compute_type_passed_to_whisper`
在 Apple Silicon/Rosetta 主机上因此**永远不可能通过**：崩溃修复把 device 无
条件钉死成 `"cpu"`，配置里显式写的 device 被悄悄丢弃。这条一直是绿的，只因
为 CI 跑的是 Linux，本地 Apple Silicon 开发机才会踩到。

**修法**：把"探测这台主机是不是需要强制 CPU"和"据此决定构造参数"拆成两层——
宿主探测的结果作为 `force_cpu` 参数传入一个纯函数
`_resolve_faster_whisper_load_params(device, compute_type, *, force_cpu)`
（`tools/transcription_tools.py:1878`——行号因 §3.2 的原生化重构在文件更前面
插入/改写了解码派发代码而后移，逻辑本身未变），而不是在函数内部重新去问主机。这个
纯函数保住了崩溃修复要保的安全属性——`force_cpu` 为真时 `device` 恒等于
`"cpu"`，绝不是 `"auto"`，绝不是某个 GPU——但只放开了**没有证据表明是安全
必需**的那一半：`compute_type` 只在用户把它留成 `"auto"`（即没有显式配置）
时才回退到 `int8`；用户显式写在 `stt.local.compute_type` 里的值，在 Apple
Silicon 上也照样生效。

### 3.2 原生化重构：SILK/AMR 解码从核心函数搬到 provider registry + 插件

§3 记录的修复，原先把 SILK/AMR 的探测（`tools/audio_container.py`）和转码
（当时的 `_convert_silk_to_wav`/`_convert_amr_to_wav`）都放进了 `tools/`。探测留在
核心是对的——两种编码是 QQ/微信共用的裸流，任何一个把语音丢给
`transcribe_audio` 的平台都要认出它们。但转码不是同一件事：这次原生化重构把
它搬出核心，落到 `agent/audio_decoder.py`（ABC）+
`agent/audio_decoder_registry.py`（registry）+ `plugins/voice_codecs/`
（SILK/AMR 两个 decoder 的实现），跟仓库里已有的六个同族 provider registry
（tts / transcription / image_gen / video_gen / web_search / browser）并列，
是第七个。

**为什么做成 registry + 插件，而不是继续留在核心**：两条理由都能在代码里读
到，不是事后补的说辞。第一条是 `AGENTS.md` 的"widen the generic plugin
surface, don't special-case it in core"——`agent/audio_decoder.py` 模块
docstring 原话引用了这条规则来解释为什么这是个插件面而不是核心分支。第二条
更具体：SILK 需要一个 Hermes 不自带的可选 Python 包（`pilk`），AMR 需要一个
Hermes 不自带的外部二进制（`ffmpeg`），而且两者都只对"这台部署会收到这种编
码的语音消息"这一类安装有意义——一台从不接 QQ/微信语音的 Hermes 实例没有理
由背上这两个依赖的重量。

**为什么插件叫 `voice_codecs`，不叫 `qq_voice` 或 `silk`**：`plugins/voice_codecs/__init__.py`
模块 docstring 把这点说得很直接——SILK 是 WeChat 和 QQ 共用的语音编码，不是
QQ 独有的；AMR 是通用的 3GPP 电话编码，还会出现在短彩信桥、通话录音这类跟 QQ
毫无关系的场景。按平台命名（`qq_voice`）会导致下一个把同样字节发过来的桥接
（比如另一个 IM 平台，或者一条电话/短信桥）不得不重新发一份几乎一样的插件；
按内容命名（`voice_codecs`）让这份实现天然对所有平台可复用。

**为什么 `kind: backend`，而不是留给 `plugins.enabled` 去 opt-in**：
`plugins/voice_codecs/plugin.yaml` 顶部的注释写明了原因——自带的 `backend`
插件在 `manifest.source == "bundled" and manifest.kind == "backend"` 时自动
加载（`hermes_cli/plugins.py` 里的加载循环，紧跟在 model-provider 分支之
后），不需要出现在 `plugins.enabled` 里。SILK/AMR 语音条今天就是开箱即转
录的（这正是 §3 那个 bug 修复的初衷），把它降级成 opt-in 会让每一个正在收这
类语音的安装遭遇一次静默回退——用户什么都没改，转录却突然又不工作了。而且这
个插件不注册任何工具（`register(ctx)` 只调用两次
`ctx.register_audio_decoder`），不进模型 schema、不占用任何 token，也就没有
`check_fn` 这种"要不要把某个工具亮给模型"的问题可言——`backend` 这个 kind 存
在的意义，本来就是给这类"该在但不该让模型知道"的能力用的。

**跟前六个同族 registry 故意不同的三处设计**，理由都能在
`agent/audio_decoder.py`/`agent/audio_decoder_registry.py` 的 docstring 里
读到原话，不是这次总结时自己编的：

1. **没有 `is_available()`。** TTS/STT 这类 provider 是用户在 `config.yaml`
   里点名选的，"配置了但用不了"值得单独报告成一种状态；解码器不是配置出来
   的，是**用户刚发来的那条语音的字节**自己选中的——如果依赖缺失就默默拒绝，
   退化成一句通用的"不支持的格式"，等于把一个本可以说清楚"装什么"的场景变
   成死胡同。`AudioDecoder` 的 docstring 原话是让 `decode()` 在依赖缺失时抛
   `AudioDecodeError` 报出缺的是什么，而不是提前用 `is_available()` 悄悄降级。
2. **用异常，不用 error dict。** `AudioDecodeError` 的 docstring 写明
   `str(exc)` 会原样显示给用户，必须是"点名缺什么、去哪装"的可操作文案，不
   是"decode failed"；调用方（`_prepare_audio_for_transcription`）用
   `try/except` 接住它，连同一个兜底的 `except Exception` 防止某个写坏的解
   码器打断整轮对话。
3. **"内建永远赢"按格式守，不按名字守。** `register_decoder()` 拒绝的是解
   码器 `container_ids`/`extensions` 与内建集合（`_BUILTIN_EXTENSIONS` 是一份
   独立字面量，靠 `tests/agent/test_audio_decoder_registry.py::TestBuiltinSync`
   钉住不会跟 `tools/transcription_tools.py::SUPPORTED_FORMATS` 漂移——两边不
   能互相 import，会成环；`_BUILTIN_CONTAINERS` 才是真正派生的，从
   `CONTAINER_TO_EXT` 按 `_BUILTIN_EXTENSIONS` 筛出来）**重叠**，而不是检查
   名字是不是叫 `mp3`——一个决心要抢走每一条语音的插件哪怕换个名字注册（比如
   叫自己 `evil-mp3-hijacker`），只要它的 `container_ids` 里有 `"mp3"`，注册
   照样在 `register_decoder()` 这一步被拒绝并打一条 warning 日志，而不是成功
   注册后在别处才发现冲突。

**临时文件归属规则**：`agent/audio_decoder.py` 模块 docstring 专门有一节
"Temp-file ownership"，"Exactly one owner per outcome, and it never changes
hands mid-flight"——成功时 `decode()` 返回的 `DecodedAudio.cleanup_dir` 归调
用方，`tools/transcription_tools.py::transcribe_audio`/
`transcribe_audio_local_fallback` 在各自的 `finally` 块里
`shutil.rmtree(cleanup_dir, ignore_errors=True)`；失败时 decoder 必须在抛出
`AudioDecodeError` 之前把自己创建的一切清理干净（`SilkDecoder.decode`/
`AmrDecoder.decode` 的 `except` 分支都是先 `shutil.rmtree(temp_dir, ...)` 再
`raise`），调用方在这条路径上拿不到任何句柄，也就没有东西可清理。

**没有插件（或插件被禁用）时的新行为**：`_unsupported_format_error()` 原来
只会说"不支持的格式"；现在如果字节能被 `sniff_container` 认出容器、但没有
内建格式接收、也没有已注册的解码器认领它，会在消息末尾追加一句指明缺插件的
提示——原文是"No audio decoder plugin claims {container} audio
({claimed_ext}) — install or enable one"，末尾附一句指向 `hermes plugins list`
的提示。这句提示里的编码名字（`silk`/`amr`）不是核心自己拼出来的字符串字面
量——是 `container` 变量，来
自 `sniff_container()` 的返回值，再经 `CONTAINER_TO_EXT` 反查出对应扩展名；
`tests/tools/test_transcription_qq_containers.py::TestCoreCarriesNoCodecKnowledge`
用 AST 解析加正则搜索双重锁死这一点——`tools/transcription_tools.py` 全文不
得出现 `silk`/`amr`/`pilk`（不分大小写）这几个词，`_sniff_source_container`/
`_decoder_for_source`/`_prepare_audio_for_transcription` 三个派发函数体内不
得出现任何字节字面量，也不得引用 `_find_ffmpeg_binary`/`subprocess`/
`tempfile`——解码是插件的工作，核心一步都不能替它做。这条单测的写法直接呼应
`tests/gateway/test_qqbot.py::test_detection_helpers_carry_no_magic_bytes`
（§3 提到过的那条），是同一条"核心不许长出第二份编码知识"的规则在两处代码
上分别钉死。

---

## 4. `voice.py::speak()` 恢复的三项保证

`OneBotAdapter.send_voice()` 经 `_send_attachment()` 上线，这条路径不走
`adapter.send()`，因此绕开了文本回复路径的三项保证——`voice.py` 模块 docstring
把这点写在最前面：

1. **紧急静音（emergency mute）**——`send()` 在算任何东西之前先查
   `group_speech_muted()`；`_send_attachment()` 其实**也**查了这一项，是三项
   里唯一在旧路径上幸存的一项。但正因为它是三项里唯一对的，`voice.py`
   docstring 把这形容为"a path that gets two of three right is the most
   expensive kind of nearly-correct"——一条几乎全对的路径比一条明显全错的路径
   更危险，因为它骗过了粗略的 review。
2. **腾讯内容策略（content policy）**——`send()` 跑 `moderate_text` 并且
   fail-closed；`_send_attachment()` 完全不跑（`adapter.send()` 里那条
   `TODO(media)` 就是对这个洞的自我承认）。语音不是风控的豁免区：说出口的还
   是这个账号的话，被冻结的还是这个账号。
3. **自我记录（self-recording）**——`send()` 把每条群消息（含机器人自己发的）
   以 `is_self=True` 写进共享的 recent-chatter 缓冲区；`_send_attachment()`
   完全不写。少了这一条，`proactive.last_message_is_self` 会把房间读成"刚才
   是人在说话"，然后主动发言功能会对着自己刚发的语音消息又说一遍——语音消息
   恰恰是读者最不可能跳读过去的一种形式，这个失败模式选在这里出现是最坏的
   地方。

`speak()` 把这三项按顺序放回同一处（`plugins/platforms/onebot/voice.py:394-434`
的 docstring 详细写了顺序和理由）：静音 → 内容策略 → 连接检查 → 合成 → 整批校
验 → 发送 caption → 发送 record 分段 → 自我记录 → 清理临时文件。这个插件里所有
的语音生产者——`qq_send_voice` 的文本分支、主动语音 lane、通话会话说的每一句
——都走这一条路径，没有第二条实现。

**后续（T9）：`_send_attachment` 自己也补上了两项。** 上面三点记的是
`speak()` 如何在*语音合成*这条路径上把三项保证补回来；`_send_attachment()`
——`send_voice()`（发送一个已经录好的文件）、`send_image_file()`、
`send_video()`、`send_document()` 共用的同一个函数——当时仍然只补了紧急静
音一项。这个状态后来变了，值得记录：

* **群内媒体发送成功后，现在会写入 recent-chatter 缓冲区。**
  `OneBotAdapter._record_self_media()` 在 inline 分支（image/audio）发送成
  功之后、以及 upload 分支没有 caption 的情况下，把这次发送记进缓冲区——有
  caption 就记 caption 本身，没有就记一个简短占位符（`_SELF_MEDIA_MARKERS`：
  `image` → `[图片]`、`audio` → `[语音]`、`video` → `[视频]`、
  `document` → `[文件]`）。这补的正是本节第 3 点点名过的同一个洞，只是补在
  了 `_send_attachment` 自己身上，而不是要求调用方改走 `speak()`。记录动作
  放在**发送结果确认成功之后**，从不提前：一条被拒绝、被静音或没收到确认的
  消息不能被写进缓冲区，教坏 `proactive.last_message_is_self`。upload 分支
  带 caption 时不重复记录——那种情况会转去走 `send()`，后者已经记过一次，
  双写会让同一句话在 transcript 里出现两次。
* **inline 分支的 caption 现在过内容策略，fail-closed，拒绝会挡住整条发
  送。** 这是一处**真实发现并修复的 bug**：`_send_attachment` 的 upload 分
  支（文件上传）的 caption 一直是安全的，因为它转发给 `send()`，而
  `send()` 早就跑 `moderate_text`；但 inline 分支（image/audio 内联发送）
  的 caption 直接拼进 wire 上的消息，从来没有经过 `send()`，也就从来没有被
  这道 gate 看到过——一条本该被内容策略拒绝的话，只要是当图片或语音的
  caption 发出去，就能绕开审核。现在 `_send_attachment` 在 inline 分支里、
  编码文件之前，自己跑一遍同样的 `content_policy.moderate_text`（同样
  fail-closed：分类器本身炸了也按拒绝处理），拒绝时整条发送（媒体 +
  caption）都不发，而不是只删掉那句话——媒体是 caption 的语境，悄悄删掉一
  句被拒绝的话、再把媒体发出去，等于让媒体说了一句谁都没写过的话。**媒体
  本身的字节依然不经审核**——`adapter.send()` 里那条 `TODO(media)` 描述的
  洞还在，这次修的只是文字部分。
* **inline 分支 caption 那条文本消息的发送结果，现在不再被丢弃。** 这也是
  一处**真实发现并修复的 bug**，跟上一条不是同一个洞：上一条是"caption 有
  没有过内容策略"，这一条是"caption 真的发出去了吗"。`_send_attachment` 原
  先调用 `_send_segments` 发 caption 之后不看返回值，直接接着发媒体本身——
  哪怕 caption 那条被拒绝、被静音或超时未确认，媒体气泡照发不误，读起来像
  是机器人凭空发了一张图/一条语音、自言自语。现在 caption 的 `(ok, _mid,
  failure)` 会被检查，`ok` 为假就 `return failure`、整条发送中止，跟
  `voice.speak()` 在同一个分叉点（先发 caption 再发 record 段）的既有判断
  对齐——`voice.speak()` 一直是这样做的，`_send_attachment` 之前不是。副作
  用：`_record_self_media` 也就不会再把一条从未真正送达的 caption 写进
  recent-chatter 缓冲区，教坏 persona 以为自己说过这句话。

**同一类洞的另外两处，独立发现独立修复**：`_send_attachment` 之外，还有两
条发送路径此前也没有内容策略闸门，理由跟 inline 分支当初的洞一样——它们都
不经过 `send()`：

* **`OneBotAdapter.send_image()` 的远程 URL 分支。** `send_image` 的本地文
  件分支委托给 `send_image_file()` → `_send_attachment()`，那条已经有闸门；
  洞只在远程 URL 分支——它自己把 caption 拼上 `ImageSegment` 直接发到 wire
  上。现在这个分支在构造任何 segment 之前先跑同一套
  `content_policy.moderate_text`（fail-closed），拒绝时函数直接返回，
  caption 和图片一起被挡下，不会出现"图发了、配文被悄悄删掉"这种没人写过
  的内容。
* **`_standalone_send`**——`hermes cron` 在网关没有跑起来时走的那条路，直连
  websocket，不经过任何 adapter 实例。它本来就有紧急禁言闸门，注释里写着
  "只堵住 live adapter 等于把 D44 那个洞原样搬到隔壁进程"；封号风险的内容
  策略是同一个论证站在隔壁那道门。现在在打开 websocket 之前先跑
  `content_policy.moderate_text`，拒绝时复用 `policy_blocked_send_result`
  拍平成这条函数自己的 dict 协议返回——没有为这一条路单独发明一套新的拒绝
  语义。

---

## 5. 决策

| # | 决策 | 理由 |
|---|---|---|
| F1-1 | 不做本地 SILK 编码器 | outbound 音频以 `base64://` 内联发给后端，后端（NapCat/Lagrange）自己转码成 SILK；这不是优化省事，是结构性的——NapCat 通常跑在独立容器里读不到本进程的文件系统，所以本来就必须把字节内联过去而不是给路径，编码目标格式因此也只能由后端决定。代价是投递出去的语音气泡音质不受这个插件控制（`voice.py:40-48`）。|
| F1-2 | `SendGroupAiRecord`/`GetAiRecord` 的 params 同时发 `text` 和 `test` 两个键、值相同 | NapCat 自己的文档在这个字段名上前后矛盾：在用的 API 文档写 `text`，引入这份文档的那次提交写 `test`（`protocol.py:813-822` 的注释附了两个来源）。没有活的 NapCat 可以探测，于是照搬 `_forward_node_to_wire` 已经在用的策略——`name`/`uin` 与 `nickname`/`user_id` 双键并发、后端认哪个用哪个——两种拼法都发，而不是猜一个。`plugins/qq_voice/ai_voice.py:243-250` 的直连（direct）路径手搭 params 时特意保持同样的双键，两条路径不能在这件事上不一致。|
| F1-3 | 不做"用户想打电话"的自然语言检测器 | `voice_call.py` 模块 docstring 点名这是三个分类器里唯一没做的一个：`classify_intent` 只负责判断"已经在振铃/通话中的会话下，这句话是不是 accept/decline/hangup"，从不判断"要不要凭空发起一通呼叫"。误判后果不对称——把"不是在求呼叫"的话读成"想要呼叫"，后果是电话打给了一个没同意的人；而这恰恰是本模块在别处（accept 的判定必须整句话都同意，decline/hangup 只要一个分句命中）反复强调的同一个原则：暧昧输入永远不能读成同意。`offer()` 因此存在但没有调用点，见 §6。|
| F1-4 | 主动语音的降级矩阵不对称 | `proactive.py::VoiceVerdict` 三态：`SPOKEN`（发出去了，记账）、`UNAVAILABLE`（合成失败/超字数/传输故障——**退化成文字**继续发这条帖子）、`REFUSED`（静音或内容策略拒绝——**不**退化成文字，直接放弃这一拍，也不记账）。这条界线是有意义的：`REFUSED` 情况下，同样的文字如果改走 `adapter.send()` 会撞上同一道 gate 得到同样的拒绝，所以"退化成文字"要么是打印一条吓人日志的空操作，要么——万一两道 gate 哪天判断不一致——变成把一次拒绝洗白成一条真实发出的消息。`UNAVAILABLE` 则相反：TTS 服务临时挂掉不该让这一拍的发言彻底消失，比起没有语音，一条文字总比什么都没有强。|
| F1-5 | 主动通话 lane 自己拼开场白，不走 `adapter.start_voice_call()` 的固定振铃词 | `start_voice_call()` 存在、能用，但它说的是固定的 `voice_call.RING_LINE`（"我给你打个电话，现在方便说话吗"）。如果主动通话直接复用它，`proactive_call_prompt` 就成了永远不会生效的死配置，而且**每一通没人要求的电话都说同一句话**——这条 lane 存在的全部意义就是"这通电话是机器人自己决定打的，理应欠对方一个理由"。所以 `_place_call()`（`proactive.py:1518-1600`）复用的是 `voice_call.invite()`/`perform()`/`adapter._call_poke` 同一套底层机制，只替换 `CallDecision.line`（`CallDecision` 是 frozen dataclass，这里是复制不是原地改）。唯一没有复用的是 `adapter._call_speak`：那个函数在语音走不通时会退化成一条文本私信，这对"对方主动要求的通话"是对的（对方在等回应），但对"对方没要求的通话"是错的——"先戳一戳、再发一条没人要的文字"不是电话打不通的温和版本，是又一次骚扰。所以这里语音失败就直接挂断，不降级成文字。|
| F1-6 | 常驻 call sweeper 无论 `voice_call_enabled` 是否为真都会启动 | 理由和 `proactive_loop` 常驻的理由相同，外加一条通话特有的：功能开着的时候有一通电话在打，运维把开关关掉，这通电话必须被收掉（`EndReason.DISABLED`），而不是变成一个再也没有 sweeper 会去清理的僵尸会话。空转一拍的代价只是扫一次空字典加一次配置解析，可以忽略。|
| F1-7 | `qq_voice` 插件通过新的 live-adapter registry（`client.set_live_adapter`/`get_live_adapter`）而不是既有的 live-client registry 找到运行中的 gateway | 已有的 `get_live_client` 只给传输层的 socket，够 `get_cookies` 这种只读调用用；但一次 outbound 语音发送需要的是紧急静音、内容策略、recent-chatter 记录——这些都挂在 adapter 上，不在 client 上。工具层只拿到 client 就得自己重建这份 gate 列表，而 §4 已经证明这类重建每一次都会漏掉几项。所以 adapter 把自己也发布出来，工具层优先用它，没有 gateway 活着（CLI/cron）时才退回裸传输——见 `plugins/qq_voice/README.md`"delivery_path"一节。|
| F1-8 | `qq_voice.max_text_chars`/`qq_voice.daily_max` 是 `config.yaml` 顶层 `qq_voice:` 段的键，不是环境变量，也不放在 `platforms.onebot.extra` 下 | 这两个是阈值，不是密钥——`AGENTS.md`"What we don't want"明确说 `.env` 只留给密钥，非密钥配置走 `config.yaml`。不放进 `platforms.onebot.extra` 是因为这两个键限制的是**模型工具**（`qq_send_voice`/`qq_ai_voice`），跟 `platforms.onebot` 下那一串 OneBot 平台自己的行为开关不是一回事——`plugins/qq_voice/quota.py` 模块 docstring 记了参照实现原先用的是 `QQ_VOICE_MAX_TEXT_LEN`/`QQ_VOICE_DAILY_LIMIT` 两个环境变量，前缀"只是因为它们那时候是环境变量"，搬进 `qq_voice:` 之后自然就不需要了。|
| F1-9 | 网关发送超时（`run_on_adapter_loop` 等不到结果）也算一次配额消耗 | 两个都不对的答案里选代价更小的那个：不计费的后果更糟——超时的成因（后端卡住、事件循环打满）本身会重复出现，模型只要在超时后继续重试，就能在计数器纹丝不动的情况下刷出一串真实计费的语音消息，配额恰好在它存在的理由上失效。计费的代价是最多少算一次真正没送达的消息，而这个损失当场就写在这次调用返回的 `voice_quota` 块里，运维能直接看到、能调大 `qq_voice.daily_max`。`plugins/qzone/state.py::mark_comment` 对"不知道到底发没发出去"的场景做的是同一个选择——未知结果按"已经发生"记，因为传输层错误意味着谁也不知道，而更安全的假设是它确实发生了。|
| F1-10 | `qq_send_voice` 的 `audio_file` 分支（发送一个已经录好的文件，不走本地合成）同样计入每日配额，跟 `text` 分支一视同仁 | 配额限的是"发出去一条 QQ 语音气泡"这件事本身，不是"这次调用有没有花钱合成"。只对 `text` 分支计费会留一个明显的绕过口子：模型可以先用 `text_to_speech` 工具（计费方式完全一样）合成好文件，再用 `audio_file` 分支把它发出去，配额就形同虚设。 |
| F1-11 | `qq_ai_voice` 只受 `qq_voice.max_text_chars` 约束，完全不占 `qq_voice.daily_max` 的配额 | 两个上限管的是不同的资源。字数上限是对**听众**的承诺，语音气泡念多久跟谁付了合成的钱毫无关系——五分钟的机器人语音在 QQ AI 声聊里一样没法跳读、没法搜索、没法快进，所以这条同样适用。每日配额管的是**这个部署要付的账单**：`qq_ai_voice` 的音频是腾讯在它自己那一侧合成的，这次调用对这个部署没有任何边际成本，把它算进一个为"我们付钱的合成"设的预算，等于逼模型去省一个根本不花钱的资源，还会让一个免费工具挤占一个真花钱工具（`qq_send_voice`）的配额。群里说太多话这件事本身，已经由 adapter 自己的紧急禁言和内容策略在网关路径上管着（`speak_ai_character` 走的是同一套闸门），不需要 `qq_voice.daily_max` 再管一遍。所以 `qq_ai_voice` 的结果里从不出现 `voice_quota` 块——没有对应的预算可报。|
| F1-12 | SILK/AMR 解码从 `tools/transcription_tools.py` 里的函数原生化重构成 `agent/audio_decoder_registry.py` 的 provider registry + `plugins/voice_codecs/` 插件（§3.2） | 两条各自独立成立、共同指向同一个结论的理由：`AGENTS.md`"widen the generic plugin surface, don't special-case it in core"要求把编码特定的知识挪出核心；而 SILK/AMR 分别依赖 Hermes 不自带的可选包（`pilk`）和外部二进制（`ffmpeg`），只对会收到这种编码的安装有意义，把这份重量挂在核心上等于让所有不接 QQ/微信语音的安装陪绑。插件命名为 `voice_codecs` 而不是 `qq_voice`/`silk`，是因为 SILK 是微信和 QQ 共用、AMR 是通用电话编码，按平台命名会导致下一个字节相同的桥再造一份轮子。`kind: backend` 而不是 opt-in，是因为 SILK/AMR 语音条今天已经开箱即转录，降级成 opt-in 对每一个正在收这类语音的安装都是一次静默回退，且这个插件不注册工具、不占模型 schema，没有需要用户显式选择的理由。|

---

## 6. 已知缺口，如实列出

1. **没有对活的 NapCat 做过任何验证。** 以下几处形状全部来自阅读文档/源码推
   断，不是实机观测：`send_group_ai_record`/`get_ai_record` 的 `text`/`test`
   双键该发哪个（§5-2）；`get_ai_characters` 的返回结构
   `[{"type": "推荐"|"古风"|"现代", "characters": [...]}]`
   （`protocol.py:638-649` 的 docstring）；Lagrange/go-cqhttp 对这几个
   NapCat-only action 的拒绝到底回什么 retcode（`plugins/qq_voice/ai_voice.py`
   里的 `NAPCAT_ONLY_HINT` 是"很可能是这个原因"的猜测，不是实测确认）。
2. **投递出去的语音气泡音质不受这段代码控制。** §5-1：本进程只内联字节，SILK
   转码在 NapCat 那一侧完成，这里没有旋钮可调。
3. **`voice_call.offer()` 已导出但没有任何调用点。** "对方要求通话"这个方向
   在状态机里是完整的（`voice_call.py:671-693`），但 §5-3 记录的"不做 NL 检测
   器"的决定意味着目前没有任何东西会调用它——`offer()` 目前只有测试在用。
4. **`get_record` 的 `out_format` 只是发出了请求，没有验证过后端真的照办。**
   `protocol.GetRecord(file=file_id, out_format="mp3")`
   （`adapter.py::_VOICE_OUT_FORMAT`）假设后端支持按参数转码；`_resolve_record`
   对拿回来的 `url`/`base64`/`file` 三种形状都做了兜底，但"后端确实按
   `out_format` 转了"这件事本身没有实机验证过。
5. **没有任何东西会去清理一通"主动通话 lane 打出去但没人接"的电话，除了常驻
   sweeper 本身。** `proactive_call_loop`（现在确实由
   `OneBotAdapter._start_proactive_call_loop()` 在 `connect()` 里启动，见
   `plugins/platforms/onebot/proactive.py:143-147`）只负责拨号，挂断响铃超时
   的电话是 `OneBotAdapter._call_sweeper_loop` 的职责；两者靠同一个
   `speech_key(instance_id, chat_id)` 对齐，能对上，但这条 lane 因此**依赖**
   sweeper 确实在跑——一旦它没跑，一通没接的电话会把那个 `key` 卡在
   `ALREADY_LIVE`，后续同一个人不会再被拨第二次。这个方向的降级是安全的
   （最多打扰一次然后沉默），所以记为缺口而非隐患。
6. **ASF/WMA 完全不被核心嗅探器识别。** §3 记录的发现：合并 qqbot 探测器之
   前，它把 ASF/WMA 的头（`\x30\x26\xb2\x75`）错误映射成 `.ogg`；合并之后
   `tools/audio_container.py` 干脆不认识这个 magic，两边都不给出正确答案，
   但现在至少不是一个错误答案。要支持它需要在共享嗅探器里新增一个独立的
   container id，目前没有人提出过这个需求，所以没有做。
7. **代理语音回复没有覆盖 `channel_prompt`。** 通话中的回复经
   `_speak_call_reply` → `voice.speak()`，跟 B4 记录的既有缺口（proactive 文本
   回复同样不设置 `event.channel_prompt`）是同一个上游缺口，本次没有让它变得
   更差，也没有修。
8. **预算状态是进程内存，不是持久化的。** `proactive.py` 里 `_CALLS_TODAY`/
   `_LAST_CALL_MONO` 等都是模块级字典，网关重启即清零——跟 B4 记录的
   `proactive_daily_max` 同类缺口（B4 §8-5）是同一个设计取舍，这里对
   `proactive_call_daily_max`/`voice_call` 会话同样成立。

---

## 7. 验证

以下计数来自实际运行 `scripts/run_tests.sh`，非估计。这份代码在整个撰写过程
中被另一个 agent 并行改动过三轮（AMR/SILK 嗅探、qqbot 探测合并、caption 内容
策略补洞、quota.py 都是在文档写作期间陆续落地的），所以下面是**最后一次**重
跑取到的数字，不是第一次：

```
scripts/run_tests.sh tests/gateway/ -k onebot
=== Summary: 668 files, 1186 tests passed, 0 failed, 1 skipped (100% complete) ===

scripts/run_tests.sh tests/plugins/qq_voice/
=== Summary: 5 files, 170 tests passed, 0 failed (100% complete) ===

scripts/run_tests.sh tests/gateway/test_qqbot.py
=== Summary: 1 files, 119 tests passed, 0 failed (100% complete) ===

scripts/run_tests.sh tests/tools/test_transcription_tools.py
=== Summary: 1 files, 59 tests passed, 0 failed (100% complete) ===
```

外加 `tests/tools/test_audio_container.py` + `tests/tools/test_transcription_qq_containers.py`
+ `tests/agent/test_audio_decoder_registry.py` +
`tests/hermes_cli/test_plugins_audio_decoder_registration.py` +
`tests/plugins/voice_codecs/test_voice_codecs_plugin.py`：
**133 passed, 0 failed**（共享嗅探器本身、QQ/WeChat 容器在转录路径上的核心
派发契约、§3.2 记录的 provider registry 本身、`PluginContext.register_audio_decoder`
端到端注册、以及 SILK/AMR 插件的解码行为——这五个文件合起来是 §3.2 那次原
生化重构的全部测试面）。后三个文件是本次重构新增；`test_transcription_qq_containers.py`
随重构从 18 条重写成 25 条（用例数来自 `pytest --collect-only`，非估计）。

`-k onebot` 命中的文件与各自覆盖范围（本次任务新增/大幅改动的部分）：

| 文件 | 用例数 | 覆盖 |
|---|---:|---|
| `tests/gateway/test_onebot_protocol.py` | 60 | 五个新 action 的 dataclass 与 wire 序列化（含 `text`/`test` 双键）、`segments_to_media` 对 url-less record 的处理 |
| `tests/gateway/test_onebot_voice.py` | 49 | `voice.speak()` 的完整 gate 顺序：静音、内容策略、caption-先于-record、清理、self-recording |
| `tests/gateway/test_onebot_voice_inbound.py` | 17 | url-less `record` 经 `get_record` 解析的三种响应形状（url/base64/file）、无 `get_record` 能力时的降级 |
| `tests/gateway/test_onebot_voice_call.py` | 170 | `voice_call.py` 状态机本身：accept/decline/hangup 分类的宽严不对称、四类超时各自的临界点、group/allowlist 门禁 |
| `tests/gateway/test_onebot_voice_call_wiring.py` | 66 | 状态机接进 adapter 的接线：无会话时行为不变、sweeper 确实清理超时会话、通话中回复走语音、缺键从 `_PRIVATE_YAML_KEYS` 漏掉时功能打不开 |
| `tests/gateway/test_onebot_proactive_voice.py` | 68 | 主动语音/主动通话两条新 lane：modality 不影响是否/何处发言、`REFUSED` 与 `UNAVAILABLE` 两种降级路径、通话 lane 保持沉默的各种前提缺失场景 |
| `tests/gateway/test_onebot_caption_policy.py` | 30 | §4 记录的三处内容策略补洞：`send_image` 远程 URL 分支、`_standalone_send`、`_send_attachment` inline caption 的发送结果不再被丢弃；外加对 `adapter.py` 里每一处会把调用方文本送上 wire 的地方各钉一条测试，证明都过同一道闸门 |
| `tests/tools/test_audio_container.py` | 45 | 共享探测器每一种 magic byte 分支，含 AMR/SILK 两种偏移 |
| `tests/tools/test_transcription_qq_containers.py` | 25（原 18，随 §3.2 重写） | 核心派发层的契约，全部用 fake decoder，不碰真实 pilk/ffmpeg：注册的 decoder 会被咨询、内容优先于文件名、内建格式绝不会走到 registry、找不到 decoder 时报错文案点名缺口（`_unsupported_format_error`）、成功/失败两种结局下临时目录归属正确、source 在解码前按大小上限拦截、`transcribe_audio_local_fallback` 与主路径共享同一套派发；外加 `TestCoreCarriesNoCodecKnowledge`——AST 解析 + 正则搜索双重钉死核心不得出现 `silk`/`amr`/`pilk` 字样或字节字面量（§3.2）。SILK/AMR 的具体行为（真实 pilk/ffmpeg、错误文案）搬到了下一行 |
| `tests/plugins/voice_codecs/test_voice_codecs_plugin.py` | 23（新，§3.2） | 插件半：`SilkDecoder`/`AmrDecoder` 精确注册两个 decoder 且不注册任何工具、内容优先于文件名（真实 pilk/ffmpeg 调用，全部打桩）、声明的扩展名来自共享嗅探器、缺 ffmpeg/缺 pilk 时的可操作错误文案、临时目录清理（成功归调用方、失败自行清空且不泄漏）、`transcribe_audio_local_fallback` 共享同一条预转换路径 |
| `tests/agent/test_audio_decoder_registry.py` | 35（新，§3.2） | provider registry 本身：注册校验（拒绝非 `AudioDecoder`/空名字/畸形 `container_ids`/`extensions`）、内建永远赢按格式而非按名字判定（`TestBuiltinsAlwaysWin`）、与 `SUPPORTED_FORMATS` 的同步由 `TestBuiltinSync` 钉死不漂移、`find_decoder` 的内容优先/后缀兜底/按名字确定性打平/`claims()` 抛异常时被跳过而不炸、`decodable_extensions` 的联合与容错、按 scope 的注册/快照/恢复 |
| `tests/hermes_cli/test_plugins_audio_decoder_registration.py` | 5（新，§3.2） | `PluginContext.register_audio_decoder` 端到端：经 `PluginManager` 真实发现/加载后接受合法 decoder、成功注册后 `supported_formats()` 变宽而 `SUPPORTED_FORMATS` 本身不变、拒绝非 `AudioDecoder` 实例、拒绝抢占内建格式的 decoder（打 warning 而非崩溃）、卸载插件时注册随之释放 |
| `tests/tools/test_transcription_tools.py` | 59（全量） | 转录路径全量回归；本次相关的是
`test_config_device_and_compute_type_passed_to_whisper`（§3.1 记录的 Apple
Silicon 回退修复）——在这次修复之前，这条用例在 Apple Silicon/Rosetta 主机
上永远不可能通过 |
| `tests/gateway/test_qqbot.py` | 119（全量） | 本次相关的是 `_guess_ext_from_data`/`_looks_like_silk` 委托共享嗅探器的那一组：改动前后答案不变的用例（`test_extension_unchanged_by_delegation`）、答案变了且写明原因的用例（`test_extension_changed_by_delegation`，即 §3 记的六种新识别 + RIFF 收窄 + ASF/WMA 不再误判）、`.amr` 兜底仍然生效、以及 AST 解析锁死"两个探测函数体内不得再出现字节字面量"的
`test_detection_helpers_carry_no_magic_bytes` |
| `tests/plugins/qq_voice/test_qq_voice_caps.py` | 71 | `quota.py` 的两个上限：字数上限在合成之前拦截、`daily_max` 只算 `qq_send_voice`（`text` 和 `audio_file` 分支都算）、网关超时算一次消耗、`qq_ai_voice` 完全不受日配额、损坏的计数器文件降级而不是拒绝发送、并发场景下的 peek-then-charge 语义 |
| `tests/plugins/qq_voice/test_qq_send_voice.py` | 43 | 参数校验、音频校验、gateway 在场时必然走 `voice.speak()` 而不碰裸传输 |
| `tests/plugins/qq_voice/test_qq_ai_voice.py` | 24 | 先列出角色再发送、群限定、NapCat-only 拒绝提示 |
| `tests/plugins/qq_voice/test_qq_voice_plugin.py` | 22 | 注册路径、无 OneBot 端点时零 token 占用、走平台自动 toolset |
| `tests/plugins/qq_voice/test_live_adapter_seam.py` | 10 | `set_live_adapter`/`get_live_adapter` 本身：正常、socket 已断开、同一事件循环上会死锁的场景 |

其余 `-k onebot` 命中但本次任务未新增/未大改的文件（`test_onebot_plugin.py` 135、
`test_onebot_group_history.py` 81、`test_onebot_proactive.py` 81、
`test_onebot_bubble_cap.py` 65、`test_onebot_stickers.py` 70、
`test_onebot_content_gate.py` 53、`test_onebot_router.py` 53、
`test_onebot_system_message_suppression.py` 52、`test_onebot_transport.py` 36、
`test_onebot_group_mute.py` 31、`test_onebot_voice_gaps.py` 29、
`test_onebot_persona_binding.py` 21、`test_onebot_rate_limit.py` 18）是既有回归
覆盖，本任务没有修改它们，列出仅为总数对得上。
