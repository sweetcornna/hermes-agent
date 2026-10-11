# QQ via OneBot v11 (LLBot / NapCat / SnowLuma / Lagrange / go-cqhttp)

Platform name: **`onebot`**.

### LuckyLilliaBot (LLBot)

[LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot) connects through
the existing OneBot v11 adapter. Enable a **forward WebSocket** in LLBot, use
`messageFormat: array`, and set the matching endpoint in the active Hermes
profile's `config.yaml`:

```yaml
platforms:
  onebot:
    enabled: true
    extra:
      ws_url: ws://127.0.0.1:3001
```

Preserve the existing group whitelist, reply switches, persona bindings and
debounce settings. Store the matching OneBot WebSocket token as
`ONEBOT_ACCESS_TOKEN` in the profile's `.env`. LLBot accepts the adapter's
`Authorization: Bearer` header and `access_token` query parameter. LLBot's
WebUI token and its runtime [Auth Token](https://auth.luckylillia.com) are
separate from this OneBot connection token. Keep the previous backend stopped when
reusing its account or port, then restart the Hermes gateway after switching.

LLBot reports `app_name: LLOneBot`; Hermes displays the backend as `llbot`.
Text, mentions, quotes, images and voice use the standard OneBot path. With a
remote backend, `enableLocalFile2Url: true` lets `get_record` include base64
audio for voice recovery. Incoming document segments containing only a
backend-local `file://` path are not downloaded by Hermes; they require a
fetchable URL.

### Conversational replies and complete answers

Set `platforms.onebot.extra.reply_debounce_seconds: 3` in `config.yaml` to
wait for three seconds of silence from a sender before starting a reply.
Each account/chat/sender has its own timer. Accepted group bursts include
the same sender's subsequent text or media without requiring another mention.
Channel gates still apply. The setting defaults to `0` (immediate dispatch).
Group and private conversations follow the same rule; `/stop`, `/new`,
`/reset`, `/clear` and `/restart` remain immediate control actions and cancel
unsent bursts in that conversation. Buffered bursts are in memory until
dispatched; a process crash during the waiting window can lose those unsent inputs.

Everyday replies normally use one short message, with `[MSG_BREAK]` between
two or three bubbles when needed. `max_bubbles_per_reply: 3` is a ceiling,
not a quota. A complex answer can start with `[LONG_REPLY]` to request a
merged-forward card regardless of length. Automatic `forward_threshold`
routing remains available. Code and source URLs survive intact; card failure
falls back to complete text. Cards require the backend to support merged forwards and a positive threshold. These choices are shared by reply and
proactive delivery.

Supported backends are LLBot, NapCat, SnowLuma, Lagrange and go-cqhttp.

For NapCat deployments, pin a complete image/runtime version instead of
tracking `latest`.
Keep both backend state
directories on persistent storage and do not delete them during an upgrade:
the QQ device state under `/app/.config/QQ` and NapCat configuration under
`/app/napcat/config` are part of the device identity and backend setup. A
typical host bind-mounts `/opt/napcat/ntqq` to the former and
`/opt/napcat/config` to the latter.

> **This is not the `qqbot` platform.** Hermes ships `gateway/platforms/qqbot/`,
> which speaks Tencent's *official* QQ Bot API v2 (`api.sgroup.qq.com`, app id +
> client secret, official bot accounts). This plugin speaks the community
> **OneBot v11** protocol to a local bridge driving an **ordinary QQ account**.
> Different protocol, different credentials, different capabilities. Configuring
> one when you meant the other fails in confusing ways, so the names are kept
> deliberately distinct.

## Topology

```
   QQ  ──  NapCat (or SnowLuma / Lagrange / go-cqhttp)  ──ws──>  Hermes onebot adapter
                    forward WebSocket server            (dials out; binds nothing)
                    default ws://127.0.0.1:3001
```

Hermes is always the **client**. It opens no listening port, so it works behind
NAT and needs no inbound firewall rule.

### Hard constraint: never write the backend's configuration

This adapter reads the WebSocket URL and access token and **does nothing else to
the backend**. It never calls the backend's WebUI API, and in particular never calls
`/api/OB11Config/SetConfig`.

That is not squeamishness. On the deployment this was written for, another
service rewrites NapCat's OneBot config on a timer; a second writer would fight
it and can knock the live QQ service offline. (The neighbouring service's own
NapCat *manager* is, separately, in a permanent crash loop on that host while
the NapCat container itself is healthy — more reason not to imitate or take over
that machinery.) If you need a dedicated token or port for Hermes, add a second
`websocketServers` entry **by hand in the backend's configuration UI** and point
`ONEBOT_WS_URL` at it; that entry is then unmanaged by anything else. For
NapCat, the default forward WebSocket is `127.0.0.1:3001/` with role
`Universal` and an auto-generated access token; configure it in the WebUI at
`http://localhost:5099` (user `admin`) or in `config/onebot.json` under
`networks.wsServers`.

### Two different secrets

`ONEBOT_ACCESS_TOKEN` is the **OneBot** access token (the `token` field of the
backend's `websocketServers` entry). The NapCat **WebUI** token and SnowLuma
**WebUI password** are separate secrets. Using either WebUI credential here
produces a permanent handshake rejection; the adapter detects that case and
reports `onebot_auth_rejected` instead of reconnecting forever.

### Transport online is not QQ-account online

The forward WebSocket and the QQ login are separate health domains. A
successful WebSocket handshake, a heartbeat frame, or a reconnect proves that
the OneBot backend is reachable; it does **not** prove that the ordinary QQ
account is logged in. Hermes keeps this distinction in its health snapshot:
`link_online` describes the WebSocket, while `account_online` describes the
last explicit account signal.

Hermes marks the account `AUTH_REQUIRED` only from signals it can consume:
a heartbeat with explicit `status.online: false`, a `bot_offline` notice, or
consistent `get_status` / `get_login_info` probe evidence. A backend
`KickedOffLine` log line is useful operational evidence for the person running
the service, but Hermes does not ingest backend logs and that line does not
change adapter state by itself. While auth is required, Hermes refuses ordinary
outbound text/media with a non-retryable result. The operator must log in again
in the backend (normally by scanning the QR code). A quick-login session/ticket
is not a permanent credential and may still expire or require manual
verification; Hermes does not claim to renew it.

Reconnecting the WebSocket cannot log QQ back in and therefore does not clear
`AUTH_REQUIRED`. A later explicit `status.online: true` heartbeat, or a
successful `get_status`/`get_login_info` probe, restores the account state.
Until the first explicit account signal the state is `unknown`, and existing
send behavior is retained so the startup window is not killed by an absent
optional status field.

---

## Quick start

```bash
# ~/.hermes/.env
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=<the OneBot token, not the WebUI token/password>
```

That is enough to answer **direct messages**. Groups stay silent until you flip
the master switch below. (DMs have their own switch too, defaulting to
answer — see "The direct-message switch".)

---

## ⚠️ The group master switch

```
ONEBOT_GROUP_REPLIES_ENABLED   default: false
```

**Every group message is dropped while this is `false`** — before the mention
check, before the keyword check, before rate limiting. Direct messages are
unaffected.

It is a **two-way** gate (D44). Inbound, nothing is answered. Outbound, nothing
is *sent* to a group either: `send()`, every media send, and the out-of-process
`standalone_sender_fn` all refuse a group target while the switch is off, so a
cron job delivering to `onebot:g<id>` or a model calling `send_message` cannot
post into a group the operator believes is muted. Before D44 only the inbound
half existed, which is how the source system's "emergency mute" quietly became
a switch that stopped replies while scheduled output kept going.

A refused send comes back as `SendResult(success=False, …)` carrying
`raw_response["onebot_group_muted"] = True` — never a silent pretend-success and
never an exception. Its `error_kind` is deliberately **not** `forbidden` or
`not_found`: those two are what `gateway.dead_targets` treats as permanent, and
a mute is a switch someone will flip back. Use
`plugins.platforms.onebot.adapter.is_muted_send_result(result)` to tell "muted"
from "the send failed" — it accepts both the `SendResult` and the dict the
standalone sender returns.

**What the switch does not cover:** direct messages, by design. Two of the
migrated QQ digests (`sanhu`, `jlu`) deliver to `onebot:2104743984`, which is a
*DM*, so this flag is not a kill switch for them. `qunjlu` is suppressed
structurally instead (`deliver=local` + no send tools, D45) — that suppression
stays, on top of this one, because it does not depend on any runtime config
being read correctly.

The full group pipeline (whitelist → mention/keyword → cooldown → token buckets
→ hard speech cap) is implemented and tested; this switch is the only thing
standing between it and production traffic. The default is `false` because the
deployment this was ported for runs exactly that way: whitelist, keywords and
rate limits all configured, master switch never turned on, so the bot answers
DMs only. Defaulting to `false` reproduces today's behaviour on cutover, and

```
ONEBOT_GROUP_REPLIES_ENABLED=true
```

is the single line that turns groups on. Turn it on deliberately, ideally with
a whitelist already in place.

---

## The direct-message switch

```
ONEBOT_DIRECT_REPLIES_ENABLED   default: true
```

The mirror image of the group master switch above, gating the other lane —
but with the **opposite default**, because the two switches reproduce two
different starting behaviours. Before this key existed, private chat was
dispatched unconditionally (see "Reply gating, in order" below); that is the
behaviour every deployment already relies on, corlinman's production QQ
account included, which answers DMs today. Defaulting to `true` reproduces
that exactly with the key unset — adding this switch changes nothing until
someone sets it.

**Every private message is dropped while this is `false`**: `dispatch()`
returns before a `Binding` is even built, so no turn is started and no reply
is generated. This is an **inbound-only** gate, unlike the group switch —
D44's two-way mute is deliberately not repeated here. It does not touch
`send()`, `send_message`, or `standalone_sender_fn`, so a cron job or a model
tool call can still deliver to a DM chat id (`onebot:2104743984`) while this
is `false`, the same way the group switch does not cover `sanhu` / `jlu`'s
cron deliveries to a DM (see above). Turning this off only stops the bot from
*answering* an inbound DM; it does not suppress outbound delivery to one.

One purpose of this switch is a **receive-only observation mode**. With
`ONEBOT_DIRECT_REPLIES_ENABLED=false` and the group master switch left at its
own default (`false`), the adapter connects and receives traffic — and, if
group history is separately enabled, archives it — while never answering
anyone, group or DM. This is the posture a group-archive smoke test needs
when another bot instance is still live on the same QQ account and answering
its DMs: without this switch, both bots would answer the same private
message, visibly, to a real person.

```
ONEBOT_DIRECT_REPLIES_ENABLED=false
```

is the single line that switches DMs to receive-only. Leaving the key unset
(or setting it `true`) keeps today's behaviour unchanged.

---

## 主窗口 — the one chat that drives this gateway

**A QQ group is never the console.** Reminders land in one private chat, and
slash commands are honoured only there. Two separate things were open before
this rule existed, and either one alone hands a group the controls:

* **Delivery.** Bare `deliver=onebot`, gateway restart/drain notices and every
  cron job with no explicit target resolve through `home_channel`. Nothing
  stopped that from being `g183287894` — at which point operator notices land
  in a room full of strangers.
* **Control.** A leading `/` in a group is an *explicit summons* (it bypasses
  the keyword filter and the cooldown, exactly like an @mention), and
  `gateway/slash_access.py` disables slash gating entirely for a scope whose
  `allow_admin_from` is unset — which is the staged production config's state
  for the group scope. Together: flip `group_replies_enabled` on and every
  member of a whitelisted group can run every registered slash command.

### What the lock actually is

Not a denial message — the **absence of the capability**. Every group event is
built with `MessageEvent.allow_gateway_control=False`
(`gateway/platforms/base.py`), so `is_command()` is `False` and the run loop's
command paths are never entered. `router.py` drops the summons bypass in the
same breath, because inert operator text that still punches through the keyword
gate would become a *model turn*, which is worse than being ignored.

This is deliberately structural rather than "remember to write
`group_allow_admin_from`": the migration has already hit "config says off,
behaviour says on" five times (see `plugins/corlinman_jobs/specs.py` on
`qunjlu`), and this is not a good sixth.

### Configuring it

```yaml
platforms:
  onebot:
    # Reminders, cron digests and gateway notices land here.
    home_channel:
      chat_id: "2104743984"       # a PRIVATE chat — a group id is refused
      name: "主窗口 (DM)"
    extra:
      # …and commands come from here. Defaults to home_channel's id, so the
      # two cannot drift apart. `ONEBOT_MAIN_WINDOW_CHAT_ID` is the env form.
      main_window_chat_id: "2104743984"

      # Off by default. `true` gives group members gateway commands back.
      group_gateway_control_enabled: false

      # Independent second lock, in core: listing an admin ENABLES hermes'
      # own slash gating for the DM scope (unset ⇒ every allowed DM user
      # runs every command).
      allow_admin_from: ["2104743984"]
```

| Situation | `allow_gateway_control` |
|---|---|
| Any group message | `false` (`true` only with the escape hatch) |
| DM from the main window | `true` |
| DM from anyone else, main window configured | `false` |
| DM, no main window configured anywhere | `true` — unchanged behaviour |

`main_window_chat_id` accepts `2104743984`, `private:2104743984` or
`user:2104743984`. A group id (`g183287894`, `group:183287894`) is **refused,
not promoted** — in that slot *and* in `home_channel`'s, so a config typo
cannot turn a group into the console. A group `home_channel` still works for
delivery (it is the operator's config) but logs a WARNING at every start,
because "why do restart notices show up in the group chat" is otherwise a
mystery.

Plain chat is untouched everywhere: the persona still answers groups exactly as
the reply-gating chain below decides. Only *commands* are scoped.

---

## Configuration keys

Every key can be set as an environment variable **or** under
`platforms.onebot` in `~/.hermes/config.yaml` (`extra:` works too). Env wins for
secrets; YAML is the better home for the group policy.

### Connection

| Key | Default | Meaning |
|---|---|---|
| `ONEBOT_WS_URL` | — (**required**) | Forward WebSocket URL of the backend, e.g. `ws://127.0.0.1:3001`. |
| `ONEBOT_ACCESS_TOKEN` | *(none)* | OneBot access token. Sent as `Authorization: Bearer` **and** `?access_token=`. Not the WebUI token. |
| `ONEBOT_TOKEN_IN_QUERY` | `true` | Also append `?access_token=`. Set `false` if a strict backend objects to the token appearing twice; the header alone is the form proven in production. |
| `ONEBOT_SELF_IDS` | *(none)* | Bot QQ number(s). Only a seed: the live `self_id` on each event wins, so an account switch needs no config change. |
| `ONEBOT_HTTP_URL` | *(none)* | Optional HTTP API base used by the **tool** layer (`tools/onebot_client.py`) only. The adapter is WebSocket-only. |

### Authorization (core Hermes keys)

| Key | Default | Meaning |
|---|---|---|
| `ONEBOT_ALLOWED_USERS` | *(none)* | Comma-separated QQ user ids allowed to talk to the bot. |
| `ONEBOT_ALLOW_ALL_USERS` | `false` | Disable the allowlist. Development only. |
| `ONEBOT_HOME_CHANNEL` | *(none)* | Default target for cron / notification delivery — the 主窗口. **Point it at a private chat, not a group.** |
| `ONEBOT_HOME_CHANNEL_NAME` | = id | Display label for the home channel. |
| `ONEBOT_MAIN_WINDOW_CHAT_ID` | = home channel | The one private chat whose slash commands are honoured. A group id is refused. See "主窗口" below. |
| `group_gateway_control_enabled` (YAML only) | **`false`** | Escape hatch: let group members run slash commands after all. |

### Direct-message gating

| Key | Default | Meaning |
|---|---|---|
| `ONEBOT_DIRECT_REPLIES_ENABLED` | **`true`** | Master switch for private-chat replies — see "The direct-message switch" above. Inbound-only; does not affect outbound delivery to a DM chat id. |

### Group gating

| Key | Default | Meaning |
|---|---|---|
| `ONEBOT_GROUP_REPLIES_ENABLED` | **`false`** | Master switch — see above. |
| `ONEBOT_GROUP_WHITELIST` | *(unset)* | Comma-separated group ids. **Unset** = no whitelist (every group passes). **Set but empty** = no group is ever answered. An @mention does **not** bypass it. |
| `ONEBOT_GROUP_KEYWORDS` | `{}` | JSON `{"<group_id>": ["keyword", …]}`, case-insensitive substring match. |
| `ONEBOT_GROUP_REPLY_POLICY` | `mention_or_keyword` | `mention_or_keyword`: reply to @mentions, or to a message matching that group's **explicitly configured** keywords — no keyword list means **mention-only**. `all`: legacy — a group with no keyword list gets a reply to *every* message. |
| `ONEBOT_GROUP_REPLY_COOLDOWN_SECS` | `0` | Minimum gap between **non-mention** replies in one group. An @mention always answers and resets the clock. |
| `ONEBOT_GROUP_RATE_LIMIT_WINDOW_MINUTES` | `0` (off) | Hard speech-cap window. |
| `ONEBOT_GROUP_RATE_LIMIT_MAX_MESSAGES` | `0` (off) | Messages the bot may send into one group per window. **@mentions do not bypass this.** |
| `ONEBOT_RATE_LIMIT_GROUP_PER_MIN` | `0` (off) | Inbound token bucket per group. |
| `ONEBOT_RATE_LIMIT_SENDER_PER_MIN` | `0` (off) | Inbound token bucket per sender. |

#### Being spoken to without an @

Two YAML-only additions on top of the policy above:

* **Quoting the bot is an address.** A message that quotes (QQ 引用) one of the
  bot's own posts is answered like an @mention, even when the client did not add
  the @. Recent post ids are remembered locally; older ones are resolved with
  `get_msg`. `quote_reply_addresses_bot: false` turns this off.
* **Implicit addressing** (`addressee.py`, off by default). Unaddressed group
  messages are gathered per group until the room pauses, then one auxiliary
  model — `auxiliary.onebot_address_judge` — decides whether any of them is
  aimed at the bot (a reply to what it said, a nickname, "问问 AI"). Only
  rooms where the bot posted in the last `implicit_address_recent_self_minutes`
  are watched, so a cold call ("那只老虎呢" after an hour of silence) needs an
  @ or the keyword. A yes is
  answered through the normal reply path, speech cap included; errors mean no.

```yaml
      quote_reply_addresses_bot: true
      implicit_address_enabled: false
      implicit_address_persona: ""          # blank ⇒ proactive_judge_persona
      implicit_address_quiet_seconds: 8     # judge once the room pauses this long
      implicit_address_max_wait_seconds: 20 # ...or after this long regardless
      implicit_address_min_interval_seconds: 15
      implicit_address_daily_max_checks: 300  # judge calls per group per day
      implicit_address_recent_self_minutes: 10 # only within N min of the bot's last post; 0 = always
```

Direct messages bypass the whole group chain. They are still subject to the core
`ONEBOT_ALLOWED_USERS` allowlist, and to their own `ONEBOT_DIRECT_REPLIES_ENABLED`
gate (default `true`, i.e. answered) above.

### Per-channel persona binding

| Key | Default | Meaning |
|---|---|---|
| `persona_channels` | *(unset)* | YAML/`extra` only. `{"<chat_id>": {persona, channel_owner, group, name}}`. Overrides the persona plugin's own map; **present but empty means "no bindings"**, not "fall back". |

Unset, the same map is read from
`plugins.entries.grantley.settings.channels` (the path
`docs/migration-corlinman/C1-grantley-port-notes.md` §4.1 documents). Either way
the adapter sets `MessageEvent.channel_prompt` on **both** lanes — inbound
replies and proactive posts — so the persona is framed identically whichever way
it speaks. An unbound chat id simply gets no frame; nothing breaks and no empty
block is injected. See `persona_binding.py`.

```yaml
plugins:
  entries:
    grantley:
      settings:
        channels:
          "183287894":
            persona: grantley
            channel_owner: "2104743984"   # Channel contact; no implied romantic relationship
            group: true
            name: "群聊-JLU"
```

The rendered frame is a **daily frozen snapshot** (`channel_binding.py`'s cache
contract: byte-stable within a day for a given channel), memoised per
`(channel, day)` so an inbound message does not re-read the seed pack off disk.
The `plugins.entries.…` source is read once per process; the `persona_channels`
form in `extra` is re-read live, like every other key here.

### Grantley rolling autocompaction

When `memory.provider: grantley` is active, Grantley QQ sessions use Hermes'
existing in-place context compressor. Earlier turns are summarized at a token
threshold, a recent verbatim tail stays live, and the same stable session lane
keeps accumulating until the next threshold crossing. This repeats after a
gateway restart because the rolling handoff and archived source turns live in
`state.db`; no summary logic lives in the OneBot adapter.

The defaults apply to old Grantley configurations without an `autocompact`
block. Add this only to override them:

```yaml
memory:
  provider: grantley

plugins:
  entries:
    grantley:
      settings:
        autocompact:
          enabled: true
          threshold: 0.75       # fraction of input budget, after output headroom
          threshold_tokens: null # optional lower absolute trigger
          tail_tokens: 16000    # verbatim recent-tail token budget
```

`enabled: false` disables automatic compaction for the bound Grantley QQ lane.
The policy is resolved when its agent is constructed, so change it before the
next turn/new agent (or restart the gateway); it is never mutated halfway
through a conversation. It is scoped by the same strict `channels` allowlist
above: the exact chat id must have an object entry with `persona: grantley`.
Missing, empty, malformed, or non-matching maps disable Grantley for that lane.

Compaction keeps the session id and QQ routing key unchanged. The gateway's
existing `group_sessions_per_user` rule therefore remains authoritative:
groups/users are isolated when that setting is on, shared per group when it is
off, and DMs remain separate. Proactive posts use the same OneBot source and
lane as replies; scheduled in-channel delivery continues to use the existing
cron lane-seeding rules.

Grantley aborts and rolls back when summary generation fails, records the core
durable retry cooldown, and leaves the original turns searchable. Its volatile
life state still travels in the current user-message sidecar, while the cached
system prompt bytes are retained exactly across compaction boundaries.

Grantley's state, tools, optional identity section, and autocompaction are all
gated by this same bound OneBot lane. A profile that also serves Telegram,
CLI, or unrelated OneBot channels must keep its default `SOUL.md` neutral:
`SOUL.md` is profile-global, so putting the Grantley persona there bypasses
the plugin lane gate. Use `inject_identity_section: true` for the frozen,
lane-scoped identity instead (or use a separate Grantley-only profile).

### Group context on an addressed turn

| Key | Default | Meaning |
|---|---|---|
| `ONEBOT_GROUP_CONTEXT_MESSAGES` | `12` | Lines of recent group chatter prepended to a turn the bot was addressed in. `0` turns the block off. |

A QQ group is a room, not a thread: the message that names the bot is usually
the *last* one in an exchange, and the question it refers to is in the three
before it. Every other adapter here already handles that through
`MessageEvent.channel_context` — Discord scans back to its own last post, Slack
backfills the thread — and `run.py` prepends whatever it finds ahead of a
`[New message]` marker. OneBot was the only lane that never filled the field.

The block is rendered from the in-memory chatter buffer `record_group_message`
already fills *before* the reply gate, so there is no extra API call:

```
[Recent group messages]
[张三] 这个报错怎么修
[李四] 看日志啊

[New message]
[张三] 格兰 你觉得呢
```

The window runs backwards from the message being answered and **stops at the
bot's own last post**, which is also what keeps it from repeating itself: a
line shown as context in one turn sits before the reply that turn produced, so
the next scan halts before reaching it. The buffer is RAM-only, so a restart
yields a shorter window, never a duplicated one.

Two things it deliberately is not: it never applies to a DM (the whole session
already *is* the conversation), and it does not decide whether to answer —
gating is unchanged, this only changes what an already-allowed turn can see.
It matters most under the default `group_sessions_per_user: true`, where each
speaker's transcript contains nobody else's lines. See `reply_context.py`.

### Outbound behaviour

| Key | Default | Meaning |
|---|---|---|
| `ONEBOT_REPLY_WITH_MENTION` | `true` | Prefix group replies with `@sender`. Only the **first** chunk is ever prefixed — repeating it would ping the user once per chunk, which QQ renders as spam. |
| `ONEBOT_REPLY_TO_MODE` | `first` | Quote the message being answered (QQ 引用回复). `off` disables it, including for callers that passed an explicit anchor. `all` is accepted for parity with Telegram/Discord but behaves as `first`: QQ draws a quote as an inset block, so one per bubble triples the height of a three-bubble reply. Only sends that came from an inbound turn quote anything — a proactive post, a cron delivery and a tool's `send_message` carry no anchor and stay unquoted. |
| `ONEBOT_FORWARD_THRESHOLD` | `1000` | When positive, fold the WHOLE reply into a merged-forward ("聊天记录") card if its total length exceeds this value or its `[MSG_BREAK]` count exceeds a positive `ONEBOT_MAX_BUBBLES_PER_REPLY`. Each bubble remains a card node. `0` disables cards. In groups a short lead line carries the @mention because a card cannot carry one. |
| `ONEBOT_MAX_BUBBLES_PER_REPLY` | `3` | Maximum `[MSG_BREAK]` bubbles before a card when cards are enabled. `0`/negative disables the count cap, so count alone cannot route. With cards disabled or a non-terminal card rejection, overflow is merged into the last chat bubble. |
| `ONEBOT_WAIT_FOR_SEND_ACK` | `true` | Wait for the echo-correlated response so failures are classified and message ids reported. A backend that never echoes is treated as an optimistic success rather than a false failure. |
| `ONEBOT_TYPING_INDICATOR` | `true` | OneBot v11's "对方正在输入…" indicator in DMs. QQ groups never render one, so nothing is sent there. |
| `ONEBOT_MAX_CONCURRENCY` | `2` | Concurrent agent turns. Tuned for a 2 vCPU / 2 GB host; the upstream implementation used 8. |
| `ONEBOT_HEALTH_PROBE_S` | `30` | Health check interval. |
| `ONEBOT_HEALTH_LOST_S` | `120` | Silence before the link is reported lost. |
| `account_probe_secs` (YAML under `platforms.onebot.extra`) | `60` | Fallback `get_status`/`get_login_info` account probe interval. `0` disables account probes; positive values are clamped to at least 5 seconds. Keep this separate from WebSocket liveness; it never reconnects the socket. |

### Chat ids

| Form | Meaning |
|---|---|
| `g183287894` | QQ **group** 183287894 |
| `2104743984` | QQ **user** (direct message) |

`group:<id>` / `private:<id>` / `user:<id>` are accepted as input for
convenience. The canonical `g` prefix exists because delivery targets are parsed
as `platform:chat_id:thread_id` — a colon inside the chat id would silently
route the message somewhere else.

Cron delivery: `deliver=onebot:g183287894`, or `deliver=onebot` for the home
channel.

---

## Worked example — the production configuration

Five whitelisted groups, per-group keyword `格兰`, replies on @mention and on
direct message, at most 5 messages per group per 3 minutes, the 主窗口 pinned
to the owner's DM (so reminders land there and commands come from there), and
the master switch off so cutover reproduces current behaviour exactly.

`~/.hermes/.env` (secrets only):

```bash
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=<OneBot token from the backend's websocketServers entry>
```

`~/.hermes/config.yaml`:

```yaml
platforms:
  onebot:
    enabled: true
    # ---- 主窗口: where reminders land -----------------------------------
    home_channel:
      chat_id: "2104743984"
      name: "主窗口 (DM)"
    # Optional per-group tool-loop cap; the global agent budget still wins
    # when it is lower. DMs and other groups retain their existing budget.
    # Hermes allows one final answer-only call after the budget is exhausted.
    channel_overrides:
      "g894800697":
        max_turns: 12
    extra:
      # ---- 主窗口: who may operate the gateway ---------------------------
      # Slash commands are honoured in this private chat and nowhere else;
      # a group carries allow_gateway_control=False by construction.
      main_window_chat_id: "2104743984"
      group_gateway_control_enabled: false
      allow_admin_from: ["2104743984"]

      # ---- the master switch -------------------------------------------
      # false = the bot is silent in every group and answers DMs only.
      # Everything below is configured and ready; flip this one line to
      # switch the group pipeline on.
      group_replies_enabled: false

      # ---- group gating -------------------------------------------------
      group_whitelist:
        - 1082225370
        - 183287894
        - 894800697
        - 149881991
        - 667528618
      group_reply_policy: mention_or_keyword
      group_keywords:
        "1082225370": ["格兰"]
        "183287894":  ["格兰"]
        "894800697":  ["格兰"]
        "149881991":  ["格兰"]
        "667528618":  ["格兰"]

      # 5 messages per group per 3 minutes, shared by replies AND by
      # proactive speaking. @mentions do NOT bypass it.
      group_rate_limit_window_minutes: 3
      group_rate_limit_max_messages: 5

      # ---- proactive speaking -------------------------------------------
      # Off. Production ran with this true and never sent a message (the
      # master switch above silenced it), so turning it on here would be new
      # behaviour, not a restoration. See the section below.
      proactive_enabled: false

      # ---- outbound -----------------------------------------------------
      reply_with_mention: true
      max_concurrency: 2
```

Equivalent environment form:

```bash
ONEBOT_HOME_CHANNEL=2104743984
ONEBOT_MAIN_WINDOW_CHAT_ID=2104743984
ONEBOT_GROUP_REPLIES_ENABLED=false
ONEBOT_GROUP_WHITELIST=1082225370,183287894,894800697,149881991,667528618
ONEBOT_GROUP_KEYWORDS='{"1082225370":["格兰"],"183287894":["格兰"],"894800697":["格兰"],"149881991":["格兰"],"667528618":["格兰"]}'
ONEBOT_GROUP_REPLY_POLICY=mention_or_keyword
ONEBOT_GROUP_RATE_LIMIT_WINDOW_MINUTES=3
ONEBOT_GROUP_RATE_LIMIT_MAX_MESSAGES=5
```

With `group_replies_enabled: false` the whitelist, keywords and rate limits are
loaded and inert — exactly today's behaviour. Setting it to `true` activates all
of them at once.

---

## Reply gating, in order

The order is the contract; the tests pin it.

1. **Direct message** → `direct_replies_enabled` gate (default `true`) ⇒
   dispatched unless explicitly switched off.
2. **Group**:
   1. `group_replies_enabled` — off ⇒ drop (an @mention cannot punch through);
   2. whitelist — a hard gate; an @mention does **not** bypass it, and an empty
      whitelist blocks every group;
   3. explicit summons = @mention — **or** a slash command, but only with
      `group_gateway_control_enabled: true` (see "主窗口"); by default a
      leading `/` in a group buys nothing, because the command could not
      execute there anyway;
   4. otherwise the reply policy (keywords), then the per-group cooldown.
3. Empty text (pure sticker, recall placeholder) → drop.
4. Per-group and per-sender token buckets — **after** the gate, so a filtered
   message never consumes anyone's budget.
5. **Hard speech cap** — after any slash-command handling, before the model call.
   Operator commands are never locked out; a capped group never burns an LLM
   call; @mentions do not bypass it (they *do* reset the cooldown clock).
   Neither does a slash prefix in a group: with the group lock on,
   `RoutedRequest.is_command` is `False` there, so typing `/` is not a way
   around the cap.

## Behaviour worth knowing

* **Long replies are chunked** at 3800 characters with `(n/N)` prefixes, on
  paragraph → line → sentence boundaries. The adapter declares
  `splits_long_messages = True`, so the gateway hands it the full payload
  instead of truncating at 4000 characters.
* **`[MSG_BREAK]`** in a reply splits it into separate chat bubbles, 0.3 s apart.
* **Attachments**: images and voice notes go inline as `base64://` segments;
  everything else goes to the group/private file area. The inline ceiling is
  **8 MiB** (the source implementations used 30 MiB — base64 inflates ~4/3 and
  the frame is buffered in RAM at both ends, which a 2 GB host cannot afford).
  Larger files fall back to a literal path, which only works when the backend
  shares this filesystem; the fallback is logged.
* **Health** is two separate questions: `link_online` (is the WebSocket up) and
  `account_online` (is the QQ account logged in). They diverge exactly when the
  account is kicked — the socket keeps heartbeating while the bot silently goes
  mute. See `OneBotAdapter.health_snapshot()`.
* **Reconnect** ladder is 1s → 2s → 5s → 10s → 30s (last repeats), with a 30 s
  protocol ping. Transient failures never surface to the gateway.
* **Inbound overflow** drops the *oldest* queued event, never the newest — the
  message a user just sent is the one that matters.

---

## Voice — messages and calls (`voice.py`, `voice_call.py`)

### Inbound: voice notes now transcribe reliably

QQ voice notes arrive as `record` segments in one of two shapes: gocq ships a
URL (an ordinary download); the backend may ship a **name-only** segment — no
URL, just a `file` id, which used to be dropped silently (every url-less
segment was skipped) so the user's message simply arrived empty. It is now
resolved through the OneBot v11 `get_record` action
(`protocol.GetRecord` → `OneBotAdapter._resolve_record`), which asks the
backend to transcode the note and hands back a URL, inline base64, or — least
reliably — a path on the backend's own filesystem.

Whatever shape the bytes arrive in, the file itself is SILK or AMR — raw
codec streams QQ and WeChat use, not containers a transcription provider can
read. Before this pass the transcription layer picked its decoder from the
file's (usually wrong) extension, so a SILK payload saved as `.amr` chose the
wrong codec and **every QQ voice note failed to transcribe**, landing in the
model's context as `[voice message could not be transcribed automatically]`.
`tools/audio_container.py` now sniffs the container from its magic bytes
before anything decides how to decode it, and `tools/transcription_tools.py`
dispatches a sniffed SILK/AMR source to a registered
`agent.audio_decoder.AudioDecoder` before transcription — content over
filename, fixed once for every platform that can hand this codebase a
QQ/WeChat voice note, not patched locally in this adapter. The decoders
themselves (SILK via `pilk`, AMR via `ffmpeg`) live in the bundled
`plugins/voice_codecs/` plugin, not in core: it's a `kind: backend` plugin, so
it auto-loads with Hermes — nothing to add to `plugins.enabled`, and SILK/AMR
voice notes transcribe out of the box. `pilk` is lazy-installed (via the
`stt.silk` lazy-dep group) the first time a `.silk` voice note actually
arrives; `ffmpeg` is a system binary and is never installed automatically. If
either is missing, transcription fails with a message naming exactly what to
install rather than a silent drop. Full failure chain, why the decoders were
later split out of `tools/transcription_tools.py` into that plugin, and why
it's `kind: backend`: `docs/migration-corlinman/F1-qq-voice-notes.md` (§3,
§3.2).

### Outbound: `voice.speak()` — the one path a voice bubble leaves through

`OneBotAdapter.send_voice()` still exists — it is what a caller handing an
**already-recorded file** should use — but it reaches the wire through
`_send_attachment()`, which runs outside `adapter.send()`.  `voice.py::speak()`
is still the only path for **synthesizing text into speech**: mute → content
policy → connection check → synthesize → vet the whole batch → send →
self-record → cleanup, and every text-driven voice *producer* in this plugin
(`qq_send_voice`'s text branch, the proactive voice lane, every line a call
session speaks) goes through it, never through `send_voice()`. Nothing here
encodes to SILK locally — the backend transcodes on its own side, which is
also why the audio quality of a delivered bubble is not something this
plugin controls.

`_send_attachment()` itself no longer skips as much as it used to: a
successful group send now records itself into the shared recent-chatter
buffer (caption when there is one, else a short `[语音]`/`[图片]`/`[视频]`/
`[文件]` marker), so the proactive lane still knows the bot just spoke even
on the file-send path, and its inline (image/audio) branch now runs the
caption — and only the caption — through the same fail-closed content
policy, refusing the whole send rather than posting media with a silently
dropped sentence. The media **bytes** themselves are still never moderated;
that remains the acknowledged `TODO(media)` gap in `adapter.send()`. Full
history of what changed and when: `docs/migration-corlinman/F1-qq-voice-notes.md`.

Speech synthesis reuses the **existing `tts:` block** in
`~/.hermes/config.yaml` — the same provider and voice every other spoken
reply already uses. No separate credential or provider setting exists for
QQ.

`qq_ai_voice` (群 AI 声聊 — Tencent's own built-in AI voice characters) is a
different thing entirely: nothing is synthesized locally, the text goes to
Tencent as plaintext and Tencent posts the audio. Its
`send_group_ai_record` / `get_ai_record` / `get_ai_characters` actions are
OneBot v11 extensions implemented by SnowLuma and NapCat; Lagrange and
go-cqhttp may not provide them. It only exists in groups; QQ has no
private-chat equivalent.

Both model-facing tools are additionally capped through the native
plugin-config path, `plugins.entries.qq_voice.settings` in
`~/.hermes/config.yaml` (`max_text_chars`, and `daily_max` for
`qq_send_voice` only). Both keys are declared in that plugin's
`config_schema`, so the loader type-checks what an operator wrote and names
the offending key by its full path — config, not an environment variable,
because these are thresholds rather than secrets. Full defaults and
reasoning in `plugins/qq_voice/README.md`, which documents both tools in
full.

### Voice calls (`voice_call.py`) — OFF by default

> **A QQ voice call in the real-time-audio sense is not reachable over
> OneBot.** OneBot backends expose messages, plus the AI-voice actions above
> and `send_poke` — no call signalling, no RTP, no media
> channel. What ships here instead is a **bounded conversational session over
> voice messages** with call semantics: an invite/ring, an accept or a
> decline, an active period, and a hangup — by either side, by going quiet,
> or by hitting a cap. Both sides are still trading voice bubbles; the value
> is the *framing*, not a lower-latency transport. Lifting this limitation
> needs a backend action that no OneBot v11 implementation exposes today —
> nothing here is a stepping stone toward one, it is the whole feature.

A call is a small per-chat state machine: `RINGING` / `INCOMING` (one side
placed or asked for a call, awaiting the other's answer) → `ACTIVE`
(accepted — while up, this chat hears the bot in voice instead of reading
text) → `ENDED` (declined, hung up by either side, or a deadline crossed:
ring timeout, idle timeout, max duration, or max turns).

* **Starting one** is programmatic: `OneBotAdapter.start_voice_call()`,
  called by a model-facing tool or the proactive-call lane below. There is
  deliberately **no natural-language "the user wants a call" detector** — a
  false positive rings a real person's phone unprompted — so
  `voice_call.offer()` (the "they asked for it" direction) exists in the
  state machine but nothing calls it yet.
* **Answering** one is natural language: while a call is `RINGING` or
  `INCOMING`, the other side's next message is read as the answer rather
  than run as a chat turn — accept vocabulary ("好的" / "接吧" / "ok"…) or
  decline/hangup vocabulary, classified conservatively (ambiguous input is
  never read as consent).
* **Ending** one is a hangup word at any point, either side going quiet past
  `voice_call_idle_timeout_secs`, an unanswered ring past
  `voice_call_ring_timeout_secs`, or hitting `voice_call_max_duration_secs` /
  `voice_call_max_turns`. A resident sweeper ticks once a second and collects
  every expired session — even with nobody talking, and even while
  `voice_call_enabled: false`, so a session left over from disabling the
  feature mid-call still gets collected — and it also runs right after every
  turn so a cap does not wait out the tick.

```yaml
platforms:
  onebot:
    extra:
      voice_call_enabled: false            # ← the switch
      voice_call_ring_timeout_secs: 60.0
      voice_call_idle_timeout_secs: 180.0
      voice_call_max_duration_secs: 900.0
      voice_call_max_turns: 40
      voice_call_allow_groups: false       # a call in a group is loud; opt in deliberately
      voice_call_allowed_users: []         # empty ⇒ anyone the adapter already lets talk to it
```

**Same YAML-only rule as `proactive_*` below, for the same reason**: the
switch deciding whether the bot rings a real person's phone lives in exactly
one place, and an env var read once at process start could never hot-apply.
A key missing from `_PRIVATE_YAML_KEYS` is silently dropped by the config
merge — for a switch like this, that means a feature that can never be
turned on.

---

## Proactive speaking (`proactive.py`) — OFF by default

> **Enabling this is a new outward behaviour, not the restoration of an old
> one.** The implementation it was ported from ran in production with
> `proactive_enabled = true` and never sent a single message, because
> `group_replies_enabled = false` silences all group speech including
> proactive posts. Setting `proactive_enabled: true` here makes the bot start
> speaking, unprompted, in whichever groups it is pointed at — for the first
> time. Full context, ported test map and known gaps:
> `docs/migration-corlinman/B4-proactive-speech-notes.md`.

A resident loop (started on connect, stopped on disconnect) that sleeps a
random gap, then posts one persona message into one eligible group — or, at
the model's own choosing, says nothing at all.

Gate ladder, in order; the tests pin it:

1. `proactive_enabled`, re-read from the live config **every beat**;
2. active hours, in an **explicit** timezone;
3. health — link up **and** the QQ account not known-offline;
4. the bot's own uin is known;
5. **the emergency mute** — `group_replies_enabled` silences proactive too;
6. `proactive_probability` — a person doesn't post every time they glance;
7. the overall daily allowance (`proactive_daily_max_total`, all groups together);
8. per group: daily budget, minimum gap, the **shared** speech cap, and
   whether anyone has spoken since our last post;
9. **the pre-judge** (`proactive_judge.py`): a separate auxiliary model —
   `auxiliary.onebot_proactive_judge` — reads the room and says whether an
   unprompted line would read as a regular member chiming in. It fails
   closed (timeout, error, wrong-shaped answer, empty buffer ⇒ no post).
   How *often* the bot speaks is the caps' job; the judge only picks moments.

```yaml
platforms:
  onebot:
    extra:
      group_replies_enabled: false     # proactive ALSO requires this true
      proactive_enabled: false         # ← the switch
      proactive_groups: []             # empty ⇒ falls back to group_whitelist
      proactive_min_gap_minutes: 45
      proactive_max_gap_minutes: 0     # < min ⇒ min × 4 (a window, not a beat)
      proactive_daily_max: 4           # per group per day
      proactive_daily_max_total: 0     # all groups together; 0 = per-group only
      proactive_judge_enabled: true    # the pre-judge (off ⇒ the old ladder)
      proactive_judge_persona: ""      # who the bot is — the main tightness knob
      proactive_active_start_hour: 9   # [start, end); start > end wraps overnight
      proactive_active_end_hour: 23
      proactive_timezone: Asia/Shanghai
      proactive_probability: 1.0       # 0.0–1.0, clamped
      proactive_context_messages: 30   # 0 disables the transcript
      proactive_prompt: ""             # blank ⇒ the ported default
```

Worth knowing:

* **One speech budget, not two.** A proactive post spends from the same
  `group_speech_allowed()` window as a reply, so "5 per 3 minutes" stays a
  promise about the bot's total volume in the room.
* **`proactive_groups` cannot reach outside `group_whitelist`.** Entries
  outside it are dropped with a warning; the whitelist is the last barrier
  between a config typo and a message in a stranger's group. If *every*
  requested group is outside it, proactive speech stays **off** rather than
  falling back to the whole whitelist — narrowing the target with a mistyped
  id must not turn into speaking in every group. (Leaving `proactive_groups`
  unset still means "all my whitelisted groups"; the two empties differ on
  purpose. This corrects a defect in the implementation this was ported from —
  see `docs/migration-corlinman/B4-proactive-speech-notes.md` §5.)
* **The model may decline.** Answering `SKIP` (or hermes' `[SILENT]` /
  `NO_REPLY`) posts nothing. This is deliberate: a bot that always has
  something to say is the most obvious kind of bot.
* **The bot will not talk to itself.** If the newest message in the group is
  its own, the group is skipped until a human speaks.
* **Hot-apply.** The loop is resident even while disabled, re-checking every
  60 s, so enabling / disabling / retuning takes effect on the next beat with
  no channel restart.
* **No `ONEBOT_PROACTIVE_*` environment form**, on purpose: the switch that
  decides whether the bot talks to real people lives in one place, and an env
  var read once at startup could not hot-apply anyway. The `proactive_voice_*`
  and `proactive_call_*` keys below, and the `voice_call_*` keys above, follow
  the identical rule for the identical reason — every one of them is YAML-only.
* **Timezone is explicit.** Unset means `Asia/Shanghai`, never the process
  zone (the production host runs `Asia/Tokyo`, one hour off the intended
  window). A bad zone name falls back loudly; no usable tz database at all
  skips the beat rather than guessing.

### Proactive voice (`proactive_voice_*`) — OFF by default

A *modality*, not a gate: it never decides whether the bot speaks, only
whether the post the group-lane ladder above already approved goes out as a
voice bubble instead of text. It ships off, and even once enabled defaults
to a `0.0` probability — two deliberate keystrokes, not one, before the bot
says anything out loud.

```yaml
platforms:
  onebot:
    extra:
      proactive_voice_enabled: false     # ← the switch
      proactive_voice_probability: 0.0   # 0.0–1.0, clamped; 0.0 ⇒ text every time
      proactive_voice_max_chars: 120     # over this, sent as text instead
      proactive_voice_provider: ""       # blank ⇒ whatever tts: is configured to use
```

Worth knowing:

* **Goes through `voice.speak()`**, the same gate order the section above
  documents — `adapter.send_voice()` is never used here either.
* **The degrade matrix is asymmetric, on purpose.** A voice bubble that could
  not be *synthesized* (no provider, a provider error, the script is over
  `proactive_voice_max_chars`) falls back to a normal text post — the beat
  still produces a message. A voice bubble that was *refused* (the emergency
  mute, or the Tencent content policy) does **not** fall back to text: the
  same words would run past the same gate through `adapter.send()` and get
  the same refusal, so a "fallback" there would either be a no-op that logs a
  scary error, or — if the two gates ever disagreed — a way to launder a
  refusal into a delivered message. Nothing is booked against the budget
  either way.

### Proactive calls (`proactive_call_*`) — OFF by default

The bot placing a call nobody asked for. This is the most intrusive thing in
this plugin — it pokes a real person's phone and then talks at them — and it
is gated hardest: it needs **both** `proactive_call_enabled` and
`voice_call_enabled` (above) switched on, it never targets a group, and it
never guesses who to call — an empty `proactive_call_targets` leaves the lane
off, with no fallback to "anyone the bot is allowed to talk to".

```yaml
platforms:
  onebot:
    extra:
      voice_call_enabled: false            # required for this lane too
      proactive_call_enabled: false        # ← the switch
      proactive_call_targets: []           # QQ uins, private chats only; empty ⇒ off
      proactive_call_daily_max: 1          # per target per day
      proactive_call_min_gap_minutes: 240
      proactive_call_active_start_hour: 10 # narrower than the group lane's 9–23 on purpose
      proactive_call_active_end_hour: 22
      proactive_call_probability: 1.0      # 0.0–1.0, clamped
      proactive_call_prompt: ""            # blank ⇒ the ported default opening-line prompt
```

Worth knowing:

* **A group id in `proactive_call_targets` is dropped, loudly** — this lane
  places private calls only; a call-shaped session in a group would put every
  bubble in front of everyone, which `voice_call_allow_groups` is a separate,
  explicit decision to make. A bare id that also happens to be a whitelisted
  *group* id is dropped the same way, on the same "wrong number" suspicion.
* **The opening line is the persona's, not the canned ring line.** Placing the
  call reuses the same `voice_call.invite()` / `perform()` machinery
  `start_voice_call()` uses, but substitutes the model's own line (composed
  from `proactive_call_prompt`) for `voice_call.RING_LINE` — a call nobody
  asked for owes the person a reason, and routing through
  `start_voice_call()` unmodified would make every unprompted call the same
  fixed sentence.
* **No emergency-mute rung.** `group_replies_enabled` is a group-speech switch
  everywhere else in this plugin; this lane never touches a group, so giving
  the same flag a fourth, wider meaning here would mean the group panic
  switch silently disables an unrelated private-chat feature.
* **Started on connect, like every other resident loop here.**
  `OneBotAdapter._start_proactive_call_loop()` creates
  `proactive.proactive_call_loop()` as a task in `connect()` and cancels it in
  `disconnect()` — always, even while `proactive_call_enabled` is `false`, so
  flipping the switch (plus `voice_call_enabled` and a non-empty
  `proactive_call_targets`) takes effect on the loop's next beat with no
  channel restart, the same hot-apply property the group text/voice lane has.

## Group-message archive (`group_history.py`) — OFF by default

> This is the **writer** for `qq_group_history.sqlite`, the store the three
> migrated QQ monitors (`sanhu` / `jlu` / `qunjlu`, in `plugins/corlinman_jobs`)
> read and that nothing in this port used to write. Without it those monitors
> go permanently, silently quiet once corlinman is decommissioned. Full
> reasoning, cutover steps and known gaps:
> `docs/migration-corlinman/D3-group-history-writer-notes.md`.

Every inbound message from a captured group is appended to a SQLite table in
corlinman's own schema — recorded **before** the reply gate, so a digest of
"what did the room say today" sees the whole room and not just the messages
the bot answered. Direct messages are never archived.

```yaml
platforms:
  onebot:
    extra:
      group_whitelist: ["183287894", "980927602"]
      group_history_enabled: false      # ← the switch
      group_history_groups: []          # empty ⇒ falls back to group_whitelist
      group_history_db: ""              # blank ⇒ $HERMES_HOME/plugin-data/corlinman_jobs/qq_group_history.sqlite
      group_history_retention_days: 7   # floor 2 — the monitors read a 24h window
      group_history_batch_rows: 200     # commit on N rows...
      group_history_flush_secs: 30      # ...or T seconds, whichever first
      group_history_queue_max: 2000     # hard bound; overflow is dropped, not buffered
```

Worth knowing:

* **Two variables, not one.** `ONEBOT_GROUP_HISTORY_DB` names the file this
  gateway *writes*; the monitors' own `QQ_GROUP_HISTORY_DB` names the file
  they *read*. During the corlinman coexistence window those are two different
  files on purpose — the reader points at corlinman's live store while the
  writer fills ours. Cutover is: back-fill ours, then move the reader onto it.
  The writer additionally **refuses** to open a WAL-mode database, because
  corlinman opens its store in WAL and this module never does: a WAL target is
  almost certainly corlinman's live file, and two writers on one SQLite file
  is the corruption case this separation exists to avoid.
* **The event loop never waits on the disk.** `record()` is a bounded
  `put_nowait`; a background thread owns the connection and commits in
  batches. On the target host SQLite stays in DELETE journal mode (3.40.1
  carries the WAL-reset corruption bug, and `hermes_state.py` declines to
  enable WAL on such builds), where every commit is a full fsync — one
  transaction per message on 2 vCPUs is not affordable.
* **Bounded, and it drops rather than grows.** A full queue drops the new row
  and counts it. The alternative — buffering behind a stalled disk — trades
  the gateway's 512 MB cap for chat lines the monitors would tolerate losing.
* **Fail-open.** No failure here reaches the inbound path: a broken archive
  costs rows, never replies.
* **DELETE, never VACUUM.** Rows past the retention horizon are deleted in
  chunks (so one prune cannot hold the write lock against a reading cron job);
  freed pages are reused rather than rewriting the whole file.
* **It complements, and does not replace, the 30-row proactive buffer.** That
  one is in-memory prompt context; this one is durable history. Both are fed
  at the same point, before the reply gate.
* **One-shot import from corlinman**, idempotent by QQ message id so a re-run
  never duplicates a row — including messages this writer already captured
  live under a different `received_at_ms`:

  ```bash
  python -m plugins.platforms.onebot.group_history_backfill \
      --source /opt/corlinman/execution-state/qq_group_history.sqlite \
      --days 7 --dry-run
  ```

## Not implemented here (deliberately)

* **Group digests** (scheduled plain-language summaries of a group's chatter)
  are out of scope for this adapter: it captures the messages, the summarising
  is `plugins/corlinman_jobs`' three cron monitors.
* **Document-corpus retrieval for proactive posts.** The source folded
  `kb.sqlite` snippets into the prompt; hermes has no such corpus and this
  port does not invent one. The snippet-folding logic and its "query on other
  people's words, never our own" rule are ported behind
  `proactive.set_context_provider()`, which is unset by default.
* **QR login / backend lifecycle management.** Out of scope by design; see the
  "never write the backend's configuration" constraint above.
* **Message editing / reactions.** QQ has no edit API.
* **Real-time audio calls.** OneBot v11 exposes no call signalling, no RTP, no
  media channel — see "Voice calls" above. `voice_call.py` ships a
  conversational session built out of voice messages instead; it is not a
  lower-latency version of the same feature in progress, because the backend
  action it would need does not exist to build on.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `onebot_auth_rejected` in the log | Wrong token — most often the WebUI token instead of the OneBot one. |
| Connects, but nothing arrives from groups | `ONEBOT_GROUP_REPLIES_ENABLED` is still `false` (the default). |
| Connects, but direct messages are never answered | `ONEBOT_DIRECT_REPLIES_ENABLED` has been explicitly set `false` (the default is `true` — answered). Likely a leftover from a receive-only observation window. |
| Connects, group is whitelisted, @mention still ignored | The group id is missing from `ONEBOT_GROUP_WHITELIST`; an @mention does not bypass it. |
| Replies stop after a few messages in one group | The hard speech cap. Check `ONEBOT_GROUP_RATE_LIMIT_*`. |
| Reconnect loop with no obvious error | The URL points at a *reverse* WebSocket entry. This adapter needs a **forward** (`websocketServers`) entry — the backend as the server, Hermes as the client. |
| Inbound images never reach the model | The backend shipped segments without a `url` (offline media), or the URL failed the SSRF safety check. |
| `HERMES_PLUGINS_DEBUG=1` | Prints plugin discovery to stderr — start here if the platform does not appear at all. |
