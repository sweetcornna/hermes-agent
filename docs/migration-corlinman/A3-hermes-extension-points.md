# A3 — Hermes 原生扩展点映射 + 部署足迹分析

**批次**: 1（侦察，只读）
**范围**: 为 corlinman → hermes 迁移提供实现者可直接照做的扩展点契约
**基线**: `NousResearch/hermes-agent` @ `8911e2e0e` (main)，本地检出 `/Users/cornna/project/hermes-agent`
**核实状态**: 本文所有行号与签名均已直接对照代码核实。个别无法核实的断言以 `UNVERIFIED` 内联标注。

---

## 0. 前置更正 —— 开工前必须知道的三件事

### 0.1 hermes 已经有一个 QQ 适配器，但不是 OneBot

`gateway/platforms/qqbot/`（4 837 LOC）对接的是 **QQ 开放平台官方机器人 API v2**：WebSocket 网关入站、`api.sgroup.qq.com` REST 出站、appid/secret 鉴权。枚举成员在 `gateway/config.py:346` (`QQBOT = "qqbot"`)。

它**不是** OneBot，**不要**去扩展它。全仓库 `onebot` / `napcat` / `go-cqhttp` 零命中 —— 与 `00-PLAN.md:36` 的判断一致。

但它是本仓库里**最接近**你要写的东西的结构范本（WS 入站 + HTTP 出站 + token + 群/私聊策略 + 媒体上传 + 重连退避）。与 WhatsApp 的 `whatsapp` / `whatsapp_cloud` 双适配器同理，新适配器应注册一个**全新的独立平台名**（建议 `onebot`）。

### 0.2 `gateway/platforms/ADDING_A_PLATFORM.md` 的第 2–16 节已经过时

该文档第 1 节（Plugin Path）仍然准确且是**唯一应当遵循**的路径。第 2–16 节描述的是 built-in 路径，其中多项已被注册表钩子取代：

| 过时章节 | 它让你改的东西 | 实际取代者 |
|---|---|---|
| §2 | `gateway/config.py` Platform 枚举 + `_apply_env_overrides` | `Platform._missing_()` (`gateway/config.py:349`) + `env_enablement_fn` |
| §3 | `gateway/run.py::_create_adapter` if/elif | 注册表优先分支 |
| §4 | `_is_user_authorized()` 两个 dict | `allowed_users_env` / `allow_all_env` |
| §6 | `agent/prompt_builder.py::PLATFORM_HINTS` | `platform_hint` |
| §7 | `toolsets.py` 手写 toolset | `toolsets.py:833-848` 自动生成 |
| §8 | `cron/scheduler.py::platform_map` | `cron_deliver_env_var` |
| §9 | `tools/send_message_tool.py::platform_map` | `standalone_sender_fn` |
| **§11** | `gateway/channel_directory.py` 里的 `for plat_name in ("telegram", "whatsapp", "signal", "your_platform")` | **该循环已不存在**，现为 `Platform` 枚举遍历 + `plugin_entries()` 遍历 |
| §13 | `hermes_cli/gateway.py::_PLATFORMS` | `setup_fn` + `_all_platforms()` 注册表合并 |

权威文档改用 `website/docs/developer-guide/adding-platform-adapters.md`（787 行；自动处理清单在 `:210-236`，完整 `register()` 骨架在 `:141-194`）。

### 0.3 提示词缓存是不可逾越的红线

`AGENTS.md:19-23`：

> - **Per-conversation prompt caching is sacred.** A long-lived conversation reuses a cached prefix every turn. Anything that mutates past context, swaps toolsets, or rebuilds the system prompt mid-conversation invalidates that cache and multiplies the user's cost. We do not do it (the one exception is context compression).

`AGENTS.md:88-91`：

> - **Cache-, alternation-, and invariant-safe.** Preserve prompt caching, strict message role alternation (never two same-role messages in a row; never a synthetic user message injected mid-loop), and a system prompt that is byte-stable for the life of a conversation.

这条直接决定了"格兰"life events + decay 的实现形态（见 §6）。

---

## 1. 平台适配器 —— 如何加 QQ / OneBot v11

### 1.1 注册契约：`PlatformEntry`

`gateway/platform_registry.py:63`（全文件 698 行）。必填四项：

```python
@dataclass
class PlatformEntry:
    """Metadata and factory for a single platform adapter."""
    name: str                                # :67  config.yaml 里的标识符
    label: str                               # :70  人类可读名
    adapter_factory: Callable[[Any], Any]    # :75  PlatformConfig -> adapter 实例
    check_fn: Callable[[], bool]             # :82  被动依赖探测，必须无副作用
```

可选项 —— **每一项都替你省掉一处核心代码修改**：

| 字段 | 行 | 取代的核心改动 |
|---|---|---|
| `validate_config` | `:87` | — |
| `ensure_deps_fn` | `:104` | 主动安装器；见 `:96-103` 的 #79812 说明 |
| `is_connected` | `:109` | `get_connected_platforms()` |
| `required_env` / `install_hint` | `:112` / `:115` | setup 展示 |
| `setup_fn` | `:121` | `hermes_cli/gateway.py` 向导 |
| `allowed_users_env` / `allow_all_env` | `:133` / `:135` | `_is_user_authorized()` 授权表 |
| `max_message_length` | `:139` | 分片长度 |
| `pii_safe` / `emoji` / `allow_update_command` | `:143` / `:147` / `:151` | 展示与策略 |
| `platform_hint` | `:156` | `PLATFORM_HINTS` |
| `env_enablement_fn` | `:166` | `_apply_env_overrides()` |
| `apply_yaml_config_fn` | `:181` | `gateway/config.py` YAML schema |
| `cron_deliver_env_var` | `:187` | `cron/scheduler.py::platform_map` |
| `parse_target_ref_fn` | `:202` | `send_message` 原生目标语法 |
| `validate_target_ref_fn` | `:208` | — |
| `send_message_handler` | `:214` | 整请求投递覆写 |
| `standalone_sender_fn` | `:229` | 进程外 cron 投递 |

两个关键签名：

```python
# gateway/platform_registry.py:190
apply_yaml_config_fn: Optional[Callable[[dict, dict], Optional[dict]]] = None
# (yaml_cfg, platform_cfg) -> 合并进 PlatformConfig.extra 的 extras

# gateway/platform_registry.py:229
standalone_sender_fn: Optional[Callable[..., Awaitable[dict]]] = None
# async (pconfig, chat_id, message, *, thread_id=None, media_files=None,
#        force_document=False) -> {"success": True, "message_id": ...} | {"error": str}
```

**`check_fn` 必须是被动的** —— `hermes setup` / `hermes status` / dashboard readiness / `load_gateway_config()` 都会随意调用它。安装逻辑一律放 `ensure_deps_fn`（`gateway/platform_registry.py:96-103` 记录了把二者混同导致 desktop 启动死循环的 #79812）。

### 1.2 插件侧入口：`ctx.register_platform`

`hermes_cli/plugins.py:2774`：

```python
    def register_platform(
        self,
        name: str,
        label: str,
        adapter_factory: Callable,
        check_fn: Callable,
        validate_config: Callable | None = None,
        required_env: list | None = None,
        install_hint: str = "",
        **entry_kwargs: Any,
    ) -> Optional[PluginRegistration]:
```

`**entry_kwargs` 原样转发给 `PlatformEntry(...)`（`:2818`）；未知键抛 `TypeError`。`source="plugin"` 与 `plugin_name` 自动填入。

Telegram 的实际调用（`plugins/platforms/telegram/adapter.py:10867`）：

```python
    ctx.register_platform(
        name="telegram",
        label="Telegram",
        adapter_factory=_build_adapter,
        check_fn=telegram_deps_present,
        ensure_deps_fn=check_telegram_requirements,
        is_connected=_is_connected,
        required_env=["TELEGRAM_BOT_TOKEN"],
        install_hint="Run `hermes setup` to install Telegram support.",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="TELEGRAM_ALLOWED_USERS",
        allow_all_env="TELEGRAM_ALLOW_ALL_USERS",
        cron_deliver_env_var="TELEGRAM_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4096,
        emoji="✈️",
        allow_update_command=True,
    )
```

Feishu 同形（`plugins/platforms/feishu/adapter.py:5876-5895`），额外用了 `ensure_deps_fn` + `validate_config`。

### 1.3 基类：**恰好四个** `@abstractmethod`

`gateway/platforms/base.py:2890` `class BasePlatformAdapter(ABC)`（全文件 7 357 行）。经直接核实，抽象方法**只有四个**：

```python
# base.py:3894
    @abstractmethod
    async def connect(self, *, is_reconnect: bool = False) -> bool:

# base.py:3914
    @abstractmethod
    async def disconnect(self) -> None:

# base.py:3919
    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:

# base.py:7146
    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """
        Returns dict with at least:
        - name: Chat name
        - type: "dm", "group", "channel"
        """
```

> `ADDING_A_PLATFORM.md:88-98` 把 `send_typing` 也列为 required —— **不对**。`send_typing`（`base.py:4298`）是带默认实现的非抽象方法。

`connect` 的 `is_reconnect` 语义（`base.py:3899-3908`）：`False` = 冷启动，可以丢弃服务端队列；`True` = 看门狗重连，**必须保留**队列。

构造：`base.py:3011` `def __init__(self, config: PlatformConfig, platform: Platform):`

常用可覆写方法（均非抽象）：

| 方法 | 行 |
|---|---|
| `send_typing` / `stop_typing` | `:4298` / `:4307` |
| `send_image` / `send_image_file` | `:4396` / `:4732` |
| `send_animation` / `send_voice` / `send_video` / `send_document` | `:4415` / `:4486` / `:4632` / `:4659` |
| `send_multiple_images` | `:4339` |
| `edit_message` / `delete_message` | `:3976` / `:4005` |
| `send_draft`（流式草稿） | `:3274` |
| `send_clarify` / `send_slash_confirm` | `:4204` / `:4169` |
| `format_message` / `truncate_message` | `:7177` / `:7189` |
| `build_source` | `:7047` |
| `handle_message`（入站分发口） | `:5981` |
| `list_channels`（鸭子类型，可选） | 消费点 `gateway/channel_directory.py:154` |

按钮回调 id 约定是跨平台共享的：`cl:<id>:<idx>`、`appr:<id>:<choice>`、`sc:<choice>:<id>`（`ADDING_A_PLATFORM.md:125`）。照抄即可复用网关侧 resolver。

### 1.4 能力声明是**类属性**，不是注册表字段

全部定义在 `BasePlatformAdapter` 上：

| 属性 | 行 | 默认 | 含义 |
|---|---|---|---|
| `supports_code_blocks` | `base.py:2909` | `False` | 是否渲染 ``` 代码块 |
| `supports_status_text` | `base.py:2917` | `False` | typing 指示是文本型（Slack 式） |
| `supports_async_delivery` | `base.py:2951` | `True` | 轮次结束后仍可推消息 |
| **`splits_long_messages`** | `base.py:2959` | `False` | **见下方警告** |
| `typed_command_prefix` | `base.py:2971` | `"/"` | Slack/Matrix 用 `"!"` |
| `supports_inchannel_continuable` | `base.py:2986` | `False` | 目前仅 Slack |
| `interactive_resume` | `base.py:2999` | `True` | 有人在场可回答续跑提示 |
| `REQUIRES_EDIT_FINALIZE` | `base.py:3947` | `False` | 需要显式收尾编辑 |
| `MAX_MESSAGE_LENGTH` | 子类自定 | — | Telegram 4096 / WeCom 4000 / IRC 450 |
| `message_len_fn` | `base.py:3123`（property） | `len` | Telegram 覆写为 `utf16_len` |

> ⚠️ **`splits_long_messages` 直接影响 12 个 cron 任务的产出完整性。**
> `gateway/delivery.py:29` 定义 `MAX_PLATFORM_OUTPUT = 4000`；`:488` 起对超长内容做「完整存档 + 截断投递」，而 `:503` 的判断是
> `if getattr(adapter, "splits_long_messages", False):` —— 只有置 `True` 的适配器才能拿到**完整**内容自行分片。
> OneBot 适配器如果在 `send()` 里自己分片，**必须**设 `splits_long_messages = True`，否则每日播报会被砍到 4000 字。

反应（reaction）与线程不是布尔开关，而是「有没有实现对应方法」。入站反应扇出：`base.py:3683 set_reaction_handler(...)`；通用平台事件：`base.py:3632 set_platform_event_handler(...)`。跨 profile 凭证互斥锁：`base.py:3527 _acquire_platform_lock(scope, identity, resource_desc)`。

### 1.5 入站数据结构

`MessageEvent` —— `gateway/platforms/base.py:2299`。字段：`text`、`message_type`、`user_id`、`user_name`、`source: SessionSource`、`raw_message`、`message_id`、`platform_update_id`、`media_urls: List[str]`（**本地缓存文件路径，不是远程 URL**）、`media_types`、`reply_to_message_id` / `_text` / `_author_id` / `_author_name` / `_is_own_message`、`prompt_response`、`auto_skill`、**`channel_prompt`**、`channel_context`、`internal`、`metadata: Dict`、`timestamp`、`allow_gateway_control`。方法：`is_command()` `:2391`、`get_command()` `:2395`、`get_command_args()` `:2410`。

`MessageType` —— `base.py:2278`：`TEXT / LOCATION / PHOTO / VIDEO / AUDIO / VOICE / DOCUMENT / STICKER / COMMAND`。

`SessionSource` —— `gateway/session.py:149`：`platform`、`chat_id`、`chat_name`、`chat_type`（`"dm" | "group" | "channel" | "thread"`）、`user_id`、`user_name`、`thread_id`、`chat_topic`、`user_id_alt`、`chat_id_alt`、`is_bot`、`scope_id`、`guild_id`（废弃别名）、`parent_chat_id`、`message_id`、`role_authorized`、`profile`。

**永远不要手工构造 `SessionSource`** —— 用 `base.py:7047 build_source(...)`，它同时负责按 `gateway.profile_routes` 盖上 `source.profile`（`:7069-7074`），也就是 per-channel profile 路由的落地点。

会话键：`gateway/session.py:1090 build_session_key(source, group_sessions_per_user=True, thread_sessions_per_user=False, profile=None) -> str`。私聊形如 `<ns>:<platform>:dm:<chat_id>[:<thread_id>]`（`:1140-1147`）。

最小入站路径（`plugins/platforms/ntfy/adapter.py:332-390`）：按 message_id 去重 → 丢弃自身/回环 → `build_source` → `MessageEvent` → `await self.handle_message(event)`。

### 1.6 出站数据结构

`SendResult` —— `base.py:2465`：`success`、`message_id`、`error`、`raw_response`、`retryable`、`retry_after`、`continuation_message_ids`、`error_kind`。

**`error_kind` 应当用 `base.py:2565 classify_send_error(exc, error_text="") -> str` 填**，取值来自 `base.py:2516 SEND_ERROR_KINDS`：`too_long`、`bad_format`、`forbidden`、`not_found`、`rate_limited`、`transient`、`unknown`。这套分类驱动 dead-target 检测；不填则退化成字符串匹配。

**没有独立的"出站 payload 类"** —— 出站 payload 就是 `send()` 的四元参数 `(chat_id, content, reply_to, metadata)`。`metadata` 是扩展点，网关会塞入 `thread_id`、`notify` 等键。

路由：`tools/send_message_tool.py` 全程走注册表 —— `platform_registry.get(...)` 在 `:396` / `:652` / `:885`，分别消费 `send_message_handler`（`:493`）、`parse_target_ref_fn`（`:670`）、`standalone_sender_fn`（`:889`）。缺 `standalone_sender_fn` 时进程外 cron 会报 `No live adapter for platform '<name>'`（`:920`）。

> ⚠️ **`DeliveryTarget.parse` 对未知平台是静默降级，不报错。**
> `gateway/delivery.py:230`，格式 `platform[:chat_id[:thread_id]]`。`:270` 与 `:278` 两处 `except ValueError:` 都 `return cls(platform=Platform.LOCAL)` —— 平台名拼错不会抛异常，任务会"成功"但只写本地文件。配 `deliver=` 时务必核对拼写。

### 1.7 媒体

`gateway/platforms/base.py` 缓存助手，均返回本地绝对路径：

- `cache_image_from_bytes(data, ext=".jpg") -> str` — `:854`
- `async cache_image_from_url(url, ext=".jpg", retries=2)` — `:883`
- `cache_audio_from_bytes(data, ext=".ogg")` — `:1005`
- `async cache_audio_from_url(...)` — `:1025`
- `cache_document_from_bytes(data, filename)` — `:2126`

全部内部调用 `validate_inbound_media_size(...)`（`:779`，上限来自 `:758 get_inbound_media_max_bytes()`）。结果路径放进 `MessageEvent.media_urls`，vision 工具才读得到。出站本地路径须过 `validate_media_delivery_path`（`:4761`）。

### 1.8 启用与配置

**不要**往 `Platform` 枚举加成员。`gateway/config.py:317-324`：

> Built-in platforms have explicit members. Plugin platforms use dynamic members created on-demand by `_missing_()` so that `Platform("irc")` works without modifying this enum.

`_missing_`（`:349`）接受两类名字：(a) `plugins/platforms/` 下同时含 `__init__.py` 与 `plugin.yaml` 的目录（`_scan_bundled_plugin_platforms`，`:395-412`）；(b) `platform_registry.is_registered(value)` 为真的运行时注册项（`:380-390`）。

> ⚠️ **目录名规则（`hermes_cli/plugins.py:4417 _platform_name_from_manifest`）**
> 平台名写在 adapter 模块里，而框架**刻意不提前导入**它，因此从清单反推：
> 去掉 `manifest.name` 结尾的 `-platform`，否则回退到目录 basename。
> 所以 `name: onebot-platform` + 目录 `onebot/` ⇒ 平台名 `onebot`。**两者不一致会让惰性加载静默失效。**

`PlatformConfig` —— `gateway/config.py:639`：`enabled`、`token`、`api_key`、`home_channel`、`reply_to_mode`、`gateway_restart_notification`、`typing_indicator`、`typing_status_text`、`channel_overrides: Dict[str, ChannelOverride]`、`extra: Dict[str, Any]`。

核心**免费**桥接的通用 YAML 键（`gateway/config.py:1620-1706` 的 shared-key 循环）：`reply_in_thread`、`cron_continuable_surface`、`require_mention`、`send_read_receipts`、`free_response_channels`、`mention_patterns`、`exclusive_bot_mentions`、`dm_policy`、`allow_from`、`allow_admin_from`、`user_allowed_commands`、`group_policy`、`group_allow_from`、`group_allow_admin_from`、`group_user_allowed_commands`、`channel_prompts`、`gateway_restart_notification`、`typing_indicator`、`typing_status_text`，外加 `channel_overrides`。

> 私有键才用 `apply_yaml_config_fn`。**切勿**在返回值里重复上述通用键 —— 该钩子的返回值是 `dict.update()` 覆盖式合并，会打翻核心已算好的优先级（见 Telegram 的 `_GENERIC_MERGE_KEYS` 防护，`plugins/platforms/telegram/adapter.py:10852-10857`）。

QQBot 的配置块可直接作 schema 蓝本（`gateway/platforms/qqbot/adapter.py:8-25`）：

```yaml
platforms:
  qq:
    enabled: true
    extra:
      app_id: "..."
      client_secret: "..."
      dm_policy: "pairing"      # open | allowlist | disabled | pairing
      allow_from: ["openid_1"]
      group_policy: "pairing"
      group_allow_from: ["group_openid_1"]
```

**Toolset 自动生成** —— `toolsets.py:833-848`：任何已注册平台自动获得 `hermes-<name>` toolset（`_HERMES_CORE_TOOLS` + 该插件注册到同名 toolset 的工具）。**无需改 `toolsets.py`。**

插件发现顺序（`hermes_cli/plugins.py:4062-4092`）：bundled `plugins/` → bundled `plugins/platforms/` → `$HERMES_HOME/plugins/` → `./.hermes/plugins/`（需 `HERMES_ENABLE_PROJECT_PLUGINS`）→ pip entry points。**bundled 平台插件无条件自动加载；`~/.hermes/plugins/` 下的需 `plugins.enabled` 显式开启**（`hermes_cli/plugins.py:1055-1058`、`hermes_cli/gateway.py:6019-6023`）。

setup 向导自动收录：`hermes_cli/gateway.py:5994 _all_platforms()` 合并静态 `_PLATFORMS`（`:5802`）与 `platform_registry.all_entries()`（`:6038`）。

排障：`HERMES_PLUGINS_DEBUG=1` 把插件发现日志输出到 stderr（`hermes_cli/plugins.py:123`）。

### 1.9 传输层范本选择

| 范本 | 路径 | 为什么 |
|---|---|---|
| **结构最全** | `plugins/platforms/ntfy/adapter.py`（617 行） | 注册表钩子**全都用上了**且每条都有注释；HTTP 流式，重连梯度 `RECONNECT_BACKOFF = [2, 5, 10, 30, 60]`（`:102-108`），60 s 稳定后重置（`:255`），致命 401/404 走 `_set_fatal_error(..., retryable=False)`（`:278`/`:289`） |
| **传输最像** | `plugins/platforms/simplex/adapter.py`（约 1 442 行） | 裸 `websockets` 连**本机守护进程** `ws://127.0.0.1:5225`；`connect()` 先探活再起 `_ws_listener` + `_health_monitor`（`:210`、`:287`）；`_standalone_send` 开临时 WS（`:1240`+） |
| **协议最像** | `plugins/platforms/wecom/adapter.py`（1 932 行） | 手写 aiohttp WS：鉴权握手（`:312`）、`req_id` 请求/响应 future 关联表（`:458 _send_request`）、应用层心跳（`:406`）、重连梯度（`:365`）。**`req_id` 直接对应 OneBot 的 `echo` 字段** |

Feishu 虽是 WebSocket，但透过厂商 `lark_oapi` SDK 驱动（`plugins/platforms/feishu/adapter.py:1334` 甚至猴补丁了 `ws_client_module.websockets.connect`），传输层不归你写，参考价值低。

**结论：钩子清单抄 ntfy，传输层抄 simplex + wecom。**

### 1.10 分步实施

```
plugins/platforms/onebot/          # 或 ~/.hermes/plugins/onebot/；目录名必须 == 平台名
├── plugin.yaml                    # name: onebot-platform, label: QQ (OneBot v11), kind: platform
├── __init__.py                    # from .adapter import register ;  __all__ = ["register"]
└── adapter.py                     # OneBotAdapter + register(ctx)
```

1. **`plugin.yaml`** —— 整体照抄 `plugins/platforms/ntfy/plugin.yaml`。`kind: platform` 必填。`requires_env` / `optional_env` 的富字典条目会被 `hermes_cli/config.py:5820 _inject_platform_plugin_env_vars()` 自动注入 `OPTIONAL_ENV_VARS`，setup 向导即可见。名字以 `_TOKEN`/`_SECRET`/`_KEY`/`_PASSWORD` 结尾的自动打码（`:5868`）。
   建议变量：`ONEBOT_WS_URL`（默认 `ws://127.0.0.1:3001`）、`ONEBOT_ACCESS_TOKEN`、`ONEBOT_WEBUI_URL`（`http://127.0.0.1:6099`）、`ONEBOT_ALLOWED_USERS`、`ONEBOT_ALLOW_ALL_USERS`、`ONEBOT_HOME_CHANNEL`、`ONEBOT_HOME_CHANNEL_NAME`。
2. **`class OneBotAdapter(BasePlatformAdapter)`**，`super().__init__(config=config, platform=Platform("onebot"))`。凭证读取遵循 `extra.get(...) or os.getenv(...)`，并用 ntfy 的 `_get_scoped_secret` 模式（`plugins/platforms/ntfy/adapter.py:75`）以支持 profile 隔离。
3. **类属性**：`MAX_MESSAGE_LENGTH`（QQ 实测约 4500–5000）、`splits_long_messages = True`、`supports_code_blocks = False`（QQ 不渲染 markdown）。
4. **`connect()`**：连 `ws://127.0.0.1:3001`，头 `Authorization: Bearer <token>`（或 `?access_token=`），`asyncio.create_task(self._run_stream())`，`self._mark_connected()`，返回 `True`。重连/心跳照 wecom `:365`/`:406`。
5. **API 调用关联**：OneBot 的 `echo` 字段 ←→ wecom `req_id` future 表（`:458`）。
6. **入站**：过滤 `post_type != "message"`、过滤 `self_id`、按 `message_id` 去重（可用 `gateway/platforms/helpers.py:27 MessageDeduplicator`），映射 `private`→`chat_type="dm"` / `group`→`"group"`，剥掉 `[CQ:at,qq=<self>]`（参照 wecom `:559` 的 `re.sub(r"^@\S+\s*", "", text)`），下载媒体段缓存为本地路径，`build_source` → `MessageEvent` → `handle_message`。
7. **出站**：`send_private_msg` / `send_group_msg`；媒体用 `[CQ:image,file=file:///abs/path]`（NapCat 在本机，无需上传 API）。返回 `SendResult`，失败时用 `classify_send_error` 填 `error_kind`。
8. **`list_channels()`**：映射 `get_group_list` + `get_friend_list` → `[{"id","name","type"}]`。多数平台做不到，OneBot 能做，白拿。
9. **`register(ctx)`**：照 §1.2 的 Telegram 形状，必带 `cron_deliver_env_var="ONEBOT_HOME_CHANNEL"`、`standalone_sender_fn`、`allowed_users_env`、`allow_all_env`、`platform_hint`、`max_message_length`。
10. **可选装饰**（都不阻塞功能）：`gateway/display_config.py:121 _PLATFORM_DEFAULTS` 加 `"onebot": _TIER_LOW`（QQ 无消息编辑，与 `wecom` 同档，`:171`）；`hermes_cli/web_server.py:8346 _PLATFORM_OVERRIDES` 加描述与文档链接、`:8594 _PLATFORM_ORDER` 调顺序；`hermes_cli/status.py:491` 的 `platforms` 表加一行。
11. **测试**：契约测试照抄 `tests/gateway/test_plugin_platform_interface.py`（其伪 `register_platform` 在 `:58`）；完整样例见 `tests/gateway/test_line_plugin.py:278`。

> **Contract summary — 平台适配器**
> - 建 `plugins/platforms/<name>/{plugin.yaml,__init__.py,adapter.py}`；`plugin.yaml` 必须 `kind: platform`；**目录 basename 必须等于平台名**（`hermes_cli/plugins.py:4417`）。
> - 继承 `BasePlatformAdapter`（`gateway/platforms/base.py:2890`），实现**恰好四个**抽象方法：`connect(*, is_reconnect=False)`（`:3894`）、`disconnect()`（`:3914`）、`send(chat_id, content, reply_to=None, metadata=None)`（`:3919`）、`get_chat_info(chat_id)`（`:7146`）。`send_typing` 非必需。
> - 入站：`build_source(...)`（`:7047`）→ `MessageEvent`（`:2299`）→ `await self.handle_message(event)`；媒体先 `cache_*_from_bytes` 再放进 `media_urls`。
> - 出站：返回 `SendResult`（`:2465`）；`error_kind` 用 `classify_send_error`（`:2565`）填 `SEND_ERROR_KINDS`（`:2516`）中的值；瞬时错误置 `retryable` / `retry_after`。
> - **设 `splits_long_messages = True`**（`base.py:2959`），否则 cron 产出被 `MAX_PLATFORM_OUTPUT = 4000` 截断（`gateway/delivery.py:29`，判断在 `:503`）。
> - 导出 `register(ctx)` 调 `ctx.register_platform(...)`（`hermes_cli/plugins.py:2774`），至少带 `allowed_users_env`、`allow_all_env`、`cron_deliver_env_var`、`standalone_sender_fn`、`platform_hint`、`max_message_length`。
> - `check_fn` 无副作用；安装逻辑放 `ensure_deps_fn`。
> - **零核心文件改动**。不要加 `Platform` 枚举成员，不要改 `toolsets.py`。
> - 配置在 `platforms.<name>.{enabled,extra,channel_overrides,...}`；密钥在 `$HERMES_HOME/.env`。
> - 传输层范本：`simplex`（本机 WS）+ `wecom`（握手/关联/心跳）；钩子范本：`ntfy`。
> - 排障：`HERMES_PLUGINS_DEBUG=1`。

---

## 2. 定时任务（cron）

> 本节全部断言已于本轮直接对照 `cron/jobs.py`、`cron/scheduler.py`、`cron/blueprint_catalog.py`、`cron/executions.py`、`cron/notepad.py`、`cron/scheduler_provider.py`、`tools/cronjob_tools.py` 重新核实。

### 2.1 任务模型

存储：**单个 JSON 文件** `$HERMES_HOME/cron/jobs.json`（`cron/jobs.py:4-5`、`:85 JOBS_FILE = CRON_DIR / "jobs.json"`），原子写。**按 profile 隔离是刻意设计**（`cron/jobs.py:66-79`，安全边界 issue #4707）：profile `coder` 的任务在 `~/.hermes/profiles/coder/cron/jobs.json`，并以该 profile 的 `.env` / `config.yaml` / skills 执行。

**没有 dataclass** —— 任务就是 `create_job` 里的 dict 字面量。签名（`cron/jobs.py:1780`）：

```python
def create_job(
    prompt: Optional[str],
    schedule: str,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    origin: Optional[Dict[str, Any]] = None,
    skill: Optional[str] = None,
    skills: Optional[List[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    script: Optional[str] = None,
    context_from: Optional[Union[str, List[str]]] = None,
    enabled_toolsets: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    no_agent: bool = False,
    attach_to_session: Optional[bool] = None,
    monitor_script: Optional[str] = None,
    monitor_url: Optional[str] = None,
) -> Dict[str, Any]:
```

落盘的 job dict（`cron/jobs.py:1948-1991`，逐字核实）：

```python
    job = {
        "id": job_id,
        "name": name or label_source[:50].strip(),
        "prompt": prompt_text,
        "skills": normalized_skills,
        "skill": normalized_skills[0] if normalized_skills else None,
        "model": normalized_model,
        "provider": normalized_provider,
        "provider_snapshot": provider_snapshot,
        "model_snapshot": model_snapshot,
        "base_url": normalized_base_url,
        "script": normalized_script,
        "no_agent": normalized_no_agent,
        "monitor_script": normalized_monitor_script,
        "monitor_url": normalized_monitor_url,
        "monitor_state": None,
        "context_from": context_from,
        "schedule": parsed_schedule,
        "schedule_display": parsed_schedule.get("display", schedule),
        "repeat": {"times": repeat, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now,
        "next_run_at": next_run_at,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "failure_streak": 0,
        "deliver": deliver,
        "origin": origin,
        "enabled_toolsets": normalized_toolsets,
        "workdir": normalized_workdir,
    }
```

`attach_to_session` 仅在显式设置时写入（`:1993-1997`），以保持既有任务字节不变。

调度语法 —— `cron/jobs.py:694 parse_schedule(schedule) -> Dict[str, Any]`，返回 `{"kind": "once"|"interval"|"cron", ...}`：

- `"every 2h"` → interval（`:717-724`）
- `"0 9 * * *"` → cron，由 **croniter** 解析，**惰性导入**（`:44-62`，`_ensure_croniter()`）
- `"2026-06-01T09:00:00"` → 一次性
- `"30m"` / `"2h"` / `"1d"` → 从现在起一次性

执行模式由字段组合推导，无枚举：

| 模式 | 字段 | 行为 |
|---|---|---|
| LLM 任务（默认） | `prompt` 和/或 `skills` | 完整 agent 运行 |
| 纯脚本 | `no_agent=True` + `script` | stdout 原样投递，**不走 LLM** |
| 脚本作上下文 | `script`，`no_agent=False` | stdout 注入 prompt 后再跑 agent |
| 监控 | `monitor_script` XOR `monitor_url` | 先哈希比对，未变化则抑制 agent |

### 2.2 调用 LLM 并投递到聊天

Prompt 组装：`cron/scheduler.py:3768 _build_job_prompt(...)`。层次：`job["prompt"]` → `## Script Output` → 上游 `context_from` 产出 → notepad 段 → skills。

执行：`cron/scheduler.py:4585 run_job(...)`。

投递：`cron/scheduler.py:2510 _deliver_result(job, content, adapters=None, loop=None) -> Optional[str]`。目标解析在 `:2296 _resolve_delivery_targets(job)`，按逗号切分后逐项经 `_expand_routing_tokens`（`:2278`）与 `_resolve_single_delivery_target`，最后按 `(platform, chat_id, thread_id)` 去重（`:2317-2325`）。

`deliver` 取值文法：

| 值 | 含义 |
|---|---|
| `"local"` | 只存本地，不投递（`:2307`，且不算失败） |
| `"origin"` | 回到 `job["origin"]` 的平台/会话 |
| `"onebot"` | 该平台的 home channel |
| `"onebot:183287894"` | `<platform>:<chat_id>[:<thread_id>]` |
| `"all"` | 所有配了 home chat_id 的平台（`_expand_routing_tokens`） |
| `"origin,all"` | 逗号组合并集 |

产出以 `[SILENT]` 开头/结尾/整体时抑制投递但仍保存（`SILENT_MARKER` `cron/scheduler.py:513`，匹配规则 `:515-527`，与 webhook 通道共用 `gateway.response_filters.is_autonomous_silence_response`）。

投递内容默认加页眉页脚包裹；`cron.wrap_response: false` 可关（`cron/scheduler.py:2545-2553`）。

**插件平台是一等 cron 目标。** `_plugin_cron_env_var`（`cron/scheduler.py:1899-1915`）经 `platform_registry` 回落到你的 `cron_deliver_env_var`；`_is_known_delivery_platform`（`:1917-1928`）与 `_resolve_home_env_var`（`:1930-1941`）均消费它。因此 `deliver="onebot:183287894"` 无需改 `:459` 的硬编码 `_KNOWN_DELIVERY_PLATFORMS`（其内容为 telegram/discord/slack/whatsapp/signal/matrix/mattermost/homeassistant/dingtalk/feishu/wecom/wecom_callback/weixin/sms/email/webhook/bluebubbles/qqbot/yuanbao）。

### 2.3 Blueprint 是什么，什么时候用

`cron/blueprint_catalog.py:1-22`：

> A *blueprint* is a one-place definition of an automation that every surface renders natively:
> Dashboard / GUI app → a form; CLI / TUI / messenger → a pre-filled `/blueprint` slash command; Agent → a seed prompt; Docs catalog → a copy-paste command + a `hermes://` deep-link.
> ...`fill_blueprint` validates user-supplied values and turns a blueprint into a `cron.jobs.create_job` kwargs dict (so there is no second job engine).

结构：`BlueprintSlot`（`:61`：`name`、`type`、`label`、`default`、`options`、`optional`、`help`、`strict`）与 `AutomationBlueprint`（`:83`：`key`、`title`、`description`、`category`、`schedule_template`、`prompt_template`、`slots`、`deliver_default`、`skills`、`tags`）。

**不是数据文件** —— 是 Python 列表 `CATALOG`（`:120`），在树内共 **16** 条。

**结论：12 个迁移任务不要用 blueprint。** Blueprint 的存在意义是「让终端用户不用手写 cron 表达式，在 dashboard 上填表」。加 12 条意味着改核心树，且毫无收益。直接用 `create_job` / CLI / `cronjob` 工具。

### 2.4 执行、并发、留存

- **网关进程内 60 s ticker 线程 + 线程池**，不依赖 systemd timer 或系统 crontab。`InProcessCronScheduler.start()` — `cron/scheduler_provider.py:491`；`tick(...)` — `cron/scheduler.py:6730`；默认间隔 `TICKER_INTERVAL_SECONDS = 60`（`cron/jobs.py:100`）。
- 心跳文件 `$HERMES_HOME/cron/ticker_heartbeat` 与 `ticker_last_success`（`cron/jobs.py:91-97`），让 `hermes cron status` 能区分「进程活着但 ticker 线程死了」。
- 跨进程文件锁 `$HERMES_HOME/cron/.tick.lock`（`LOCK_EX|LOCK_NB`）。
- 执行前先推进 `next_run_at`，实现 at-most-once。
- `workdir` 任务进 `max_workers=1` 串行池，其余并行；上限 `HERMES_CRON_MAX_PARALLEL` env > `cron.max_parallel_jobs` > 无限。
- 三重重叠防护：进程内 `try_register_running_job`、持久化 fire claim `claim_job_for_fire`（`cron/jobs.py:2805`）、一次性 run claim `claim_dispatch`（`cron/jobs.py:2592`）。
- 执行台账：**SQLite** `$HERMES_HOME/cron/executions.db`（`cron/executions.py:32`）。表 schema 在 `:41-57`，状态枚举 `('claimed','running','completed','failed','unknown')`。留存 `MAX_TERMINAL_EXECUTIONS = 1000`（`:25`）。`PRAGMA synchronous=FULL`（`:39`）。模块 docstring `:1-5` 明确：**这是审计台账，不是重试队列**。
- 产出文档：`$HERMES_HOME/cron/output/<job_id>/<timestamp>.md`（`cron/jobs.py:5`）。
- 补跑：积压塌缩为**一次**触发，不会爆发。
- **没有失败退避** —— 失败任务照下一个排期跑。有的是失败连击提醒 `_failure_streak_nudge`（`cron/scheduler.py:152`），阈值 `cron.failure_nudge_threshold`。

`cron/lifecycle_guard.py`（801 行）阻止任务重启/停掉自己所在的网关，`check_gateway_lifecycle` 在 `create_job` 里调用。`cron/monitor.py`（212 行）做精确字节 SHA-256 变化抑制。

`cron/scheduler_provider.py:1-19` —— 调度器**仅在"触发"这一轴**可插拔：

> ⚠️ EXPERIMENTAL — this interface is validated by exactly ONE consumer (the built-in) until an external provider (Chronos, Phase 4) shakes it out. ...
> A CronScheduler decides *when* a due job fires. It does NOT decide what firing means: execution + delivery stay in cron.scheduler.run_job / _deliver_result, shared by all providers.

Provider 从 `plugins/cron_providers/` 发现（树内仅 `chronos` 一个），由 `cron.provider` 选择，空 = 内置。**保持 `cron.provider: ""`。**

`cron/notepad.py`（187 行）—— 按任务的 KV 便签，SQLite `$HERMES_HOME/cron/notepad.db`（`:34`），上限 `MAX_VALUE_BYTES = 16*1024`、`MAX_KEY_CHARS = 128`、`MAX_JOB_TOTAL_BYTES = 64*1024`（`:35-37`）。无模型工具，agent 经终端调 `hermes cron notepad <job_id> set <k> <v>`。**这是跨轮次游标/水位线的正确存放处，也是格兰 decay 状态的候选载体。**

### 2.5 模型可调用面与门控

只有一个工具：`cronjob`，注册于 `tools/cronjob_tools.py:1771`，toolset `"cronjob"`，`check_fn=check_cronjob_requirements`。动作：`create|list|update|pause|resume|remove|run`。

两道门：
1. 可用性 —— `check_cronjob_requirements()`（`tools/cronjob_tools.py:1746`）仅在 `HERMES_INTERACTIVE` / `HERMES_GATEWAY_SESSION` / `HERMES_EXEC_ASK` 下为真。
2. cron 上下文拒绝 —— `_resolve_cron_disabled_toolsets`（`cron/scheduler.py:358`）默认不给 cron 派生的 agent `cronjob` 工具，除非 `cron.allow_agent_scheduling: true`（默认 **False**）。`messaging`、`clarify`、`memory` 在 cron 上下文**永远**禁用。

`tools/cronjob_tools.py:1785-1790` 明确不从模型参数读 `model`/`provider`/`base_url`：agent 不得把无人值守的开销指向别的模型。

### 2.6 最小可用示例 —— 一个每日 LLM 任务投递到 QQ 群

CLI：

```bash
hermes cron create "0 9 * * *" \
  "读取最近 3 条格兰生活事件，以格兰的口吻写一条群播报，200 字以内。\
若今天没有值得说的事，只回复 [SILENT]。" \
  --name "qunjlu 每日播报" \
  --deliver "onebot:183287894" \
  --skill grantley-broadcast \
  --model "gemini-3.7-flash-tiered" --provider cornna
```

模型工具等价 payload：

```json
{"action":"create","schedule":"0 9 * * *","prompt":"…",
 "name":"qunjlu 每日播报","deliver":"onebot:183287894",
 "skills":["grantley-broadcast"],"enabled_toolsets":["web","file"]}
```

纯脚本 decay 任务（零 LLM 开销）：

```python
create_job(
    prompt=None, schedule="every 6h", name="persona.decay",
    script="grantley_decay.py",   # 解析于 ~/.hermes/scripts/
    no_agent=True, deliver="local",
)
```

12 个任务批量导入时，循环调 `cron.jobs.create_job` 即可 —— 不要手写 `jobs.json`。

> **Contract summary — cron**
> - 任务存 `$HERMES_HOME/cron/jobs.json`（**按 profile 隔离**）；只经 `cron.jobs.create_job`（`cron/jobs.py:1780`）、`hermes cron create`、或 `cronjob` 工具创建 —— **不要手改文件**（会绕过 `check_gateway_lifecycle` 与模式校验）。
> - 调度：`"0 9 * * *"` | `"every 2h"` | `"30m"` | ISO 时间戳。
> - 聊天投递靠 `deliver` 字段：`<platform>:<chat_id>[:<thread_id>]`，或裸平台名走 `<PLATFORM>_HOME_CHANNEL`。
> - 插件平台成为合法 `deliver=` 目标的条件：`PlatformEntry` 上同时设 `cron_deliver_env_var` **和** `standalone_sender_fn`。
> - **用原始 job，不用 blueprint**（blueprint 是终端用户表单目录，加条目要改核心树）。
> - 调度器跑在网关进程内 —— **网关必须常驻**。带 `workdir` 的任务串行，其余用 `cron.max_parallel_jobs` 限流（12 个日任务会在整点撞车）。
> - 跨轮次状态放 `cron/notepad.py`（16 KB/键，64 KB/任务），agent 经 `hermes cron notepad` 写。
> - prompt 里教模型在无内容时返回 `[SILENT]` 以抑制投递。
> - 无失败退避；靠 `cron.failure_nudge_threshold` 的连击提醒发现问题。

---

## 3. Skills

### 3.1 磁盘格式

一个 skill = **含 `SKILL.md` 的目录**，YAML frontmatter。约定子目录（`AGENTS.md:1027-1030`）：`scripts/`、`references/`、`templates/`。

frontmatter 字段（`AGENTS.md:942-952`）：`name`、`description`、`version`、`author`、`license`、`platforms`（OS 门控）、`prerequisites`、`metadata.hermes.tags`、`metadata.hermes.category`、`metadata.hermes.related_skills`、`metadata.hermes.config`。顶层 `tags:` / `category:` 由 loader 镜像。

实例：`skills/social-media/xurl/SKILL.md:1-15`。

硬性写作规范（`AGENTS.md:954-1034`，摘要）：`description` **≤ 60 字符**、一句话、句号结尾；正文引用工具须用 hermes 原生工具名加反引号（`grep`→`search_files`、`cat`→`read_file`、`sed`→`patch`）；`platforms:` 须与脚本实际 import 对账；章节顺序 `## When to Use / ## Prerequisites / ## How to Run / ## Quick Reference / ## Procedure / ## Pitfalls / ## Verification`；复杂 ~200 行、简单 ~100 行；测试放 `tests/skills/test_<skill>_skill.py`。

### 3.2 发现、加载、暴露给模型

运行期 skills 在 **`$HERMES_HOME/skills/`**（`tools/skills_tool.py:143-159`）。仓库 `skills/` 树是**播种源**：`setup-hermes.sh:399-415` 调 `tools/skills_sync.py`，启动时也会同步（`hermes_cli/main.py:2867`、`:3214`）。

扫描顺序（`tools/skills_tool.py:673-780`，先到先得）：受信任的项目目录 → `$HERMES_HOME/skills/` → `skills.external_dirs`。经 `skill_matches_platform`（`:253`）与 `skill_matches_environment`（`:263`）门控，再过 disabled 集合（`:623`）。

**暴露给模型的是系统提示里的紧凑索引，不是正文**：`agent/prompt_builder.py:1763 build_skills_system_prompt(...)`，渲染 `## Skills (mandatory)` + `<available_skills>` 的名称/描述行（`:2105-2130`）。双层缓存（进程内 LRU + 磁盘快照 `$HERMES_HOME/.skills_prompt_snapshot.json`，`:1534`）。模型再用 `skill_view(name=…)` 拉正文。

`optional-skills/` 随仓库发布但默认不激活，经 `OptionalSkillSource`（`tools/skills_hub.py:3273`）暴露，用 `hermes skills install official/<category>/<skill>` 安装。

配置（`hermes_cli/config_defaults.py:1963-1990`）：`skills.external_dirs`、`skills.project_discovery`、`skills.trusted_project_dirs`、`skills.template_vars`、`skills.inline_shell`、`skills.inline_shell_timeout`。单 skill 自身配置落 `skills.config.<key>`（`agent/skill_utils.py:1114-1137`）。

### 3.3 何时用 skill、何时用核心工具、何时用插件

`AGENTS.md:182-211`（原文）：

> ### The Footprint Ladder (new capability decision)
> Each rung adds more permanent surface than the one above. Choose the highest (least-footprint) rung that correctly solves the problem:
> 1. **Extend existing code** — the capability is a variation of something that already exists. Zero new surface.
> 2. **CLI command + skill** — manages config/state/infra expressible as shell commands. The agent runs `hermes <subcommand>` guided by a skill. Zero model-tool footprint. Default choice for subscriptions, scheduled tasks, service setup. Examples: `hermes webhook`, `hermes cron`, `hermes tools`.
> 3. **Service-gated tool (`check_fn`)** — needs structured params/returns AND only appears when a prerequisite is configured. Zero footprint otherwise. Examples: Home Assistant tools (gated on token), memory-provider tools.
> 4. **Plugin** — third-party/niche/user-specific capability that doesn't ship in core. Lives in `~/.hermes/plugins/` or a pip package, discovered at runtime.
> 5. **MCP server (in the catalog)** …
> 6. **New core tool** — only when the capability is fundamental, broadly useful to nearly every user, and unreachable via terminal + file (or an MCP server).

以及禁令（`AGENTS.md:1006-1009`）：

> - **Lazy-reading escape hatches on instructional tools.** No `offset`/`limit` pagination on tools that load content the agent must read fully (skills, prompts, playbooks). Models will read page 1 and skip the rest.

> **Contract summary — skills**
> - `<dir>/SKILL.md` + 可选 `scripts/` `references/` `templates/`；frontmatter 必带 `name`、`description`（**≤60 字符**）、`version`、`author`、`license`、`platforms`、`metadata.hermes.*`。
> - 安装到 `$HERMES_HOME/skills/<category>/<name>/`，或把路径加入 `skills.external_dirs`。
> - 只有名称+描述进系统提示；正文按需经 `skill_view` 加载。
> - 同名冲突按 项目 → 本地 → 外部 先到先得；在 disabled 集合中的直接丢弃。
> - 凡是「散文 + shell + 现有工具」能表达的能力，一律用 skill —— skill 的模型 schema 成本为零。

---

## 4. 插件

### 4.1 清单（manifest）

`plugin.yaml` 解析为 `PluginManifest` —— `hermes_cli/plugins.py:1031-1108`。字段：`name`、`version`、`description`、`author`、`requires_env`（字符串或富字典）、`provides_tools`、`provides_hooks`、`source`、`path`、**`kind`**（`standalone` | `backend` | `exclusive` | `platform`，语义见 `:1056-1058`；`_VALID_PLUGIN_KINDS` 另含 `model-provider`，`:620`）、`key`、`portable`、`skill_namespace`、`capabilities`，以及 v2 增量字段 `manifest_version`、`api_version`、`requires_plugins`、`python_dependencies`、`config_schema`、`license`、`homepage`、`tags`、`emits`、`listens`。未知键告警但可加载（`_KNOWN_MANIFEST_FIELDS` `:652-663`）。

最小实例（`plugins/disk-cleanup/plugin.yaml`）：

```yaml
name: disk-cleanup
version: 2.0.0
description: "Auto-track and clean up ephemeral files …"
author: "@LVT382009 (original), NousResearch (plugin port)"
hooks:
  - post_tool_call
  - on_session_end
```

### 4.2 入口与发现

`__init__.py` 导出 `def register(ctx) -> None`，在 `hermes_cli/plugins.py:4789-4795` 被调用；没有则记 `"no register() function"`。

发现顺序见 §1.8。**发现时机陷阱**（`AGENTS.md:794-798`）：`discover_plugins()` 只作为 `import model_tools` 的副作用运行；不经该路径的代码须显式调用（幂等）。

### 4.3 `PluginContext` 全表面

`hermes_cli/plugins.py:1388`。你会用到的注册方法：

| 方法 | 行 |
|---|---|
| `register_tool(name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False, description="", emoji="", override=False)` | `:1700` |
| `register_platform(...)` | `:2774` |
| `register_system_prompt_section(id, content, *, position="after_memory", max_chars=4000)` | `:3134` |
| `register_hook(hook_name, callback)` | `:3109` |
| `register_cli_command(...)` / `register_command(...)` | `:2061` / `:2101` |
| `register_auxiliary_task(key, *, display_name, description, defaults=None)` | `:2935` |
| `register_skill(...)` | `:3319` |
| `register_memory_provider` | `:2285` |
| `register_context_engine` | `:2205` |
| `register_image_gen_provider` / `_video_gen_provider` | `:2318` / `:2428` |
| `register_tts_provider` / `_transcription_provider` | `:2648` / `:2708` |
| `register_web_search_provider` / `_browser_provider` / `_secret_source` | `:2477` / `:2527` / `:2581` |
| `spawn_task(coro, *, name=None) -> asyncio.Task` | `:1637` |
| `emit(event, payload)` / `subscribe(event, callback)` | `:3213` / `:3265` |
| `get_config(key, default)` / `set_config(key, value)` | `:1422` / `:1450` |
| `state` → `PluginState` | `:1507` |
| `llm` | `:1565` |
| `call_mcp(...)` | `:1811` |
| `inject_message(...)` | `:1968` |
| `on_unload(callback)` | `:1623` |

生命周期钩子 —— `VALID_HOOKS`（`hermes_cli/plugins.py:156-215`）：`pre_tool_call`、`post_tool_call`、`transform_terminal_output`、`transform_tool_result`、**`transform_llm_output`**（`:163` 注明「Useful for vocabulary/personality transformation」）、`pre_llm_call`、`post_llm_call`、`on_stream_start` / `_delta` / `_end`、`on_interim_message`、`pre_verify`、`pre_api_request`、`post_api_request`、`api_request_error`、`transform_api_error_classification`、`on_session_start`、`on_session_end`、`on_session_finalize`、`on_session_reset` 等。

### 4.4 端到端样例：`plugins/disk-cleanup/`

四个文件（`plugin.yaml`、`__init__.py` 316 行、`disk_cleanup.py`、`README.md`）。入口原文（`plugins/disk-cleanup/__init__.py` 末尾）：

```python
def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "disk-cleanup",
        handler=_handle_slash,
        description="Track and clean up ephemeral Hermes session files.",
    )
```

其余是普通模块级 Python：带锁的 `Dict[str, Set[str]]` 按 task/session 归集（`:39-40`）、工具结果解析器、slash 处理器。

### 4.5 存储

两条正路：

1. **`plugins/plugin_storage.py`** —— 官方文件/SQLite 约定。docstring（`:1-30`）说明了原因：`<hermes home>/plugins/<name>/` 是**安装目录**，`hermes plugins remove/update` 会摧毁它。
   - `plugin_data_dir(name) -> Path`（`:51`）→ `<hermes home>/plugin-data/<name>/`，跨更新/卸载存活，**每次调用都重新解析 profile**（「Don't cache the result across profile switches」）。
   - `plugin_db(name, filename="data.db") -> sqlite3.Connection`（`:66`）→ WAL、`foreign_keys=ON`、`check_same_thread=False`。
   - 密钥刻意不在此约定内（`:15-17`），走 `agent.secret_scope` / `.env`。
2. **`ctx.state` → `PluginState`**（`hermes_cli/plugins.py:1315-1386`）：带配额的 JSON KV，含 `data_dir`、`path`、`quota_bytes`、`get`、`set`，文件+线程双锁。

并发助手：`plugins/plugin_utils.py` 的 `lazy_singleton`（`:42`）与 `SingletonSlot`（`:83`），纯 stdlib，解决 `:3-27` 描述的懒汉单例竞态。

### 4.6 配置与启用

插件设置在 `plugins.entries.<plugin_id>.settings`，按 manifest 的 `config_schema` 校验（仅告警，`hermes_cli/plugins.py:814`）。启停用 `plugins.enabled` / `plugins.disabled`，键为 `manifest.key`（路径推导：`plugins/image_gen/openai` → `image_gen/openai`，`:1061-1064`）。覆盖内置工具需 `plugins.entries.<id>.allow_tool_override: true`（`:1723-1735`）。

**硬性规则**（`AGENTS.md:876-881`）：

> **Rule (Teknium, May 2026):** plugins MUST NOT modify core files (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`, etc.). If a plugin needs a capability the framework doesn't expose, expand the generic plugin surface (new hook, new ctx method) — never hardcode plugin-specific logic into core.

以及落地位置的政策（`AGENTS.md:894-912`）：第三方产品集成插件**不进本仓库树**，应作为独立插件仓库安装到 `~/.hermes/plugins/`。**格兰系统与 OneBot 适配器都应遵循这一条。**

> **Contract summary — 插件**
> - `<dir>/plugin.yaml`（非 `standalone` 需写 `kind:`）+ `<dir>/__init__.py` 导出 `register(ctx)`。
> - 安装到 `$HERMES_HOME/plugins/<name>/`（需 `plugins.enabled`）或树内 `plugins/<name>/`（bundled；`kind: platform` 的 bundled 自动加载）。
> - 工具 `ctx.register_tool`；钩子 `ctx.register_hook(<VALID_HOOKS 之一>, cb)`；CLI `ctx.register_cli_command`；斜杠 `ctx.register_command`；后台任务 `ctx.spawn_task(coro)`。
> - 持久状态用 `plugin_data_dir(name)` / `plugin_db(name)`，**绝不用安装目录**；小 KV 用 `ctx.state`。
> - 配置 `plugins.entries.<key>.settings`，经 `ctx.get_config` 读；密钥留在 `.env`。
> - 永不修改核心文件；表面不够就扩通用钩子/ctx 方法。

---

## 5. 工具

### 5.1 声明与注册

自动发现：任何 `tools/*.py` 中含**顶层** `registry.register(...)` 调用的模块在启动时被导入 —— 用 AST 校验而非文本匹配（`tools/registry.py:76-108`），判定结果按 `(mtime_ns, size)` 记忆化到 `$HERMES_HOME/cache/tool_discovery_cache.json`（`discover_builtin_tools`，`:110-166`）。

`ToolRegistry.register` —— `tools/registry.py:737-752`：

```python
def register(
    self, name: str, toolset: str, schema: dict, handler: Callable,
    check_fn: Callable = None, requires_env: list = None,
    is_async: bool = False, description: str = "", emoji: str = "",
    max_result_size_chars: int | float | None = None,
    dynamic_schema_overrides: Callable = None,
    override: bool = False, scope: Optional[str] = None,
):
```

`ToolEntry` 基于 `__slots__`（`:204-233`）。`dynamic_schema_overrides` 是零参可调用对象，在 `get_definitions()` 时浅合并，用于依赖运行期配置的描述文本（`:224-232`）。

最小范本 —— `tools/focus_pane_tool.py`（64 行）：一个返回 JSON 字符串的普通函数 + 模块级 `*_SCHEMA` 字典（OpenAI function-schema 形状）+：

```python
registry.register(
    name="focus_pane",
    toolset="desktop_ui",
    schema=FOCUS_PANE_SCHEMA,
    handler=lambda args, **kw: focus_pane_tool(pane=args.get("pane", "")),
    emoji="🪟",
)
```

**所有 handler 必须返回 JSON 字符串**（`AGENTS.md:585`）。错误走 `tool_error()`；分发边界把错误体截到 `_MAX_TOOL_ERROR_CHARS = 2048`（`tools/registry.py:33`，`_bound_json_error_result` `:51`）。

### 5.2 Toolset 是独立且必需的一步（仅限核心路线）

`AGENTS.md:577-581`：

> **2. Add to `toolsets.py`** — either `_HERMES_CORE_TOOLS` (all platforms) or a new toolset. **This step is required:** auto-discovery imports the tool and registers its schema, but the tool is only *exposed to an agent* if its name appears in a toolset.

`_HERMES_CORE_TOOLS` 在 `toolsets.py:31-101`；`TOOLSETS` 字典 `:107`；平台包 `"hermes-telegram"` `:516`；并集 `"hermes-gateway"` `:646-650`。按平台启停用 `hermes tools` 或 `config.yaml` 的 `tools.<platform>.enabled` / `.disabled`（`AGENTS.md:1051-1053`）。

**插件路线不需要这步** —— `toolsets.py:833-848` 为已注册平台自动生成 `hermes-<name>`，内容为 `_HERMES_CORE_TOOLS` ∪ 该插件注册到同名 toolset 的工具。

### 5.3 门控机制（`check_fn`）

`check_fn` 是零参谓词；返回 False 时该工具**完全不出现在 schema 里**，token 成本为零。范本 `tools/homeassistant_tool.py:345-347`：

```python
def _check_ha_available() -> bool:
    """Tool is only available when HASS_TOKEN is set."""
    return bool(get_secret("HASS_TOKEN"))
```

四个 HA 注册均传入（`:480-514`）。结果按 `(fn, profile scope)` 做 TTL 缓存（`_check_fn_cached` `tools/registry.py:324`），并有 last-good 宽限窗（`:369-380`）避免探针抖动导致工具中途消失；`invalidate_check_fn_cache()` 在 `:394`。

### 5.4 为什么不加核心工具，以及官方替代

`AGENTS.md:23-27`：

> - **The core is a narrow waist; capability lives at the edges.** Every model tool we add is sent on every API call, so the bar for a new *core* tool is high. Most new capability should arrive as a CLI command + skill, a service-gated tool, or a plugin — not as core surface.

`AGENTS.md:549-556`：

> Before adding any tool, settle the footprint question first (see "The Footprint Ladder"): most capabilities should NOT be core tools. For custom or local-only tools, do **not** edit Hermes core. Use the plugin route instead: create `~/.hermes/plugins/<name>/plugin.yaml` and `~/.hermes/plugins/<name>/__init__.py`, then register tools with `ctx.register_tool(...)`. Plugin toolsets are discovered automatically and can be enabled or disabled without touching `tools/` or `toolsets.py`.

**qzone 工具族（publish / reply / friends）的正解：插件内 `ctx.register_tool`，并用 `check_fn` 绑定 QQ 凭证。** 这同时踩中梯子第 3 与第 4 级 —— 凭证缺失时 schema 成本为零。

> **Contract summary — 工具**
> - 一个工具 = 返回 **JSON 字符串**的函数 + schema 字典 + `registry.register(...)`。
> - 核心路线：`tools/<name>.py` + 在 `toolsets.py` 里登记名字。**两步缺一不可**。
> - 插件路线（本次采用）：`register(ctx)` 里 `ctx.register_tool(name, toolset, schema, handler, check_fn=…, emoji=…)` —— 零核心改动，按 profile 作用域注册。
> - 非普适工具一律加 `check_fn`，让前置条件缺失时 schema 消失。
> - 覆盖内置工具需 `override=True` **且** `plugins.entries.<id>.allow_tool_override: true`。
> - 状态路径用 `get_hermes_home()`，schema 描述里用 `display_hermes_home()` —— 绝不写死 `Path.home()/".hermes"`。

---

## 6. Persona / 角色系统

> 本节所有机制均于本轮直接核实。

### 6.1 原生已有的五种机制

| 机制 | 位置 | 提示词层级 | 可按频道区分？ | 缓存安全？ |
|---|---|---|---|---|
| **`SOUL.md`** | `$HERMES_HOME/SOUL.md`，加载器 `agent/prompt_builder.py:2254` | **stable，身份槽位 #1** | 否（按 profile） | 是（每会话冻结） |
| **Personalities** | `hermes_cli/personality.py:43` + `agent.personalities` | ephemeral 叠加 | 否 | 是 |
| **`channel_overrides.system_prompt`** | `gateway/config.py:589` | ephemeral | **是** | 是 |
| **`channel_prompts`**（旧） | `gateway/platforms/base.py:2789` | ephemeral | **是** | 是 |
| **Profile 路由** | `gateway/profile_routing.py:1` | 整个独立 `HERMES_HOME` | **是** | 是 |
| Skin / 主题 | `hermes_cli/skin_engine.py` | **无 —— 纯 CLI 视觉** | — | 不适用 |

**`SOUL.md` 就是角色的基础系统提示。** `agent/system_prompt.py:381-396` 优先加载它，失败才回落 `DEFAULT_AGENT_IDENTITY`。首次运行由 `_ensure_default_soul_md` 播种（`hermes_cli/config.py:850-868`），模板在 `hermes_cli/default_soul.py:3`。被它替换的旧模板原文（`hermes_cli/default_soul.py:26-29`）说得最直白：

> This file defines the agent's personality and tone. The agent will embody whatever you write here.

README 直接称其为 "**SOUL.md** — persona file"（`README.md:204`）。

**`ChannelOverride` 是原生的按频道人格。** `gateway/config.py:588-599`：

```python
@dataclass
class ChannelOverride:
    """
    Per-channel override for model, provider, and system prompt.

    Used in config under platforms.<name>.channel_overrides[channel_id].
    Enables different channels (e.g. Discord #daily vs #dev) to use different
    models and personas without running separate gateway instances.
    """
    model: Optional[str] = None
    provider: Optional[str] = None
    system_prompt: Optional[str] = None
```

查找 `_get_channel_override`（`gateway/run.py:3645`），键序 chat_id → thread_id → parent_id。经 `_get_system_prompt_for_channel`（`gateway/run.py:9151-9177`）解析，并在 `gateway/run.py:5207-5221` 与 `event.channel_prompt`、平台上下文拼接成 `combined_ephemeral`。

**Personalities**：`hermes_cli/personality.py:43 BUILTIN_PERSONALITIES: Dict[str, str]` 共 29 条内置（helpful/concise/technical/creative/teacher/kawaii/catgirl/pirate/shakespeare/surfer/noir/uwu/…）。用户自定义合并自 `agent.personalities`（`:112-119`，配置默认 `hermes_cli/config_defaults.py:2350`），选择键 `personality`（`:1198`）。**这是短人格叠加，不适合承载格兰这种长期角色**，但可作为 Telegram 侧 `lycaon` 的轻量实现。

**Profile 才是长期角色的正确形态。** `AGENTS.md:1237-1243`：profile 是完全隔离的 `HERMES_HOME` —— 各自的 `SOUL.md`、`config.yaml`、`skills/`、`plugins/`、`cron/`、`memories/`、`state.db`。`gateway/profile_routing.py:1-4`：

> Allows a single Hermes instance to route specific Discord guilds/channels/threads to different profiles — each with their own model, tools, memory, and persona.

匹配优先级 thread(14) → channel(6) → guild(2) → 默认（`:5-9`）；`ProfileRoute` dataclass 在 `:54-64`（`name`、`platform`、`profile`、`guild_id`、`chat_id`、`thread_id`、`enabled`）。

"Bot Mode"（`tools/bot_mode_probe.py:1-28`）是已发布的多角色先例：每个 desktop bot 就是一个带独立 `SOUL.md` 的 profile。

### 6.2 长期状态与 decay

内置记忆 = `MEMORY.md` + `USER.md`，散文按裸 `§` 分隔（`agent/learning_graph.py:196-201`）。注入是**冻结快照**，这是刻意设计 —— `tools/memory_tool.py:686-688`：

> This returns the state captured at load_from_disk() time, NOT the live state. Mid-session writes do not affect this. This keeps the system prompt stable across all turns, preserving the prefix cache.

**decay：树内只有一份实现。** `plugins/memory/holographic/`：
- 配置项 `temporal_decay_half_life: 0`（天，0=关闭）—— `plugins/memory/holographic/__init__.py:15`、`:170-175`；`retrieval.py:28`、`:35`
- 公式 `retrieval.py:61` / `:645`：`decay = 0.5^(age_days / half_life)`
- 应用点 `retrieval.py:109-111`，与每条 fact 的 `trust_score` 相乘
- 实现 `retrieval.py:644-666 _temporal_decay(timestamp_str)`，禁用或缺时间戳时返回 `1.0`

**内置 `MEMORY.md` 存储没有 decay、没有 ttl、没有时间戳。**

**衰减内容允许落在哪里。** `MemoryProvider.prefetch(query)` 的产出进入**用户消息的 API 副本**，不进系统提示：`agent/turn_context.py:1284` 调 `agent._memory_manager.prefetch_all(_query)` → `agent/memory_manager.py::build_memory_context_block` 包成 `<memory-context>…</memory-context>` → `agent/turn_context.py:54 compose_user_api_content(...)` 作为 `api_content` sidecar 挂在消息上（`:66`、`:89 substitute_api_content`）。这是唯一缓存安全的动态注入通道。

`MemoryProvider` ABC —— `agent/memory_provider.py:104`。抽象方法只有四个：`name`（`:108`）、`is_available`（`:114`）、`initialize`（`:122`）、`get_tool_schemas`（`:219`）。可选钩子含 `system_prompt_block`（`:157`）、**`prefetch`（`:166`）**、`queue_prefetch`（`:180`）、`sync_turn`（`:201`）、`handle_tool_call`（`:229`）、`shutdown`（`:237`）、`on_turn_start`（`:242`）、`on_session_end`（`:251`）、`get_config_schema`（`:330`）、`save_config`（`:352`）、`backup_paths`（`:388`）。

**插件系统提示段** —— `hermes_cli/plugins.py:3134`：

```python
    def register_system_prompt_section(
        self, id: str, content: Union[str, Callable[[Mapping[str, Any]], str]],
        *, position: str = "after_memory",
        max_chars: int = DEFAULT_SYSTEM_PROMPT_SECTION_MAX_CHARS,
    ) -> PluginRegistration:
        """Register bounded context that is frozen into each new session prompt."""
```

约束：`SYSTEM_PROMPT_SECTION_POSITIONS = frozenset({"after_memory"})`、`MAX_SYSTEM_PROMPT_SECTION_CHARS = 4_000`、`MAX_SYSTEM_PROMPT_SECTIONS = 32`、`MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS = 8_000`（`hermes_cli/plugins.py:495-499`）。渲染一次即冻结（`agent/system_prompt.py:188-212`）。

> ⚠️ **关键限制**：传给回调的 session-info 映射只有 `{session_id, model, provider, platform, profile_name, cwd}`（`agent/system_prompt.py:178-185`）—— **没有 `chat_id`**。所以插件段只能按平台/profile 变化，**不能按频道变化**。

### 6.3 缓存规则禁止什么

依据 §0.3 的 `AGENTS.md` 条款，以及执行现场（`build_system_prompt` 缓存在 `agent._cached_system_prompt`，`agent/system_prompt.py:865-880`；仅压缩后失效 `:891`；连日期行都做成按天粒度以保字节稳定 `:814-822`）：

**禁止：**
1. 会话中途往 `SOUL.md` 追加生活事件再 `invalidate_system_prompt()` —— `SOUL.md` 在 **stable** 层，会击穿整个前缀缓存。
2. 把连续衰减权重算进系统提示 —— decay 是连续量，等于每轮必然 cache miss。
3. 任何日内变化的值进系统提示（如"距事件 N 已过 X 天"）。
4. 中途插入合成 user 消息夹带事件（`AGENTS.md:89-90`）。

**允许（这就是应当采用的设计）：**
- **角色基础提示** → `SOUL.md`，每 profile 一份。
- **按频道人格** → `platforms.<name>.channel_overrides.<chat_id>.system_prompt`。它走 `ephemeral_system_prompt` 通道，被显式排除在缓存提示之外 —— `agent/system_prompt.py:740-741`：*"ephemeral_system_prompt is NOT included here. It's injected at API-call time only so it stays out of the cached/stored system prompt."* 拼接点在 `agent/conversation_loop.py:1435-1436`、`:2237-2238`、`agent/chat_completion_helpers.py:2927-2928`。**零新代码。**
- **带衰减的生活事件** → 自建 `MemoryProvider`，由 `prefetch()` 返回衰减后 top-N，落进用户消息 sidecar。
- **会话内冻结的角色事实** → `ctx.register_system_prompt_section`。

一句话规则：**会话内会变的东西放用户消息 sidecar 或 `ephemeral_system_prompt`；会话内冻结的才可以进系统提示。**

> **Contract summary — persona**
> - 一个角色 = 一个 **profile**（`$HERMES_HOME/profiles/<name>/`），自带 `SOUL.md`、memory、skills、cron。格兰 → profile `grantley`；lycaon → profile `lycaon` 或轻量 `personality`。
> - 频道绑角色：`gateway.profile_routes`（`gateway/profile_routing.py:54`）做完全隔离，或 `platforms.<p>.channel_overrides.<chat_id>.system_prompt` 做共享 profile 上的人格覆盖。
> - **绝不在会话中途改 `SOUL.md` 或任何系统提示层。**
> - 生活事件 + decay 走 `MemoryProvider.prefetch()` → 用户消息 `api_content` sidecar；半衰期公式照抄 `plugins/memory/holographic/retrieval.py:645-666`。
> - 会话内冻结的角色事实可用 `ctx.register_system_prompt_section`（≤4 000 字符，合计 ≤8 000，锚点仅 `after_memory`）—— 但它**看不到 `chat_id`**。
> - 演化在带外进行：cron 任务追加/衰减事件库，变更在**下一个**会话生效，绝不在会话中途。

---

## 7. 配置与 Provider

### 7.1 配置在哪

- `$HERMES_HOME/config.yaml` —— 全部行为设置。默认值 `hermes_cli/config_defaults.py:7 DEFAULT_CONFIG`。带注释样例 `cli-config.yaml.example`（100 KB）。
- `$HERMES_HOME/.env` —— **仅密钥**（chmod 600）。元数据注册表 `OPTIONAL_ENV_VARS` 同文件。
- `$HERMES_HOME/auth.json` —— OAuth 凭证。

硬性规则（`AGENTS.md:100-107`）：

> - **New `HERMES_*` env vars for non-secret config.** `.env` is for secrets only (API keys, tokens, passwords). All behavioral settings — timeouts, thresholds, feature flags, display prefs — go in `config.yaml`. ... Reject PRs that tell users to "set X in your .env" unless X is a credential.

三个配置加载器（`AGENTS.md:664-674`）：

| Loader | 使用方 | 位置 |
|---|---|---|
| `load_cli_config()` | CLI 模式 | `cli.py` |
| `load_config()` | `hermes tools`、`hermes setup`、多数子命令 | `hermes_cli/config.py:3313` |
| 直接读 YAML | 网关运行时 | `gateway/run.py` + `gateway/config.py` |

顶层小节（`AGENTS.md:631-636`）：`model`、`agent`、`terminal`、`compression`、`display`、`stt`、`tts`、`memory`、`security`、`delegation`、`smart_model_routing`、`checkpoints`、`auxiliary`、`curator`、`skills`、`gateway`、`logging`、`cron`、`profiles`、`plugins`、`honcho`。

加键：追加到 `DEFAULT_CONFIG`；**只有**需要迁移（改名/改结构）才 bump `_config_version`，新增键由深合并处理（`AGENTS.md:623-630`）。

### 7.2 Provider 与模型别名

`ProviderProfile` —— `providers/base.py:39`。身份：`name`、`api_mode="chat_completions"`、`aliases`。鉴权/端点：`env_vars`、`base_url`、`models_url`、`auth_type`、`supports_health_check`。能力：`supports_vision`、`supports_vision_tool_messages`、`supports_prompt_cache_key`。目录：`fallback_models`、`hostname`。怪癖：`default_headers`、`fixed_temperature`、`default_max_tokens`、`default_aux_model`。可覆写钩子：`resolve_aux_model`（`:104`）、`build_api_kwargs_extras`、`fetch_models`。

每个 provider 是 `plugins/model-providers/<name>/` 下的插件，`__init__.py` 在导入时调 `register_provider(ProviderProfile(...))`。发现是**惰性且独立**于 `PluginManager` 的（`providers/__init__.py::_discover_providers()`，首次 `get_provider_profile()` / `list_providers()` 时扫描）。扫描顺序（`AGENTS.md:900-910`）：树内 `plugins/model-providers/<name>/` → `$HERMES_HOME/plugins/model-providers/<name>/` → 旧路径 `providers/<name>.py`。**同名用户插件覆盖内置**（last-writer-wins）。

**对接 `https://api.cornna.xyz/antigravity/` 这类 OpenAI 兼容端点，直接用内置 `custom` provider** —— `plugins/model-providers/custom/__init__.py`：`aliases=("ollama","local","vllm","llamacpp","llama.cpp","llama-cpp")`、`env_vars=()`、`base_url=""`（用户配）、`default_max_tokens=65536`。它已处理 GLM/ARK/vLLM/Ollama 的 `reasoning_effort` / `think` 怪癖矩阵（`build_api_kwargs_extras`，`:29-84`）。**只有当怪癖不同才需要自写 profile。**

```yaml
model:
  default: "gemini-3.7-flash-tiered"
  provider: "custom"
  base_url: "https://api.cornna.xyz/antigravity/"
  # api_key 放 .env
```

**模型别名** —— `cli-config.yaml.example:1663-1683`：

> Map short aliases to exact (model, provider, base_url) tuples. Used by `/model` tab completion and `resolve_alias()`. Aliases are checked BEFORE the models.dev catalog, so they can route to endpoints not in the catalog (e.g. Ollama Cloud, local servers).

```yaml
model_aliases:
  opus:
    model: claude-opus-4-6-thinking
    provider: custom
    base_url: "https://api.cornna.xyz/antigravity/"
  sonnet:
    model: claude-sonnet-4-6
    provider: custom
    base_url: "https://api.cornna.xyz/antigravity/"
```

**辅助（副 LLM）路由**：`auxiliary.<task>` 块可为 curator / vision / embedding / title generation / session_search 等各自钉 provider/model/base_url/max_tokens，解析顺序见 `agent/auxiliary_client.py::_resolve_auto`（`AGENTS.md:638-642`）。插件可用 `ctx.register_auxiliary_task`（`hermes_cli/plugins.py:2935`）自建任务，网关启动时桥接到 `AUXILIARY_<KEY_UPPER>_*` 环境变量。

### 7.3 密钥与优先级

`get_secret(name, default=None)` —— `agent/secret_scope.py:132`，docstring `:135-153`：

1. 真正全局的变量始终读 `os.environ`。
2. 装了 secret scope（多路复用轮次）时以 scope 为准；multiplex **关闭**时 scope 未命中会回落 `os.environ`（这样 systemd `Environment=`、`op run`、shell export 仍然有效）。
3. 未装 scope：multiplex 关 → 读 `os.environ`；multiplex 开 → **fail closed**，抛 `UnscopedSecretError`。

有效优先级：**CLI 参数 > 环境变量 > `config.yaml` > `DEFAULT_CONFIG`**。`cli-config.yaml.example:4-5` 明述：*"only documented secret environment variables in .env take precedence over their corresponding settings."* 平台 YAML 桥接同理 —— 所有 `apply_yaml_config_fn` 的写入都用 `not os.getenv(...)` 守卫（`gateway/platform_registry.py:174-178`）。

> **Contract summary — 配置与 provider**
> - 行为设置 → `$HERMES_HOME/config.yaml`（默认值加到 `hermes_cli/config_defaults.py:7`）；凭证 → `$HERMES_HOME/.env`（在 `OPTIONAL_ENV_VARS` 登记）。
> - OpenAI 兼容端点：`model.provider: "custom"` + `model.base_url` + `.env` 里的 key。**无需新建 provider 插件。**
> - 短名：`model_aliases.<alias>.{model,provider,base_url}` —— 早于 models.dev 目录解析。
> - 真要新 profile：`$HERMES_HOME/plugins/model-providers/<name>/{plugin.yaml (kind: model-provider), __init__.py}` 调 `register_provider(ProviderProfile(...))`。
> - 读密钥一律 `agent.secret_scope.get_secret`，不要裸 `os.getenv`，否则 profile 隔离失效。
> - 优先级：CLI > env > config.yaml > 默认。

---

## 8. 部署面 —— 针对 2 vCPU / 1.9 GB RAM / 5.8 GB 可用磁盘的 Debian 主机

> 本节全部数字于本轮直接核实。

### 8.1 Python 版本与依赖足迹

`pyproject.toml:15`：`requires-python = ">=3.11,<3.14"`。`.python-version` → `3.11`。目标机自带 Python 3.11.2，**正好落在支持区间**。上界是有意义的（`pyproject.toml:8-14`）：3.14 上 `pydantic-core` 无 wheel，会退化成 maturin/Rust 源码构建 —— 在这台机器上必然失败。

**基础依赖：声明 32 条，Linux 上实装 28 条**（`pyproject.toml:19-164`；4 条是 Windows-only：`tzdata`、`pywinpty`、`pywin32`、`concurrent-log-handler`）。全部精确钉版 `==X.Y.Z`（供应链政策 `:20-39`）。

主要条目：`openai`、`certifi`、`python-dotenv`、`fire`、`httpx[socks]`、`rich`、`tenacity`、`pyyaml`、`ruamel.yaml`、`requests`、`jinja2`、`pydantic`、`prompt_toolkit`、**`croniter`**、`packaging`、`Markdown`、`PyJWT[crypto]`、`urllib3`、`cryptography`、`psutil`、**`websockets==15.0.1`**、`pathspec`、`fastapi`、`uvicorn[standard]`、`python-multipart`、`ptyprocess`、`Pillow==12.3.0`、`nemo-relay`。

**经 grep 确认，基础依赖与全部 extras 中均不存在**：`torch`、`transformers`、`tensorflow`、`playwright`（Python 包）、`camoufox`、`opencv`、`pandas`。（`pyproject.toml` 里唯一的 "torch" 出现在 `:184` 的一句注释中。）`numpy` **只**出现在 `[voice]`（`:200`）与 `[wake]`（`:215`）两个 extras。ffmpeg 根本不是 Python 依赖。

**关键结论：OneBot 适配器需要的 `websockets` 与 `httpx[socks]` 都已在基础依赖里 —— 新适配器零新增依赖。** `croniter` 也在基础依赖里，12 个 cron 任务同样零新增。

**44 个 extras**（`pyproject.toml:166-366`，经计数核实）。磁盘炸弹：

| Extra | 行 | 内容 | 风险 |
|---|---|---|---|
| `voice` | `:196-201` | `faster-whisper`、`sounddevice`、`numpy` | 传递拉入 `ctranslate2` + `onnxruntime`，**数百 MB ~ 1 GB** |
| `wake` | `:208-221` | `openwakeword`、`onnxruntime`、`sherpa-onnx`、`sentencepiece`、`pvporcupine`、`sounddevice`、`numpy` | 同上，更重 |
| `matrix` | `:188` | `mautrix[encryption]` → `python-olm` | 需 `libolm-dev` + C 工具链；已被刻意排除出 `[all]` |
| `dev` | `:184` | pytest / ruff / ty / setuptools | 生产不需要 |

**免费的空别名**（内容已进核心，纯向后兼容）：`cron = []`（`:186`）、`vision = []`（`:235`）、`pty = []`（`:240`）、`nemo-relay = []`（`:259`）。

`all` 只含 9 项（`:335-366`）：`cron`、`pty`、`mcp`、`homeassistant`、`sms`、`acp`、`google`、`web`、`youtube`。其政策注释（`:336-346`）说明：能被 `tools/lazy_deps.py` 惰性安装的一律不放进 `[all]`。

一切可选件在首次使用时经 `tools/lazy_deps.py` 解析（约 42 个注册键）。

### 8.2 安装方式

`setup.py` 是**构建拦截器**，不是安装器 —— `setup.py:34-36`：

> Building wheels or sdists for hermes-agent is not supported.
> Hermes is distributed via the shell installer, Docker image, or Nix.

**没有 PyPI 包。** 可选方式：`scripts/install.sh`（真正的 curl 安装器）、`setup-hermes.sh`（开发克隆）、Docker、Nix（`flake.nix`）、Termux pip。

`setup-hermes.sh` **无任何命令行开关**，默认执行 `uv sync --extra all --locked`（`:254`）。它不装 Node/Electron/浏览器。

`scripts/install.sh` **默认会装** Node 26 + Playwright Chromium + Browser Use CLI。相关开关：`--skip-browser` / `--no-playwright`（`:106`）、`--skip-computer-use`（`:110`）、`--no-skills`（`:114`）、`--non-interactive`（`:144`）、`--include-desktop`（**默认关**，`:146`）、`--hermes-home PATH`（`:155`）、`--ensure DEPS`（可选 `node, browser, ripgrep, ffmpeg`，`:201`）。

### 8.3 Docker —— 本次不要用

`Dockerfile`（457 行）：4 个 `FROM` 阶段（`:5` `debian:13.4` 构建 SQLite、`:43` uv、`:51` `node:26-bookworm-slim`、`:52` `debian:13.4` 运行时）。**只有一个运行时镜像，没有 slim/minimal target，也没有第二个 Dockerfile。**

体积驱动因素：`npx playwright install --with-deps chromium --only-shell`（`:201`）、含 `ffmpeg gcc g++ make cmake python3-dev libffi-dev libolm-dev` 的 apt 层、从源码编译 SQLite 3.53.4（`:5-10`）、Node 26 + npm workspaces、以及 `uv sync --extra all --extra messaging --extra otlp --extra anthropic --extra bedrock --extra azure-identity --extra hindsight --extra matrix`。

**估算 4–6 GB**（UNVERIFIED —— 未实际构建，仅由层内容推断）。**5.8 GB 可用磁盘装不下且无余量运行。**

端口：**Dockerfile 中没有任何 `EXPOSE` 指令**；`docker-compose.yml` 用 `network_mode: host`（`:35`、`:67`）。卷：`VOLUME ["/opt/data"]`（`Dockerfile:422`），compose 中绑定 `~/.hermes:/opt/data`（`:37`、`:71`），镜像内 `ENV HERMES_HOME=/opt/data`（`Dockerfile:378`）。

### 8.4 端口

| 服务 | 默认端口 | 定义位置 | 改法 |
|---|---|---|---|
| **Dashboard / Web UI** | **9119** | `hermes_cli/subcommands/dashboard.py:27` | `--port` / `--host` |
| Gateway OpenAI 兼容 API | 8642 | `hermes_cli/web_server.py:3080` | `platforms.api_server.port`（需 `API_SERVER_KEY`） |
| Webhook 接收器 | 8644 | `hermes_cli/web_server.py:3080` | `platforms.webhook.port` |
| msgraph_webhook | 8646 | 同上 | `platforms.msgraph_webhook.port` |
| feishu webhook | 8765 | 同上 | `webhook_port` |
| wecom_callback / bluebubbles | 8645 | 同上 | `port` / `webhook_port` |
| sms | 8080 | 同上 | `webhook_port` |
| whatsapp_cloud | 8090 | 同上 | `webhook_port` |
| line | 8646 | 同上 | `port` |
| TUI bridge / MCP server | **无端口** | — | stdio JSON-RPC |

会绑定端口的平台是一个封闭集合 —— `gateway/config.py:429 PORT_BINDING_PLATFORM_VALUES = frozenset({webhook, api_server, msgraph_webhook, feishu, wecom_callback, bluebubbles, sms, whatsapp_cloud, line})`。

> **对本次部署的意义：**
> - **OneBot 适配器不在这个集合里** —— 它是**向外拨号**连 `ws://127.0.0.1:3001`，**不监听任何端口**，与 NapCat 零端口冲突。
> - 默认配置下**唯一会监听的就是 dashboard 的 9119**，且只在你显式跑 `hermes dashboard` 时。
> - 对照 `00-PLAN.md:0` 记录的占用端口（22, 80, 443, 3001, 6005, 6099, 8000, 8100, 9222, 18080, 18181, 18443, 19080, 1443, 10443）：**9119 空闲**，可直接用。

### 8.5 磁盘上的运行期状态

`HERMES_HOME` 解析 —— `hermes_constants.py:114-139`：**context-local override → `HERMES_HOME` 环境变量 → 平台默认**；POSIX 默认 `Path.home()/".hermes"`。

其下内容：`state.db`（主 SQLite，默认 WAL）、`cache/`（含 `cache/images`、`cache/web`、`cache/tool_discovery_cache.json`）、`logs/`、`sessions/`、`plugins/` + `plugin-data/`、`skills/`、`memories/`、`cron/`（`jobs.json`、`executions.db`、`notepad.db`、`output/`）、`bin/`、`node/`（若安装，约 150–200 MB）、`checkpoints/`、`channel_directory.json`、`SOUL.md`、`config.yaml`、`.env`、`auth.json`。多 profile 时 `profiles/<name>/` 下各有一整套。

增长排序：`sessions/` > `state.db` + WAL > `cache/` > `node/` > `logs/` > `checkpoints/`。`gateway/agent_cache_pressure.py:5-6` 直言单个工具密集会话的 transcript 可达「tens of MB」。

### 8.6 内存 —— 真正的风险，且默认值对这台机器是错的

`gateway/agent_cache_pressure.py:1-24` 直陈问题：网关按会话缓存 `AIAgent`，每个都钉住完整 transcript，而 LRU 上限**按条数计，不按字节**：

> the LRU cap counts *entries*, not bytes, and 128 warm transcripts is several GB;

默认值：`_AGENT_CACHE_MAX_SIZE = 128`（`gateway/run.py:81`）、`_AGENT_CACHE_IDLE_TTL_SECS = 3600.0`（`:82`）、`_AUTO_BUDGET_FRACTION = 0.65`（`agent_cache_pressure.py:41`）、`_AUTO_BUDGET_FLOOR_MB = 512`（`:44`）、`_DEFAULT_MAX_EVICTIONS_PER_PASS = 16`（`:46`）、`_DEFAULT_PROTECT_RECENT = 8`（`:50`）。

> ⚠️ **`memory_high_mb: "auto"` 在这台机器上算出约 1 235 MB**（`resolve_memory_high_mb`，`agent_cache_pressure.py:156-182`：`limit * 0.65 / MB`，1.9 GB × 0.65 ≈ 1235，高于 512 的下限所以生效）。压力驱逐要等网关自身占到 1.2 GB 匿名 RSS 才触发 —— 而机器只剩约 190 MB 可用、swap 已用 4 GB。**内核会先 OOM-kill。必须显式设成小整数**（非 `"auto"` 的正数走 `_positive_int` 直通，`:176`）。

`gateway/memory_status.py:52-56` 的阈值 `_CRITICAL_AVAILABLE_KIB = 64*1024`（或 5%）、`_ELEVATED_AVAILABLE_KIB = 128*1024`（或 15%）会在 `/api/status` 上报 —— 这台机器会长期处于 elevated/critical。

`gateway/scale_to_zero.py` 仅适用于 Fly.io（`:64` 读 `/.fly/api`）且默认关闭，**不适用**。

### 8.7 推荐安装路径（可直接执行）

**安装路径：源码浅克隆 + `uv pip install -e "."`，不带任何 extra。**

```bash
# 1) 浅克隆到独立目录（与 corlinman 隔离）
git clone --depth 1 https://github.com/NousResearch/hermes-agent /opt/hermes-agent
cd /opt/hermes-agent

# 2) 用系统 Python 3.11 建独立 venv
uv venv /opt/hermes-agent/venv --python 3.11

# 3) 只装基础依赖 —— 不加 --extra
UV_NO_CONFIG=1 VIRTUAL_ENV=/opt/hermes-agent/venv \
  uv pip install -e "."

# 4) 独立状态目录（不要用 root 的 ~/.hermes，避免与将来其他实例混淆）
export HERMES_HOME=/var/lib/hermes
```

**必须包含的 extras：无。** `[cron]`、`[pty]`、`[vision]`、`[nemo-relay]` 都是空别名，装了等于没装。OneBot 适配器所需的 `websockets` / `httpx[socks]`、cron 所需的 `croniter`、dashboard 所需的 `fastapi` / `uvicorn` **全都已在基础依赖中**。

**确定要排除的 extras**：`all`、`voice`、`wake`、`matrix`、`messaging`、`dev`、`teams`、`otlp`、`tts-premium`、`mistral`、`bedrock`、`vertex`、`azure-identity`、`hindsight`、`supermemory`、`mem0`、`honcho`、`google`、`youtube`、`modal`、`daytona`、`vercel`、`exa`、`firecrawl`、`parallel-web`、`fal`、`edge-tts`、`dingtalk`、`feishu`、`slack`、`wecom`、`computer-use`、`acp`、`termux*`。

**可按需后补（都很小）**：`[mcp]`（+3 包）、`[web]`（+4，多数已在核心）、`[anthropic]`（+1）。

**绝不要跑**：`setup-hermes.sh`（默认 `--extra all`）、裸 `scripts/install.sh`（默认拉 Node 26 + Chromium）、任何 Docker 路径。若一定要用官方安装器：

```bash
bash scripts/install.sh --skip-browser --skip-computer-use --no-skills \
     --non-interactive --hermes-home /var/lib/hermes
```

**估算磁盘占用**（UNVERIFIED —— 基于包体量推算，未实测）：

| 组成 | 估算 |
|---|---|
| 浅克隆源码树 | ~150–250 MB（含 `package-lock.json` 680 KB、`uv.lock` 700 KB、`cli.py` 950 KB 等大文件） |
| venv（28 个基础包，主要是 `cryptography`、`pydantic-core`、`Pillow`、`uvloop`） | ~250–400 MB |
| `$HERMES_HOME` 初始 | < 20 MB |
| **初装合计** | **~450–670 MB** |
| 稳态（含 sessions/cache 增长） | 建议按 1.5 GB 规划上限 |

留出的余量对 5.8 GB 可用空间是安全的。**部署前仍应先按 `00-PLAN.md` D5 回收 docker 空间。**

**`config.yaml` 关键项：**

```yaml
toolsets: ["file", "terminal"]        # 从 _HERMES_CORE_TOOLS 裁掉 browser/image_gen/tts/video
max_live_sessions: 2                  # 默认 16（config_defaults.py:34）
max_concurrent_sessions: 2
agent:
  agent_cache:
    max_size: 4                       # 默认 128（gateway/run.py:81）
    idle_ttl_secs: 300                # 默认 3600
    memory_high_mb: 300               # 绝不能用 "auto"（会算成 ~1235 MB 而形同虚设）
    protect_recent: 1
    max_evictions_per_pass: 32
database:
  journal_mode: "wal"
  wal_autocheckpoint: 200
  journal_size_limit: 33554432
browser:
  backend: "off"
cron:
  max_parallel_jobs: 2                # 12 个日任务会在整点撞车
  provider: ""                        # 保持内置调度器
```

**环境：** `HERMES_HOME=/var/lib/hermes`；`HERMES_DISABLE_LAZY_INSTALLS=1`（`tools/lazy_deps.py:533`），防止误选后端时静默拉下 `faster-whisper` 撑爆磁盘。

**systemd：** 给 unit 加 `MemoryHigh=` / `MemoryMax=` —— `resolve_memory_high_mb("auto")` 会读 cgroup 限额（`agent_cache_pressure.py:99-140`），但既然我们已显式设了整数，cgroup 限额的作用是**第二道 OOM 保护**，仍然应该设。

**可省略组件汇总：** 浏览器自动化（Playwright/Chromium）、camoufox、Electron 桌面端、媒体生成（`image_gen` / `video_gen` 插件）、TTS/STT（`voice` / `wake` / `tts-premium` / `mistral`）、本地模型、Node 托管树（仅 TUI + dashboard SPA 需要）、ffmpeg、`cua-driver`、Docker。

> **Contract summary — 部署**
> - Python **3.11**（目标机自带 3.11.2，符合 `>=3.11,<3.14`）。
> - 安装路径：浅克隆 + `uv pip install -e "."`，**不带任何 extra**。无 PyPI 包；`setup.py` 拒绝构建 wheel。
> - Linux 上 28 个基础包；无 torch/transformers/playwright/camoufox/numpy/pandas。**OneBot 适配器与 12 个 cron 任务均零新增依赖**（`websockets`、`httpx[socks]`、`croniter` 已在核心）。
> - 排除 `all` / `voice` / `wake` / `matrix` / `messaging` / `dev` 及全部后端 extras；`cron`/`pty`/`vision`/`nemo-relay` 是空别名。
> - 跳过 Docker（估算 4–6 GB 镜像）、跳过 `setup-hermes.sh`、跳过默认 `install.sh`。
> - **监听端口：默认零**。仅 `hermes dashboard` 时监听 **9119**（`hermes_cli/subcommands/dashboard.py:27`），该端口在目标机空闲。OneBot 适配器向外拨号连 `127.0.0.1:3001`，不监听。
> - **状态目录：`$HERMES_HOME`，本次设为 `/var/lib/hermes`**（默认为 `~/.hermes`，`hermes_constants.py:114-139`）。体积主要来自 `sessions/`、`state.db`、`cache/`。
> - 估算初装 ~450–670 MB，按稳态 1.5 GB 规划。
> - **必须把 `agent.agent_cache.memory_high_mb` 设成小整数**（如 300）—— `"auto"` 在 1.9 GB 机器上算出 ~1 235 MB，压力驱逐永远等不到触发，内核会先 OOM-kill。

---

## Gaps — 必须从零构建

以下四项在框架中**确实不存在**。其余全是组装。

### G1. OneBot v11 / NapCat 传输层

仓库中没有任何代码理解 OneBot 协议。`gateway/platforms/qqbot/` 是腾讯官方 Bot API，线格式完全不同。

**建**：`plugins/platforms/onebot/{plugin.yaml,__init__.py,adapter.py}`，按 §1.10。按 `AGENTS.md:126-137` 与 `:894-912`，应作为**独立插件仓库**安装进 `~/.hermes/plugins/`，而不是向 `plugins/platforms/` 提 PR。钩子清单抄 `plugins/platforms/ntfy/`；传输层抄 `plugins/platforms/simplex/adapter.py:210,287`（本机 WS）与 `plugins/platforms/wecom/adapter.py:312,365,406,458`（握手/重连/心跳/`req_id`→`echo` 关联）；重连梯度与媒体处理可参 `gateway/platforms/qqbot/adapter.py:516-760`、`:1678-1791`、`:2399`。

### G2. 带衰减的生活事件存储

`plugins/memory/holographic/` 是树内唯一的 decay 实现，但它是完整的 HRR/FTS5 语义存储，不是情景事件日志。内置 `MEMORY.md` 存储连时间戳都没有。

**建**：一个 `MemoryProvider` 子类（`agent/memory_provider.py:104`），作为 `~/.hermes/plugins/grantley-memory/` 发布，经 `ctx.register_memory_provider` 注册，用 `memory.provider: grantley` 选中。底层用 `plugin_db("grantley-memory")`（`plugins/plugin_storage.py:66`）建 append-only 表 `(id, created_at, salience, text)`。实现 `prefetch(query)`（`agent/memory_provider.py:166`）返回按 `0.5^(age_days/half_life)` 加权后的 top-N —— 公式与应用点照抄 `plugins/memory/holographic/retrieval.py:645-666` 与 `:107-111`。

**不要**为衰减内容实现 `system_prompt_block()`（`:157`）—— prefetch 的产出落在缓存安全的用户消息 sidecar（`agent/turn_context.py:1284`、`:54`），system_prompt_block 落在系统提示里会每轮击穿缓存。

注意树内政策（`AGENTS.md:882-893`）：`plugins/memory/` 的 provider 集合**已封闭**，新后端必须是独立仓库。

### G3. 按频道且内容动态的人格

`channel_overrides.system_prompt` 是按 `chat_id` 键的静态 YAML —— 对"固定人格绑固定频道"完美，对"人格文本需运行期计算"无解。而 `register_system_prompt_section` 的 session-info 映射没有 `chat_id`（`agent/system_prompt.py:178-185`），插件段无法按频道变化。

**建（惯用路线，零核心改动）**：让**适配器**来算。`OneBotAdapter` 在每条入站消息上设 `MessageEvent.channel_prompt` —— 这正是 Discord/Slack/Telegram/Feishu/Mattermost 的做法（`gateway/platforms/base.py:2789 resolve_channel_prompt`；调用点 `plugins/platforms/discord/adapter.py:6536`、`plugins/platforms/feishu/adapter.py:3310`、`plugins/platforms/telegram/adapter.py:10516`）。网关在 `gateway/run.py:5211-5213` 把它并进 `combined_ephemeral`。

因为它是 ephemeral，缓存合法；但**必须在单个会话生命周期内字节稳定**，所以要从角色的**每日冻结快照**计算，不能读实时衰减值。

**建（备选）**：每角色一个 profile + `gateway.profile_routes`（`gateway/profile_routing.py:54`）。更重（各自一套 `HERMES_HOME`），但每个角色白拿独立 memory/skills/cron —— 对格兰(QQ) / lycaon(TG) 这种双角色场景是更干净的划分。

### G4. 演化驱动器

框架不提供任何"人格随时间变化"的调度逻辑。cron 是载体，逻辑要自己写。

**建**：在角色 profile 里放两个 cron 任务。
- **decay / 固化任务**（`no_agent=True` + `script`）：给事件库做时间衰减，并重写适配器要读的「每日冻结人格快照」。因为 `no_agent`，**零 LLM 开销** —— 正好对应 corlinman 里跑了 1 260 次的 `persona.decay`。游标存 `cron/notepad.py`（`hermes cron notepad <job_id> set …`）。
- **生活事件生成任务**（LLM，`deliver="local"` 或让模型输出 `[SILENT]`）：追加新事件。

两者都通过插件自己的 `plugin_db(...)` 写入，**绝不改 `SOUL.md`** —— 改 `SOUL.md` 正是被禁止的模式（`AGENTS.md:88-91`；`SOUL.md` 位于 stable 层，`agent/system_prompt.py:381-396`）。

### 落地位置总图

```
~/.hermes/plugins/
├── onebot/                    # G1 — kind: platform；register_platform()
│   ├── plugin.yaml            #      name: onebot-platform（目录名必须是 onebot）
│   ├── __init__.py
│   └── adapter.py
└── grantley/                  # G2+G3+G4 — kind: standalone
    ├── plugin.yaml
    ├── __init__.py            # register(ctx):
    │                          #   register_memory_provider(...)
    │                          #   register_tool("qzone_publish", check_fn=…)
    │                          #   register_tool("qzone_reply", check_fn=…)
    │                          #   register_tool("qzone_friends", check_fn=…)
    │                          #   register_system_prompt_section("grantley.facts")
    │                          #   register_cli_command("grantley", …)
    ├── memory_provider.py     # MemoryProvider + prefetch() 里的半衰期
    ├── events.py              # plugin_db("grantley") schema
    └── scripts/decay.py       # 由 no_agent cron 任务调用

/var/lib/hermes/profiles/grantley/
├── SOUL.md                    # 角色基础提示（冻结）
├── config.yaml                # provider / 模型别名 / platforms.onebot.* /
│                              #   platforms.onebot.channel_overrides.*
├── cron/jobs.json             # 12 个迁移任务 + 3 个 QQ monitor 播报
└── skills/                    # 角色专属流程
```

`/Users/cornna/project/hermes-agent/` 下**无需修改任何文件**。

---

## 附录：本文核实状态

| 断言类别 | 状态 |
|---|---|
| 平台适配器（§1）全部行号与签名 | 本轮直接核实（含四个抽象方法、能力类属性、`MAX_PLATFORM_OUTPUT`、`DeliveryTarget.parse`、`SEND_ERROR_KINDS`、`_platform_name_from_manifest`、`toolsets.py:833-848`） |
| cron（§2）字段名、持久化、执行路径 | 本轮直接核实（`create_job` 签名与 job dict 逐字对照、`parse_schedule`、`_deliver_result`/`_resolve_delivery_targets`、`executions.db` schema、notepad 上限、blueprint 结构与 16 条目数、scheduler_provider ABC 注释） |
| skills（§3） | 本轮直接核实 |
| 插件（§4） | 本轮直接核实 |
| 工具（§5） | 本轮直接核实 |
| persona（§6） | 本轮直接核实（SOUL.md 加载器、`ChannelOverride`、`channel_prompt`、`register_system_prompt_section` 及其约束常量、`BUILTIN_PERSONALITIES`、holographic 半衰期公式与应用点、`MemoryProvider` ABC、prefetch→`api_content` 通路、`ProfileRoute`） |
| 配置与 provider（§7） | 本轮直接核实 |
| 部署（§8）Python 版本、依赖计数、44 个 extras、端口表、`HERMES_HOME`、内存默认值 | 本轮直接核实 |
| **Docker 镜像体积 4–6 GB** | **UNVERIFIED** —— 未实际构建，由层内容推断 |
| **§8.7 磁盘占用估算 450–670 MB** | **UNVERIFIED** —— 由包体量推算，未在目标机实测 |
| `tools/lazy_deps.py` 注册键约 42 个 | 由 grep 计数得出，可能因匹配模式有偏差 |
