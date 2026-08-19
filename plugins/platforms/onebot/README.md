# QQ via OneBot v11 (NapCat / Lagrange / go-cqhttp)

Platform name: **`onebot`**.

> **This is not the `qqbot` platform.** Hermes ships `gateway/platforms/qqbot/`,
> which speaks Tencent's *official* QQ Bot API v2 (`api.sgroup.qq.com`, app id +
> client secret, official bot accounts). This plugin speaks the community
> **OneBot v11** protocol to a local bridge driving an **ordinary QQ account**.
> Different protocol, different credentials, different capabilities. Configuring
> one when you meant the other fails in confusing ways, so the names are kept
> deliberately distinct.

## Topology

```
   QQ  ──  NapCat (or Lagrange / go-cqhttp)  ──ws──>  Hermes onebot adapter
                    forward WebSocket server            (dials out; binds nothing)
                    default ws://127.0.0.1:3001
```

Hermes is always the **client**. It opens no listening port, so it works behind
NAT and needs no inbound firewall rule.

### Hard constraint: never write the backend's configuration

This adapter reads the WebSocket URL and access token and **does nothing else to
the backend**. It never calls NapCat's WebUI API, and in particular never calls
`/api/OB11Config/SetConfig`.

That is not squeamishness. On the deployment this was written for, another
service rewrites NapCat's OneBot config on a timer; a second writer would fight
it and can knock the live QQ service offline. (The neighbouring service's own
NapCat *manager* is, separately, in a permanent crash loop on that host while
the NapCat container itself is healthy — more reason not to imitate or take over
that machinery.) If you need a dedicated token or port for Hermes, add a second
`websocketServers` entry **by hand in the NapCat WebUI** and point
`ONEBOT_WS_URL` at it; that entry is then unmanaged by anything else.

### Two different secrets

`ONEBOT_ACCESS_TOKEN` is the **OneBot** access token (the `token` field of the
NapCat `websocketServers` entry). The NapCat **WebUI** token is a *separate*
secret. Using the WebUI token here produces a permanent handshake rejection; the
adapter detects that case and reports `onebot_auth_rejected` instead of
reconnecting forever.

---

## Quick start

```bash
# ~/.hermes/.env
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=<the OneBot token, not the WebUI token>
```

That is enough to answer **direct messages**. Groups stay silent until you flip
the master switch below.

---

## ⚠️ The group master switch

```
ONEBOT_GROUP_REPLIES_ENABLED   default: false
```

**Every group message is dropped while this is `false`** — before the mention
check, before the keyword check, before rate limiting. Direct messages are
unaffected.

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
| `ONEBOT_HOME_CHANNEL` | *(none)* | Default target for cron / notification delivery. |
| `ONEBOT_HOME_CHANNEL_NAME` | = id | Display label for the home channel. |

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

Direct messages bypass the whole group chain. They are still subject to the core
`ONEBOT_ALLOWED_USERS` allowlist.

### Outbound behaviour

| Key | Default | Meaning |
|---|---|---|
| `ONEBOT_REPLY_WITH_MENTION` | `true` | Prefix group replies with `@sender`. Only the **first** chunk is ever prefixed — repeating it would ping the user once per chunk, which QQ renders as spam. |
| `ONEBOT_FORWARD_THRESHOLD` | `1000` | Fold a reply bubble longer than this into a merged-forward ("聊天记录") card. `0` disables. In groups a short lead line carrying the @mention is posted first, because a card cannot carry one. |
| `ONEBOT_WAIT_FOR_SEND_ACK` | `true` | Wait for the echo-correlated response so failures are classified and message ids reported. A backend that never echoes is treated as an optimistic success rather than a false failure. |
| `ONEBOT_TYPING_INDICATOR` | `true` | NapCat's "对方正在输入…" in DMs. QQ groups never render one, so nothing is sent there. |
| `ONEBOT_MAX_CONCURRENCY` | `2` | Concurrent agent turns. Tuned for a 2 vCPU / 2 GB host; the upstream implementation used 8. |
| `ONEBOT_HEALTH_PROBE_S` | `30` | Health check interval. |
| `ONEBOT_HEALTH_LOST_S` | `120` | Silence before the link is reported lost. |

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
direct message, at most 5 messages per group per 3 minutes, and the master
switch off so cutover reproduces current behaviour exactly.

`~/.hermes/.env` (secrets only):

```bash
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=<OneBot token from the NapCat websocketServers entry>
```

`~/.hermes/config.yaml`:

```yaml
platforms:
  onebot:
    enabled: true
    extra:
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

1. **Direct message** → always dispatched.
2. **Group**:
   1. `group_replies_enabled` — off ⇒ drop (an @mention cannot punch through);
   2. whitelist — a hard gate; an @mention does **not** bypass it, and an empty
      whitelist blocks every group;
   3. explicit summons = @mention **or** a slash command;
   4. otherwise the reply policy (keywords), then the per-group cooldown.
3. Empty text (pure sticker, recall placeholder) → drop.
4. Per-group and per-sender token buckets — **after** the gate, so a filtered
   message never consumes anyone's budget.
5. **Hard speech cap** — after any slash-command handling, before the model call.
   Operator commands are never locked out; a capped group never burns an LLM
   call; @mentions do not bypass it (they *do* reset the cooldown clock).

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
7. per group: daily budget, minimum gap, the **shared** speech cap, and
   whether anyone has spoken since our last post.

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
  var read once at startup could not hot-apply anyway.
* **Timezone is explicit.** Unset means `Asia/Shanghai`, never the process
  zone (the production host runs `Asia/Tokyo`, one hour off the intended
  window). A bad zone name falls back loudly; no usable tz database at all
  skips the beat rather than guessing.

## Not implemented here (deliberately)

* **Group digests** (scheduled plain-language summaries of a group's chatter)
  are out of scope for this adapter.
* **Document-corpus retrieval for proactive posts.** The source folded
  `kb.sqlite` snippets into the prompt; hermes has no such corpus and this
  port does not invent one. The snippet-folding logic and its "query on other
  people's words, never our own" rule are ported behind
  `proactive.set_context_provider()`, which is unset by default.
* **QR login / NapCat lifecycle management.** Out of scope by design; see the
  "never write the backend's configuration" constraint above.
* **Message editing / reactions.** QQ has no edit API.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `onebot_auth_rejected` in the log | Wrong token — most often the WebUI token instead of the OneBot one. |
| Connects, but nothing arrives from groups | `ONEBOT_GROUP_REPLIES_ENABLED` is still `false` (the default). |
| Connects, group is whitelisted, @mention still ignored | The group id is missing from `ONEBOT_GROUP_WHITELIST`; an @mention does not bypass it. |
| Replies stop after a few messages in one group | The hard speech cap. Check `ONEBOT_GROUP_RATE_LIMIT_*`. |
| Reconnect loop with no obvious error | The URL points at a *reverse* WebSocket entry. This adapter needs a **forward** (`websocketServers`) entry — NapCat as the server, Hermes as the client. |
| Inbound images never reach the model | The backend shipped segments without a `url` (offline media), or the URL failed the SSRF safety check. |
| `HERMES_PLUGINS_DEBUG=1` | Prints plugin discovery to stderr — start here if the platform does not appear at all. |
