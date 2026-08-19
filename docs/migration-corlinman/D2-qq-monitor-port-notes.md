# D2 — corlinman QQ monitors → hermes-native cron

Three QQ group-digest monitors ran on the corlinman production host, a
config-driven subsystem separate from the twelve scheduler jobs D1 ported
(`[[channels.qq.instances.default.monitors]]` in `config.toml`, not
`scheduler_runtime_jobs.json`; A1 §4). **All three are ported.** They join
D1's nine jobs inside the same `plugins/corlinman_jobs/` plugin — same
installer, same preflight, same manifest, same `hermes corlinman-jobs`
CLI — rather than a parallel plugin or a second command surface.

Every job is created **paused**, exactly like D1's nine. Nothing in this
package can enable one.

---

## 1. What changed

No new files. Every change extends a file D1 already delivered:

| Path | What was added |
|---|---|
| `plugins/corlinman_jobs/specs.py` | `MONITOR_SPECS` (3 `JobSpec`s), `MONITOR_NAMES`, `ALL_SPECS = JOB_SPECS + MONITOR_SPECS` |
| `plugins/corlinman_jobs/prompts.py` | `QQ_MONITOR_STYLE_PROMPT` / `QQ_MONITOR_FOCUS_PROMPT` (VERBATIM, see §2.4) + `qq_monitor_digest()` |
| `plugins/corlinman_jobs/preflight.py` | `check_qq_group_history()`, `run_checks()` gained `include_qzone` / `include_qq_history` |
| `plugins/corlinman_jobs/installer.py` | `qq_group_history_db_path()`, `needs_qzone()`, `needs_qq_history()`, broadened `needs_qq()`, a `script_call` case for the three monitors, every default `specs=` parameter moved from `JOB_SPECS` (9) to `ALL_SPECS` (12) |
| `plugins/corlinman_jobs/scripts/corlinman_jobs_lib.py` | `main_qq_monitor_digest()` + four ported helpers (`_qq_monitor_window_desc`, `_qq_monitor_collection_ids`, `_qq_monitor_query`, `_qq_monitor_format_lines`) |
| `plugins/corlinman_jobs/plugin.yaml` | `QQ_GROUP_HISTORY_DB` documented under `optional_env`, description updated |

`hermes corlinman-jobs plan|install|status` now cover all twelve jobs. No
new CLI surface, no new subcommand, no new plugin.

---

## 2. The three monitors

| monitor | source group(s) | filter | delivery target | source `daily_time` | **actual trigger (China time, D25)** | **migrated schedule** |
|---|---|---|---|---|---|---|
| `qunjlu` | `183287894` | only `1076712858` | back into group `183287894` — **suppressed, §4** | 09:00 | **08:00** | `5 8 * * *` (08:05) |
| `sanhu` | `980927602` | everyone | private chat `2104743984` | 10:00 | **09:00** | `5 9 * * *` (09:05) |
| `jlu` | `183287894` | everyone, `1076712858` ★-marked | private chat `2104743984` | 11:00 | **10:00** | `5 10 * * *` (10:05) |

Common to all three (verbatim, A1 §4 / `.migration-export/config/config.toml`
L59-L110): `schedule_type="daily"`, `window_minutes=1440`,
`send_when_empty=false`, `timezone=""` (unset), `style_extra=""`,
`instance_id="default"`.

### 2.1 Why minus one hour (D25)

None of the three monitors set an explicit `timezone`, and
`[channels.qq.instances.default]` sets no `proactive_timezone` either — the
one fallback `_qq_monitor_tzinfo` would otherwise reach for. So all three
evaluated their daily `HH:MM` against the **host's process-local zone**,
confirmed `Asia/Tokyo` (JST, +0900), not `Asia/Shanghai`. Nominal 09:00 /
10:00 / 11:00 therefore actually fired at 08:00 / 09:00 / 10:00 China time.

Once this plugin pins `HERMES_TIMEZONE=Asia/Shanghai` (the same contract D1
already established for the nine scheduler jobs — `preflight.check_timezone`
refuses to install otherwise), a literal port of the configured `HH:MM`
would shift all three schedules an hour **later** than what production
actually did. D25 (Orchestrator, already decided before this task started):
**port the observed behaviour, not the literal config value** — the same
principle D16 applied to the Telegram jobs' chat id. The recipient of
`sanhu`/`jlu`, user `2104743984`, has been receiving these digests at the
compensated times for a long time; reproducing the literal `HH:MM` would be
a real-world one-hour drift for that person, not a neutral technical choice.

**This compensation applies to these three monitors only.** The eight
`hermes.*` jobs D1 ported all declared `source_timezone="Asia/Shanghai"`
explicitly and are reproduced verbatim, with no adjustment.

### 2.2 The new minutes, and why

Occupied before this task (D1's nine, from `specs.py`'s existing
`stagger_reason`s): `07:07`, `09:09`, `09:24`, `21:24`, `13:33`, `15:12`,
`22:21`, `23:06`, `23:41`, and hourly `:17` (`persona.decay`).

- **`qunjlu` — 08:00 → 08:05.** No D1 job runs in the 08:00 hour; the only
  things to clear are the hourly `:17` tick and `:00` itself.
- **`sanhu` — 09:00 → 09:05.** The 09:00 hour already carries
  `hermes.competition_daily` (`:09`) and `hermes.qzone_reply` (`:24`); `:05`
  sits ahead of both with ≥4 minutes of clearance and is clear of `:17`.
- **`jlu` — 10:00 → 10:05.** No D1 job runs in the 10:00 hour; same
  reasoning as `qunjlu`.

`tests/plugins/corlinman_jobs/test_specs.py::TestInvariantStagger` now
checks all twelve schedules together — no job on the hour, no two jobs on
the same minute, `persona.decay` keeps ≥4 minutes of margin from everything
— so a future edit that lands a new job on `:05`, `:09` or `:17` fails a
test rather than silently contending for SQLite's DELETE-mode write lock
(P1).

### 2.3 `qunjlu` stays suppressed (D26)

Production's `[channels.qq.instances.default]` carries
`group_replies_enabled = false`. Read directly off the running source
(`corlinman-channels/src/corlinman_channels/service.py:2076-2081`,
`_qq_monitor_run_once`):

```python
if spec.target_type == "group" and not bool(
    _attr(params.config, "group_replies_enabled", True)
):
    # The emergency mute silences ALL group speech — scheduled
    # digests to a group included (private-chat digests still go).
    raise RuntimeError("group replies disabled")
```

This check runs **before** any history is fetched or any text generated.
`qunjlu`'s digest — the only one of the three targeting a group — has never
once reached group `183287894`. `sanhu`/`jlu` target a private chat and are
unaffected.

**The mechanism does not carry over as-is.** This port's OneBot adapter
(`plugins/platforms/onebot/`) only reads `group_replies_enabled` in two
places — `router.py` (passive replies to inbound messages) and
`proactive.py` (proactive speech). `adapter.py`'s `send()` — the function a
cron job's `deliver=onebot:g...` target actually calls — does **not**
consult it at all; verified by reading `adapter.py:1258-1340`, where `send`
resolves the target and writes segments with no config check in between.
Copying the flag's name into `qunjlu`'s spec would therefore recreate,
exactly, the failure class this migration has hit five times already
(00-PLAN.md §7/§14/§12's "P2"): *"config says off, behaviour says on"*
— except here the direction is inverted (a QQ-facing switch that looks like
it should gate a send, but doesn't).

**Mechanism actually used — two independent, structural checks, neither
reading a runtime config value:**

1. `qunjlu.deliver == "local"`. `cron._resolve_delivery_targets` returns an
   empty target list for `deliver == "local"` unconditionally
   (`cron/scheduler.py:2153-2154`) — no matter what the model's turn
   produces, nothing is auto-delivered anywhere. This is the primary,
   load-bearing mechanism.
2. `qunjlu.enabled_toolsets == ()`. The model has no tool capable of
   sending a QQ message on its own, so even an unexpected model action
   cannot reach the outside world.

The script still runs on schedule and still produces a real digest — it
just never leaves `$HERMES_HOME/cron/output/<qunjlu job id>/`, visible via
`hermes cron logs qunjlu`. This lets an operator see what the digest would
have looked like before deciding whether to unsuppress it, and costs one
cheap model turn a day whether or not it is ever read.

**How to lift the suppression, when the business decides to:**

1. Edit `plugins/corlinman_jobs/specs.py` — change `qunjlu`'s `deliver`
   from `"local"` to `"onebot:g183287894"` (optionally give it the `onebot`
   toolset too, though delivery works without it).
2. `hermes cron rm qunjlu` — D1's installer never updates an existing job
   (`test_installer.py::TestIdempotency::test_reinstalling_never_re_enables_an_operator_enabled_job`),
   so the old paused job has to be removed first.
3. `hermes corlinman-jobs install --only qunjlu`, then `hermes cron resume
   qunjlu`.

That is a reviewed code change through the normal commit/deploy path, not a
config flag an operator can flip by accident — deliberately, per D26 ("不得
'顺手放开'").

### 2.4 The prompt is VERBATIM, sourced by direct inspection

Unlike `hermes.qzone_reply`'s RECONSTRUCTED prompt (D1 §4, A5 §3.12 —
that source file was never exported), the monitor digest's style
instructions were read **character-for-character** off the running
production checkout over a read-only SSH session against `corlinman-prod`
during this task:

```
_QQ_MONITOR_STYLE_PROMPT   service.py:1339-1349
_QQ_MONITOR_FOCUS_PROMPT   service.py:1351-1356
```

(`corlinman-channels/src/corlinman_channels/service.py`, a private module
global never exported to this repository or to any prior migration
document — A1 §4 records the monitors' *config*, not this prompt text.)
Two source sentences are intentionally not carried over:
`_qq_monitor_compose_prompt`'s "multiple groups, keep them separate"
instruction, and `_QQ_MONITOR_REDUCE_FOCUS_PROMPT` (map-reduce only — see
§3). All three migrated monitors have exactly one source group each, so
neither omission changes any monitor's behaviour.

Same structural gap as every other job in this plugin (D1 §3.1's
system-prompt folding): the source ran monitor digests as a **neutral,
persona-free** chat turn (`_qq_monitor_generate` passes `persona_id=None`
deliberately — not `grantley`). hermes cron has no per-job persona
override, so the migrated job still inherits whatever system prompt the
profile configures underneath this instruction block. Not a new
limitation; documented here because it is easy to miss for a job whose
source specifically avoided the persona.

---

## 3. The map-reduce path is not reproduced — documented, not silent

The source split any window over 1,000 messages
(`_QQ_MONITOR_CHUNK_MESSAGES`) into 1,000-message chunks, summarised each
chunk with a **parallel chat turn** (`asyncio.gather` over its own chat
service, `_qq_monitor_summarize`), then ran one more turn to merge the
partial summaries. A hermes cron job gets exactly **one** model call —
there is no seam here for a script to launch several concurrent LLM turns
of its own outside that call. Doing so would mean the job quietly starts
making its own paid model calls from Python, invisible to hermes's own
accounting of what a cron run costs — a materially different architecture,
not "port a job onto the established pattern."

**Consequence, and it is real:** on a day where a monitor's window holds
more than `QQ_MONITOR_PROMPT_MESSAGE_CAP` (1,000) messages, this port keeps
only the **newest** 1,000 and marks the digest
`仅展示最新一部分，更早的消息未纳入本次汇总`. This is expected most days for
`sanhu`: group `980927602` alone produced 45,578 of the 52,649-row export
(roughly 15,000/day), so its window will hit this cap on almost every real
run — confirmed against the migrated snapshot during this task (see §6).
`jlu`'s group (`183287894`, everyone) is smaller (~1,500/day) but can also
exceed the cap on a busy day; `qunjlu`'s filtered-to-one-sender window is
far smaller and unlikely to ever hit it.

The cap constant and the reasoning above live next to the code:
`corlinman_jobs_lib.QQ_MONITOR_PROMPT_MESSAGE_CAP`.

---

## 4. The data source has no writer in this port — the largest known gap

`qq_group_history.sqlite`'s `group_messages` table (schema:
`corlinman_server/qq_group_history.py`) is the **only** data source for all
three monitors. In production it is populated by `_qq_dispatch_loop`
(`service.py:2694-2716`), which records *every* inbound message from a
monitored group — including ones the reply router itself would filter out.

**This port has no equivalent writer.** `plugins/platforms/onebot/`'s own
group-message buffer (`adapter.record_group_message` /
`adapter.recent_group_messages`) is an **in-memory**, 30-message,
process-lifetime ring buffer for the proactive-speech context window
(`proactive.py`) — it is never persisted to SQLite, was never designed to
be a monitor data source, and building a persistent capture pipeline was
out of scope for this task (D2's brief is the three monitors' *schedule
migration*, not a new ingestion feature; B2 did not build one either).

Practical consequence, spelled out because it changes what "installing
these three jobs" actually buys an operator:

- **During the coexistence window** (corlinman still running on the same
  host, D1 §4.5), pointing `QQ_GROUP_HISTORY_DB` at corlinman's own live
  file (`/opt/corlinman/execution-state/qq_group_history.sqlite`) gives the
  monitors fresh data — reading it is safe: `QqGroupHistory` opens the file
  in WAL mode and this port only ever opens it `mode=ro`.
- **Once corlinman is decommissioned**, nothing writes new rows into that
  table anymore. The store's own retention is ~3 days
  (`_QQ_MONITOR_DEFAULT_RETENTION_HOURS = 72.0`, enforced by corlinman's own
  pruning — this port does not prune, since it never writes). Without a
  successor writer, all three monitors degrade to permanently-empty digests
  (silent, by design — `send_when_empty=false` — but silent all the same)
  within roughly that window after cutover.
- **Not building that pipeline is a scope decision, not an oversight,
  called out here so it is not discovered as a surprise weeks after
  cutover.** The clean next step, if wanted, is a persistence hook inside
  `plugins/platforms/onebot/adapter.py`'s inbound path, writing into the
  same `group_messages` schema this port already reads — flagged as a
  follow-up, not attempted here.

`preflight.check_qq_group_history()` reports row count and reachability at
plan/install/status time so this is visible before cutover, but it cannot
detect "the store exists and has rows, but nothing has written a new one in
four days" — that would need a max-timestamp staleness check this task did
not add (see §7).

---

## 5. Trade-offs, stated plainly

- **`writes_public_feed=False` for all three**, even `qunjlu`. They send a
  QQ chat message (group or private), not a QQ空间 public post —
  `plugins/qzone` is never touched by this code, and there is no equivalent
  of an un-deletable public feed entry. `dry_run_agent_safe` is therefore
  `True` for all three (consistent with D1's own `writes_public_feed` ↔
  `dry_run_agent_safe` invariant, asserted in
  `test_specs.py::TestInvariantNothingEnabled`).
- **`enabled_toolsets=()` for all three**, matching `hermes.diary_summary`'s
  precedent (D1 §3.4) and the source's own "neutral, tool-free turn"
  design. Translated by the installer's existing `NO_TOOLS_SENTINEL`
  mechanism into `["no_mcp"]` on the stored job — the same trap D1 already
  found and fixed (an empty list is falsy and would silently fall back to
  *more* tools, not fewer); this port reused that fix rather than
  rediscovering it.
- **`preflight.run_checks()` gained two new optional parameters**
  (`include_qzone`, `include_qq_history`), decoupled from the pre-existing
  `include_qq`. Before this task, "needs onebot" and "needs the qzone
  ledgers migrated" were the same flag, because every prior QQ-touching job
  used the qzone toolset. `sanhu`/`jlu` need onebot connectivity but never
  touch `plugins/qzone`; `qunjlu` needs neither onebot nor qzone but does
  need the history store. Left coupled, `hermes corlinman-jobs plan --only
  sanhu` would have spuriously demanded migrated qzone dedup ledgers for a
  job that never calls a qzone tool. Both new parameters default to
  mirroring `include_qq` when not given explicitly, so every existing
  caller that only ever set `include_qq` — including every test written
  before this task — keeps behaving exactly as before; only the installer's
  own calls (computed per selected spec set) pass all three explicitly.
- **Timestamps in the rendered digest use `Asia/Shanghai`**, not each
  message's original JST-implied rendering. This is a deliberate,
  system-wide consequence of pinning one declared zone across the whole
  plugin (D8) — a cosmetic difference in how a timestamp prints, not a
  change to which messages fall inside the 24-hour window.
- **`qunjlu`/`sanhu`/`jlu` are separate names, not `hermes.`-prefixed.**
  D1's nine jobs kept corlinman's own `scheduler_runtime_jobs.json` `name`
  field verbatim (which already carried a `hermes.` prefix in the source).
  The three monitors' own source identity is their bare config `id` field
  (`"sanhu"`, literally) — there is no separate prefixed name to preserve,
  and the migration contract this task was handed (D25/D26, and the table
  in the task brief) refers to them by these bare names throughout, so
  renaming them would break that 1:1 mapping for no benefit.

---

## 6. Verification performed this task

All of the following ran against the real, exported production snapshot
(`.migration-export/sqlite/qq_group_history.sqlite`, 52,649 rows,
2026-08-15→19) with no network access:

- `main_qq_monitor_digest` against `sanhu`'s real group (980927602):
  produces a 1000-line, truncation-flagged digest — confirming the busy-day
  cap in §3 is not a hypothetical.
- Against `qunjlu`'s filter (`watch_user_ids=["1076712858"]` on group
  `183287894`): correctly narrows to that sender only (168 rows in the
  snapshot).
- Against `jlu`'s filter (no watch, focus `1076712858`): correctly collects
  everyone and ★-marks the focus member's lines, hitting the same
  truncation cap.
- An empty-result filter (a sender not present in the window): confirmed
  empty stdout + a stderr diagnostic, nothing on stdout.
- A full `hermes corlinman-jobs install` through the **real CLI** (not just
  the Python API) against an isolated `$HERMES_HOME`, plugin enabled via
  `config.yaml: plugins.enabled`, all preflight env vars set including
  `QQ_GROUP_HISTORY_DB` pointed at the real exported snapshot: all twelve
  jobs created paused, `hermes corlinman-jobs status` reports
  `qq_group_history.sqlite reachable, 52649 row(s)` and correctly staggered
  `next_run_at` timestamps for all three monitors (`2026-08-20T08:05:00+08:00`
  etc.).
- `hermes plugins list` (real scanner, isolated `HERMES_HOME`): plugin still
  discoverable, unaffected by this task's changes.

---

## 7. Known defects and residual risk

### 7.1 Inherited from the architecture, not from a source bug

- **No writer feeds `qq_group_history.sqlite` in this port** — the single
  largest gap; see §4 in full. Not something this task's scope covered
  (schedule migration, not a new ingestion feature); flagged loudly rather
  than left to be discovered after cutover.
- **The map-reduce summarisation path is not reproduced** — see §3.
  `sanhu` in particular will lose the older two-thirds of a normal day's
  messages to the newest-1000 cap, most days.

### 7.2 Introduced by this port

- **`check_qq_group_history` cannot detect staleness**, only reachability
  and row count. A store that stopped receiving new rows four days ago
  (exactly the failure mode in §4 once corlinman is retired) reports `OK`
  with a large row count, because every one of those rows is real — just
  old. A `MAX(received_at_ms)` staleness check would close this; not added
  here to keep this task's scope to the schedule migration it was asked
  for, flagged as a natural, low-risk follow-up.
- **No `--only`-scoped variant of "everything is ready"** beyond what D1
  already built: `plan`/`install`/`status` with no `--only` now cover all
  twelve jobs uniformly, which is the intended behaviour, but it does mean
  a profile missing `QQ_GROUP_HISTORY_DB` blocks a full install even for an
  operator who only cares about the nine scheduler jobs — `--only` (already
  built by D1) is the escape hatch, unchanged by this task.

### 7.3 Not done, on purpose

- **No uninstall / no lift-suppression subcommand.** Rollback for the three
  monitors is the same as D1's: `hermes cron rm <name>` +
  `rm $HERMES_HOME/scripts/corlinman_qq_monitor_*.py`. Lifting `qunjlu`'s
  suppression is a code change (§2.3), not a subcommand, deliberately.
- **No agent dry-run** for the monitors either — same as D1's nine, for the
  same reason: nothing to suppress via a dry run once `deliver=local` (or
  `enabled_toolsets=()`) already makes the risky action structurally
  unreachable, so a dry-run flag would add a code path without adding
  safety.

---

## 8. Cutover addendum (extends D1 §5)

Before installing, in addition to D1's own checklist:

```bash
# The three monitors' only data source. During coexistence, point this at
# corlinman's own live file so the digests are not built from an aging
# snapshot (§4):
QQ_GROUP_HISTORY_DB=/opt/corlinman/execution-state/qq_group_history.sqlite
```

`sanhu`/`jlu` need `ONEBOT_WS_URL`/`ONEBOT_HTTP_URL` (already required for
D1's three qzone jobs — nothing new to configure if those are already
enabled). `qunjlu` needs neither onebot connectivity nor the qzone ledgers,
only the history store.

Enabling order, extending D1 §5.3's blast-radius-ascending list: `sanhu` and
`jlu` are private-chat digests with no toolset — the same risk class as
D1's steps 3-6 (spend tokens, but only read/summarise, no external write
beyond the one cron delivery itself) — so they can be resumed in that same
early batch, independent of the QQ-write jobs (steps 7-9). `qunjlu` is not
part of any enabling sequence: resuming it changes nothing observable
(§2.3) until the separate code change described there is made and
deployed.

```bash
hermes cron resume sanhu
hermes cron resume jlu
hermes cron trigger sanhu   # fire once, check `hermes cron logs sanhu`
hermes cron trigger jlu
```

`qunjlu` can be resumed at any point with zero externally-visible effect
(D26) — there is no ordering constraint to observe for it specifically.

---

## 9. Verification

```
.venv/bin/python -m pytest tests/plugins/corlinman_jobs/ -q
    → 291 passed

.venv/bin/python -m pytest tests/plugins/corlinman_jobs tests/plugins/qzone \
                          tests/plugins/grantley tests/cron -q
    → 1452 passed, 1 skipped
```

Entirely offline: no SSH, no HTTP, no QQ session, no Telegram call. The
`.migration-export/sqlite/qq_group_history.sqlite` reads in the manual
verification pass (§6) used the already-exported, gitignored local copy —
not a live connection.
