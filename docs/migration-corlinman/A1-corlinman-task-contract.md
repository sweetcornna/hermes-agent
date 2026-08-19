# Corlinman Scheduler — Migration Contract (A1)

Revision 3 — a second review caught that this agent (and a parallel audit) had been analyzing run history from `/opt/corlinman/data/scheduler.sqlite`, which turned out to be a **dead, frozen snapshot**: production split its runtime-state storage on 2026-07-27 and that file stopped being written at that moment. All run-history-dependent conclusions in Revision 2 (shadow/live status, "22 days of silence", `hermes.youtube_daily`'s failure rate) have been **redone against the live file**, `/opt/corlinman/execution-state/scheduler.sqlite`, independently queried by this agent via `sqlite3.connect("file:...?mode=ro", uri=True)` over `ssh corlinman-prod`. Job-definition content (§1's storage-location analysis, §2's 8 raw `hermes.*` job bodies, §3's behavioral specs) required no changes from that split and is carried forward unedited except where new evidence added detail. Secrets remain `***REDACTED***`.

---

## 1. Job definition storage

Job definitions live in **three merged sources**, assembled fresh at every gateway boot (`gateway/lifecycle/entrypoint.py` → `_effective_scheduler_config()`), plus a fourth thing worth calling out separately: the **import tool** that actually populated source (a) with the 8 `hermes.*` jobs. **Read this whole section before building any migration ETL — see the storage-split warning at the end.**

**(a) `<data_dir>/scheduler_runtime_jobs.json`** — production: `/opt/corlinman/data/scheduler_runtime_jobs.json`. Flat JSON: `{"version": 2, "jobs": [ {...}, ... ]}`. Each object: `name`, `cron` (5-field croniter grammar), `action_type` (`"<plugin>.<tool>"` slug), `timezone`, `enabled`, `persona_id`, `prompt_template`, `qq_account`, `qq_instance_id`, `metadata` (free-form dict — the real parameter bag most builtins read), `execution_mode` (`live`|`shadow`), `source_system`, `source_job_id` (idempotency key), `last_run_at_ms/ok/qzone_url/error`, `created_at_ms`, `updated_at_ms`. This is the **only durable store for admin/API-created ("runtime") jobs** — all 8 `hermes.*` jobs live here.
- Read/write code: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/infra/_scheduler_lib.py` (`_runtime_jobs()`, `_rehydrate_runtime_jobs()`, `_persist_runtime_job_rows()`, `_store_job()`).
- Write path: the admin HTTP API in `.../infra/scheduler.py` (`POST/PATCH/DELETE /admin/scheduler/jobs[/{name}]`, `.../pause`, `.../resume`, qzone-template-enable, QQ-instance-migration).
- Loaded lazily into `AdminState.extras["scheduler_runtime_jobs"]` on first access; rehydrated and every enabled job's tick loop re-registered on the live `SchedulerHandle` at boot via `rehydrate_runtime_jobs_on_boot()`.
- **This file lives under `CORLINMAN_DATA_DIR` (`/opt/corlinman/data/`) only — confirmed still true after the storage split** (re-verified this session: `find /opt/corlinman/data /opt/corlinman/execution-state -maxdepth 1 -iname 'scheduler_runtime_jobs*'` returns two hits, both under `/opt/corlinman/data/`, none under `execution-state/`). `AdminState.data_dir` (the admin-routes' handle) resolves to `CORLINMAN_DATA_DIR`, not `CORLINMAN_EXECUTION_STATE_DIR` — job *definitions* and job *run history* are governed by two different environment variables and live in two different directories on this deployment. See the warning below.

**(b) In-code hardcoded defaults — never persisted.** Six `_register_default_*_job()` functions in `gateway/lifecycle/scheduler_integration.py` build a `SchedulerJob` in memory at every boot, unless an operator `[[scheduler.jobs]]` TOML entry of the same name already exists. **Confirmed** (full production `config.toml` read in Revision 2, re-confirmed unaffected by the storage split): no `[scheduler]`, `[system.update_check]`, `[persona.life_advance]`, `[evolution.engine]`, or `[evolution.shadow]` section exists anywhere in it, so **every one of these six defaults runs with its unmodified repo default**:
  - Unconditional (always registered): `persona.decay`, `evolution.darwin_curate`, `system.update_check` (gated only on `[system.update_check].enabled`, defaults `true` when the section is absent).
  - Default-**off**, gated on their own flags (all default `false` when absent): `persona.life_advance`, `evolution.engine_run_once`, `evolution.shadow_test`. **Confirmed never registered on this deployment** (zero rows for any of the three in the live `scheduler_runs` table either — see §2).

**(c) `[[scheduler.jobs]]`** in `config.toml` — parsed by `_scheduler_job_from_config_entry()`. **Confirmed zero entries.**

**(d) The real creation path for the 8 `hermes.*` jobs** — a **private, out-of-repo CLI import tool**, not a live external system. Located at `/opt/corlinman-private/import_jobs.py` (`click` command `migrate-hermes-jobs`), part of a private scheduler-builtin bundle wired via a systemd drop-in:
```
/etc/systemd/system/corlinman.service.d/hermes-migration.conf   (and an identical drop-in for corlinman-agent.service)
  Environment=CORLINMAN_SCHEDULER_PRIVATE_PATH=/opt/corlinman-private
  Environment=CORLINMAN_SCHEDULER_PRIVATE_MODULES=corlinman_private_jobs
```
Read by `scheduler/builtins/registry.py::load_private_builtin_modules()` at gateway boot. `import_jobs.py`'s `migrate-hermes-jobs` command:
1. Takes `--source-jobs` (a read-only exported `jobs.json` from the user's prior personal "Hermes" task/reminder system — the predecessor of *this* `hermes-agent` project) and `--metadata` (a secret-free per-source-id metadata map).
2. Validates against a **hardcoded 8-entry table** (`_ACTIONS`) of `(name, action_type, cron, expected_enabled)` — the authoritative migration inventory.
3. Builds import rows with **`execution_mode` hardcoded to `"shadow"`** on every import — an operator must separately flip a job to `live` afterward (via `PATCH /admin/scheduler/jobs/{name}`).
4. `merge_runtime_jobs()` performs an idempotent upsert into `scheduler_runtime_jobs.json` keyed on `(source_system, source_job_id)`.
5. `--apply` writes atomically; without it, dry-run report only.

### ⚠ Storage split — `CORLINMAN_DATA_DIR` vs `CORLINMAN_EXECUTION_STATE_DIR`

Production runs with **two separate state directories**, both set as plain `Environment=` lines directly in the base `/etc/systemd/system/corlinman.service` unit (not a drop-in — confirmed by reading the effective unit with `systemctl cat corlinman.service`):
```
Environment=CORLINMAN_DATA_DIR=/opt/corlinman/data
Environment=CORLINMAN_EXECUTION_STATE_DIR=/opt/corlinman/execution-state
```
This is a designed-in feature of the codebase (`corlinman_runtime.resolve_execution_state_dir`, referenced from `gateway/lifecycle/entrypoint.py`: `execution_state_dir = resolve_execution_state_dir(data_dir=resolved_data_dir)`), not something unique to this deployment — but on **this** production host the two directories hold **disjoint** live vs. dead data:

| | `/opt/corlinman/data/` (`CORLINMAN_DATA_DIR`) | `/opt/corlinman/execution-state/` (`CORLINMAN_EXECUTION_STATE_DIR`) |
|---|---|---|
| `scheduler_runtime_jobs.json` | **present, live** (job *definitions* — mtime 2026-07-31) | absent |
| `scheduler.sqlite` (run history) | **dead** — 1563 rows, last write 2026-07-27 01:03:39 UTC | **live** — 838 rows as of this session, last write 2026-08-18 15:30:02 UTC |
| `config.toml` | live (the only copy) | — |
| owning group | `corlinman` | `corlinman-execution` (setgid dir, `drwxrws---`, shared with `corlinman-agent.service`; a separate drop-in `shared-umask.conf` sets `UMask=0007` specifically so both services can write files the other can read) |

Root cause, reconstructed from the gateway source: `SchedulerStore.open()` is called in `entrypoint.py` against `execution_state_dir / "scheduler.sqlite"` (not `data_dir`), so **every scheduler firing since the split has recorded its outcome in the new file**; the old file is a frozen artifact of whatever `data_dir`-rooted scheduler store predated the split (or a one-time copy) and has not been written to since 2026-07-27 01:03:39 UTC, ~37 minutes before the coordinator-reported split time of 01:40 UTC.

**⚠ Any migration ETL that points at `/opt/corlinman/data/scheduler.sqlite` for run-history/telemetry will silently ingest a three-week-stale snapshot with no error, no warning, and no obviously-wrong row count** (1563 real rows, not zero or corrupt — it just quietly stopped growing). This exact mistake fooled two independent audit passes on this document before being caught. **Job *definitions* (`scheduler_runtime_jobs.json`, `config.toml`) are unaffected — they only ever lived under `data_dir`** and remain the correct source for §1/§2's definitional content. Only *run-history* queries need to target `execution_state_dir`.

`scheduler.sqlite` schema (`SCHEDULER_SCHEMA_SQL` in `scheduler/persistence.py`, identical in both copies) — two tables, run-history + delivery-idempotency, **no job-definition columns**: `scheduler_runs(id, job_name, run_id, action_kind, outcome_kind, error_kind, exit_code, duration_ms, fired_at_ms, result_json, execution_mode, scheduled_for_ms, occurrence_key)` and `scheduler_effects(...)`.

Other sqlite files under `/opt/corlinman/data/` and `/opt/corlinman/execution-state/` were listed but not further schema-inspected beyond `scheduler.sqlite` and the `scheduler_runtime_jobs.json` presence check.

---

## 2. Job definitions

| Job name | Cron | TZ | Enabled | action_type | Registered builtin? | Source |
|---|---|---|---|---|---|---|
| `persona.decay` | `0 0 */1 * * * *` | 7-field, UTC (hourly) | always | `persona.decay` | yes (in-repo) — **but see live-history note: 100% failing, see below** | in-code default |
| `system.update_check` | `0 0 */6 * * * *` (interval_hours=6, confirmed default) | 7-field, UTC | true (confirmed default) | `system.update_check` | yes (in-repo) | in-code default |
| `evolution.darwin_curate` | `0 30 3 * * * *` | 7-field, UTC (daily 03:30) | always | `evolution.darwin_curate` | yes (in-repo) — **100% failing, see below** | in-code default |
| `hermes.competition_daily` | `0 9 * * *` | 5-field, Asia/Shanghai | true | `briefing.competition_daily` | yes — private plugin | `scheduler_runtime_jobs.json` |
| `hermes.diary_summary` | `30 23 * * *` | 5-field, Asia/Shanghai | true | `personal.diary_summary` | yes — private plugin | `scheduler_runtime_jobs.json` |
| `hermes.daily_agenda` | `0 7 * * *` | 5-field, Asia/Shanghai | **false** | `personal.daily_agenda` | yes — private plugin (never fires, disabled) | `scheduler_runtime_jobs.json` |
| `hermes.qzone_daily` | `0 22 * * *` | 5-field, Asia/Shanghai | true | `qzone.daily_publish` | yes (in-repo, `qzone_daily.py`) | `scheduler_runtime_jobs.json` |
| `hermes.qzone_reply` | `0 9,21 * * *` | 5-field, Asia/Shanghai | true | `qzone.reply_comments` | yes (in-repo, `qzone_reply.py`) | `scheduler_runtime_jobs.json` |
| `hermes.qzone_friends` | `30 13 * * *` | 5-field, Asia/Shanghai | true | `qzone.comment_friends` | yes — private plugin | `scheduler_runtime_jobs.json` |
| `hermes.youtube_daily` | `0 23 * * *` | 5-field, Asia/Shanghai | true | `briefing.youtube_daily` | yes — private plugin — **100% failing in live data, see below** | `scheduler_runtime_jobs.json` |
| `hermes.analysis_digest` | `0 15 * * *` | 5-field, Asia/Shanghai | true | `personal.analysis_digest` | yes — private plugin | `scheduler_runtime_jobs.json` |
| `grantley.qzone_reply` | unrecoverable | unrecoverable | unrecoverable | `qzone.reply_comments` (confirmed) | yes (in-repo) | partially reconstructed; **dormant since 2026-07-26, zero rows in live history** |

Nothing in the raw definitions below was affected by the storage-split discovery — reproduced verbatim, unchanged from Revision 2, per the contract's requirement to never summarize away parameter values.

### Raw definitions

**`persona.decay`** (in-code, `_register_default_persona_decay_job`):
```python
SchedulerJob(name="persona.decay", cron="0 0 */1 * * * *",
             action=JobAction.run_tool(plugin="persona", tool="decay"))  # timezone unset → UTC
```

**`system.update_check`** (in-code, `_register_default_update_check_job`; `interval_hours` confirmed default 6):
```python
SchedulerJob(name="system.update_check", cron="0 0 */6 * * * *",
             action=JobAction.run_tool(plugin="system", tool="update_check"))
```

**`evolution.darwin_curate`** (in-code, `_register_default_darwin_curate_job`):
```python
SchedulerJob(name="evolution.darwin_curate", cron="0 30 3 * * * *",
             action=JobAction.run_tool(plugin="evolution", tool="darwin_curate"))
```

**The 8 `hermes.*` jobs — verbatim from `/opt/corlinman/data/scheduler_runtime_jobs.json` (current, "live" state; note this file is unaffected by the storage split — see §1):**
```json
{
  "version": 2,
  "jobs": [
    {
      "name": "hermes.competition_daily", "cron": "0 9 * * *",
      "action_type": "briefing.competition_daily", "timezone": "Asia/Shanghai", "enabled": true,
      "persona_id": null, "prompt_template": null, "qq_account": null, "qq_instance_id": null,
      "metadata": { "telegram_chat_id": -1003990634877, "telegram_topic_id": 13 },
      "execution_mode": "live", "source_system": "hermes", "source_job_id": "ead0ccfdbd38"
    },
    {
      "name": "hermes.diary_summary", "cron": "30 23 * * *",
      "action_type": "personal.diary_summary", "timezone": "Asia/Shanghai", "enabled": true,
      "persona_id": null, "prompt_template": null, "qq_account": null, "qq_instance_id": null,
      "metadata": { "telegram_chat_id": -1003990634877, "telegram_topic_id": 11, "user_id": "1114483029" },
      "execution_mode": "live", "source_system": "hermes", "source_job_id": "5a2aa0aaa7de"
    },
    {
      "name": "hermes.daily_agenda", "cron": "0 7 * * *",
      "action_type": "personal.daily_agenda", "timezone": "Asia/Shanghai", "enabled": false,
      "persona_id": null, "prompt_template": null, "qq_account": null, "qq_instance_id": null,
      "metadata": { "telegram_chat_id": -1003990634877, "telegram_topic_id": 12, "agenda_path": "scheduler_data/class_schedule.yaml" },
      "execution_mode": "live", "source_system": "hermes", "source_job_id": "fc6c8be7d0cb"
    },
    {
      "name": "hermes.qzone_daily", "cron": "0 22 * * *",
      "action_type": "qzone.daily_publish", "timezone": "Asia/Shanghai", "enabled": true,
      "persona_id": null, "prompt_template": null, "qq_account": null, "qq_instance_id": "default",
      "metadata": {
        "persona_id": "grantley", "qq_account": "1010679324",
        "prompt_template": "用今日的视角写一条 200 字以内的 QQ 空间说说。语气轻松自然，结合此刻生活状态，避免重复近期内容；结尾调用 qzone_publish 发布。",
        "qq_instance_id": "default"
      },
      "execution_mode": "live", "source_system": "hermes", "source_job_id": "1d116b77bed7"
    },
    {
      "name": "hermes.qzone_reply", "cron": "0 9,21 * * *",
      "action_type": "qzone.reply_comments", "timezone": "Asia/Shanghai", "enabled": true,
      "persona_id": null, "prompt_template": null, "qq_account": null, "qq_instance_id": "default",
      "metadata": { "persona_id": "grantley", "qq_account": "1010679324", "max_replies": 3, "lookback_posts": 15, "qq_instance_id": "default" },
      "execution_mode": "live", "source_system": "hermes", "source_job_id": "3d43e796bdc4"
    },
    {
      "name": "hermes.qzone_friends", "cron": "30 13 * * *",
      "action_type": "qzone.comment_friends", "timezone": "Asia/Shanghai", "enabled": true,
      "persona_id": null, "prompt_template": null, "qq_account": null, "qq_instance_id": null,
      "metadata": { "persona_id": "grantley", "owner_uin": "2104743984", "qq_instance_id": "default" },
      "execution_mode": "live", "source_system": "hermes", "source_job_id": "63c47a8759a3"
    },
    {
      "name": "hermes.youtube_daily", "cron": "0 23 * * *",
      "action_type": "briefing.youtube_daily", "timezone": "Asia/Shanghai", "enabled": true,
      "persona_id": null, "prompt_template": null, "qq_account": null, "qq_instance_id": null,
      "metadata": {
        "telegram_chat_id": -1003990634877, "telegram_topic_id": 680,
        "youtube_channels": ["https://www.youtube.com/@tiabtc", "https://www.youtube.com/@CakeBaBa"],
        "state_file": "scheduler_state/youtube_daily.json"
      },
      "execution_mode": "live", "source_system": "hermes", "source_job_id": "03e42ec536f8"
    },
    {
      "name": "hermes.analysis_digest", "cron": "0 15 * * *",
      "action_type": "personal.analysis_digest", "timezone": "Asia/Shanghai", "enabled": true,
      "persona_id": null, "prompt_template": null, "qq_account": null, "qq_instance_id": null,
      "metadata": { "telegram_chat_id": -1003990634877, "telegram_topic_id": 680, "user_id": "1114483029", "channels": ["telegram", "gateway"] },
      "execution_mode": "live", "source_system": "hermes", "source_job_id": "43f40d8e09f3"
    }
  ]
}
```

**Import-tool source of truth** (`/opt/corlinman-private/import_jobs.py::_ACTIONS`, cross-checked byte-for-byte against the JSON above — identical):
```python
_ACTIONS = {
    "ead0ccfdbd38": ("hermes.competition_daily", "briefing.competition_daily", "0 9 * * *", True),
    "5a2aa0aaa7de": ("hermes.diary_summary", "personal.diary_summary", "30 23 * * *", True),
    "fc6c8be7d0cb": ("hermes.daily_agenda", "personal.daily_agenda", "0 7 * * *", False),
    "1d116b77bed7": ("hermes.qzone_daily", "qzone.daily_publish", "0 22 * * *", True),
    "3d43e796bdc4": ("hermes.qzone_reply", "qzone.reply_comments", "0 9,21 * * *", True),
    "63c47a8759a3": ("hermes.qzone_friends", "qzone.comment_friends", "30 13 * * *", True),
    "03e42ec536f8": ("hermes.youtube_daily", "briefing.youtube_daily", "0 23 * * *", True),
    "43f40d8e09f3": ("hermes.analysis_digest", "personal.analysis_digest", "0 15 * * *", True),
}
```

**Shadow/live execution-mode history**: every job above was created with `execution_mode="shadow"` by `import_jobs.py` (§1d). A backup snapshot, `scheduler_runtime_jobs.json.bak-pre-live-20260728-173911`, captures the pre-flip state (`execution_mode: "shadow"` for all 8, plus `metadata.qq_account: "2104743984"` for `hermes.qzone_daily`/`hermes.qzone_reply` instead of the current `"1010679324"`, and `hermes.qzone_friends` missing the `qq_instance_id` metadata key). The current file (bulk-flipped 2026-07-28 17:39 UTC) shows `execution_mode: "live"` for all 8 — and, per the live-history re-audit below, this `"live"` setting is now confirmed to reflect real behavior, not just a stale flag.

### Run-history — redone against the live file (`/opt/corlinman/execution-state/scheduler.sqlite`)

Queried directly by this agent this session. **Old-file numbers (dead since 2026-07-27 01:03:39 UTC) are kept alongside for contrast — do not use them for anything migration-relevant.**

| Job | OLD (dead) n / ok / fail, last | NEW (live) n / ok / fail, last | Live fail % |
|---|---|---|---|
| `persona.decay` | 1260 / 0 / 1260, 07-27 01:00 | 543 / 0 / 543, 08-18 15:00 | **100%** |
| `system.update_check` | 228 / 228 / 0, 07-27 00:00 | 90 / 90 / 0, 08-18 12:00 | 0% |
| `evolution.darwin_curate` | 56 / 0 / 56, 07-26 03:30 | 23 / 0 / 23, 08-18 03:30 | **100%** |
| `hermes.competition_daily` | 3 / 2 / 1, 07-27 01:03 | 22 / 1 / 21, 08-18 01:02 | 95.5% |
| `hermes.diary_summary` | 2 / 2 / 0, 07-22 00:55 | 23 / 1 / 22, 08-18 15:30 | 95.7% |
| `hermes.daily_agenda` | 2 / 2 / 0, 07-22 00:55 | 0 / — / —, never (disabled) | n/a |
| `hermes.qzone_daily` | 2 / 2 / 0, 07-22 00:56 | 23 / 20 / 3, 08-18 14:00 | 13.0% |
| `hermes.qzone_reply` | 3 / 3 / 0, 07-27 01:00 | 45 / 41 / 4, 08-18 13:00 | 8.9% |
| `hermes.qzone_friends` | 2 / 2 / 0, 07-22 00:56 | 23 / 23 / 0, 08-18 05:30 | **0%** |
| `hermes.youtube_daily` | 2 / 1 / 1, 07-22 00:57 | 23 / 0 / 23, 08-18 15:00 | **100%** |
| `hermes.analysis_digest` | 2 / 2 / 0, 07-22 00:57 | 23 / 2 / 21, 08-18 07:00 | 91.3% |
| `grantley.qzone_reply` | 1 / 1 / 0, 07-26 13:30 | 0 / — / —, never since split | n/a |

**Total**: old file 1563 rows (dead, frozen 2026-07-27 01:03:39 UTC); live file 838 rows as of query time (2026-08-18 15:42 UTC session time), most recent firing 2026-08-18 15:30:02 UTC. **The scheduler has been ticking continuously — there was no 22-day silence, only the ~37-minute file-cutover gap on 2026-07-27.**

### Corrected conclusions (superseding Revision 2 in full)

**1. Shadow vs. live — resolved with certainty.** All 8 `hermes.*` jobs' `execution_mode` is `"live"` in the current `scheduler_runtime_jobs.json`, and live-history `result_json` confirms this is real: none of the recent samples carry `"shadow": true` / `"delivery_suppressed": true` except where a builtin's own logic independently decided there was nothing to do. **Whether a message/post actually lands, however, splits sharply by target channel**:
   - **QQ-targeted jobs (`hermes.qzone_daily`, `hermes.qzone_reply`, `hermes.qzone_friends`) are genuinely posting to production QQ right now.** `qzone_friends` is at 100% success (real `qzone_post_comment` calls, e.g. `"comments_posted": 2` on 2026-08-18); `qzone_reply` and `qzone_daily` succeed 87-91% of the time, with real `qzone_url`s in the successful `qzone_daily` payloads (e.g. `https://user.qzone.qq.com/1010679324/mood/1cbe3d3cef13836a62a70700`). **A naive migration that just ports these three jobs' definitions and re-enables them will resume posting to the real QQ account immediately.**
   - **Telegram-targeted private-plugin jobs (`hermes.competition_daily`, `hermes.diary_summary`, `hermes.analysis_digest`, and most of `hermes.youtube_daily`'s failures) generate correct content but then fail at the final delivery step 91-96% of the time**, with `result_json.error = "telegram_send_failed"`, `error_type = "SendHttpError"` — the LLM/research/summarization logic is demonstrably working (full, well-formed Chinese text is present in the failed rows' `text` field), only the outbound `deliver_telegram_text()` HTTP call to the Telegram Bot API fails. **In practice, almost no Telegram messages are currently being delivered by these jobs, despite being configured `live` and despite the underlying job logic working.** Root cause not diagnosed in this session (candidates: revoked/invalid bot token, wrong `chat_id`, or — plausibly, given this is a Tencent-Cloud-hosted host with QQ/China-oriented infrastructure and an `antigravity`-proxied LLM provider — an egress network path to `api.telegram.org` that doesn't work reliably from this host). **Migration-relevant**: verify Telegram reachability/credentials from the *new* host independently; do not assume "it was live in production" implies "it was actually delivering."

**2. `persona.decay` and `evolution.darwin_curate` have a 100% failure rate across their *entire* recorded history in both the dead and the live file** — every single one of 1803 combined firings (1260+543 for decay, 56+23 for darwin_curate) returns `{"ok": false, "reason": "data_dir_unavailable"}`. This is **not** caused by the storage split (the pattern is identical before and after) — it is a standing, since-inception bug. Root cause, confirmed by reading the gateway source in this session: `persona_decay.py` and `evolution_darwin_curate.py` each implement a local `_resolve_data_dir(context)` that only checks `getattr(context.app_state, "data_dir", None)` / `getattr(context.admin_state, "data_dir", None)`. A repo-wide grep confirms **`app.state.data_dir` is never assigned anywhere** in `gateway/lifecycle/entrypoint.py` — the resolved data directory is threaded through boot as a local variable (`resolved_data_dir`) and passed by keyword into individual components, never published as an attribute on the FastAPI `app.state` object the scheduler tick loop hands to every builtin. Compounding this, the scheduler tick loop's `dispatch()` never populates `BuiltinContext.admin_state` at all (only the manual "fire now" admin route does), so the second probe is always `None` too. **Net effect: mood/fatigue decay and the darwin skill-quality curator have never actually executed their core logic on this production deployment, ever.** Migration recommendation: either fix this wiring gap when porting (straightforward — read `CORLINMAN_EXECUTION_STATE_DIR` or `CORLINMAN_DATA_DIR` directly, matching what `chat_driver.resolve_data_dir()` and the qzone builtins already do correctly), or explicitly decide these two jobs are safe to omit from the migration's "must replicate exact current behavior" scope since their current behavior is "always no-ops."

**3. `hermes.youtube_daily` — re-audited against live data, materially different from the Revision-2 finding.** The single `timeout` failure Revision 2 flagged came from the *dead* file and does not reproduce in live data (no `error: "timeout"` rows in the 23 live firings). The live picture is worse and more varied: **100% failure (23/23)**, split across two distinct causes — (a) `telegram_send_failed` after fully successful video research/summarization (the common case, same delivery problem as finding #1 above), and (b) occasional fast `chat_error` failures (duration ~1s, `tools_called: []`, e.g. one sample elsewhere in the batch showed `chat_error_reason: "model_not_found"` / `chat_error_message: "404 page not found"` on a sibling job, suggesting an intermittent upstream model-routing/provider issue rather than anything youtube-specific). **Migration recommendation, revised**: the earlier "increase the per-firing timeout budget" advice from Revision 2 no longer applies (timeouts aren't occurring in live data) — replace it with "fix Telegram delivery (see finding #1) and add retry/backoff around transient `chat_error`/model-routing failures"; the research/summarization logic itself needs no rework.

**`grantley.qzone_reply`** — unchanged from Revision 2's reconstruction (its one recorded run, `2026-07-26 13:30:24 UTC`, predates and is unaffected by the split); zero rows in the live file confirm it has fired **zero** times since, consistent with being dormant/superseded rather than actively broken. Recommendation unchanged: do not migrate as a distinct job; treat as superseded by `hermes.qzone_reply`.

---

## 3. Builtin action specs

### In-repo builtins (`python/packages/corlinman-server/src/corlinman_server/scheduler/builtins/`)

**`registry.py`** (infrastructure, not an action). Defines `BuiltinContext`, `BUILTIN_ACTIONS`, `register_builtin()`/`run_builtin()`, `load_private_builtin_modules()`, and the shared `resolve_data_dir()` helper (note: several *other* builtins — see `persona.decay`/`evolution.darwin_curate` below — implement their **own** non-shared copy of this pattern, which is the actual root cause of a confirmed production bug).

**`chat_driver.py`** (shared library, not an action). Bounded internal agent-chat turn plumbing: `resolve_metadata`, `build_internal_chat_request()`, `drive_chat_turn()` (default timeout 300s). Its own `resolve_data_dir()` correctly checks `execution_state_dir`/`data_dir` attrs *and* the `CORLINMAN_EXECUTION_STATE_DIR`/`CORLINMAN_DATA_DIR` env vars as a fallback — this is the version that actually works in production (contrast with `persona_decay.py`'s below).

**`delivery.py`** (shared library, not an action). Effect-safe Telegram delivery. Shadow mode → no-op; live mode → idempotent `scheduler_effects` reservation, send via `app_state.telegram_sender`, mark `sent`/`unknown`. **In production this is the failing step for most Telegram-targeted private-plugin jobs** (see §2 finding #1) — `error_type: "SendHttpError"` is raised inside the `sender.send_message`/`send_photo` call and caught here, recorded with `state="unknown"` (transport-ambiguous, replay-blocked by design).

**`scheduled_agent.py`** (shared library). `run_scheduled_agent()` — one isolated chat turn, returns `{ok, text, duration_ms, tools_called, shadow, delivery_suppressed}`.

**`persona_decay.py`** — `persona.decay`. *Intended* behavior: sweeps `agent_state.sqlite`, applies elapsed-hours mood/fatigue/recent-topics decay. **Confirmed production behavior: fails 100% of the time** (`{"ok": false, "reason": "data_dir_unavailable"}`) — its local `_resolve_data_dir()` only checks `app_state.data_dir`/`admin_state.data_dir`, neither of which is ever populated (see §2 finding #2 for full root-cause). Has never successfully decayed a single persona row on this deployment.

**`system_update_check.py`** — `system.update_check`. Calls `UpdateChecker.poll(force=False)`. **Confirmed working**: 100% success (318 combined firings across both files). HTTP GET to GitHub Releases API; updates `<data_dir>/.update_check.json`.

**`evolution_darwin_curate.py`** — `evolution.darwin_curate`. *Intended* behavior: scores `SKILL.md` files, emits quality signals. **Confirmed production behavior: fails 100% of the time**, same `data_dir_unavailable` root cause as `persona.decay` (identical local `_resolve_data_dir()` pattern). Has never successfully scanned a skill on this deployment.

**`evolution_engine_run_once.py`** / **`evolution_shadow_test.py`** / **`persona_life_advance.py`** — never fired (default-off, zero rows in both old and live `scheduler_runs`). Note: all three implement the **same** local `_resolve_data_dir()` pattern as `persona_decay.py`/`evolution_darwin_curate.py` (confirmed by source inspection) — if ever enabled without first fixing the `app.state.data_dir` wiring gap, they would fail identically.

**`memory_dream.py`** / **`memory_reconcile.py`** — never scheduled (no default job; `[[scheduler.jobs]]` absent). Unchanged from Revision 1/2 spec.

**`qzone_daily.py`** — `qzone.daily_publish`. **Confirmed working in live production**: 87% success, real posts landing (`qzone_url` values observed). Occasional `chat_error` failures (e.g. `model_not_found` / "404 page not found" from the upstream LLM provider) — transient, not builtin-specific.

**`qzone_reply.py`** — `qzone.reply_comments`. **Confirmed working in live production**: 91% success. Typically finds 0 new comments to reply to per firing (`replies_posted: 0` is the common, correct outcome, not a failure).

### Private out-of-repo builtins — `/opt/corlinman-private/corlinman_private_jobs/`

All six build on the in-repo `chat_driver`/`delivery`/`registry` modules.

**`briefing.py` → `briefing.competition_daily`**. Fires: web-search-enabled agent turn for open/upcoming university CS/software-engineering competitions, delivers to Telegram. **Confirmed production behavior: content generation succeeds reliably (well-formed, detailed Chinese competition listings observed in every sample), but final Telegram delivery fails ~96% of the time** (`telegram_send_failed`/`SendHttpError` — see §2 finding #1). Errors: `missing_telegram_chat_id`, `empty_briefing`, upstream chat errors, and (dominant in practice) `telegram_send_failed`.

**`briefing.py` → `briefing.youtube_daily`**. Fires: per-channel new-video research with a watermark state file, delivers to Telegram. **Confirmed production behavior: 100% failure in live data** — mix of successful-research-then-`telegram_send_failed` (majority) and fast `chat_error` (upstream model-routing issue, minority). See §2 finding #3 for the full breakdown and revised migration recommendation (fix delivery + add chat-error retry; timeout budget is *not* the issue).

**`personal.py` → `personal.diary_summary`**. Fires: journal-derived daily first-person summary (secret-redacted, tools-disabled turn), delivers to Telegram. **Confirmed production behavior: ~96% delivery failure**, same `telegram_send_failed` pattern; also observed one fast `chat_error` sample. Content generation (including the correct "nothing to summarize today" fallback text) works.

**`personal.py` → `personal.analysis_digest`**. Fires: 24h journal keyword-filtered analysis/research summary, delivers to Telegram. **Confirmed production behavior: ~91% delivery failure**, same pattern — including on the "no analysis found" fallback path, which still attempts (and fails) a Telegram send.

**`agenda.py` → `personal.daily_agenda`**. Renders a YAML timetable to an SVG/PNG card, delivers to Telegram. **Never fires in production** (`enabled: false`, zero rows in both history files) — spec unchanged from Revision 2, unverified against live behavior since it doesn't run.

**`qzone_friends.py` → `qzone.comment_friends`**. Fires: browses ~15 friend-feed posts, comments 0-3 in-voice via `qzone_post_comment`, skips `on_mission` life-state and already-seen pairs. **Confirmed production behavior: 100% success** (23/23), consistently posting 2 real comments per firing (`comments_posted: 2`) to actual QQ friends' posts.

---

## 4. QQ monitors

Config location/parsing: `[[channels.qq.instances.<instance_id>.monitors]]` in `config.toml`, parsed by `_qq_monitor_parse_entry` in `corlinman-channels/src/corlinman_channels/service.py`. Full production config (verbatim, no secrets in this section) and per-monitor summary are unchanged from Revision 2 — reproduced with one important correction below.

```toml
[[channels.qq.instances.default.monitors]]
id = "sanhu"
enabled = true
sources = [{ group = "980927602", watch_user_ids = [], focus_user_ids = [] }]
schedule_type = "daily"
daily_time = "10:00"
timezone = ""
window_minutes = 1440
target_type = "user"
target_id = "2104743984"
send_when_empty = false

[[channels.qq.instances.default.monitors]]
id = "jlu"
enabled = true
schedule_type = "daily"
daily_time = "11:00"
timezone = ""
window_minutes = 1440
target_type = "user"
target_id = "2104743984"
send_when_empty = false
[[channels.qq.instances.default.monitors.sources]]
group = "183287894"
watch_user_ids = []
focus_user_ids = ["1076712858"]

[[channels.qq.instances.default.monitors]]
id = "qunjlu"
enabled = true
schedule_type = "daily"
daily_time = "09:00"
timezone = ""
window_minutes = 1440
target_type = "group"
target_id = "183287894"
send_when_empty = false
[[channels.qq.instances.default.monitors.sources]]
group = "183287894"
watch_user_ids = ["1076712858"]
focus_user_ids = []
```

**Timezone — resolved this session, corrects an open gap from Revision 2.** None of the three monitors set an explicit `timezone`, and no `proactive_timezone` is set anywhere in `[channels.qq.instances.default]` either, so per `_qq_monitor_tzinfo`'s fallback chain (own `timezone` → `proactive_timezone` → process-local), all three evaluate their daily HH:MM against the **host's process-local zone**, independently confirmed this session (`timedatectl show --property=Timezone` → **`Asia/Tokyo`**, i.e. **UTC+9** — *not* `Asia/Shanghai`/UTC+8, despite every job/monitor's *displayed* schedule being written as if China-local). **Concretely: `sanhu` fires at 10:00 JST = 09:00 China time; `jlu` at 11:00 JST = 10:00 China time; `qunjlu` at 09:00 JST = 08:00 China time.** Migration-critical: if the target host runs in a different system timezone (or the migration explicitly pins `Asia/Shanghai`), all three fire times shift by up to an hour relative to current production behavior unless deliberately compensated.

Per-monitor summary, behavior-when-fired, and delivery mechanics are otherwise unchanged from Revision 2: `sanhu` (group `980927602`, everyone, → private chat `2104743984`), `jlu` (group `183287894`, everyone + focus `1076712858`, → private chat `2104743984`), `qunjlu` (group `183287894`, filtered to `1076712858` only, → back into the same group `183287894` — **but currently suppressed in practice**, since `group_replies_enabled=false` in `[channels.qq.instances.default]` mutes all group-targeted sends including monitor digests; `sanhu`/`jlu`'s private-chat targets are unaffected by this switch).

---

## 5. Gaps

Resolved this session (no longer gaps): run-history source-of-truth (§1 storage split), shadow/live status for all 8 `hermes.*` jobs (§2), `hermes.youtube_daily`'s actual live failure mode (§2), monitor host timezone (§4 — `Asia/Tokyo`), `persona.decay`/`evolution.darwin_curate`'s true production behavior and root cause (§2/§3).

Remaining:
1. **Root cause of `telegram_send_failed`/`SendHttpError`** (affects `hermes.competition_daily`, `hermes.diary_summary`, `hermes.analysis_digest`, and part of `hermes.youtube_daily` — 91-100% of their firings) is not diagnosed beyond the exception class name recorded in `result_json`. Candidates: invalid/revoked bot token, wrong `chat_id`/`topic_id`, or an unreliable egress path to `api.telegram.org` from this host. Would need application logs (not queried this session) or a manual test send to isolate. **Directly affects whether the migration should expect these 4 jobs to "just work" once re-enabled — current evidence says no, without further fixing.**
2. **`grantley.qzone_reply`'s `cron`/`timezone`/`enabled`** remain unrecoverable (unchanged from Revision 2). Recommend not migrating as a distinct job.
3. **`hermes.qzone_reply`'s execution-mode edit history before 2026-07-28** (why it fired live even while its own runtime-JSON snapshot showed `"shadow"` at one point) — noted in Revision 2, not re-investigated this session, not migration-blocking (current definition is unambiguous).
4. Other `/opt/corlinman/{data,execution-state}/*.sqlite` files beyond `scheduler.sqlite` were not schema-inspected.
5. `corlinman_private_jobs/briefing.py.bak-pre-datadir-fix` not diffed against current `briefing.py` — likely a pre-fix snapshot, not migration-relevant.
6. The intermittent `chat_error`/`model_not_found` ("404 page not found") failures observed on top of the Telegram-delivery problem (seen at least once each on `hermes.qzone_daily` and `hermes.diary_summary`/`hermes.youtube_daily`) were not investigated for frequency or root cause beyond "transient upstream provider issue" — worth a dedicated pass if the target framework needs a hard reliability number.
