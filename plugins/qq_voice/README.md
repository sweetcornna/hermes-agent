# QQ Voice (`plugins/qq_voice/`)

Two model-facing tools over a QQ account already bridged through OneBot v11
(SnowLuma / NapCat / Lagrange / go-cqhttp): `qq_send_voice` sends a native QQ voice bubble (语音消息);
`qq_ai_voice` posts a voice message spoken by one of QQ's own built-in AI
voice characters (群 AI 声聊). Bundled and auto-loaded (`kind: backend`, like
`plugins/qzone/`), gated on a configured OneBot endpoint, no separate
installation step.

This is the tool layer. For the adapter-side voice path these tools call into
— `voice.speak()`, inbound transcription, voice call sessions — see
`plugins/platforms/onebot/README.md`'s "Voice — messages and calls" section
and `docs/migration-corlinman/F1-qq-voice-notes.md`.

## Layout

```
plugins/qq_voice/
├── plugin.yaml    kind: backend (bundled, auto-loaded)
├── __init__.py    register(ctx) — two ctx.register_tool calls
├── common.py      id coercion, target resolution, audio vetting,
│                  synthesis, and the live-vs-direct transport choice
├── quota.py       the text-length cap and the daily send budget
├── send_voice.py  qq_send_voice
└── ai_voice.py    qq_ai_voice
```

Why a plugin and not `tools/*.py`: every model tool registered in the core is
sent on every API call, so the bar for core surface is high and this pair
clears none of it — it is useful only on an install with a QQ account bridged
through OneBot. As a plugin the tools carry a `check_fn`, so when no OneBot
endpoint is configured they never enter the model's schema and cost exactly
zero tokens (see "Availability" below).

They register into the `onebot` toolset, which the framework folds into the
auto-generated `hermes-onebot` toolset for the registered OneBot platform.
That keeps the pair attached to the platform whose login state it borrows and
needs no edit to `toolsets.py`.

## `qq_send_voice`

Send a voice message to a QQ user or group — synthesized from text with the
configured TTS provider, or an existing local audio file.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string | one of `text` / `audio_file` | Synthesized with the user's configured TTS provider and voice. |
| `audio_file` | string | one of `text` / `audio_file` | Path to an existing local audio file: `mp3`, `wav`, `ogg`, `amr`, `silk`, `m4a`, `flac`, `aac`, `opus`; 8 MiB max. |
| `user_id` | string | one of `user_id` / `group_id` | Target QQ number for a private-chat voice message. |
| `group_id` | string | one of `user_id` / `group_id` | Target QQ group number for a group-chat voice message. |

Exactly one of `text` / `audio_file`, and exactly one of `user_id` /
`group_id`; both empty or both set is refused as `invalid_args` rather than
guessed — a wrong guess posts a voice message to a chat, or in a form, nobody
asked for.

## `qq_ai_voice`

Post a voice message in a QQ group spoken by one of QQ's own AI voice
characters. Tencent synthesizes the audio server-side from a `character_id`
and the text; nothing is synthesized locally and no audio passes through this
process.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `group_id` | string | yes | QQ AI voice characters exist only in groups — there is no `send_private_ai_record`. |
| `text` | string | when `character` is given | The words the character should speak. |
| `character` | string | no | A `character_id` from a previous listing call. Omit to list the group's available characters instead of guessing one. |

Called without `character`, the tool lists what the group offers
(`get_ai_characters`) instead of sending anything — inventing a
`character_id` gets rejected by the backend with a bare retcode the model
cannot read, so discovery is folded into the same tool rather than split into
a second one. A `user_id` argument is refused by name, pointing at
`qq_send_voice`, rather than silently ignored.

Two platform constraints, both Tencent's rather than this plugin's:

* **Group-only** — no `send_private_ai_record` exists.
* **OneBot v11 extension** — `send_group_ai_record` / `get_ai_characters` are
  implemented by SnowLuma and NapCat; Lagrange and go-cqhttp may reject them.
  Every failure from this tool is annotated with that likely cause, because a
  raw `retcode=1200` tells the model nothing actionable on its own.

## `delivery_path`: gateway vs. direct

Every result that got as far as choosing a transport — success or failure —
carries a `delivery_path` field. A refusal raised before that point (invalid
arguments, either cap, a failed synthesis) carries none: there is no path to
name yet, and naming one anyway would tell the reader a send was attempted
when none was. The difference between the two is not cosmetic:

* **`"gateway"`** — a OneBot adapter is live in this process (the normal case
  when the Hermes gateway is running), so the send was handed to
  `plugins/platforms/onebot/voice.py`. The emergency group mute, the Tencent
  content policy, and the recent-chatter self-recording all applied, in that
  order, exactly as they do for any other spoken reply.
* **`"direct"`** — no gateway is running in this process (CLI, `hermes cron`),
  so none of that state exists to apply, and the send went out best-effort
  over the plain OneBot transport (`tools/onebot_client.py::onebot_call`).
  This is the ordinary answer there, not a degraded one — but a caller who
  cares whether a muted group or a policy-refused sentence could still get
  through should read `delivery_path` before trusting a success.

A reader should care about this distinction because the two paths do not
offer the same guarantees: `"gateway"` means a pressed emergency-mute switch
or a Tencent-policy refusal would have stopped the send before a byte left
the process; `"direct"` means there was no mute and no policy to consult in
this process in the first place, so the send simply went out. Which path runs
is decided automatically (`common.live_adapter()`) — nothing about the call
chooses it — and it also decides which underlying function does the work:
`voice.speak()` (text) / `voice.speak_ai_character()` on the gateway path,
versus a hand-built `send_msg` / `send_group_ai_record` call on the direct
path.

## No credentials of its own

The QQ session belongs to the running OneBot backend. This plugin
needs `ONEBOT_WS_URL` (or `ONEBOT_HTTP_URL`) — shared with the `onebot`
platform plugin and with `plugins/qzone/`, so do not point it at a second
backend — and optionally `ONEBOT_ACCESS_TOKEN` (the **OneBot** token, not the
NapCat WebUI token or SnowLuma WebUI password; they are separate secrets).

## Availability: zero token cost without a configured endpoint

Both tools carry a `check_fn` (`_check_qq_voice_available`) that requires
`onebot_configured()` to be true. Without a configured OneBot endpoint the
tools never enter the model's schema at all — not "return an error when
called", genuinely absent from every API call — so an install with no QQ
account bridged pays nothing for carrying this plugin.

## Speech synthesis

`qq_send_voice`'s `text` path reuses the existing `tts:` block in
`~/.hermes/config.yaml` — whatever provider and voice the operator already
configured for spoken replies elsewhere. No separate TTS credential or
provider setting is introduced here, and `qq_ai_voice` does not use it at
all: that tool's audio is synthesized by Tencent, not by this process.

## Caps: a length limit and a daily budget

Every *autonomous* voice path in this install is already bounded —
`proactive_daily_max` (4 per group per day), `proactive_call_daily_max` (1 per
day), `proactive_voice_max_chars` (120), and the adapter's shared
sliding-window speech cap. The model-callable tools were the one unbounded
path, and on a deployment with a paid per-synthesis provider
(`tts.provider: fish`) each `qq_send_voice` call is a line on an invoice.
`quota.py` closes that.

```yaml
# ~/.hermes/config.yaml — the whole block is optional
plugins:
  entries:
    qq_voice:
      settings:
        max_text_chars: 200  # perceived characters per spoken message; 0 = no cap
        daily_max: 30        # qq_send_voice sends per local day;       0 = no cap
```

`plugins.entries.<id>.settings` is the native plugin-config path — the one
`PluginContext.get_config` reads, and the only one the loader type-checks
against a plugin's declared `config_schema`. Both keys are declared in
[`plugin.yaml`](plugin.yaml) with these defaults and a description each, so a
`max_text_chars: "soon"` is reported at load as
`plugins.entries.qq_voice.settings.max_text_chars should be int (got str)`
rather than silently doing nothing. A plugin owns no top-level `config.yaml`
key, so there is deliberately **no** top-level `qq_voice:` section: a stray one
is ignored, and the caps stay at their defaults.

`quota.py::_settings` reads the block with `cfg_get(load_config(), ...)` rather
than through `ctx`, because a tool handler runs long after `register(ctx)`
returned and holds no context — the same shape as
`plugins/grantley/__init__.py::load_plugin_config`. An absent block yields `{}`
and the defaults above apply, so nothing is registered in `DEFAULT_CONFIG` and
an operator who has never heard of this plugin still gets the caps. Re-read on
**every** tool call, so raising the budget mid-session applies to the next call
rather than the next restart. `0` or a negative value means "no cap", the same
spelling `plugins/platforms/onebot/rate_limit.py::SlidingWindowCounter.allow`
uses for its own `max_count <= 0`. These are thresholds, not secrets, so they
are config keys and not environment variables (AGENTS.md, "What we don't
want"); the OneBot URL and access token go the other way for the same reason —
they are a credential pair belonging to the backend, so they stay in `.env`.

| Cap | `qq_send_voice` | `qq_ai_voice` |
|---|---|---|
| `max_text_chars` | yes | **yes** |
| `daily_max` | yes (both the `text` and the `audio_file` branch) | **no** |

The split is not an oversight — the two caps exist for two different reasons.
`max_text_chars` is a promise to the *listener*: a multi-minute voice bubble
cannot be skimmed, searched or re-read, and that is equally true whether we
synthesized it or Tencent did, so it covers both tools. Exempting `qq_ai_voice`
would also hand the model an easy way around the rule — say the wall of text
as an AI character instead. `daily_max` is a cost control on *us*, and
`qq_ai_voice` costs this install nothing per call (群 AI 声聊 is synthesized on
Tencent's side), so charging it against a paid budget would make the model
ration the wrong resource and let a free tool starve the paid one. Group
chattiness, the other thing a daily count could bound, is already the
adapter's job on the gateway path. `qq_ai_voice` results therefore carry no
`voice_quota` block; there is no budget there to report.

Within `qq_send_voice`, the budget covers the `audio_file` branch too. That
branch runs no synthesis of its own, but it is still a voice bubble in a real
chat, and exempting it would leave an obvious bypass: synthesize with the
`text_to_speech` tool — which bills identically — and post the resulting file.

### The counter

`<hermes home>/plugin-data/qq_voice/voice_quota.json`, holding
`{"date": "YYYY-MM-DD", "count": N}` — the same two fields the reference
deployment wrote, so an existing file can be copied straight in. The directory
comes from `plugins.plugin_storage.plugin_data_dir`, this repo's convention for
plugin-owned durable state and the one `plugins/qzone/state.py` already uses;
it resolves through `get_hermes_home()` per call, so the budget follows the
active profile.

* **Local days, not UTC.** A budget that resets at 09:00 because the box runs
  in UTC is a budget the operator cannot reason about. The reference
  deployment reset locally too.
* **Charged only once a message is — or may be — in the chat.** A synthesis
  failure, an emergency mute, a content-policy refusal and a backend rejection
  all leave the counter untouched. A *partly* delivered multi-chunk direct
  send is charged, once: those bubbles are audible and cannot be recalled.
* **One unit per tool call**, not per audio chunk. Long text comes back from
  the TTS backend as several files and goes out as several bubbles, but the
  model asked for one message and the operator's budget is stated in messages.
* **The gateway timeout is charged.** That is the one error that costs budget,
  and it is a choice between two wrong answers. The coroutine may be
  mid-`send_msg`, so nobody knows. Not charging is the worse option: the
  conditions that produce a timeout are the ones that repeat, so a model
  retrying past one could post an unbounded run of paid voice bubbles while
  the counter never moves — the cap failing open on exactly the path it exists
  to bound. Charging costs at most one send for a message that never arrived,
  it is visible in the `voice_quota` block on that very error, and the
  operator can raise the limit. `plugins/qzone/state.py::mark_comment` makes
  the same call for the same reason.
* **A damaged counter never blocks a send.** A missing, empty, truncated or
  malformed file reads as "nothing sent today" and logs one warning. Refusing
  every voice message because a *counter* is corrupt is a worse outcome than
  briefly over-spending a soft budget; the file is rewritten whole on the next
  successful send.
* **Concurrency.** Two tool calls can overlap, so every increment is a
  read-modify-write under an in-process lock plus an `flock`'d sidecar,
  written with `utils.atomic_json_write` — the same pattern, for the same
  reason, as `plugins/qzone/state.py`. The pre-check is a *peek* (no
  reservation), matching `SlidingWindowCounter.allow(record=False)`, so two
  simultaneous calls at `limit - 1` can overshoot by one. That is the right
  trade: reserving up front means refunding on failure, and a refund lost to a
  crash burns budget for a message that never existed.

### What the model sees

Every `qq_send_voice` result — success or failure — carries the budget, so the
model can pace itself without a second call:

```json
"voice_quota": {"used_today": 4, "limit": 30, "remaining": 26}
```

`remaining` is `null` when the cap is off (`limit` `0`). Over either cap the
tool returns a `text_too_long` or `quota_exhausted` error whose text is
guidance rather than a wall — it says to send the message as text instead,
because whether to answer in voice at all was the persona's own choice in the
first place.

## Nothing here retries

A silently retried send double-posts, and a voice bubble is the one message
form a reader cannot skim past twice. A timeout on either delivery path is
reported as an unknown outcome — the coroutine or the request may still be
mid-flight — rather than attempted again; the model has to decide what to do
next, the same way it would for any other unresolved send. `qq_send_voice`'s
direct path, sending several chunked files for long synthesized text, reports
exactly which message ids already went out if a later chunk fails, rather
than retrying the whole batch and risking a repeat of the ones that landed.

## Risk

These tools drive an unofficial automation path over an ordinary QQ account,
not Tencent's official Bot API. They can fail on a stale OneBot login state
or when Tencent's risk control intervenes, and a voice message posted into a
real chat cannot be unsent from here — there is no delete tool on either
side of this plugin. Automating an ordinary QQ account this way carries an
account-ban risk under Tencent's Terms of Service; that risk is inherent to
the automation, not something this plugin's error handling can remove.
