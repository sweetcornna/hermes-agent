# A5 — OneBot v11 (QQ / NapCat) + QQ空间 (Qzone) 移植规格

**状态**: 只读调研产物，供实施者直接施工。
**目标仓库**: `/Users/cornna/project/hermes-agent`（分支 `feat/corlinman-migration`）
**来源 1**: `/Users/cornna/project/personal_hermes_ref`（`sweetcornna/personal_hermes` @ `feat/qzone-publish`）
**来源 2**: `/Users/cornna/project/corlinman`（Python 平面 `python/packages/`）

> 本文档**不**重复 hermes 自身插件/平台框架的深挖（A3 并行进行中）。第 5 节用框架中立语言描述"要建哪些模块、每个模块要实现什么行为"，只在必需处引用 hermes 的落地位置。

---

## 0. 先决事实（已核实）

| 事实 | 证据 |
|---|---|
| upstream hermes **没有任何 OneBot/NapCat 支持** | `grep -rli onebot /Users/cornna/project/hermes-agent` 仅命中 `docs/migration-corlinman/00-PLAN.md` |
| upstream hermes **有** `gateway/platforms/qqbot/` —— 但那是 **QQ 官方机器人 API v2**（`api.sgroup.qq.com` + 官方 WS 网关），与 OneBot 无关 | `/Users/cornna/project/hermes-agent/gateway/platforms/qqbot/adapter.py:1-30` |
| upstream 的大多数聊天平台适配器已迁到 `plugins/platforms/<name>/`（discord/telegram/feishu/slack/... 共 22 个），每个目录含 `__init__.py` + `adapter.py` + `plugin.yaml` | `/Users/cornna/project/hermes-agent/plugins/platforms/` |
| 平台插件入口约定：`register(ctx)` → `ctx.register_platform(...)` | `/Users/cornna/project/hermes-agent/plugins/platforms/telegram/adapter.py:10865-10885` |
| 平台适配器抽象方法只有 4 个：`connect`/`disconnect`/`send`/`get_chat_info` | `/Users/cornna/project/hermes-agent/gateway/platforms/base.py:3895,3915,3920,7147`（基类 `BasePlatformAdapter` 在 `:2890`） |
| 工具注册签名与来源 1 完全兼容（`name/toolset/schema/handler/check_fn/requires_env/emoji`） | `/Users/cornna/project/hermes-agent/tools/registry.py:737-751`；`tool_error` 在 `:1282` |
| 目标仓库已有 `tools/tts_tool.py`、`tools/image_generation_tool.py`（来源 1 的两个工具依赖它们）；**没有** `tools/image_with_refs.py` | 目录列举 |

---

## 1. 来源 1 的 OneBot 客户端（`tools/onebot_client.py`）

文件：`/Users/cornna/project/personal_hermes_ref/tools/onebot_client.py`（245 行）。
模块级注释明确声明"这是一个纯 helper 模块，不注册任何工具"（`:25-26`），所以工具注册表扫描不会把它当工具导入。

### 1.1 传输层

**按 URL scheme 自动选择传输**，`http(s)://` → OneBot v11 HTTP API；`ws(s)://` → OneBot v11 正向 WebSocket。

```
onebot_client.py:107  def onebot_call(action: str, params: dict | None = None, *, timeout: int = ONEBOT_TIMEOUT) -> dict
onebot_client.py:122      if _is_ws_url(base): return _onebot_call_ws(base, action, params or {}, timeout)
onebot_client.py:124      return _onebot_call_http(base, action, params or {}, timeout)
```

- **HTTP**（`:131 _onebot_call_http`）：`POST {base}/{action}`，body 为 `json.dumps(params)`，`Content-Type: application/json`。一次动作一次 POST，无连接复用。
- **WS**（`:172 _onebot_call_ws` → `:196 _ws_roundtrip`）：**每次调用新建一条短连接**，发一个 action，读到自己的 `echo` 为止，跳过所有推送事件（最多读 500 帧，`:224`），然后关闭。注释原文："these tools are low-frequency, so a fresh connect per action keeps the sync API simple and stateless"（`:175-177`）。
- WS 依赖 `websockets` 包，**按需惰性 import**；缺包时抛出带安装提示的 `RuntimeError`（`:203-209`）。
- **同步 API 包住异步**：若当前线程没有 event loop 就 `asyncio.run`；若已在 loop 内则丢进 `ThreadPoolExecutor(max_workers=1)` 隔离，避免嵌套 loop（`:179-193`），worker 超时为 `timeout + 15`。

### 1.2 鉴权

```
onebot_client.py:60  def onebot_access_token() -> str        # os.getenv("ONEBOT_ACCESS_TOKEN", "").strip()
onebot_client.py:136-138  headers["Authorization"] = f"Bearer {token}"          # HTTP 分支
onebot_client.py:212-215  uri = f"{uri}{sep}access_token={urllib.parse.quote(token)}"  # WS 分支：查询参数
```

⚠️ **两种传输的鉴权方式不同**：HTTP 走 `Authorization: Bearer`，WS 走 **URL 查询参数** `?access_token=`。OneBot v11 规范两者都允许，NapCat 也都接受。**但来源 2 走的是 WS 握手头**（见 §3.2），这是两源的第一处语义分歧。

### 1.3 配置

```
onebot_client.py:48  def onebot_base_url() -> str    # ONEBOT_HTTP_URL 优先，回退 ONEBOT_WS_URL，rstrip("/")
onebot_client.py:65  def onebot_configured() -> bool # 用作工具的 check_fn，未配置时工具整体不进模型 schema
onebot_client.py:41  ONEBOT_TIMEOUT = 15
```

全部配置来自**进程环境变量**，无 YAML/config 层：`ONEBOT_HTTP_URL`、`ONEBOT_WS_URL`、`ONEBOT_ACCESS_TOKEN`。

### 1.4 事件/消息模型

**没有事件模型。** 这是一个纯出站 RPC 客户端：只有"发一个 action、拿一个 data"。推送事件在 WS 往返中被**丢弃**（`:223-231` 只匹配自己的 echo，其余 `continue`）。没有任何 `MessageEvent` / segment 数据类 / 事件分发。

### 1.5 API 调用面

`onebot_call` 是唯一入口，动作名由调用方传字符串。实际用到的动作只有 3 个：

| 动作 | 调用点 |
|---|---|
| `send_msg` | `tools/onebot_voice_tool.py:209` |
| `get_login_info` | `tools/qzone_tool.py:99` |
| `get_cookies`（`{"domain": "user.qzone.qq.com"}`） | `tools/qzone_tool.py:108` |

### 1.6 错误处理

```
onebot_client.py:83  def _check_onebot_payload(payload: dict, action: str) -> dict
```
- 非 dict → `RuntimeError("... returned a non-object response.")`
- `status == "failed"` → `RuntimeError(f"... failed: {msg} (retcode={retcode})")`，msg 取 `message` 或 `wording`
- `data is None` → `RuntimeError("... returned no data.")`
- HTTP 分支另有 `HTTPError`（带前 200 字符响应体）/ `URLError`（"is NapCat/Lagrange running?"）/ JSON 解析失败三类映射（`:144-163`）
- WS 分支：`OSError`/`TimeoutError` → "Cannot reach OneBot"；读满 500 帧仍无 echo → `"got no matching reply (echo timeout)"`（`:243-245`）

### 1.7 重试 / 重连

**完全没有。** 无退避、无重连、无心跳、无长连接。每次调用独立；失败直接抛 `RuntimeError`，由工具 handler 转成 `tool_error(...)` 原样给模型看。工具文档字符串明说 "the tool never silently retries"（`onebot_voice_tool.py:22-23`、`qzone_tool.py:32-33`）。

### 1.8 语音工具（`tools/onebot_voice_tool.py`，297 行）

注册一个 LLM 可调用工具 `qq_send_voice`，toolset `qq_voice`（`:289-297`，`check_fn=onebot_configured`，`requires_env=["ONEBOT_HTTP_URL"]`，emoji `🎙️`）。

纯函数（已单测）：
```
onebot_voice_tool.py:47  def _coerce_qq_id(value, field: str) -> int          # 容忍 int / 数字字符串，必须 > 0
onebot_voice_tool.py:62  def _build_record_message(audio_b64: str) -> list    # [{"type":"record","data":{"file":f"base64://{b64}"}}]
onebot_voice_tool.py:71  def _build_send_params(message, user_id, group_id) -> dict
onebot_voice_tool.py:94  def _read_audio_file(path: str) -> bytes
onebot_voice_tool.py:119 def _synthesize_speech(text: str) -> str             # 复用 tools.tts_tool.text_to_speech_tool（惰性 import）
```
限制：`_AUDIO_EXTS = {.mp3,.wav,.ogg,.amr,.silk,.m4a,.flac,.aac}`（`:39`），`_MAX_AUDIO_BYTES = 30 MiB`（`:40`）。
音频以 `base64://` **内联**投递（`:62-68`），理由写在文档字符串里：hermes 与 NapCat 常在不同容器/主机，不能假设共享文件系统。SILK 转码交给 NapCat。
参数互斥校验：`text` 与 `audio_file` 二选一；`user_id` 与 `group_id` 二选一（`:157-183`）。

---

## 2. 来源 1 的 QQ空间工具（`tools/qzone_tool.py`，651 行）

### 2.1 支持的操作

**只有一个：发布说说。** 工具名 `qzone_publish`，toolset `qzone`（`:643-651`）。

| 能力 | 支持？ | 位置 |
|---|---|---|
| 发说说（纯文本） | ✅ | `_handle_qzone_publish` `:482` |
| 发说说（附本地图片，最多 9 张） | ✅ | `_MAX_IMAGES = 9` `:76`，`_upload_image` `:460` |
| 发说说（AI 生成配图，`generate` 提示词） | ✅ | `_generate_image` `:385`，默认 `aspect_ratio="square"` `:81` |
| 回复/评论说说（`qzone_reply`） | ❌ 无 | — |
| 好友动态流（`qzone_friends`） | ❌ 无 | — |
| 点赞 / 删除 / 读取自己的说说 | ❌ 无 | — |

> 生产上 corlinman 跑过的 scheduler job 里有 `hermes.qzone_reply` / `hermes.qzone_friends` / `hermes.qzone_daily`（`docs/migration-corlinman/00-PLAN.md:24-26`），但**来源 1 的仓库里只有 publish**。reply / friends / feed 的现成实现在 corlinman 侧，见 §3.12。

### 2.2 鉴权：借用 OneBot 的登录态

QQ 早已废弃 QZone OpenAPI 的 `emotion` 接口，所以本工具直接打 **QZone web 端点**，需要登录 cookie + `g_tk` CSRF token。它**不自己扫码登录**，而是从跑着的 NapCat/Lagrange 借：

```
qzone_tool.py:97   def _get_login_uin() -> str          # onebot_call("get_login_info") -> data["user_id"]
qzone_tool.py:106  def _get_qzone_cookie_string() -> str # onebot_call("get_cookies", {"domain": "user.qzone.qq.com"})
qzone_tool.py:122  def _compute_gtk(p_skey: str) -> int  # h=5381; h += (h<<5)+ord(ch); return h & 0x7FFFFFFF
qzone_tool.py:133  def _extract_cookie_value(cookie_str, key) -> str | None
```
- cookie 域常量 `_QZONE_COOKIE_DOMAIN = "user.qzone.qq.com"`（`:66`）
- `g_tk` 由 **`p_skey`** 算（不是 `skey`）；`p_skey` 缺失即报"登录态可能过期"并终止（`:532-537`）
- `skey` 取不到时降级为空串（`:538`）
- 请求头固定桌面 UA（`_DESKTOP_UA` `:69-72`），注释说明移动 UA 会走到另一套流程

### 2.3 端点与线格式

```
qzone_tool.py:58  QZONE_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
qzone_tool.py:62  QZONE_UPLOAD_URL  = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
```
两者都以 `?g_tk={gtk}` 追加 token（`:456`、`:470`），body 为 `application/x-www-form-urlencoded`，头部带 `Cookie` + `Referer: https://user.qzone.qq.com/{uin}`（`_qzone_post` `:430-451`）。

表单构造（均为纯函数，已单测）：
```
qzone_tool.py:182 _build_publish_form(text, uin, pic_infos)  # con=text, hostuin=uin, format=json, qzreferrer=...
                                                             # 有图时 richtype="1", richval=_build_richval(...)
qzone_tool.py:209 _build_upload_form(image_b64, filename, uin, skey, p_skey, gtk)  # base64="1", picfile=<b64>, albumtype="7", refer="shuoshuo"
qzone_tool.py:160 _build_richval(pic_infos)  # ",{albumid},{lloc},{sloc},{type},{height},{width},,{height},{width}" 逗号段，段间 TAB 连接
qzone_tool.py:142 _extract_pic_info(data)    # lloc/sloc 回退 photoid，url 回退 pre
```
`_build_richval` 的注释点名："If Tencent changes the format, this is the single place to fix."（`:164-165`）

响应解析：
```
qzone_tool.py:303 _extract_json_object(raw)  # re.search(r"\{.*\}", text, re.DOTALL) —— 剥掉 _Callback(...) / frameElement.callback(...) JSONP 壳
qzone_tool.py:242 _parse_publish_response(raw)  # status = ret if ret is not None else code；status==0 且 subcode in (0,None) 即成功
qzone_tool.py:275 _parse_upload_response(raw)   # 只认 ret==0
```
**注意 publish 有两种成功形状**：老式 `{"ret":0,"tid":...}` 与新式 `{"code":0,"tid":...,"feedinfo":...}`。文档字符串说新式是**实测**得来的（"verified live — NapCat/QZone returns `code` with no `ret`"，`:250-253`）。tid 取 `tid` 或 `t1_tid`。

### 2.4 持久化状态

**零。** 工具完全无状态：不缓存 cookie、不缓存 `g_tk`、不记录已发 tid、不落盘任何东西。每次调用都重新问 OneBot 要 `get_login_info` + `get_cookies`。

### 2.5 失败模式

| 失败 | 表现 | 位置 |
|---|---|---|
| 未配置 OneBot | 工具不进 schema（`check_fn=_check_qzone_available`） | `:579-581` |
| OneBot 不可达 / 未登录 | `Could not borrow QQ login state from OneBot: ...` | `:525-530` |
| cookie 为空（登录态过期） | `get_cookies returned an empty cookie string — ... re-login the NapCat/Lagrange client.` | `:110-114` |
| cookie 里没有 `p_skey` | `p_skey not found in OneBot cookies — ...` | `:532-537` |
| 图片路径坏 / 类型不支持 / 空文件 / 超 20 MiB | 网络调用**之前**就失败 | `_read_image_file` `:324`，`_MAX_IMAGE_BYTES` `:77` |
| 图片数 > 9 | 直接拒绝 | `:500-505` |
| 图片上传被拒 | `Image upload failed for '<file>': <msg>` | `:544-550` |
| QZone 拒绝发布（风控 / 验证码 / 参数漂移） | `QZone rejected the post: <msg>` + `code` | `:569-572` |
| 响应无法解析 | `unparseable QZone response: <前200字符>` | `:257-259` |
| 生图后端未配置 / 生图失败 | 在碰 QZone 之前中止 | `:518-523`，`:399-403` |

超时：发布 20s（`_QZONE_TIMEOUT` `:83`），上传/下载 60s（`_QZONE_UPLOAD_TIMEOUT` `:84`）。
模块头部明确写了合规风险："automated posting violates Tencent's Terms of Service and carries an account-ban risk"（`:28-33`）。

### 2.6 相关旁支：`tools/image_with_refs.py`

`/Users/cornna/project/personal_hermes_ref/tools/image_with_refs.py`（391 行，toolset `image_refs`，`:382-390`）。
为"格兰每日说说配图"专门写的：把场景提示词 + 0–4 张角色立绘（`~/.hermes/characters/*.png`）送到 OpenAI-compatible Responses API 的 `gpt-image-2` 工具，做形象锚定。慢（2–5 分钟/次），内置 3 次重试。`requires_env=["OPENAI_API_KEY"]`。
**目标仓库没有这个文件**，若要复刻"每日说说"链路需一并移植。

### 2.7 结论：来源 1 **没有**入站聊天平台适配器 —— 已核实

穷举验证（不是抽样）：
- `grep -rl -i onebot`（排除 `.git`）在整个 `personal_hermes_ref` 只命中 **7 个文件**：`toolsets.py`、`.env.example`、`tools/qzone_tool.py`、`tools/onebot_voice_tool.py`、`tools/onebot_client.py`、`tests/tools/test_onebot_client.py`、`tests/tools/test_onebot_voice_tool.py`、`tests/tools/test_qzone_tool.py`。
- `gateway/platforms/` 下唯一的 QQ 目录是 `qqbot/`，其 `adapter.py:1-5` 自述 "QQ Bot platform adapter using the **Official QQ Bot API (v2)** ... `api.sgroup.qq.com`"，配置读 `app_id` / `client_secret`（`:9-14`）——是官方开放平台机器人，**不是** OneBot/NapCat。
- `gateway/config.py:128` 只有 `QQBOT = "qqbot"` 一个 QQ 平台枚举，没有 onebot 项。

**因此：来源 1 只实现了出站/可调用能力（工具），没有任何入站消息适配器。这一条正如任务描述所说，确认成立。** 入站必须整套从来源 2 移植。

---

## 3. 来源 2 的入站通道（corlinman）

### 3.1 文件与规模

| 文件 | 行数 | 角色 |
|---|---|---|
| `python/packages/corlinman-channels/src/corlinman_channels/onebot.py` | 1361 | OneBot v11 正向 WS 适配器 + 线类型 |
| `.../channel.py` | 409 | `Channel` Protocol + 注册表 + `spawn_all` |
| `.../router.py` | 577 | **回不回复**的判定（白名单/@/关键词/冷却/限流/斜杠命令） |
| `.../rate_limit.py` | 281 | `TokenBucket`（令牌桶）+ `SlidingWindowCounter`（硬窗口） |
| `.../persona_inject.py` | 269 | 人格 system_prompt 注入（跨渠道共享） |
| `.../service.py` | 356 KB | QQ 编排：`run_qq_channel` / 派发循环 / 单轮处理 / 健康 / 主动发言 / 群摘要 |
| `.../common.py` | — | `ChannelBinding` / `Attachment` / `InboundEvent` |

### 3.2 连接与订阅

corlinman 是**客户端**，主动拨号 NapCat 的正向 WS（`onebot.py:6-8`）。

```
onebot.py:721  @dataclass class OneBotConfig: url; access_token=None; self_ids=[]; reconnect_schedule=RECONNECT_SCHEDULE; ping_interval=PING_INTERVAL
onebot.py:76   RECONNECT_SCHEDULE = (1.0, 2.0, 5.0, 10.0, 30.0)   # 最后一项重复（饱和）
onebot.py:79   PING_INTERVAL = 30.0
onebot.py:735  class OneBotAdapter
onebot.py:906  async def connect(self)   # 只起后台 reader task，不阻塞等握手
onebot.py:1105 async def _connect_once(self)
onebot.py:1108-1109  headers.append(("Authorization", f"Bearer {self._cfg.access_token}"))
onebot.py:1111 websockets.connect(url, additional_headers=headers or None, ping_interval=self._cfg.ping_interval)
```
- **鉴权走握手头** `Authorization: Bearer <token>`（对比 §1.2，来源 1 的 WS 走查询参数）。
- 重连循环在 `_reader_loop`（`:1074`）里：干净断开后 `attempt` 归零，连续失败按 schedule 退避。**`inbound()` 迭代器永不抛瞬时传输错误**，调用方只会看到重连后的下一个事件；只有致命配置错（空 URL）在构造时抛 `ConfigError`（`:758-759`）。
- 握手成功立刻打时间戳 `_last_event_at_ms`（`:1118-1122`），让健康监视器在重启后 ~2s 内翻回 online，而不是等 30s 心跳。

### 3.3 事件模型与规范化

**类型化线模型**（tagged union）：
```
onebot.py:88  class MessageType(StrEnum): PRIVATE="private"; GROUP="group"
onebot.py:96  Sender(user_id, nickname, card, role)
onebot.py:106 MessageEvent(self_id, message_type, user_id, message_id, message, time, sub_type, group_id, raw_message, sender)
onebot.py:122 NoticeEvent / :133 MetaEvent / :142 RequestEvent / :154 UnknownEvent
onebot.py:158 Event = MessageEvent | NoticeEvent | MetaEvent | RequestEvent | UnknownEvent
```
消息段：`TextSegment`(`:171`) `AtSegment`(`:178`) `ImageSegment`(`:184`) `ReplySegment`(`:193`) `FaceSegment`(`:200`) `RecordSegment`(`:207`) `VideoSegment`(`:215`) `FileSegment`(`:228`) `ForwardSegment`(`:243`) `OtherSegment`(`:250`，兜底带原始 JSON)。解析表在 `:270-281`。

```
onebot.py:285 def _parse_segment(raw) -> MessageSegment
onebot.py:297 def _coerce_int(value, default=0) -> int   # 绝不抛异常：一个坏字段不能拆掉整条 WS
onebot.py:318 def parse_event(raw) -> Event              # 未知 post_type → UnknownEvent
onebot.py:391 def segments_to_text(segments) -> str      # text 原样；at → "@<qq> "（保证关键词路由能看到地址）
onebot.py:407 def segments_to_attachments(segments) -> list[Attachment]
onebot.py:458 def is_mentioned(segments, self_id) -> bool  # qq == str(self_id) 或 qq == "all"
onebot.py:1305 def _normalize_message_event(ev) -> InboundEvent[MessageEvent]
```
`_normalize_message_event` 产出统一信封：`channel="qq"`、`binding`（群 → `ChannelBinding.qq_group(self_id, group_id, user_id)`，私聊 → `qq_private(self_id, user_id)`，见 `common.py:109/:119`）、`text`、`message_id`、`timestamp`、`mentioned`、`attachments`、`payload=原始 MessageEvent`。

`inbound()`（`:941`）**只吐 `MessageEvent`**，meta/notice/request 静默吞掉（但仍被解析，以免拆连接）。

### 3.4 媒体处理（入站）

`segments_to_attachments`（`:407-455`）把段映射为 `Attachment`：
| 段 | Kind | mime | 备注 |
|---|---|---|---|
| `image`（url 非空） | IMAGE | `image/*` | 带 `file_name` |
| `record`（url 非空） | AUDIO | `audio/*` | |
| `video`（url 非空） | VIDEO | `video/*` | NapCat 短视频 |
| `file`（url 非空） | DOCUMENT | `application/octet-stream` | NapCat 扩展的群文件/文档 |

**空 URL 一律跳过**（gocq 对离线媒体会给空 url）。

### 3.5 谁该被回复：`ChannelRouter.dispatch`

`router.py:294 def dispatch(self, event, *, enable_commands=True, slash_policy=None) -> RoutedRequest | None`

判定顺序（**顺序本身就是语义，必须照搬**）：

1. **@提及目标解析**（`:322-326`）：`mention_targets = [event.self_id] if event.self_id > 0 else self.self_ids`。事件自带的 `self_id` 权威，配置里的 `self_ids` 只是种子回退 —— NapCat 换号登录后不用改配置。
2. **文本扁平化**（`:328`）：`_flatten_and_trim`（`:557`）优先用 OneBot 给的 `raw_message`，否则 `segments_to_text`。
3. **私聊：无条件放行**（`:334-335`）→ `qq_private` binding。
4. **群聊闸门链**（`:336-355`）：
   a. `group_replies_enabled == False` → 直接 `None`（**在 @/关键词之前**，是"群内紧急静音"总开关）。
   b. `group_id is None` → `None`。
   c. **白名单硬闸**：`group_whitelist is not None and str(group_id) not in whitelist` → `None`。注释明说 **@提及也不能绕过**（`:342-348`、`:211-213`）。空 set 也是"只允许列表内"，即全部拒绝。
   d. `explicit = mentioned or match_command_with_args(text) is not None`（斜杠命令等同于点名召唤，`:349-352`）。
   e. `_group_reply_allowed(group_id, text, explicit)`（`:489`）：
      - `explicit` → 放行并**重置冷却时钟**。
      - 否则按 `group_reply_policy`：
        - `"mention_or_keyword"`（默认）：只有该群**显式配置了关键词列表**才做大小写不敏感子串匹配；没配 = 只回 @（`:506-510`）。
        - `"all"`（legacy）：走 `_keyword_match`（`:520`），**没配关键词 = 全部回复**。
      - 命中后再过**每群冷却** `group_reply_cooldown_secs`（仅约束非 @ 的回复，`:513-517`）。
5. **空文本丢弃**（`:360-362`）：纯表情/纯撤回占位符。
6. **限流**（`:364-378`，**在关键词/提及之后**，保证被过滤的消息不消耗令牌）：
   - 每群桶 key `f"{channel}:{thread}"`
   - 每人桶 key `f"{channel}:{thread}:{sender}"`
   - 丢弃时触发 `rate_limit_hook(channel, reason)` 与 hook bus 的 `RateLimitTriggered`（`:463-487`，`limit_type = f"{reason}_{channel}"`）
7. **斜杠命令解析**（`:380-441`）：命中且被 `SlashAccessPolicy` 拒 → `command_refused=True` + 拒绝文案；命中带 handler → 保留原文、由调用方直接执行并跳过 agent；命中只有 `wizard_prelude` → 用 prelude 改写 `content`；形似命令但未注册 → `unknown_command_notice`。
8. 返回 `RoutedRequest`（`:129`）：`binding / content / message_id / timestamp / mentioned / command_spec / command_args / sender_name / reply_to_text / unknown_command_notice / command_refused`，`session_key` 由 binding 派生（`:180-184`）。

### 3.6 限流的两套机制

`rate_limit.py` 提供**两个不同语义**的限流器，QQ 通道两个都用：

- `TokenBucket`（`:73`）：连续线性回填的**突发预算**。`per_minute(n)` → capacity = n，refill = n/60 每秒（`:101-105`）。`check(key)`（`:114`）。带后台 GC：1 小时未动的 key 被清（`GC_STALE_AFTER=3600` `:58`，`GC_INTERVAL=300` `:62`，`start_gc` `:160`）。→ 用于 router 的 group/sender 维度。
- `SlidingWindowCounter`（`:209`）：**精确硬窗口**（"M 分钟内最多 N 条"）。`allow(key, window_secs, max_count, *, record=True)`（`:243`），`window<=0 or max<=0` 即关闭。窗口/上限**每次调用传入**，所以改配置无需重建对象；实例是模块级的，**能跨通道任务重启存活**（`:218-222`）——corlinman 每次存配置都会重启 QQ 实例。→ 用于"群发言硬上限"，**回复与主动发言共用同一个预算**。

群发言硬上限的挂点在派发循环（`service.py:2822-2843`）：
```
service.py:798  def _qq_speech_window_cfg(cfg) -> tuple[float, int]   # (group_rate_limit_window_minutes*60, group_rate_limit_max_messages)
service.py:794  def _qq_speech_key(instance_id, group) -> f"{instance_id}:{group}"
service.py:776  _QQ_GROUP_SPEECH = SlidingWindowCounter()  # 模块级
```
它在**斜杠命令短路之后、模型调用之前**检查，注释写明理由：斜杠命令（运维工具）不该被锁死，而被限的群不该白烧一次模型调用；**@提及不能绕过它**（`service.py:2816-2821`）。命中时打 `rate_limit_hook("qq", "group_window")`。

### 3.7 人格注入

`persona_inject.py:187 async def inject_persona_if_enabled(request, *, humanlike_enabled, persona_id, persona_store, humanlike_resolver=None, asset_store=None, channel_name="channel")`

- 解析优先级：`humanlike_resolver()` 返回的 `(enabled, persona_id)` **覆盖**静态字段（`:226-239`）——这就是"管理端改开关，下一条消息就生效、不用重启通道"的机制。
- 命中后在 `request.messages` **最前面**插一条 `role="system"`，内容 = persona 的 `system_prompt` + 可选的 `## Available emoji` 块 + `\n\n---\n`（`:256-268`）。
- `compose_persona_emoji_block`（`:104`）列出该人格所有 `kind="emoji"` 资产的 `label: 绝对路径`，教模型可以用 `send_attachment` 发贴纸；没有资产或没接资产库就返回 `None`（不渲染空标题）。
- 还会应用人格的文本模型绑定：`apply_persona_text_model_binding`（`:91`）设置 `request.model` / `request.provider_hint`。
- **全程 best-effort**：任何 store/resolver 异常只打 warning 后原样返回，"persona is decorative; chat must keep working when it breaks"（`:222-224`）。
- QQ 侧薄封装：`service.py:4126 async def _qq_inject_persona_if_enabled(request, params)`。

### 3.8 出站发送路径

**动作类型**（`onebot.py:473-575`）：`SendPrivateMsg`、`SendGroupMsg`、`SendGroupForwardMsg`、`SendPrivateForwardMsg`(NapCat 扩展)、`SetInputStatus`(NapCat 扩展，"对方正在输入…")、`UploadPrivateFile`、`UploadGroupFile`。
序列化：`_segment_to_wire`（`:590`）、`action_to_wire`（`:630`）、`_forward_node_to_wire`（`:696` —— **同时发 go-cqhttp 的 `name`/`uin` 与 NapCat 的 `nickname`/`user_id` 两套键**，一套线格式兼容两种后端）。

**发送 API 两条**：
```
onebot.py:1018 async def send_action(self, action) -> None                      # 入队即返回
onebot.py:1032 async def call_action(self, action, *, timeout=15.0) -> dict      # 带 echo 关联，等 NapCat 的响应信封
```
`call_action` 用于"投递结果真的重要"的场合（文件上传、合并转发卡片）：retcode 0=ok、1=async-accepted，其余视为被拒。

**写循环**（`_writer_loop` `:1237`）：
- 有 `_outbound_front` 双端队列做**队首重投**，保证瞬时失败重发时不乱序（asyncio.Queue 没有 push-left）。
- 同一 action 连续失败 2 次即**丢弃**（毒消息保护，`:1272-1282`），否则重投队首并抛 `TransportError` 触发重连。
- 取消（cancel）不计入重试次数，只重投。

**读循环**（`_pump` `:1130`）：
- 忽略二进制帧、非 dict JSON。
- 每帧刷新 `_last_event_at_ms`；心跳帧读 `status.online`（兼容 `true` / `1` / 嵌在 `status.app.online` / `status.good`；都没有则判为 `False`）——这是 **NapCat WS 还活着但 QQ 号被踢下线**的唯一信号（`:1152-1199`）。
- 带 `echo` 的响应信封解掉对应 future 并 **不进入入站队列**（`:1203-1210`）。
- 入站队列 `maxsize=64`，**满了丢最旧**（保证最新一条用户消息一定在），并累计 `inbound_dropped`（`:1211-1230`）。出站队列同样 `maxsize=64`。

**回复动作构造**：
```
service.py:4378 def _build_reply_action(event, body, *, prepend_at_mention=True, image_urls=None, image_files=None) -> Action
```
群回复默认在最前面加 `AtSegment(qq=str(event.user_id))` 再接 `TextSegment(text=f" {body}")`；**分片时只有第 0 片能带 @**，否则群里会被 @ N 次、触发腾讯反垃圾（`:4396-4404` 注释）。私聊不加 @。

**单轮处理** `service.py:3765 async def handle_one_qq(chat_service, req, event, model, adapter, cancel, *, inbox=None, inbox_id=None, params=None)`：
1. 构造内部请求 → 可选人格注入 → `inbox.mark_dispatched`。
2. **私聊**起 `_qq_input_status_pulse`（`:3391`，每 5s 重发 `SetInputStatus`，NapCat ~5s 自动清除）；**群里不发**（QQ 群不渲染输入中）。
3. 消费 chat 流：`token_delta` 累积文本；`tool_call`/`tool_result` 记入 activity 列表；`send_attachment` 工具调用**在此处真正执行**（`_qq_send_attachment`）；`awaiting_approval` 走审批菜单并提前释放并发许可。
4. 组装正文：内容策略拦截 → 安全拒绝文案；错误 → `[corlinman error] <msg>`；空回复且有工具活动 → 只发 `📋 本次操作:` 摘要块；空回复且无活动 → 静默丢弃。
5. **气泡切分**：先按人格标记 `[MSG_BREAK]` 切成多条消息（`common.py:285-286`），气泡间 `asyncio.sleep(0.3)`。
6. **长文折叠**：单气泡 > `_QQ_FORWARD_TEXT_THRESHOLD = 1000`（`service.py:3262`）→ 折成一张合并转发"聊天记录"卡片（`_qq_send_forward_reply` `:3363` / `_qq_deliver_forward` `:3305`）；群里卡片带不了 @，所以先发一行引导语 `_QQ_FORWARD_LEAD_TEXT`（`:3266`）把 @ 补上。卡片被拒（retcode ∉ {0,1}）则回退普通分片，内容永不丢。
7. **分片**：`chunk_reply(bubble, _QQ_TEXT_LIMIT)`，`_QQ_TEXT_LIMIT = 3800`（`:3256`，NapCat 实测上限 4500–5000，留安全余量）。
8. 群里把自己发出去的内容写回主动发言上下文缓冲（`is_self=True`）→ `inbox.mark_done`。

**出站附件路由**（`_qq_send_attachment` `service.py:3074`）按 MIME 分三路：
| 类型 | 走法 |
|---|---|
| `image/*` | 内联 `ImageSegment(file="base64://…")`，随 `SendGroupMsg`/`SendPrivateMsg` 发（表情包/图直接出现在聊天里，不进群文件） |
| 音频（`voice_out.is_audio`，覆盖 `.silk`/`.amr` 等 mimetypes 认不出的容器） | 内联 `RecordSegment(file="base64://…")` |
| 其他 | `UploadGroupFile`/`UploadPrivateFile`；≤ `_QQ_FILE_BASE64_MAX_BYTES = 30 MiB`（`:3271`）用 `base64://`，超了退回字面路径；用 `call_action` 等响应，超时 `_QQ_UPLOAD_RESPONSE_TIMEOUT_SECS = 120.0`（`:3276`） |
**统一用 `base64://` 的理由写在注释里**：Docker 部署下 corlinman 与 NapCat 是两个容器，NapCat 读不到 `/data/...` —— 与来源 1 语音工具的判断完全一致。

### 3.9 编排、健康与主动发言

```
service.py:430  @dataclass class QqChannelParams   # config/instance_id/health/identity_guard/model/chat_service/rate_limit_hook/hook_bus/inbox/group_history/humanlike_*/persona_store/asset_store/tencent_policy_resolver/identity_ready/event_emitter/rag_search
service.py:557  async def run_qq_channel(params, cancel) -> None
service.py:2579 async def _qq_dispatch_loop(adapter, router, params, cancel, *, monitored_groups=frozenset())
service.py:2902 async def _qq_run_one(...)          # 信号量包装
service.py:2322 async def _qq_health_watcher(adapter, cancel, *, health=None)
service.py:1149 async def _qq_proactive_loop(adapter, params, cfg, cancel, *, health)
service.py:2179 async def _qq_monitor_digest_loop(...)
service.py:536  def _qq_group_whitelist(cfg)        # None = 关闭白名单
service.py:542  def _qq_router_gates(cfg)           # 5 个热更闸门值的可比元组
```
- `run_qq_channel` 校验 `ws_url` 非空（`:552-554`），建两个令牌桶（`rate_limit.group_per_min` / `sender_per_min`，`:571-580`），建 router，起 adapter，然后并行跑：派发循环 + 健康监视 + 主动发言循环 + 群摘要循环。
- **热更**：派发循环每条事件重算 `_qq_router_gates`，变了就 `dataclasses.replace` 重建 router（冷却状态与限流器随之保留），无需断 WS（`:2621-2640`）。可热更键白名单见 `corlinman-server/.../qq_instances/runtime.py:667-681`（`_QQ_HOT_APPLY_KEYS`，外加所有 `proactive_*` 前缀键）。
- **并发**：`asyncio.Semaphore(_channel_max_concurrency("QQ"))`，**先 acquire 再 create_task**，让背压顶到 WS 读端（`:2854-2857`）。
- **健康**：`_qq_health_watcher` 区分两个概念 —— `online`（WS 心跳）与 `account_online`（QQ 号真实在线）。NapCat 被踢下线时 WS 心跳照常但 `status.online` 翻 False。env 调参：`CORLINMAN_QQ_HEALTH_PROBE_S`(30)、`CORLINMAN_QQ_HEALTH_LOST_S`(120)、`CORLINMAN_QQ_ACCOUNT_PROBE_S`(60)。健康快照结构见 `new_qq_health()`（`:718-733`）。`_napcat_http_base`（`:2456`）能从 ws URL 语法推出 NapCat 的 HTTP 面（翻 scheme、去 `/onebot[/v11]` 后缀），可被 `CORLINMAN_NAPCAT_HTTP_URL` 覆盖。
- **主动发言**（生产已开启）：配置解析 `_qq_proactive_config`（`:869`），循环 `:1149`。逐条闸门：活跃时段（`proactive_active_start_hour`/`end_hour`，按 `proactive_timezone`）→ 健康在线 → 身份就绪 → **`group_replies_enabled` 总开关**（`:1223-1224`）→ 概率 `proactive_probability` → 每群每日上限 `proactive_daily_max` → 距上次 ≥ `proactive_min_gap_minutes` → 群发言硬窗口未满（`record=False` 预检）→ **群里最后一条不能是自己发的**（否则连发像刷屏，`:1243-1245`）。然后随机选一个合格群，可选 RAG 检索，组提示词，跑一次人格 turn；模型回 `SKIP` 就不发（`_qq_proactive_is_skip` `:1113`）。发完记账：日计数 + 时间戳 + 硬窗口 record + 写回上下文缓冲。
- **群摘要 monitor**（生产 3 条：sanhu 10:00、jlu 11:00、qunjlu 09:00）：`_qq_monitor_parse_entry`（`:1479`）解析 `monitors[]`，字段 `id`/`enabled`/`sources`/`target_type`(group|user)/`target_id`/`schedule_type`(daily|interval)/`daily_time`/`interval_minutes`/`window_minutes`/`timezone`/`style_extra`/`send_when_empty`。被 monitor 覆盖的群，其**全部**消息（含被 router 过滤掉的）会落库到 `qq_group_history.sqlite`（`service.py:143-155`、`:2694-2716`）。

### 3.10 持久化状态清单（来源 2）

| 状态 | 位置 | 语义 |
|---|---|---|
| inbox（消息生命周期 pending→dispatched→done/dead） | `corlinman_server.inbox`，sqlite | 崩溃后可重放（`_replay_qq_inbox_rows`） |
| 群历史 | `qq_group_history.sqlite` | 仅被 monitor 覆盖的群 |
| 群发言硬窗口计数 | **进程内模块级** `_QQ_GROUP_SPEECH` | 跨通道任务重启存活，跨进程重启丢失 |
| 群最近聊天缓冲（30 条，含自己发的） | 模块级 `_QQ_GROUP_RECENT` / `_QQ_GROUP_RECENT_MAX=30` | 主动发言的上下文 |
| 主动发言日配额 / 上次时间 | 模块级 `_QQ_PROACTIVE_SENT` / `_QQ_PROACTIVE_LAST_MONO` | 同上 |
| 每群回复冷却时钟 | router 实例字段 `_last_group_reply_mono` | 重建 router 时通过 `dataclasses.replace` 保留 |
| 健康快照 | 内存 dict（`QQ_HEALTH` / per-instance） | 管理端只读 |

### 3.11 NapCat 容器管理（`corlinman-server/.../system/napcat_manager/`，2445 行）

> **结论先行：整包不移植**（D3 约束 —— 复用既有容器，不起第二个 NapCat）。这里记录它的边界与可借鉴点。

| 文件 | 行数 | 职责 |
|---|---:|---|
| `manager.py` | 476 | 串行化、可崩溃恢复的生命周期协调器 |
| `docker_provider.py` | 450 | Docker SDK provider（每实例 1 容器 + 2 具名卷） |
| `native_provider.py` | 339 | systemd 模板单元 provider + token/env 文件写入 |
| `server.py` | 330 | root 拥有的 Unix socket 服务端 + `main()` |
| `inventory.py` | 246 | 非密清单 JSON + 路径归属断言 |
| `operation_journal.py` | 192 | 单条目、fsync 的变更前后日志 |
| `models.py` / `protocol.py` / `client.py` / `__init__.py` | 159/81/133/39 | 线数据类 / Provider Protocol / 网关侧客户端 / 门面 |

**关键点（本次只借鉴、不移植）**：
- 启停用 **docker SDK 直连，不是 compose**（`docker_provider.py:409-412`），容器名 `corlinman-napcat-{instance_id}`（`:75`），默认镜像 `mlikiowa/napcat-docker:v4.18.4`。
- **Docker 模式不映射任何 host 端口**，只靠 compose 网络的容器名寻址：`ws://{container}:3001` / `http://{container}:6099`（`docker_provider.py:304-305`）。Native 模式动态分配 `16099..16999` 区间的 `(webui, onebot)` 端口对（`manager.py:419-463`）。
- **Token 生成**：`native_provider.py:271` `_write_token_file` 用 `secrets.token_urlsafe(32)` 生成两个**互相独立**的密钥 `WEBUI_TOKEN` 与 `ONEBOT_TOKEN`，`O_CREAT|O_EXCL` + `0600` + fsync（含父目录）。存于 `<state_root>/<instance_id>/manager-secrets.env`。读取 `_read_tokens`（`:323`）兼容 `WEBUI_TOKEN` / `NAPCAT_WEBUI_TOKEN` 两个别名。
- 同一个 WebUI token 会**同时**注入容器的 `NAPCAT_WEBUI_SECRET_KEY` 与 `WEBUI_TOKEN`（`docker_provider.py:140-141`）。
- **健康检查在本包里只有"运行态观测"**：Docker 读 `container.status` 映射表（`:233-241`），Native 用 `systemctl is-active` 且**要求 stdout 恰为 `"active"`**（`native_provider.py:140`）。没有 HTTP 探活。
- 归属保护：Docker 靠 label（`io.corlinman.managed-napcat` 等，`:30-33`）+ 独立 `installation-id`（`:435`）；socket 靠 `SO_PEERCRED` uid 白名单（`server.py:84-87`）+ 双 flock。
- ⚠️ **`upgrade` 名不副实**：两个 provider 的 `upgrade` 都只是 restart（`docker_provider.py:253-255`、`native_provider.py:165`），不拉新镜像；`NapCatInstanceRecord.previous_image`（`models.py:82`）声明了但从未写入。

**真正的 QR 登录 / 健康探测 / OneBot11 配置模板不在本包**，而在 `corlinman-server/.../gateway/routes_admin_b/_napcat_lib.py`（1097 行）：

- **QR 来源是 NapCat WebUI 的 HTTP API，不是日志文件**。端点集合在 `_napcat_lib.py:45-49`：`/api/OB11Config/GetConfig`、`/api/OB11Config/SetConfig`、`/api/QQLogin/GetQQLoginQrcode`、`/api/QQLogin/RefreshQRcode`、`/api/QQLogin/RestartNapCat`、`/api/QQLogin/CheckLoginStatus`、`/api/QQLogin/SetQuickLogin`。
- **WebUI 鉴权握手**（`_NapcatClient._login` `:553`）：`sha256(access_token + b".napcat")` 的 hex → `POST /api/auth/login {"hash": ...}` → 拿 `data.Credential` → 后续 `/api/*` 带 `Authorization: Bearer <credential>`。
- **刷新算法**（`request_qrcode` `:692`）：取当前 QR → `RefreshQRcode` → 轮询 4×0.4s **等 QR 字符串真的变化**（`_wait_for_qrcode_change` `:620`）→ 没变就 `RestartNapCat` 并轮询 18×1.0s。
- **卡死恢复**（`_recover_qrcode_without_previous` `:655`）：NapCat 会一边回 `{"code":-1,"message":"QQ Is Logined"}` 一边 `CheckLoginStatus` 报 `isLogin:false, isOffline:true`；先查状态区分，真登录则抛 409 `napcat_already_logged_in`，否则重启。
- QR 呈现：`_classify_qr`（`:492`）把原始串分成 URL 或 base64 图，返回 `QrcodeOut(token, image_base64, qrcode_url, expires_at)`，有效期 120 s。
- **诊断探针**（`_probe_napcat_diagnostics` `:767`）三路独立：`credential`（token 能否换到 Credential）、`qrcode_api`、`onebot_config_api`，各自 `ok/failed/unreachable`，输出机器可读 `actions[]`。
- **corlinman 会通过 WebUI API 反写 NapCat 的 OB11 配置**（不是改盘上文件）：`_onebot_websocket_server_from_config`（`:443`）生成
  `{"enable": true, "name": "corlinman", "host": "0.0.0.0", "port": <ws_port>, "messagePostFormat": "array", "reportSelfMessage": false, "enableForcePushEvent": true, "token": <access_token>, "debug": false, "heartInterval": 30000}`
  再由 `_ensure_onebot_websocket_server`（`:881`）按 name 或 port 匹配合并回 `network.websocketServers` 并 `SetConfig`（整份配置**以 JSON 字符串**塞在 `config` 键下，`:909-912`），10 s 去抖。
  → **这条对我们极其重要**：它证明生产 NapCat 上那个 `name="corlinman"` 的服务器块是 **`host: 0.0.0.0` 的正向 WS 服务端**，`messagePostFormat: "array"`（段数组而非 CQ 码），心跳 30 s。风险 R2 因此基本可以排除，但仍建议部署前握手实测。
- 登录历史落盘：`<data_dir>/qq-accounts.json`（default 实例）或 `<data_dir>/qq-accounts/<instance_id>.json`（`:475`、`:1070`）。

### 3.12 corlinman 侧的 Qzone 实现 —— **是 Source 1 的超集**

**位置**：`/Users/cornna/project/corlinman/python/packages/corlinman-agent/src/corlinman_agent/qzone/`
- `publish.py`（999 行）、`comment.py`（1064 行）、`__init__.py`（55 行）。**全仓库只有这两个文件含 QZone HTTP 逻辑**（rust/go/node 树不存在；`corlinman-mcp-server` 零命中）。

`comment.py` 的模块注释直接写着 "Endpoint choice (**learned the hard way in hermes**)"（`comment.py:33`），`_compute_gtk` 的注释写着 "**Identical to the hermes implementation** — verified bit-for-bit against the live endpoint"（`publish.py:262-265`）。**corlinman 的 qzone 就是从 personal_hermes 那份演进来的**，因此移植方向应当是"以 corlinman 版为准，回填 hermes 的工具外壳"。

#### 5 个工具（对比 Source 1 的 1 个）

| 工具 | 位置 | 能力 |
|---|---|---|
| `qzone_publish` | `publish.py:67`（分发器 `:626`） | 发说说：文本 / 本地图 / AI 生图 |
| `qzone_list_feed` | `comment.py:95`（分发器 `:499`） | 拉好友动态时间线（含每条的评论列表），可按 `owner_uin` 过滤 |
| `qzone_get_post` | `comment.py:96`（分发器 `:565`） | 按 tid 取单条说说 + 完整评论 |
| `qzone_post_comment` | `comment.py:97`（分发器 `:630`） | 在任意说说下评论（顶层，或回复某个评论者） |
| `qzone_list_friends` | `comment.py:98`（分发器 `:836`） | 好友列表 —— **走 OneBot `get_friend_list`，完全不碰 QZone HTTP**（`comment.py:853`） |

**不存在的能力（两源皆无）**：删除说说、点赞、访客列表、相册管理。

#### 端点（**与 Source 1 有一处关键差异**）

```
publish.py:71  _QZONE_PUBLISH_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
publish.py:74  _QZONE_UPLOAD_URL  = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
comment.py:113 _QZONE_FEEDS3_URL  = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
comment.py:117 _QZONE_COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
```
⚠️ 发布端点的主机名是 **`h5.qzone.qq.com`**，而 Source 1 用的是 **`user.qzone.qq.com`**（`qzone_tool.py:58-61`）。路径完全相同。这是分歧 **S14**。

**读端点的选型理由**（`comment.py:33-41`，值得照抄进注释）：`emotion_cgi_msglist_v6` 会以 `-10000 使用人数过多` 拒绝自动化读取，因为它要一个 JS 生成的 `qzonetoken`，而借来的 cookie 罐里没有；`feeds3_html_more` **不需要** qzonetoken，同一个 `g_tk` 就能返回时间线。

#### 关键线格式

`_fetch_timeline`（`comment.py:460`）GET 参数：
```python
{"uin": my_uin, "scope": "0", "view": "1", "filter": "all", "flag": "1",
 "applist": "all", "pagenum": "1", "count": str(count), "aisortEndTime": "0",
 "aisortOffset": "0", "begintime": "0", "format": "json", "g_tk": str(gtk),
 "useutf8": "1", "outputhtmlfeed": "1"}
```
响应是 **JS 对象字面量**，每条 feed 的 `html:'…'` 字段里是 JS 转义过的渲染后 HTML。解析链：`_unescape_hex`（`:320`，必须同时解 `\xNN`、`\uNNNN` **和两字符转义** —— 注释 `:289-293` 说只解 `\xNN` 会让所有下游正则静默失效，因为标签会变成 `<\/div>`）→ `_parse_feeds3`（`:436`）→ `_feed_tid`（`:364`）/ `_feed_content`（`:372`）/ `_feed_comments`（`:382`）。风控检测：body 里没有 `"code":0` 就提取数字码抛 `feeds3 returned code=<n>`（`:486-490`）。

`dispatch_qzone_post_comment`（`comment.py:738-756`）POST 表单：
```python
{"topicId": f"{owner_uin}_{tid}__1", "feedsType": "100",
 "inCharset": "utf-8", "outCharset": "utf-8", "ref": "feeds",
 "content": final_content, "hostUin": owner_uin, "uin": my_uin,
 "format": "fs", "iNotice": "0", "private": "0", "paramstr": "1",
 "qzreferrer": f"https://user.qzone.qq.com/{owner_uin}"}
# 回复某人时追加：form["targetUin"] = reply_to_uin
```
@某人是**写进正文**的（`comment.py:672`）：`mention = f"@{{uin:{reply_to_uin},nick:{reply_to_name},who:1}} "`。

发布侧的 `_build_publish_form`（`publish.py:346`）、`_build_upload_form`（`:375`）、`_build_richval`（`:324`）、`_parse_publish_response`（`:430`）与 Source 1 **逐字段一致**（含双成功形状 `ret==0` / `code==0` + `subcode in (0,None)`）。限制也一致：≤9 图（`:88`）、≤20 MiB（`:89`）、同一套扩展名白名单（`:87`）、20s/60s 超时（`:92-93`）。

#### 鉴权（与 Source 1 同源，无差异）

`_qzone_auth`（`comment.py:183`）→ `(my_uin, cookie, gtk)`：
1. `OneBotClient.verify_identity()`（回退 `fetch_login_info`）拿 uin；
2. `fetch_cookies("user.qzone.qq.com")`（`onebot/client.py:418`）拿 cookie 串；
3. 抽 `p_skey` —— **缺失即硬失败 `qzone_cookie_stale`**（`publish.py:855-861`）；
4. 抽 `skey`（可选，默认 `""`）；
5. `_compute_gtk(p_skey)`。

`comment.py` **直接 import** `publish.py` 的 `_compute_gtk` / `_extract_cookie_value` / `_DESKTOP_UA` / `_QZONE_TIMEOUT` / `_QZONE_COOKIE_DOMAIN`（`comment.py:62-70`），保证两个工具位对位一致 —— 正是 §5.5 建议的 `qzone_client.py` 抽取方向。

OneBot 传输解析（`onebot/client.py:125-185`）：显式构造参数 → `CORLINMAN_PY_CONFIG` sidecar 的 `channels.qq.instances[<id>]` → 环境变量 `CORLINMAN_NAPCAT_HTTP_URL` / `CORLINMAN_NAPCAT_ACCESS_TOKEN` → 从 `ws_url` 推导。**指定了 `instance_id` 时环境变量回退被关闭**（`:250`、`:261`），避免多实例串号。sidecar 只写 `ws_url` + `access_token` 两项（`py_config.py:581`）。

#### 持久化状态（Source 1 为零，这里有 5 处）

| 状态 | 路径 | 位置 |
|---|---|---|
| 幂等 effect（`qzone.publish` / `qzone.comment`） | SQLite 表 `scheduler_effects` | `scheduler/persistence.py:73`；`prepare_effect` `:352` / `complete_effect` `:396` |
| 近期已发正文（防重复） | `<DATA_DIR>/qzone_post_log/[<qq_instance_id>/]<persona_id>.json`，30 条 × 500 字符 | `qzone_daily.py:769`、`:810-824`、`:846` |
| 已回复评论标记 | `<DATA_DIR>/qzone_seen_comments/<persona_id>.json`，`{"version":2,"seen":{tid:["<identity>:<ts>"]}}`，200 identity/tid × 100 tid，LRU | `qzone_reply.py:156`、`:162`、`:208` |
| 调度运行时 job（含 `last_qzone_url`） | `<data_dir>/scheduler_runtime_jobs.json` | `_scheduler_lib.py:307`、`:390` |
| 运行历史 | `scheduler_runs` 表 | `scheduler/persistence.py:53` |

**Cookie / session 从不落盘** —— 每次分发都重新向 OneBot 借。

**Effect 预留协议**（值得照搬的语义）：`_prepare_effect`（`publish.py:555`）/ `_complete_effect`（`:597`）。目标键：发布 `instance:{qq_instance_id}:account:{uin}`（`:870-872`）；评论 `instance:{id}:post:{owner_uin}:{tid}:{identity}`（`comment.py:726-728`），identity 为 `id:<comment_id>` 或 `sha256:<digest>`，都没有则字面 `"top-level"`（`:663-709`）。终态 `sent`/`failed`/**`unknown`** —— **传输失败刻意记 `unknown`**（`publish.py:938`、`comment.py:769`），因为帖子可能其实已经发出去了。

#### 错误处理与风控

- 每个分发器都是**全函数**（不抛异常），统一 `_err(...)` 信封（`publish.py:171`、`comment.py:168`）。
- JSONP 剥壳：`_extract_json_object`（`publish.py:289`，与 Source 1 同款正则）；评论侧另有 `_parse_callback_json`（`comment.py:341`），**显式锚定 `callback(`**，因为朴素的 `{.*}` 会先匹配到外壳里的 `try{…}`。
- 错误码全集：`invalid_args`、`too_many_images`、`image_not_found`、`image_read_failed`、`image_with_refs_unavailable/_failed`、`onebot_unavailable`、`onebot_failed`、**`qzone_cookie_stale`**、`image_upload_failed`、`qzone_publish_failed`、`qzone_rejected`、`qzone_read_failed`、`qzone_request_failed`、`qzone_unparseable`、`content_policy_blocked`、`scheduler_effect_*`、`qq_instance_mismatch`。
- **腾讯内容策略层**（`corlinman-content-policy`，hermes 无对应物）：出站对正文与生图提示词跑 `moderate_text`、附件跑 `moderate_media`；媒体被拦但文本非空时**降级为纯文本**而不是整体失败（`publish.py:721-726`）。入站 `_redact_feeds`（`comment.py:400`）在 feed 进入模型提示词**之前**把被拦的作者名/正文/评论改写成 `"[内容已按 QQ 风控策略隐藏]"`，并返回分类计数 `policy_redactions`；好友昵称/备注同样处理（`:872-881`）。分类器异常**fail closed**。
- **shadow 模式**：两个写工具在**任何** OneBot 鉴权/文件读/HTTP 之前就返回模拟信封（`publish.py:731-741`、`comment.py:681-694`）。
- **实例安全**：调度器的 `qq_instance_id` 与本轮运行时实例不符则 `qq_instance_mismatch` 中止（`agent_servicer.py:4468-4481`、`:4511-4524`）。

#### 暴露方式与配置

- **没有 MCP 暴露**（`corlinman-mcp-server` 零命中）。
- 走 gRPC agent servicer 的内置工具表：`agent_servicer.py:576`（publish）/`:590`（comment 家族），schema 广告 `:900`、`:904`，分发 `:4432-4571`。
- 权限：`qzone_publish` 被归入**改动型**工具（`corlinman_agent/authz/matcher.py:53`），strict/plan 模式默认拒绝。
- **没有 `[qzone]` 配置段，也没有 qzone 的 settings schema** —— 全部继承 `[channels.qq]`。qzone 的"配置"是 **scheduler job 元数据**：`_validate_qzone_daily`（`_scheduler_lib.py:766`，要 `persona_id` + `prompt_template`）、`_validate_qzone_reply`（`:804`，要 `persona_id`；`max_replies` 钳 1–10 默认 3，`lookback_posts` 钳 1–20 默认 5）。动作类型常量 `qzone.daily_publish`（`qzone_daily.py:139`）/ `qzone.reply_comments`（`qzone_reply.py:116`）—— 正好对应生产 scheduler 里的 `hermes.qzone_daily` / `hermes.qzone_reply`。
- 内置人格 job 默认**禁用**，新部署不会自动开始发帖（`bundled_personas/__init__.py:23-25`）。

⚠️ **已知缺陷（移植时应修）**：`qzone_get_post` 不打单帖 CGI，而是拉 40 条时间线再客户端筛 tid（`comment.py:592-602`，`_MAX_LIST_NUM = 40`）。滚出窗口的帖子就取不到 —— 繁忙账号即使在 `lookback_posts` 范围内也可能对 `qzone.reply_comments` 隐形。

---

## 4. 两源差异与合并特性表

### 4.1 各自独有

| 能力 | 来源 1（personal_hermes 工具） | 来源 2（corlinman 通道） |
|---|---|---|
| **入站消息接收** | ❌ 完全没有 | ✅ 正向 WS，类型化事件模型 |
| 长连接 / 重连 / 心跳 | ❌ 每次调用新建短连接 | ✅ 退避 `1,2,5,10,30`s + 30s ping |
| 事件解析（message/notice/meta/request） | ❌ 全部丢弃 | ✅ 全量解析，未知类型降级不断连 |
| 消息段模型（9 类 + 兜底） | ❌ 只会手搓 `record` 段 | ✅ 完整双向序列化 |
| @提及检测 / 关键词 / 白名单 / 冷却 | ❌ | ✅ `router.dispatch` |
| 限流（令牌桶 + 滑动窗口） | ❌ | ✅ 两套 |
| 人格注入 | ❌ | ✅ 跨渠道共享 |
| 输入中指示 / 合并转发卡片 / 分片 / 气泡 | ❌ | ✅ 全有 |
| 出站附件按 MIME 路由（图/音/文件） | 只有语音一路 | ✅ 三路 |
| 响应关联（echo → future） | 有（一次性 echo，仅为读回自己的返回） | ✅ 长连上的 `call_action` |
| 健康监视 / 掉线检测 | ❌ | ✅ WS 在线 vs 账号在线双维度 |
| 主动发言 / 群摘要 | ❌ | ✅ |
| durable inbox / 崩溃重放 | ❌ | ✅ |
| **HTTP 传输支持** | ✅（`http(s)://` 一次一 POST） | ❌ 只有 WS |
| **同步调用面**（可从同步工具 handler 里直接调） | ✅（含 loop-in-loop 隔离） | ❌ 纯 async |
| **`get_cookies` / `get_login_info` 借登录态** | ✅ | ❌ 未用 |
| **QQ空间发布** | ✅ `qzone_publish`（文本/图/AI 生图） | ✅ 同款，且是**同源演进版**（§3.12） |
| **QQ空间读取 / 评论 / 好友列表** | ❌ | ✅ `qzone_list_feed` / `qzone_get_post` / `qzone_post_comment` / `qzone_list_friends` |
| Qzone 幂等（effect 预留 / 已回复标记 / 防重复正文） | ❌ 完全无状态 | ✅ 3 套落盘状态 |
| Qzone 内容风控（出站审核 + 入站脱敏） | ❌ | ✅ `corlinman-content-policy` |
| Qzone shadow（干跑）模式 | ❌ | ✅ 在任何 IO 之前返回模拟信封 |
| **TTS → QQ 语音消息** | ✅ `qq_send_voice` | 出站附件里有音频内联，但没有"文字转语音再发"的工具 |
| 零依赖（只用 stdlib + 可选 `websockets`） | ✅ | ❌ 依赖整个 corlinman 生态（`httpx` / `structlog` / content-policy / scheduler store） |

### 4.2 语义分歧点（**必须逐条决策**）

| # | 分歧 | 来源 1 | 来源 2 | **建议** |
|---|---|---|---|---|
| S1 | WS 鉴权位置 | 查询参数 `?access_token=` | 握手头 `Authorization: Bearer` | **两个都发**。NapCat 两者皆认；同时发在反代/网关改写头的部署下更稳。实现成本一行。 |
| S2 | 传输 | HTTP 或 WS（按 scheme） | 仅 WS | **保留双传输**给工具侧（`onebot_call`），**入站强制 WS**。生产 NapCat 只暴露 `ws://127.0.0.1:3001`，HTTP 面未必开。 |
| S3 | 连接生命周期 | 每次调用短连接 | 单条长连接 | **入站长连；出站工具复用同一条长连**（见 §5.2 的 `onebot_call` 改造），避免第二条连接占用 NapCat 的连接配额、也避免语音工具与适配器抢连接。 |
| S4 | 群里无关键词配置时的默认行为 | N/A | `mention_or_keyword`（默认）= **只回 @**；`all` = 全回 | **采用 `mention_or_keyword` 默认**。生产就是这个（关键词"格兰"+@），且"人不会回群里每条消息"。 |
| S5 | @提及能否绕过白名单 | N/A | **不能** | **照搬"不能"**。白名单是硬闸，安全属性。 |
| S6 | @提及能否绕过群发言硬窗口 | N/A | **不能**（但能绕过 `group_reply_cooldown_secs`） | **照搬**。冷却是礼貌，硬窗口是预算。 |
| S7 | 失败可见性 | 一律 `tool_error(...)` 原样给模型 | 通道侧降级为日志 + `[corlinman error]` 回复 | **工具侧照搬来源 1**（模型需要看到真实错误才能自愈）；**通道侧照搬来源 2**（用户不该看到栈）。 |
| S8 | 重试 | 无（"never silently retries"） | 出站写循环最多重投 1 次，第 2 次失败丢弃 | **分层照搬**：工具无重试；适配器写循环带毒消息保护。 |
| S9 | 附件投递形式 | `base64://` 内联 | `base64://` 内联（≤30 MiB），超限退字面路径 | **一致，无分歧**。统一 30 MiB 阈值。 |
| S10 | 出站文本长度上限 | 无概念 | 3800 字符 + >1000 折成转发卡片 | **照搬来源 2**。 |
| S11 | `self_id` 来源 | 无（工具用 `get_login_info` 现问） | 事件自带 `self_id` 权威，配置 `self_ids` 仅回退 | **照搬来源 2**，并让 qzone 工具优先用适配器观测到的 `self_id`，拿不到再 `get_login_info`。 |
| S12 | 事件解析健壮性 | N/A | `_coerce_int` 绝不抛；未知 post_type/segment 降级 | **照搬**。这是 corlinman 修过的真实 BUG（`test_fix_BUG07_parse_event.py`）。 |
| S13 | 入站队列满时策略 | N/A | 丢**最旧**并计数 | **照搬**。丢最新会让"用户刚发的那条"消失。 |
| S14 | **说说发布端点主机名** | `https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/...`（`qzone_tool.py:58-61`） | `https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/...`（`publish.py:71-73`） | **用 corlinman 的 `h5.` 版本**。corlinman 是从 hermes 那份演进出来的（注释自陈），且是**生产在跑**的那一份，改主机名必然是踩过坑之后的结果。两个 URL 都保留在常量里、加注释说明可回退。 |
| S15 | HTTP 客户端 | `urllib`（同步、stdlib） | `httpx.AsyncClient`（异步，可注入 transport 便于测试） | **工具侧保留 `urllib`**（hermes 工具 handler 是同步的，引入 httpx 会拖依赖）。但**照搬 corlinman 的可注入 transport 设计**（`http_transport=` 参数），它让全部 HTTP 路径可单测而不打真网。 |
| S16 | Qzone 错误表达 | `tool_error(msg, code=...)` 字符串 | 结构化信封 `{"ok":false,"error_code":...,"message":...}` + 固定错误码集合 | **采用 corlinman 的固定错误码集合**（尤其 `qzone_cookie_stale`），但用 hermes 的 `tool_error` 承载，把 code 放进 `code=` 字段。错误码可枚举 = cron 任务能按码分支重试。 |
| S17 | Qzone 写操作幂等 | 无 | effect 预留 → 完成，传输失败记 **`unknown`** 而非 `failed` | **照搬 `unknown` 终态语义**。定时任务重试时把 `unknown` 当"可能已成功"处理，避免重复发帖 —— 这是对外可见的正确性问题。 |

### 4.3 合并后的目标特性表（实施验收清单）

**入站（新建，来自来源 2）**
- [ ] 正向 WS 客户端，退避 `1,2,5,10,30`s，30s ping，重连不向上抛瞬时错误
- [ ] `Authorization: Bearer` 头 + `?access_token=` 查询参数双写
- [ ] 事件解析：message / notice / meta_event / request / unknown；数值字段容错
- [ ] 消息段：text / at / image / reply / face / record / video / file / forward / other
- [ ] 规范化：文本扁平化（at → `@<qq> `）、附件抽取（空 url 跳过）、`mentioned` 计算
- [ ] 回复判定链：私聊全放行 → 群总开关 → 白名单硬闸 → @/斜杠命令 → 关键词（策略可选）→ 每群冷却
- [ ] 限流：每群 + 每人令牌桶（在判定之后）；每群滑动窗口硬上限（在模型调用之前，@不能绕过）
- [ ] 入站/出站队列各 64，入站满丢最旧并计数
- [ ] 心跳 `status.online` 解析（兼容 bool / int / `status.app.online` / `status.good` / 缺失）
- [ ] 健康监视：WS 在线 vs 账号在线双维度 + 掉线/恢复日志
- [ ] 出站：`send_action`（即发即忘）+ `call_action`（echo 关联，retcode 0/1 = ok）
- [ ] 写循环队首重投 + 连续 2 次失败丢弃
- [ ] 群回复只在第 0 片加 @；私聊不加
- [ ] 私聊 `set_input_status` 心跳（5s），群不发
- [ ] 分片 3800 字符；单气泡 >1000 折合并转发卡片（群里先发引导语补 @），卡片被拒回退分片
- [ ] `[MSG_BREAK]` 气泡切分 + 气泡间 0.3s
- [ ] 出站附件三路：图内联 / 音内联 / 其余 upload_*_file（`base64://` ≤30 MiB，响应等待 120s）
- [ ] 合并转发节点同时带 `name/uin` 与 `nickname/user_id` 两套键

**出站工具（移植自来源 1）**
- [ ] `onebot_call(action, params, timeout)` 同步 API，HTTP/WS 双传输
- [ ] `qq_send_voice`（TTS 或本地音频 → `record` 段）
- [ ] `qzone_publish`（文本 + ≤9 图 + AI 生图）
- [ ] （移植自 corlinman，见 §5.5）`qzone_list_feed` / `qzone_get_post` / `qzone_post_comment` / `qzone_list_friends`
- [ ] Qzone 写操作幂等：effect 预留 + `unknown` 终态（S17）
- [ ] Qzone HTTP 错误**不回显响应体**（只给状态码 + 固定文案）

**可选（生产已在用，非必需第一批）**
- [ ] 人格 system_prompt 注入 + emoji 资产块
- [ ] 主动发言循环（活跃时段/概率/日配额/最小间隔/最后一条非自己）
- [ ] 群摘要 monitor（3 条日报）+ 群历史落库

---

## 5. 移植规格

### 5.1 模块清单

| 新建/修改 | 路径（目标仓库） | 来源 | 说明 |
|---|---|---|---|
| 新建 | `tools/onebot_client.py` | 来源 1 同名文件，**逐行照搬 + 4 处改动** | 见 §5.2 |
| 新建 | `tools/onebot_voice_tool.py` | 来源 1 同名文件，**照搬** | 依赖已存在的 `tools/tts_tool.py` |
| 新建 | `tools/qzone_tool.py` | 来源 1 同名文件，**照搬 + 扩展** | 依赖已存在的 `tools/image_generation_tool.py`；扩展见 §5.5 |
| 新建 | `plugins/platforms/onebot/__init__.py` | 新写（照 `plugins/platforms/telegram/__init__.py`，2 行） | `from .adapter import register` |
| 新建 | `plugins/platforms/onebot/plugin.yaml` | 新写（照 telegram 的 manifest） | `kind: platform`，`requires_env: ONEBOT_WS_URL`，`optional_env: ONEBOT_ACCESS_TOKEN` 等 |
| 新建 | `plugins/platforms/onebot/protocol.py` | **来源 2 `onebot.py:72-720` 直接搬** | 常量 + 事件/段/动作数据类 + `parse_event` / `segments_to_text` / `segments_to_attachments` / `is_mentioned` / `action_to_wire`。**零 corlinman 依赖**（去掉 `corlinman_content_policy` 与 `corlinman_channels.common` 的 import，`Attachment` 换成 hermes 的媒体类型） |
| 新建 | `plugins/platforms/onebot/client.py` | **来源 2 `onebot.py:721-1300` 搬** | `OneBotConfig` + `OneBotAdapter`（连接/重连/pump/writer/echo 关联/健康属性）。**删掉** `inspect_action` / `send_safe_refusal` / `tencent_policy_resolver`（那是 corlinman 的内容策略层，hermes 无对应物） |
| 新建 | `plugins/platforms/onebot/router.py` | **来源 2 `router.py` 搬** | 去掉 `corlinman_channels.commands`（斜杠命令交给 hermes 自己的命令层）、去掉 `hook_bus` 分支，保留 `rate_limit_hook` |
| 新建 | `plugins/platforms/onebot/rate_limit.py` | **来源 2 `rate_limit.py` 搬** | `TokenBucket` + `SlidingWindowCounter`，零依赖，可整文件复制 |
| 新建 | `plugins/platforms/onebot/adapter.py` | 新写，糅合来源 2 `service.py` 的 QQ 部分 | 实现 hermes 的 4 个抽象方法 + `register(ctx)`；把 `_qq_dispatch_loop` 的判定顺序、`_build_reply_action`、`_qq_send_attachment`、分片/折叠/气泡、`set_input_status` 心跳搬进来 |
| 修改 | `toolsets.py` | 来源 1 `toolsets.py:242-261` | 加 `qzone` 与 `qq_voice` 两个 toolset 条目 |
| 修改 | `.env.example` | 来源 1 `.env.example:472-487` | 加 QQ/OneBot 段（键名见 §6） |
| 新建 | `tests/tools/test_onebot_client.py` | 来源 1 同名（278 行）**照搬** | |
| 新建 | `tests/tools/test_onebot_voice_tool.py` | 来源 1 同名（269 行）**照搬** | |
| 新建 | `tools/qzone_client.py` | corlinman `publish.py:261-320` + `comment.py:183,320,341` | 鉴权/传输/解析原语，见 §5.5 |
| 新建 | `tools/qzone_feed_tool.py` | corlinman `comment.py`（1064 行） | `qzone_list_feed` / `qzone_get_post` / `qzone_post_comment` / `qzone_list_friends` |
| 新建 | `tests/tools/test_qzone_tool.py` | 来源 1 同名（638 行）**照搬** | |
| 新建 | `tests/tools/test_qzone_feed_tool.py` | corlinman `corlinman-agent/tests/test_qzone_comment.py`（588 行） | 见 §5.6 |
| 新建 | `tests/platforms/test_onebot_protocol.py` | 来源 2 `tests/test_onebot.py` 的 `TestParseEvent`/`TestSegments`/`TestSegmentHelpers`/`TestActionToWire` | 纯函数，可几乎原样搬 |
| 新建 | `tests/platforms/test_onebot_client.py` | 来源 2 `tests/test_onebot.py` 的 `TestOneBotIntegration`/`TestWriterRequeueOnSendFailure`/`TestInboundQueueDropOldest` | 需要一个进程内 `websockets` 服务端 fixture（`ws_server`，见来源 2 `tests/conftest.py`） |
| 新建 | `tests/platforms/test_onebot_router.py` | 来源 2 `tests/test_router.py`（428 行） | 去掉斜杠命令相关用例 |
| 新建 | `tests/platforms/test_onebot_rate_limit.py` | 来源 2 `tests/test_rate_limit.py`（202 行）+ `test_qq_speech_cap.py`（158 行） | |

> **不要移植**：corlinman 的 `channel.py`（`Channel` Protocol / `ChannelRegistry` / `spawn_all`）—— hermes 的 `plugins/platforms` + `platform_registry` 已经是同一职责，重复会引入两套生命周期。`persona_inject.py` 也不直接移植（hermes 有自己的 system prompt / persona 机制，C1 任务负责）；只在 §4.3 里作为可选特性登记。

### 5.2 `tools/onebot_client.py` 的 4 处改动

1. **WS 鉴权双写**（分歧 S1）：在 `_ws_roundtrip`（来源 `:196`）里保留查询参数，同时给 `websockets.connect` 传 `additional_headers=[("Authorization", f"Bearer {token}")]`。
2. **优先复用适配器长连**（分歧 S3）：`onebot_call` 先尝试从平台注册表拿到已连接的 OneBot 适配器实例；拿得到就走它的 `call_action`（跨线程用 `asyncio.run_coroutine_threadsafe` 投到适配器所在 loop），拿不到再退回现有的"短连接/HTTP"路径。**退回路径必须保留** —— 纯 CLI/cron 场景下没有跑着的网关。
3. **`ONEBOT_WS_URL` 升为一等公民**：目标环境只有 `ws://127.0.0.1:3001`。保留 `ONEBOT_HTTP_URL` 优先的既有语义（来源 `:54-56`），但 `.env.example` 里主推 `ONEBOT_WS_URL`。
4. **`websockets` 加入依赖**：来源 1 是惰性 import + 报错提示（`:203-209`）。入站适配器必需它，所以应作为 `onebot` 平台插件的声明依赖；工具侧的惰性 import 与友好报错**保留**（工具可独立于插件使用）。

### 5.3 `plugins/platforms/onebot/adapter.py` 必须实现的行为

按 hermes 的 `BasePlatformAdapter` 契约（`gateway/platforms/base.py:2890`），落 4 个抽象方法：

- **`connect(is_reconnect=False) -> bool`**：构造 `OneBotConfig(url=ONEBOT_WS_URL, access_token=..., self_ids=[...])`，`await adapter.connect()`，起派发任务（读 `adapter.inbound()`）、健康监视任务。返回 True。
- **`disconnect()`**：取消派发/健康任务，`await adapter.close()`。
- **`send(...)`**：按 hermes 的 `SendResult` 契约包装出站：气泡切分（`[MSG_BREAK]`）→ 每气泡 `chunk_reply(3800)` → >1000 折转发卡片 → 逐片 `_build_reply_action`（只有第 0 片带 @）→ `send_action`。附件走三路 MIME 分发。
- **`get_chat_info(chat_id)`**：`get_group_info` / `get_stranger_info` 的 OneBot 动作封装（用 `call_action`）。

派发循环必须**按来源 2 的顺序**（§3.5）执行判定，并且：
- 群消息**先无条件**喂给"最近聊天缓冲"（如果实现主动发言），**再**过 router —— 人格要看到整个房间，不只是它回过的话。
- 群发言硬窗口检查放在**斜杠命令短路之后、模型调用之前**。
- 并发控制：先 `semaphore.acquire()` 再 `create_task`，让背压顶到 WS 读端。
- 每条事件重算闸门值，变了就重建 router（保留冷却状态）。

`register(ctx)` 按 `plugins/platforms/telegram/adapter.py:10865-10885` 的形状：
```
ctx.register_platform(
    name="onebot", label="QQ (OneBot v11 / NapCat)",
    adapter_factory=..., check_fn=..., required_env=["ONEBOT_WS_URL"],
    max_message_length=3800, emoji="🐧",
    cron_deliver_env_var="ONEBOT_HOME_CHAT", ...)
```

### 5.4 需要落库/持久化的状态

来源 2 把大部分状态放在**模块级内存**，理由是"存配置会重启通道任务，状态必须活过重启"（`service.py:770-775`）。hermes 里通道生命周期不同，建议：

| 状态 | 建议存法 | 理由 |
|---|---|---|
| 每群滑动窗口计数 | **模块级内存**（照搬） | 进程重启后清零可接受；落库会引入写放大 |
| 每群回复冷却时钟 | router 实例字段（照搬） | 同上 |
| 群最近聊天缓冲（30 条） | 模块级 `deque(maxlen=30)`（照搬） | 只服务主动发言 |
| 主动发言日配额 / 上次时间 | **落到 hermes 状态存储** | 日配额跨进程重启丢失 = 当天可能超发，这是对外可见的行为 |
| bot 的 `self_id` / 昵称 | 内存 + 可选落盘 | 事件自带，冷启动前用配置 `self_ids` 兜底 |
| 已发说说 tid + 正文摘要 | **落盘**：`<HERMES_DATA>/qzone_post_log/<persona_id>.json`（30 条 × 500 字符） | 来源 1 完全不记（§2.4）；corlinman 有先例（§3.12）。防重复发帖需要 |
| 已回复评论标记 | **落盘**：`<HERMES_DATA>/qzone_seen_comments/<persona_id>.json`（200 identity/tid × 100 tid，LRU） | 同上；`qzone.reply_comments` 的去重依据 |
| Qzone 写操作 effect（`sent`/`failed`/**`unknown`**） | **落盘**（用 hermes 自己的状态存储实现 corlinman `scheduler_effects` 的同等语义） | 传输失败必须记 `unknown`，见 S17 |
| inbox / 崩溃重放 | **第一批不做** | hermes 有自己的会话/消息持久化；引入 corlinman inbox 会拖入整套 server 依赖 |
| 群历史（monitor 用） | 随 D2 任务一起做 | |

### 5.5 Qzone 工具族（C3）—— 以 corlinman 版为准

**关键结论**：corlinman 的 qzone 实现是 Source 1 的**同源超集**（§3.12），生产在跑，且 5 个工具齐全。所以这里不是"从 Source 1 扩展"，而是**把 corlinman 的 `publish.py` + `comment.py` 降级移植成 hermes 工具**，用 Source 1 的注册外壳与同步 `urllib` 传输。

**目标文件结构**

| 文件 | 内容 | 主要来源 |
|---|---|---|
| `tools/qzone_client.py`（新） | 鉴权与传输原语：`_compute_gtk`、`_extract_cookie_value`、`_get_login_uin`、`_get_qzone_cookie_string`、`_qzone_auth() -> (uin, cookie, gtk)`、`_qzone_post`、`_qzone_get`、`_as_text`、`_extract_json_object`、`_parse_callback_json`、`_unescape_hex`、`_DESKTOP_UA`、超时/限额常量 | `publish.py:261-320` + `comment.py:183,320,341` |
| `tools/qzone_tool.py` | `qzone_publish` 的 schema/handler/注册 + 发布线格式 | Source 1 同名文件（结构）+ `publish.py`（端点见 S14） |
| `tools/qzone_feed_tool.py`（新） | `qzone_list_feed` / `qzone_get_post` / `qzone_post_comment` / `qzone_list_friends` 四个工具 | `comment.py` |

**逐工具移植要点**

- **`qzone_publish`** — 结构照 Source 1（同步 urllib + `registry.register`），端点改 `h5.qzone.qq.com`（S14），表单/richval/响应解析三者两源本就一致。**新增**：成功后把 `tid` 与正文摘要写入 §5.4 的落盘状态（对齐 corlinman 的 `qzone_post_log`，30 条 × 500 字符上限）。
- **`qzone_list_feed`** — GET `feeds3_html_more`，参数表见 §3.12。**必须照搬 `_unescape_hex` 的完整实现**（同时解 `\xNN`、`\uNNNN` 与两字符转义），否则所有下游正则静默失效。风控检测：body 无 `"code":0` 就抽数字码报 `feeds3 returned code=<n>`。默认 10 条、上限 40 条（`_DEFAULT_LIST_NUM` / `_MAX_LIST_NUM`）。
- **`qzone_get_post`** — corlinman 是"拉 40 条时间线再筛 tid"。**建议移植时保持行为一致但在工具描述里写明限制**（滚出窗口即不可达），不要自作主张换端点 —— 换端点等于新的逆向工作。
- **`qzone_post_comment`** — POST `emotion_cgi_re_feeds`，表单见 §3.12。回复某人时：正文前插 `@{uin:<uin>,nick:<nick>,who:1} `，并加 `targetUin`。正文上限 1500 字符（`_MAX_COMMENT_LEN`）。
- **`qzone_list_friends`** — **不碰 QZone**，直接 `onebot_call("get_friend_list")`。这是最简单的一个，可先落地。

**要照搬的横切语义**

1. **错误码集合**（S16）：至少 `invalid_args` / `onebot_unavailable` / `onebot_failed` / `qzone_cookie_stale` / `image_upload_failed` / `qzone_rejected` / `qzone_read_failed` / `qzone_request_failed` / `qzone_unparseable` / `too_many_images` / `image_not_found`。
2. **`unknown` 终态**（S17）：写操作的传输层失败必须记为"可能已成功"，供 cron 重试时判重。
3. **可注入 HTTP transport**（S15）：所有网络调用走一个可替换的 `_http_post` / `_http_get` 间接层，测试注入假实现。
4. **HTTP 错误不回显响应体**：corlinman 有专门一条测试锁这个（`test_list_feed_http_error_does_not_echo_response_body`），避免把腾讯的错误页/风控页塞进模型上下文。Source 1 目前会回显前 200 字符（`qzone_tool.py:445-449`）—— **改成只回显状态码 + 固定文案**。
5. **不移植**：`corlinman-content-policy`（hermes 无对应物）、scheduler `effect` 的 SQLite 表（用 hermes 自己的状态存储实现同语义）、shadow 模式（除非 hermes 的 cron 有干跑概念）。

**定时任务侧（D1/D2 任务的输入，此处只登记契约）**
corlinman 的两个 builtin 与生产 job 名对应关系：`qzone.daily_publish`（`qzone_daily.py:139`）↔ `hermes.qzone_daily`，`qzone.reply_comments`（`qzone_reply.py:116`）↔ `hermes.qzone_reply`。job 元数据字段：daily 要 `persona_id` + `prompt_template`；reply 要 `persona_id`，`max_replies` 钳 1–10（默认 3），`lookback_posts` 钳 1–20（默认 5）。`hermes.qzone_friends` 在 corlinman 里没有对应 builtin —— 推测是用 `qzone_list_friends` + `qzone_list_feed` 组合出来的自定义 job，**需向用户确认它到底做什么**。

### 5.6 要照搬的测试用例（精确清单）

**从来源 1（`/Users/cornna/project/personal_hermes_ref/tests/tools/`）**

`test_onebot_client.py`（278 行，全部照搬）：
- `TestConfig`（`:105`）：`test_base_url_strips_trailing_slash`、`_strips_whitespace`、`_empty_when_unset`、`_falls_back_to_ws_url`、`test_http_url_wins_over_ws_url`、`test_access_token_strips_whitespace`、`_empty_when_unset`
- `TestConfigured`（`:142`）：4 例（http 设置 / 仅 ws 设置 / 未设 / 空白）
- `TestOnebotCallHTTP`（`:167`）：`test_raises_when_url_unconfigured`、`_returns_data_on_success`、`_raises_on_failed_status`、`_raises_on_missing_data`、`_raises_on_non_json`、`test_auth_header_sent_when_token_configured`
- `TestOnebotCallWS`（`:227`）：`_returns_data_on_success`、`test_uses_ws_url_fallback`、`_raises_on_failed_status`、`_raises_on_missing_data`、`test_access_token_in_uri`、`test_no_access_token_in_uri_when_absent`
  → **改动 S1 后需新增**：`test_auth_header_sent_on_ws_handshake`
- fixture：`_FakeHTTPResponse`（`:26`）、`_FakeWS`（`:46`）、`_fake_connect`（`:91`）

`test_onebot_voice_tool.py`（269 行，全部照搬）：
`TestCoerceQqId`(7) / `TestBuildRecordMessage`(2) / `TestBuildSendParams`(3) / `TestReadAudioFile`(5) / `TestSynthesizeSpeech`(4) / `TestHandlerValidation`(6) / `TestHandlerSend`(4) / `TestRegistration`(1)

`test_qzone_tool.py`（638 行，全部照搬）：
`TestComputeGtk`(4) / `TestExtractCookieValue`(6) / `TestBuildPublishForm`(6) / `TestParsePublishResponse`(**11** — 含 `test_success_with_code_field` `:179`、`test_error_code_nonzero` `:186`、`test_code_success_with_bad_subcode` `:193`，这三条锁住新式响应形状) / `TestCheckAvailable`(3) / `TestOnebotCall`(4) / `TestHandler`(7) / `TestReadImageFile`(5) / `TestBuildUploadForm`(3) / `TestParseUploadResponse`(3) / `TestExtractPicInfo`(3) / `TestBuildRichval`(3) / `TestHandlerImages`(6) / `TestDownloadImage`(4) / `TestLoadImageReference`(3) / `TestGenerateImage`(5) / `TestHandlerGenerate`(5) / `TestRegistration`(1)

**从来源 2（`/Users/cornna/project/corlinman/python/packages/corlinman-channels/tests/`）**

`test_onebot.py`（896 行）：
- `TestParseEvent`（`:99`）：`test_group_message_event`、`test_heartbeat_decodes_as_meta_event`、`test_unknown_post_type_maps_to_unknown_event`
- `TestSegments`（`:148`）：`test_seven_segment_types`（参数化）、`test_unknown_segment_collapses_to_other`
- `TestSegmentHelpers`（`:182`）：`test_text_extraction_flattens_segments`、`test_attachments_cover_image_and_record`、`test_attachments_cover_video_and_file`、`test_attachments_skip_empty_url_video_and_file`、`test_attachments_skip_empty_urls`、`test_attachments_empty_for_text_only`、`test_is_mentioned_handles_at_all`、`test_is_mentioned_returns_false_when_unmentioned`
- `TestActionToWire`（`:256`）：`test_send_group_msg_envelope`、`test_set_input_status_envelope`、`test_upload_private_file_envelope`、`test_upload_group_file_envelope`、`test_image_segment_serializes_inline`、`test_image_segment_serializes_file_without_url`、`test_record_segment_serializes_file_without_url`、`test_video_and_file_segments_serialize`、**`test_group_forward_msg_nodes_carry_both_key_dialects`**（`:365`，锁住双键方言）、`test_send_private_forward_msg_envelope`
- `TestConfigValidation`（`:414`）：`test_empty_url_raises_config_error`
- `TestOneBotIntegration`（`:425`，需 `ws_server` fixture）：`test_adapter_yields_normalized_event`、`test_heartbeat_detects_self_id_before_message`、`test_self_id_observer_only_fires_on_change`、`test_self_id_observer_failure_does_not_break_pump`、`test_self_id_observer_retries_after_failure`、`test_adapter_drops_non_message_events`、`test_send_action_round_trips_through_ws`、`test_call_action_correlates_response_by_echo`、`test_call_action_times_out_without_response`
- `TestWriterRequeueOnSendFailure`（`:725`）：`test_send_failure_requeues_action_at_front`、`test_two_consecutive_failures_drop_poison_action`
- `TestInboundQueueDropOldest`（`:842`）：`test_overflow_drops_oldest_and_counts`
- `test_fix_BUG07_parse_event.py`（70 行）：整文件照搬（数值字段容错，防止一个坏帧拆连接）

`test_router.py`（428 行）：
- `test_parse_keywords_env_json`、`test_parse_empty_env_returns_empty_map`、`test_parse_non_object_payload_raises`、`test_parse_coerces_int_keys`
- `test_dispatch_all_when_group_absent_from_map`、`test_group_replies_disabled_drops_everything_in_groups`
- **`test_whitelist_blocks_even_mentions`**（`:129`）、**`test_empty_whitelist_blocks_all_groups`**（`:149`）— 锁住 S5
- **`test_default_policy_is_mention_only_without_keywords`**（`:158`）、`test_default_policy_honours_explicit_keywords`（`:168`）— 锁住 S4
- **`test_cooldown_gates_keyword_replies_but_not_mentions`**（`:177`）— 锁住 S6 的一半
- `test_keyword_match_is_case_insensitive`、`test_mention_bypasses_keyword_filter`
- `test_dispatch_uses_event_self_id_without_mutating_fallback`、`test_dispatch_event_self_id_replaces_stale_mention_seed`、`test_dispatch_zero_self_id_uses_configured_fallback` — 锁住 S11
- `test_private_message_always_dispatches`、`test_empty_group_message_drops`、`test_session_key_stable_across_events`
- `test_dispatch_drops_when_group_over_limit`、`test_dispatch_drops_when_sender_over_limit`、`test_rate_limit_drops_do_not_cross_groups`

`test_rate_limit.py`（202 行）：整文件照搬（令牌桶回填 / GC 清扫 / 滑动窗口精确性）
`test_qq_speech_cap.py`（158 行）：`test_cap_drops_over_budget_even_for_mentions`（`:119`，锁住 S6 的另一半）、`test_cap_disabled_by_default`、`test_inbound_group_chatter_feeds_context_buffer`

**从来源 2 的 Qzone 测试**（`/Users/cornna/project/corlinman/python/packages/corlinman-agent/tests/`）

`test_qzone_comment.py`（588 行）—— 移植 `qzone_feed_tool.py` 时的验收基线：
- 纯函数：`test_unescape_hex_decodes_js_escapes`（`:148`）、`test_parse_feeds3_extracts_feed_and_comment`（`:155`）、`test_parse_callback_json`（`:171`）、`test_schemas_are_openai_shaped`（`:178`）
- list_feed：`test_list_feed_happy`（`:196`）、`test_list_feed_owner_filter_excludes_others`（`:214`）、`test_list_feed_bad_owner_uin_rejected`（`:231`）、`test_list_feed_login_failure_envelope`（`:242`）、**`test_list_feed_stale_cookie_envelope`**（`:256`，锁 `qzone_cookie_stale`）、**`test_list_feed_qzone_error_code`**（`:271`，锁风控码提取）、**`test_list_feed_http_error_does_not_echo_response_body`**（`:290`，锁 §5.5 第 4 条）
- get_post：`test_get_post_found_and_missing`（`:317`）、`test_get_post_requires_tid`（`:341`）
- post_comment：`test_post_comment_top_level`（`:351`）、**`test_post_comment_reply_prepends_mention`**（`:510`，锁 `@{uin:…,nick:…,who:1}` 格式）、`test_post_comment_rejected_by_qzone`（`:533`）、`test_post_comment_validates_args`（`:549`）
- 幂等（若实现 §5.4 的落盘去重则一并移植）：`test_post_comment_live_scheduler_records_effect_receipt`（`:368`）、`test_comment_identity_mismatch_does_not_reserve_effect`（`:415`）、`test_scheduled_reply_requires_source_comment_identity`（`:445`）、`test_scheduled_top_level_comment_deduplicates_by_source_post`（`:461`）、`test_content_fallback_identity_includes_commenter_uin`（`:484`）
- friends：`test_list_friends_with_filter`（`:563`）、`test_list_friends_empty`（`:580`）

`test_qzone_publish.py`（1090 行）：与来源 1 的 `test_qzone_tool.py` 高度重叠，**优先用来源 1 那份**（同步 API 形状一致）；只挑 corlinman 独有的 effect/幂等用例补进来。
`test_qzone_policy.py`（184 行）：**不移植**（依赖 `corlinman-content-policy`）。
`test_qzone_daily.py`（1243 行）/ `test_qzone_reply.py`（727 行）/ `test_admin_scheduler_qzone.py`（930 行）：属于 D1/D2 定时任务范畴，不在本任务内。

---

## 6. 配置 / 密钥清单

> **本节只写"值在哪里"，不写任何值本身。**

### 6.1 hermes 侧要新增的环境变量

| 键 | 必需 | 目标环境的值来源 | 说明 |
|---|---|---|---|
| `ONEBOT_WS_URL` | 入站必需 | 生产已知：NapCat OneBot v11 正向 WS 监听 `ws://127.0.0.1:3001`（`docs/migration-corlinman/00-PLAN.md:15`；corlinman 侧配置在 `/opt/corlinman/data/config.toml` 的 `[channels.qq.instances.default].ws_url`） | 入站适配器 + 工具回退传输 |
| `ONEBOT_HTTP_URL` | 可选 | 若 NapCat 未开 HTTP 面则留空 | 来源 1 语义：优先于 `ONEBOT_WS_URL` |
| `ONEBOT_ACCESS_TOKEN` | 视 NapCat 配置 | **`/opt/corlinman/data/config.toml` → `[channels.qq.instances.default].access_token`**（该键在 corlinman 里被登记为 secret：`corlinman-server/.../routes_admin_a/_channels_lib.py:236`）。若写成 `{ env = "..." }` 形式则真值在 NapCat 容器的环境或 `/opt/corlinman/data/.napcat/` 下 | OneBot WS 鉴权 |
| `ONEBOT_SELF_IDS` | 可选 | 同上 `[channels.qq.instances.default].self_ids` | 冷启动前的 @ 目标种子；事件自带 `self_id` 会覆盖 |
| `ONEBOT_GROUP_WHITELIST` | 生产必需 | 同上 `.group_whitelist`（生产 5 个群 id） | 逗号分隔；**空 = 全部拒绝**，未设 = 不启用白名单（照搬 S5 语义，务必区分"未设"与"空"） |
| `ONEBOT_GROUP_KEYWORDS` | 生产必需 | 同上 `.group_keywords`（生产每群关键词"格兰"） | JSON `{"<group_id>": ["kw", ...]}`；解析器见来源 2 `router.py:94` |
| `ONEBOT_GROUP_REPLY_POLICY` | 可选 | 同上 `.group_reply_policy` | `mention_or_keyword`(默认) / `all` |
| `ONEBOT_GROUP_REPLIES_ENABLED` | 可选 | 同上 `.group_replies_enabled`（**生产为 `false`，见风险 R1**） | 群内总静音开关 |
| `ONEBOT_GROUP_REPLY_COOLDOWN_SECS` | 可选 | 同上 `.group_reply_cooldown_secs`（corlinman 默认 20） | 仅约束非 @ 回复 |
| `ONEBOT_GROUP_RATE_LIMIT_WINDOW_MINUTES` | 生产必需 | 同上 `.group_rate_limit_window_minutes`（**生产 3**） | 与下一项组成"5 条 / 3 分钟" |
| `ONEBOT_GROUP_RATE_LIMIT_MAX_MESSAGES` | 生产必需 | 同上 `.group_rate_limit_max_messages`（**生产 5**） | |
| `ONEBOT_RATE_LIMIT_GROUP_PER_MIN` | 可选 | 同上 `.rate_limit.group_per_min` | 令牌桶维度，未设即关闭 |
| `ONEBOT_RATE_LIMIT_SENDER_PER_MIN` | 可选 | 同上 `.rate_limit.sender_per_min` | 同上 |
| `ONEBOT_HOME_CHAT` | 可选 | 由运维选定 | cron/通知的默认投递目标（对齐 telegram 的 `TELEGRAM_HOME_CHANNEL`） |
| `ONEBOT_HEALTH_PROBE_S` / `ONEBOT_HEALTH_LOST_S` | 可选 | 默认 30 / 120（对齐来源 2 `CORLINMAN_QQ_HEALTH_PROBE_S` / `_LOST_S`） | |
| `ONEBOT_MAX_CONCURRENCY` | 可选 | 默认对齐来源 2 `_channel_max_concurrency("QQ")` | |
| `ONEBOT_TOOL_SUMMARY` | 可选 | 对齐来源 2 `CORLINMAN_QQ_TOOL_SUMMARY`（默认开，`0` 关） | 是否在回复前加 `📋 本次操作:` 块 |

### 6.2 主动发言 / 群摘要（若实现）

对应 corlinman `[channels.qq.instances.default]` 的扁平键，权威列表见 `corlinman-server/.../routes_admin_a/_channels_lib.py:246-269`：
`proactive_enabled`(bool，**生产 true**)、`proactive_groups`(int list)、`proactive_min_gap_minutes`(默认 45)、`proactive_max_gap_minutes`(默认 = min×4)、`proactive_daily_max`(默认 4)、`proactive_active_start_hour`(默认 9)、`proactive_active_end_hour`(默认 23)、`proactive_probability`(默认 1.0)、`proactive_context_messages`(默认 30)、`proactive_prompt`(str)、`proactive_timezone`(str)。
群摘要：`monitors`(嵌套 list，字段见 §3.9)、`monitor_retention_hours`。
生产的 3 条 monitor：`sanhu` 10:00 → user、`jlu` 11:00 → user、`qunjlu` 09:00 → group（`docs/migration-corlinman/00-PLAN.md:29-30`）。

### 6.3 Qzone 工具的凭据

**没有独立凭据，两个源都一样。** 全部从 OneBot 借（§2.2、§3.12）：`get_login_info`/`verify_identity` 拿 uin，`get_cookies {"domain":"user.qzone.qq.com"}` 拿 cookie（含 `p_skey`/`skey`），`g_tk` 本地由 `p_skey` 算。
corlinman 侧也**确认没有 `[qzone]` 配置段**（§3.12 末），qzone 完全继承 `[channels.qq]`。
因此 qzone 工具族唯一的配置依赖就是 `ONEBOT_WS_URL` / `ONEBOT_HTTP_URL` + `ONEBOT_ACCESS_TOKEN`。
需要新增的**非密**运行状态目录（对齐 corlinman，见 §3.12 状态表）：
- `<HERMES_DATA>/qzone_post_log/<persona_id>.json` —— 近期已发正文，防重复
- `<HERMES_DATA>/qzone_seen_comments/<persona_id>.json` —— 已回复评论标记

**hermes 不存储任何 QQ 密码。** 登录态的真正宿主是 NapCat 容器的持久卷（compose 里挂 `../../.napcat/app:/app/napcat` 与 `../../.napcat/ntqq:/app/.config/QQ`，见 `/Users/cornna/project/corlinman/docker/compose/docker-compose.qq.yml:45-48`）。

### 6.4 语音工具的凭据

复用 hermes 现有 TTS 配置（`~/.hermes/config.yaml` 的 `tts:` 段 + 各 provider 的 API key 环境变量），**不引入新键**（来源 1 `onebot_voice_tool.py:119-131`）。

### 6.5 NapCat 侧（**只读不改**）

| 项 | 位置 |
|---|---|
| OneBot v11 正向 WS | `127.0.0.1:3001`（compose 里对内网 `expose: 3001`；生产映射到 host 回环） |
| WebUI + 扫码登录 HTTP API | `127.0.0.1:6099`（compose `ports: "127.0.0.1:6099:6099"`） |
| WebUI token（**旧版 compose 路径**） | 环境变量 `WEBUI_TOKEN` / `NAPCAT_WEBUI_TOKEN`，由 `deploy/install.sh:877-892` 生成并写入 **`/opt/corlinman/data/.napcat/legacy-secrets.env`**（0600，属主 = 服务用户） |
| WebUI token + OneBot token（**新版 managed 路径**） | `napcat_manager` 生成的**两个独立** `secrets.token_urlsafe(32)`，写入 **`/opt/corlinman/data/.napcat/managed/instances/<instance_id>/manager-secrets.env`**（`O_CREAT\|O_EXCL`，0600，fsync）：键名 `WEBUI_TOKEN`（别名 `NAPCAT_WEBUI_TOKEN`）与 `ONEBOT_TOKEN`。生成点 `native_provider.py:271`，读取点 `:323` |
| corlinman 配置里的对应键 | `[channels.qq.instances.<id>].napcat_url`（WebUI 基址）+ `.napcat_access_token`（WebUI token）；`.ws_url` + `.access_token`（OneBot 面）。**四个是两组不同的东西，别混** |
| NapCat 镜像与版本 | `mlikiowa/napcat-docker:${NAPCAT_VERSION:-v4.18.4}`，容器名 `corlinman-napcat`（compose）或 `corlinman-napcat-<instance_id>`（managed） |
| QQ 会话持久卷 | compose：`<repo>/.napcat/app`、`<repo>/.napcat/ntqq`；managed：具名卷 `<resource>-app` / `<resource>-qq` |
| 生产 NapCat 的 OneBot 服务器块 | 由 corlinman 通过 WebUI API 反写，`name="corlinman"`、`host="0.0.0.0"`、`messagePostFormat="array"`、`heartInterval=30000`、`enableForcePushEvent=true`（`_napcat_lib.py:443`） |

**约束（D3）**：hermes 只当 OneBot 客户端接入既有实例，**不得**启动第二个 NapCat（1.9 GB 内存起不动第二个 NTQQ，`00-PLAN.md:48`）。因此来源 2 的 `napcat_manager` 整包（2445 行：容器编排/启停/端口分配/token 生成/清单/日志）**整体不移植**，`_napcat_lib.py` 的扫码登录与 WebUI 配置反写也**不移植**（会与 corlinman 抢着改同一份 NapCat 配置）。只借鉴 §3.11 的健康判定思路。

⚠️ **共存冲突**：corlinman 的 `_ensure_onebot_websocket_server` 每 10 s 去抖地把它自己的 `name="corlinman"` 服务器块写回 NapCat。hermes **不要**也去写 OB11 配置；如果 hermes 需要独立的 token/端口，应当由运维**在 NapCat WebUI 里手工加一个第二服务器块**（例如 `name="hermes"`、另一个端口），否则两边会互相覆盖。最简方案：**hermes 直接复用 corlinman 那个块的 ws 地址与 token**（只读消费，不改配置）。

---

## Open questions / risks

**R1 —（阻断级）生产配置自相矛盾：`group_replies_enabled = false` 与 `proactive_enabled = true` 并存。**
按来源 2 的语义：`group_replies_enabled=false` 会在 @/关键词判定**之前**丢弃所有群消息（`router.py:337-338`），并且**同时使主动发言循环每一拍都跳过**（`service.py:1223-1224`）。也就是说，生产配置若原样搬过来，"5 个群白名单 + 关键词格兰 + @回复 + 5 条/3 分钟限流 + 主动发言"这一整套**全部不会生效**，机器人在群里完全静默，只回私聊。
→ **必须先向用户确认**：(a) 生产上 QQ 群是否确实已静默？(b) 迁移后期望的目标状态是"群内活跃"还是"维持静默"？在得到答复前，实现要保证这两个键的语义**与来源 2 完全一致**，不要"善意地"让 `proactive_enabled` 绕过总开关。

**R2 —（已大幅降级）目标 NapCat 的 WS 方向。**
corlinman 通过 WebUI API 反写的服务器块是 `host: "0.0.0.0"` 的 **`websocketServers`** 条目（`_napcat_lib.py:443`），即 **NapCat 作服务端、客户端拨号**的正向 WS —— 与来源 2 适配器的方向一致。同时 `messagePostFormat: "array"` 确认消息以**段数组**而非 CQ 码下发，来源 2 的段解析器可直接用。**仍建议**部署前用 `websocat ws://127.0.0.1:3001` + token 实测一次握手。

**R3 — 未验证：`ONEBOT_ACCESS_TOKEN` 的真实取值形态与来源。**
两条可能路径：(a) 旧版 compose —— token 在 `/opt/corlinman/data/config.toml` 的 `[channels.qq.instances.default].access_token`；(b) 新版 managed —— token 由 `napcat_manager` 生成在 `/opt/corlinman/data/.napcat/managed/instances/<id>/manager-secrets.env` 的 `ONEBOT_TOKEN` 键。配置里的值还可能是 `{ env = "..." }` 间接引用。**必须现场确认走的是哪条**，并注意 WebUI token 与 OneBot token 是**两个不同的密钥**（§6.5），拿错了会一直 401。**不要**把值复制进 hermes 的 `.env` 之外的任何地方（尤其不要进 git）。

**R4 —（已解除）`qzone_reply` / `qzone_friends` 有现成实现。**
corlinman 的 `comment.py`（1064 行）提供了完整的 `qzone_list_feed` / `qzone_get_post` / `qzone_post_comment` / `qzone_list_friends`，并有 588 行测试。移植路径清晰（§5.5）。**新的残留问题**：生产 job `hermes.qzone_friends` 在 corlinman 里**没有对应的 builtin**（只有 `qzone.daily_publish` 与 `qzone.reply_comments`），推测是用这几个工具拼的自定义 job —— 它到底做什么需要向用户确认，否则迁移后该任务无法复现。

**R5 — richval 线格式脆弱。**
`_build_richval`（`qzone_tool.py:160-179`）是社区逆向产物，腾讯改格式即失效。已单测锁住当前形状（`TestBuildRichval` 3 例），但**测试只能防回归、不能防上游变更**。建议在发布路径加一条"发布成功但 tid 为空"的告警，作为格式漂移的早期信号。

**R6 — 双写鉴权（S1）可能触发 NapCat 的严格模式。**
同时发 header 与 query param 在 OneBot v11 规范下合法，但个别 NapCat 构建对"token 出现两次"可能有意见。落地时要在目标实例上实测；若报错则退回**只发 header**（来源 2 在生产验证过的形式）。

**R7 — 内存预算。**
目标机剩余内存约 190 MB（`00-PLAN.md:15,88`）。`websockets` 本身很轻，但入站队列 64 条 + 出站 64 条 + `base64://` 内联附件（单文件最大 30 MiB，编码后约 40 MiB）可能瞬时吃掉大量内存。建议把 `_QQ_FILE_BASE64_MAX_BYTES` 从 30 MiB **下调**（例如 8 MiB），超限走字面路径或直接拒绝。

**R8 — 与既有 `qqbot`（官方 API）适配器的命名冲突。**
目标仓库已有 `gateway/platforms/qqbot/`（官方 QQ 机器人）和 `gateway/config.py:128` 的 `QQBOT = "qqbot"`。新平台务必用**不同的 name**（建议 `onebot`，label "QQ (OneBot v11 / NapCat)"），否则注册表会撞名；同时要在文档里说清两者不是一回事，避免运维配错。

**R9 — 斜杠命令层的落差。**
来源 2 的 router 深度耦合 corlinman 的 `commands.py`（`CommandSpec` / `SlashAccessPolicy` / `wizard_prelude`）。移植时若直接剥掉，`explicit = mentioned or 命令匹配`（`router.py:352`）会退化成 `explicit = mentioned` —— 群里发 `/xxx` 将不再被视为"点名召唤"，会被关键词闸门拦掉。需要在 hermes 的命令层找到等价的"这条文本是命令"判定并接回去，否则群内斜杠命令不可用。

**R10 — `sender_name` / `reply_to_text` 在来源 2 的 router 里声明了但 QQ 路径未填充。**
`RoutedRequest.sender_name`（`router.py:159`）与 `reply_to_text`（`:164`）在 `dispatch` 的返回里**没有赋值**（`:443-453`），实际的发送者名是在派发循环里另行从 `payload.sender.card or nickname` 取的（`service.py:2688-2691`）。移植时不要以为 router 会给你这两个字段。

**R11 —（高）与 corlinman 抢写 NapCat 配置。**
corlinman 的 `_ensure_onebot_websocket_server`（`_napcat_lib.py:881`）会周期性把自己的 OB11 服务器块写回 NapCat（10 s 去抖）。hermes 若也去写 OB11 配置，两边会互相覆盖，最坏情况是打断 corlinman 的连接、连带打断线上 QQ 服务。**硬性要求：hermes 只读消费既有 ws 地址与 token，绝不调用 `/api/OB11Config/SetConfig`。** 若确实需要独立 token，走运维手工在 WebUI 加第二个服务器块（另一端口），并在文档里写清该块不受 corlinman 管理。

**R12 — Qzone 读路径极其脆弱（比 publish 更脆）。**
`feeds3_html_more` 返回的是 **JS 对象字面量里嵌 JS 转义的渲染后 HTML**，靠正则抠字段（`comment.py:309-436`）。腾讯改一次前端 markup 就全断。缓解：(a) 完整照搬 `_unescape_hex` 的三类转义处理（漏一类会**静默**失效）；(b) 解析结果为空时把原始响应片段打进日志（corlinman 就是这么做的）；(c) 把 `qzone_list_feed` 的失败视为**可预期**，调用它的 cron 任务必须能容忍空结果而不是报错退出。

**R13 — `qzone_get_post` 的 O(时间线) 语义会静默丢帖。**
它拉 40 条时间线再筛 tid（`comment.py:592-602`），滚出窗口即不可达。移植时至少要在工具描述里写明，并让 `lookback_posts` 的语义与这个上限对齐，否则"回复我最近 5 条帖子下的评论"在繁忙账号上会静默漏掉。
