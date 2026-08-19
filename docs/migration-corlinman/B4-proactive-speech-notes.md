# B4 — Proactive group speech, ported to hermes

> ## ⚠️ Read this first
>
> **This feature ships OFF, and turning it on is a NEW outward behaviour — not
> the restoration of an old one.**
>
> corlinman's production config sets `proactive_enabled = true`. It has never
> sent a single message. A second switch, `group_replies_enabled = false` (the
> emergency mute), silences *all* group speech including proactive posts:
>
> ```python
> # service.py:1223
> # The emergency mute silences ALL group speech, proactive included.
> if not bool(_attr(params.config, "group_replies_enabled", True)):
>     continue
> ```
>
> Corroboration: `journalctl -u corlinman.service --since "7 days ago" |
> grep -ic proactive` → **0**.
>
> So there is **no production behaviour to reproduce, only a mechanism to
> port**. Setting `proactive_enabled: true` makes the bot begin speaking,
> unprompted, into **five real QQ groups** for the first time. Anyone who
> describes this migration as "proactive speech restored" is wrong: nothing
> was ever lost, and enabling it introduces outward behaviour those five
> groups have never seen.
>
> Acceptance for this task was therefore done against the **source code and
> the ported test cases**, never against production logs — there are none.

---

## 1. What landed

| File | Lines | What |
|---|---:|---|
| `plugins/platforms/onebot/proactive.py` | 797 (new) | Config resolution, the gate ladder, prompt composition, the SKIP hatch, the resident loop. |
| `plugins/platforms/onebot/adapter.py` | +85 / −7 | Lifecycle wiring (start on connect, stop on disconnect), context-buffer hygiene, the `proactive_*` YAML keys. |
| `plugins/platforms/onebot/README.md` | +85 / −9 | Documents the feature; removes the "not implemented" entry. |
| `tests/gateway/test_onebot_proactive.py` | 879 (new) | 78 tests, fully offline. |

No file outside `plugins/platforms/onebot/` and `tests/gateway/` was touched.
`plugins/platforms/onebot/` is a migration-created directory; the invariant
that this migration makes **zero edits to pre-existing hermes files** still
holds.

## 2. How it works

One resident asyncio task per adapter, started in `connect()` and stopped in
`disconnect()`. Each beat sleeps a *random* gap, then walks a fixed ladder:

| # | Gate | Notes |
|---|---|---|
| 1 | `proactive_enabled` | Re-resolved from live config **every beat** |
| 2 | active hours | Explicit timezone; see §4 |
| 3 | health | `link_online` up **and** `account_online` not false |
| 4 | identity | the bot's own uin is known |
| 5 | **emergency mute** | `group_replies_enabled` — silences proactive too |
| 6 | probability | `proactive_probability` — a person doesn't post every glance |
| 7 | per-group eligibility | daily budget · min gap · **shared** speech cap · "did a human speak since we did" |

Then: compose prompt → one agent turn → SKIP check → send → book the budget.

The loop runs **even while the feature is off**, idling on a 60-second
re-check (`IDLE_RECHECK_SECS`). That is what makes enabling, disabling and
retuning hot-apply without restarting the channel — the property
`test_qq_hot_apply.py` existed to protect, ported as `TestHotApply`.

### Things shared with the reactive reply path, on purpose

* **The speech cap.** One process-wide `SlidingWindowCounter`
  (`adapter._GROUP_SPEECH`, reached through `group_speech_allowed()`), peeked
  with `record=False` during eligibility and `record()`-ed only after a
  message actually goes out. Two counters would quietly double how much the
  bot says in a group. (D33; promised in `README.md:201` by B2.)
* **The context buffer.** `adapter._GROUP_RECENT` holds inbound member
  messages *and* the bot's own posts. Both uses matter: it is the transcript
  the persona reads, and it is how the loop knows the bot spoke last.
* **Outbound shaping.** The post goes out through `adapter.send()` — the same
  call the reply path uses — so `[MSG_BREAK]` bubbles, the 3800-char chunker,
  the merged-forward card and the self-recording all behave identically. A
  private send path here is exactly how `[MSG_BREAK]` ends up printed
  literally in a QQ message.

### The SKIP hatch

The model may answer `SKIP` to stay quiet, and the composed prompt tells it
so. This is the "like a person" bit and it is not optional: without it the bot
always says *something*, which is the single most botlike thing it can do.

The port **widens** the hatch: `is_skip()` accepts the source's `SKIP`
vocabulary *and* hermes' own autonomous-lane silence markers (`[SILENT]`,
`NO_REPLY`) via `gateway.response_filters.is_autonomous_silence_response`,
which cron and the webhook lane already teach models to emit. A model fluent
in either idiom gets the escape hatch. An empty answer counts as silence too.

## 3. Configuration

Under `platforms.onebot` in `config.yaml` (top level or under `extra:`; both
are merged). **All keys are optional and the feature is off without
`proactive_enabled`.**

```yaml
platforms:
  onebot:
    extra:
      # The emergency mute. Proactive speech ALSO requires this to be true.
      group_replies_enabled: false

      proactive_enabled: false          # ← the switch. Off = silent.
      proactive_groups: []              # empty ⇒ falls back to group_whitelist
      proactive_min_gap_minutes: 45
      proactive_max_gap_minutes: 0      # < min ⇒ min × 4 (a window, not a beat)
      proactive_daily_max: 4            # per group per day
      proactive_active_start_hour: 9    # [start, end); start > end wraps overnight
      proactive_active_end_hour: 23
      proactive_timezone: Asia/Shanghai # explicit; see §4
      proactive_probability: 1.0        # 0.0–1.0, clamped
      proactive_context_messages: 30    # 0 disables the transcript entirely
      proactive_prompt: ""              # blank ⇒ the ported default
```

Every default above is the source's, read out of `service.py` — except
`proactive_timezone`, see §4.

**There is deliberately no `ONEBOT_PROACTIVE_*` environment form.** Every other
adapter key has one, and this one does not, for two reasons: the switch that
decides whether a bot talks to real people should live in exactly one place,
and an environment variable is read once at process start, so it could not
hot-apply anyway — it would be a key that silently disagrees with the
documented behaviour of every other proactive key.

## 4. Timezone (D32, and constraint 4)

`proactive_timezone` defaults to **`Asia/Shanghai`**, and the active-hours
window uses the configured literal `9–23`.

* The source defaulted to `""` ⇒ **process-local time**. The production host
  runs `Asia/Tokyo` while the business day is Beijing time, so an implicit
  fallback there is a silent one-hour drift — a landmine this migration has
  already stepped on twice.
* No `-1h` compensation was applied (unlike D25 for the QQ monitors). D25's
  premise is "there is an observed firing time to reproduce". There isn't one
  here: the feature has never fired. With no observation, the only honest
  basis is the config's stated intent, which is 9–23 Beijing.
* Recorded difference: had the source ever run, it would have fired at JST
  9–23 = Beijing **8–22**. This port fires at Beijing 9–23.
* A bad zone name falls back to `Asia/Shanghai` **loudly**, never to the
  process zone. If there is no usable tz database at all, `now_parts()`
  returns `None` and the beat is skipped — staying silent beats posting at an
  hour nobody asked for.

## 5. The whitelist is the last line of defence (D34)

`proactive_groups` entries outside `group_whitelist` are dropped with a
warning. Ported verbatim from `service.py:882` ("@mentions don't bypass the
whitelist — proactive speech must not either").

One inherited surprise, ported deliberately and pinned by
`test_groups_entirely_outside_whitelist_fall_back_to_it`: if *every* entry in
`proactive_groups` is outside the whitelist, the filtered list becomes empty
and then hits the "empty ⇒ use the whitelist" rule. So the bot speaks in the
whitelisted groups rather than in the requested one. Surprising, but safe by
construction — the outcome can never leave the whitelist, which is the
property that matters. Changing it would be inventing behaviour, so it was
left as-is and documented instead.

## 6. Retrieval — the one deliberate design divergence

**Choice: (a-adapted) — use hermes' native memory path, keep the source's
retrieval logic behind an unwired seam, and record the corpus gap honestly.**

The source folded up to 3 snippets from the gateway's `kb.sqlite` document
corpus into the prompt. Hermes has nothing equivalent, and the brief forbids
inventing one.

What was considered and rejected:

* **Wire C1's `plugins/grantley/memory_provider.py` as the retrieval
  backend.** Rejected on inspection: `GrantleyMemoryProvider.prefetch()`
  **ignores its `query` argument entirely** — it returns live persona state
  plus salience-ranked life events. It is a state injector, not a retriever.
  Calling it as if it were a RAG backend would be a false equivalence, and it
  already runs on every turn anyway.
* **Build a corpus.** Out of scope, and forbidden by the brief.

What was done instead:

1. The proactive turn is an ordinary hermes agent turn, so the Grantley memory
   provider injects live state and salient life events exactly as it does for
   a reply. No extra call needed — it comes for free with the turn.
2. The prompt keeps the source's line telling the model it may recall relevant
   memories before deciding what to say. Pull-based recall by the model
   replaces push-based retrieval by the channel loop, and the model already
   has its memory tools in a normal turn.
3. The snippet-folding code and the source's sharpest insight — **query on
   other people's words only, never the bot's own, or the persona ends up
   retrieving its own echo** — are ported intact behind
   `proactive.set_context_provider()`, which is **unset by default**. Nothing
   calls it today.

**Recorded gap:** with no provider attached, no `资料库` section is emitted.
A future corpus plugs in with one call and inherits the ported query rule,
the top-3 cap and the 300-char snippet cap, all already tested.

## 7. Ported test coverage

`tests/gateway/test_onebot_proactive.py`, 78 tests, all offline.
Command and result: `.venv/bin/python -m pytest tests/gateway/test_onebot_proactive.py -q`
→ **78 passed**. Whole OneBot suite (7 files): **382 passed**.

Ported from `test_qq_proactive.py` (all 34 cases): config resolution and
defaults, whitelist intersection, probability clamping, timezone/context
parsing, active-hours (normal / overnight / degenerate), the delay draw,
cancel-aware sleep, SKIP variants and non-variants, context rendering, the
`你自己` self label, last-message-is-self, snippet folding and caps, the
human-chatter-only retrieval query, daily budget roll-over, speech-window
parsing, bubble splitting on send, and every loop gate (post + budget,
emergency mute, probability 0, model SKIP, speech cap, min gap, bot-spoke-last,
post-recorded-as-self, retrieval reaching the prompt, retrieval failure being
non-fatal).

Ported from `test_qq_speech_cap.py`: the shared-budget property, restated as
`test_the_post_spends_the_shared_speech_budget` — a proactive post makes the
*reactive* path see one unit spent. The source's dispatch-loop cap tests were
**not** re-ported: B2 already covers that half in
`tests/gateway/test_onebot_plugin.py::TestGroupSpeechCap`, and re-testing it
here would duplicate, not verify.

Ported from `test_qq_hot_apply.py`: `TestProactiveHotApply` (enable and
disable mid-loop). The file's other classes — `TestDispatchGateHotApply` and
`TestMonitorHotApply` — were **not** ported: the first is the reactive router
(B2's scope, and hermes' router reads its gates per event already), the second
is the group-digest monitor, which is not part of B4.

Added beyond the source, because the port introduces the risk:

* the timezone fallback goes to `Asia/Shanghai`, not process-local, and `None`
  when there is no tz database at all;
* hermes' `[SILENT]` / `NO_REPLY` markers also trip the SKIP hatch;
* the synthetic event pings nobody (`onebot_at_user_id` absent) and cannot be
  read as a gateway slash command (`allow_gateway_control=False`);
* a reconnect does not start a second speaker;
* the emergency mute is honoured from **both** the live config and the reply
  path's own router flag, so the two lanes can never disagree;
* a blank inbound message (sticker, recall) does not count as "a human spoke"
  and so cannot unblock a post.

## 8. Known gaps and residual risk

1. **No document-corpus retrieval.** §6. The seam exists; nothing is attached.
2. **No end-to-end run against a real group.** By design and by constraint —
   every test is offline. The first real post will happen the first time
   somebody sets `proactive_enabled: true` with `group_replies_enabled: true`,
   and it will be genuinely novel behaviour. Recommend enabling in a single
   test group with `proactive_probability` low and `proactive_daily_max: 1`
   before anything wider.
3. **`event.channel_prompt` is not set** — by the reactive path either. C1's
   `plugins/grantley/channel_binding.py` documents that the OneBot adapter
   *should* set `event.channel_prompt = resolve_channel_prompt(binding)` on
   each inbound message; B2 did not wire it. Proactive deliberately does not
   wire it either: doing it on one lane only would make the persona's
   per-channel framing differ between a reply and a proactive post. **This is
   a pre-existing B2/C1 integration gap, not a B4 regression**, but B4 is
   where it becomes load-bearing (proactive posts are pure persona output with
   no user message to carry the framing).
4. **Media directives are stripped, not delivered.** A proactive turn that
   somehow produces `MEDIA:` tags or image URLs has them stripped by the
   adapter's own `extract_media` / `extract_images`; the media itself is
   dropped rather than sent. A proactive chat message should be text, and
   silently posting a file into a group is worse than not posting it.
5. **Budget state is process-local.** Daily counters and last-post clocks live
   in module dicts. A gateway restart resets them, so a restart loop could in
   principle exceed `proactive_daily_max` in a day. Identical to the source,
   which made the same trade for the same reason (state that survives a
   *channel* restart, not a *process* restart).
6. **Turn concurrency.** The proactive turn does not take the adapter's
   `max_concurrency` semaphore — it is at most one turn per adapter and runs
   at a 45-minute-plus cadence, so it cannot fan out. It can, however, run
   concurrently with up to `max_concurrency` reactive turns, briefly making
   three where the config says two. On a 1.9 GB host that is worth knowing;
   it was judged acceptable because the alternative (holding the semaphore
   across a whole proactive turn) would let a slow proactive turn delay a real
   user's reply.

## 9. Decisions this task made on its own

| # | Decision | Why |
|---|---|---|
| B4-1 | Generate by calling `adapter._message_handler(event)` directly, not `handle_message()` | The answer must be inspected for SKIP *before* delivery; `handle_message` sends it for us. Cron and the webhook lane call the handler directly for the same reason. |
| B4-2 | Synthetic event uses `user_id="proactive"` | It lands in the session key, so the proactive thread never barges into a session a human is mid-way through — the source's "dedicated proactive session" property, expressed in hermes' own session-key vocabulary. |
| B4-3 | Send through `adapter.send()` rather than a private send path | Constraint 2 in spirit: bubbles, chunking and self-recording must be one implementation, not two. |
| B4-4 | Emergency mute requires **both** the live config value and the reply path's router flag | Reading only the live value could let proactive speak while replies are muted, if the two ever diverge. Both-must-be-on can only ever be stricter. |
| B4-5 | No `ONEBOT_PROACTIVE_*` env keys | §3. |
| B4-6 | Retrieval seam unwired by default | §6. |
| B4-7 | `record_group_message()` now drops blank text and caps at 200 chars | It is prompt input, and a blank inbound entry would read as "a human spoke" to the anti-spam gate. Matches the source's `_qq_record_group_message`. |
