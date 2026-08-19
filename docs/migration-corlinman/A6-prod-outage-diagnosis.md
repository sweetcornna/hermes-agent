# A6 — Production "silent failure" diagnosis (corlinman)

Investigated 2026-08-18/19 (JST) on the live box, read-only. No service was restarted, no
config modified, nothing written outside this document.

**Timezone note:** the host is `Asia/Tokyo (JST, +0900)`, not `Asia/Shanghai`. Every timestamp
below is normalised to **UTC** unless explicitly suffixed `JST`. `journalctl` output was taken
with `--utc` where it matters.

---

## Verdict

**The premise of both prior audits is wrong: nothing stopped on 2026-07-27, and there is no
outage.** At **2026-07-27 ~01:40 UTC (10:40 JST)** a storage split landed that introduced
`CORLINMAN_EXECUTION_STATE_DIR=/opt/corlinman/execution-state`. From that instant both services
write their execution-state SQLite databases to `/opt/corlinman/execution-state/`, and
`/opt/corlinman/data/scheduler.sqlite` + `/opt/corlinman/data/memory.sqlite` became **abandoned
frozen snapshots**. The audits queried the dead files. The live
`/opt/corlinman/execution-state/scheduler.sqlite` holds 837 run rows spanning
`2026-07-27T01:40:39Z → 2026-08-18T15:00:01Z`, with `persona.decay` firing on the hour every
hour up to the moment of investigation. The scheduler tick loop has never stopped, and the
07-31 restart is a red herring — it did not need to "resume" anything.

**One real, permanent bug does exist, and it is narrower than reported.** `persona.decay` and
`evolution.darwin_curate` have genuinely never succeeded — but *not* because of the shared
`resolve_data_dir()` helper in `registry.py`, which they do not use. Both builtins define their
own private two-probe `_resolve_data_dir()` that looks for an attribute literally named
`data_dir` on `context.app_state` and `context.admin_state`. The scheduler tick loop passes
**Starlette's `app.state` namespace object** as `app_state` (`entrypoint.py:1749`,
`app_state=app.state`), on which the storage roots are published under the names
`corlinman_data_dir` / `corlinman_execution_state_dir` / `corlinman` / `corlinman_state` — never
plain `data_dir`. And `context.admin_state` is **never populated** by the tick loop
(`runner.py:591` omits it entirely). Both probes therefore miss on every single firing, forever,
deterministically. Sibling builtins that use either the shared `registry.resolve_data_dir()`
(which additionally probes `.corlinman_state` / `.corlinman`) or
`chat_driver.resolve_data_dir()` (which falls back to the `CORLINMAN_EXECUTION_STATE_DIR`
environment variable) succeed normally — which is exactly the observed pass/fail split.

**The direct journal evidence for 2026-07-27 is irrecoverably gone.** Journal retention on this
box is roughly **seven hours** (oldest retained entry `2026-08-18T08:13:26Z`), because two
orphaned systemd units — `corlinman-napcat.service` (`NRestarts=309304`) and
`corlinman-napcat-manager.service` (`NRestarts=605725`) — are in a permanent crash-restart loop
at ~30 restarts/minute and account for **61% of all retained journal lines**. I state this
plainly rather than reconstructing a plausible narrative: the 07-27 and 07-31 log windows no
longer exist on disk.

---

## Q1 — Why did everything stop on 2026-07-27, and why did the 07-31 restart not resume it?

**Answer: it did not stop. The data moved. The 07-31 restart is irrelevant to the symptom.**

### The live processes hold different files open than the ones that were audited

`lsof` / `/proc/<pid>/fd` on the running gateway (PID 2581308) and agent (PID 2581307):

```
corlinman 2581307 corlinman-agent 30ur REG 254,1 524288 2246640 /opt/corlinman/execution-state/scheduler.sqlite
corlinman 2581308       corlinman 52ur REG 254,1 524288 2246640 /opt/corlinman/execution-state/scheduler.sqlite
```

Neither process has `/opt/corlinman/data/scheduler.sqlite` open. The gateway's environment
(`/proc/2581308/environ`) explains why:

```
CORLINMAN_DATA_DIR=/opt/corlinman/data
CORLINMAN_EXECUTION_STATE_DIR=/opt/corlinman/execution-state
```

`AppState` documents the split directly (`gateway/core/state.py`):

> `data_dir` remains the gateway-private control-plane root. `execution_state_dir` is the
> deliberately shared gateway/Agent root for journals, inboxes, memory, personas, files, and
> other execution artifacts. They are identical unless the operator sets
> `CORLINMAN_EXECUTION_STATE_DIR`.

The operator set it. So they are no longer identical.

### The two databases are two halves of one timeline, not one database that died

| file | rows | first run (UTC) | last run (UTC) | file mtime (UTC) |
|---|---|---|---|---|
| `/opt/corlinman/data/scheduler.sqlite` (**dead**) | 1563 | 2026-05-31T06:00:00Z | **2026-07-27T01:03:39Z** | 2026-07-27T01:40:10Z |
| `/opt/corlinman/execution-state/scheduler.sqlite` (**live**) | 837 | **2026-07-27T01:40:39Z** | 2026-08-18T15:00:01Z | 2026-08-16T12:00:00Z |

The old file's last write and the new file's first write are **29 minutes apart on the same
day**. That is a cutover, not an outage.

Identical picture for the memory database, which disposes of the "this is not scheduler-specific"
inference (it was correct that it is not scheduler-specific — it is *storage*-specific):

| file | `mk_observations` rows | `ts_ms` range (UTC) |
|---|---|---|
| `/opt/corlinman/data/memory.sqlite` (**dead**) | 758 | 2026-07-17T16:08:43Z → **2026-07-27T01:30:08Z** |
| `/opt/corlinman/execution-state/memory.sqlite` (**live**) | **2313** | 2026-07-28T08:30:56Z → **2026-08-18T13:58:50Z** |

Conversation logging never stopped; it has 2313 rows and was still writing 30 minutes before I
started work.

### Corroborating live activity, observed today

```
2026-08-18T22:00:01+0900 env[2581307]: agent.chat.start  session=scheduler:qzone:grantley:a648c138
2026-08-18T22:00:13+0900 env[2581307]: agent.memory.stored  session=scheduler:qzone:grantley:a648c138
2026-08-19T00:00:01+0900 env[2581307]: agent.chat.start  session=scheduler:hermes.youtube_daily:509950dedaf04129bd3ecf43e27a366c
```

### Why the 07-31 restart "did not resume" anything

Because there was nothing to resume. The restart at `2026-07-31 13:21:14 JST` is unrelated;
both units have been `active (running)` since, and the scheduler wrote run rows continuously
across that boundary (rows exist on both sides of it in the live DB).

### Direct log evidence for 07-27 no longer exists — stated plainly

```
$ journalctl --disk-usage
Archived and active journals take up 96.9M in the file system.

$ journalctl --list-boots
IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY
  0 08b3d22dcaa34e83bbdc2f049c038f4f Tue 2026-08-18 17:13:26 JST Wed 2026-08-19 00:26:51 JST

$ journalctl -u corlinman --since "2026-07-31 13:21" --until "2026-07-31 13:30"
-- No entries --
```

The host has `up 85 days` and `systemd-journald` has been running since 2026-05-25, so this is
**not** a reboot — it is rotation pressure. Persistent storage exists
(`/var/log/journal/df0a499421f34ceabb9e8448fac338c3`), and `journald.conf` is at defaults
(`[Journal]` with no directives), so the 96.9 MB cap is being consumed in ~7 hours. Cause,
measured:

```
$ journalctl --no-pager | wc -l
149317
$ journalctl --no-pager | grep -oE "corlinman-napcat[a-z-]*" | sort | uniq -c | sort -rn
  65252 corlinman-napcat-manager
  25451 corlinman-napcat
```

61% of the retained journal is two crash-looping units (detail in Q4). **Conclusion: any
`journalctl` evidence from 2026-07-27 or 2026-07-31 was overwritten weeks ago and cannot be
recovered from this host.** The cutover conclusion above rests entirely on filesystem and
database evidence, which is direct and unambiguous.

---

## Q2 — Is the scheduler tick loop running right now?

**Yes. Definitively.** Four independent observations, in decreasing order of strength:

1. **The live run-history table is being appended to on schedule.** The most recent rows at
   time of investigation:

   ```
   2026-08-18T15:00:01.390Z | hermes.youtube_daily | non_zero_exit  | builtin_not_ok
   2026-08-18T15:00:00.443Z | persona.decay        | non_zero_exit  | builtin_not_ok
   2026-08-18T14:00:02.326Z | hermes.qzone_daily   | non_zero_exit  | builtin_not_ok
   2026-08-18T14:00:00.121Z | persona.decay        | non_zero_exit  | builtin_not_ok
   2026-08-18T13:00:13.981Z | hermes.qzone_reply   | success        | -
   2026-08-18T13:00:00.299Z | persona.decay        | non_zero_exit  | builtin_not_ok
   2026-08-18T12:00:01.188Z | system.update_check  | success        | -
   ```

   `persona.decay` fires at `HH:00:00` **every hour without a gap** — precisely its
   `0 0 */1 * * * *` cron. This is the run-history store the tick loop writes via
   `_maybe_record(app_state, ...)` in `runner.py`; rows appearing means `dispatch()` is being
   called by a live tick task.

2. **A job fired during the investigation.** `hermes.youtube_daily` at
   `2026-08-19T00:00:01 JST`, visible in the journal in real time.

3. **`system.update_check` is succeeding every 6 hours**, returning live data:

   ```json
   {"available": false, "current": "1.56.5", "last_checked_at": 1787054400172, "latest": "1.56.5", "ok": true}
   ```

   `last_checked_at` advances between firings, so this is a fresh network round-trip per tick,
   not a cached row.

4. **The handle is wired.** `entrypoint.py:1749` spawns it and publishes it:
   `app.state.corlinman_scheduler_handle = scheduler_handle`, plus
   `admin_b_state.scheduler = scheduler_handle`, followed by
   `rehydrate_runtime_jobs_on_boot(admin_b_state)`.

**Method note on what did *not* work.** The admin HTTP surface requires auth, as expected:

```
$ curl -s http://127.0.0.1:6005/admin/scheduler/jobs
{"detail":{"error":"unauthorized","reason":"missing_authorization"}}   # HTTP 401
```

I did not attempt to obtain or forge a token. `rehydrate_runtime_jobs_on_boot()` emits no log
line of its own, and the boot window is outside journal retention, so the boot-time log-line
approach was unavailable. The run-history table is the stronger evidence anyway: it is the
tick loop's own write path.

---

## Q3 — Root cause of `persona.decay`'s permanent `data_dir_unavailable`

**This one is a genuine, permanent, deterministic code bug — but the diagnosis in the brief is
wrong about which helper is at fault.**

### There are four different `resolve_data_dir` implementations, not one

```
scheduler/builtins/registry.py:202     def resolve_data_dir(context: BuiltinContext) -> Path | None
scheduler/builtins/chat_driver.py:253  def resolve_data_dir(app_state: Any | None) -> Path | None
scheduler/builtins/persona_decay.py:67 def _resolve_data_dir(context: BuiltinContext) -> Path | None   # private copy
scheduler/builtins/evolution_darwin_curate.py:51 def _resolve_data_dir(...)                              # private copy
```

Plus two more private copies in `evolution_engine_run_once.py:63`,
`evolution_shadow_test.py:69`, and `persona_life_advance.py:81`. `registry.py`'s docstring
claims the copy-paste was consolidated —

> this exact three-probe walk was copy-pasted across six builtin modules before landing here;
> new builtins must use this instead of a seventh copy

— but **the consolidation was never actually applied to the existing copies.** `persona_decay.py`
imports `BuiltinContext` from `registry` and then ignores `registry.resolve_data_dir` in favour
of its own.

### What each implementation probes

| implementation | probes | reaches a path in prod? |
|---|---|---|
| `persona_decay._resolve_data_dir` | `app_state.data_dir`, `admin_state.data_dir` | **No** |
| `evolution_darwin_curate._resolve_data_dir` | `app_state.data_dir`, `admin_state.data_dir` | **No** |
| `registry.resolve_data_dir` | `app_state.data_dir`, `app_state.corlinman_state.data_dir`, `app_state.corlinman.data_dir`, `admin_state.data_dir` | Yes |
| `chat_driver.resolve_data_dir` | `app_state.execution_state_dir`, `app_state.data_dir`, then **`os.environ["CORLINMAN_EXECUTION_STATE_DIR"]`**, then `os.environ["CORLINMAN_DATA_DIR"]` | Yes (via env) |

### Why the two-probe version can never succeed

**Probe 1 — `app_state` is Starlette's `app.state`, not the `AppState` dataclass.**
`entrypoint.py:1749`:

```python
scheduler_handle = _spawn_scheduler(
    sched_cfg, sched_bus, cancel, app_state=app.state
)
```

`app.state` is a generic namespace. The storage roots are published on it under these names
(from the boot wiring):

```
app.state.corlinman_data_dir             =
app.state.corlinman_execution_state_dir  =
app.state.corlinman                      =
app.state.corlinman_state                =
```

There is **no** `app.state.data_dir`. So `getattr(app_state, "data_dir", None)` returns `None`.
Note the deep irony: the real `AppState` dataclass *does* declare
`data_dir: Path | None = None` and *is* correctly populated — it is simply parked one level
down, under `app.state.corlinman` / `app.state.corlinman_state`, which the two-probe version
never looks at and the four-probe `registry` version does.

**Probe 2 — `admin_state` is never populated in the tick-loop path.** `runner.py:591`:

```python
ctx = BuiltinContext(
    app_state=app_state,
    run_id=run_id,
    name=spec.name,
    metadata=dict(spec.metadata),
    ...
)
```

`admin_state` is omitted, so it defaults to `None`, and the loop body `if owner is None: continue`
skips it. Both probes exhausted → `return None` → `{"ok": false, "reason": "data_dir_unavailable"}`.

Notably the **manual "fire now" path does pass it** (`_scheduler_lib.py:1306`:
`BuiltinContext(app_state=app_state, admin_state=state, ...)`), so manual invocation may behave
differently from the cron path — a classic reason this bug survived 14 months of operation.

### Confirmed: this is the shared-context bug, not a decay-specific bug

Per-job tallies from the **live** database (this corrects the brief's numbers, which came from
the dead file):

```
persona.decay            | non_zero_exit | builtin_not_ok | n=543 | 2026-07-27T01:40:39Z -> 2026-08-18T15:00:00Z
evolution.darwin_curate  | non_zero_exit | builtin_not_ok | n= 23 | 2026-07-27T03:30:00Z -> 2026-08-18T03:30:00Z
system.update_check      | success       | -              | n= 90 | 2026-07-27T06:00:00Z -> 2026-08-18T12:00:01Z
hermes.qzone_reply       | success       | -              | n= 41 | 2026-07-27T13:00:14Z -> 2026-08-18T13:00:13Z
hermes.qzone_friends     | success       | -              | n= 23 | 2026-07-27T05:30:28Z -> 2026-08-18T05:30:40Z
hermes.qzone_daily       | success       | -              | n= 20 | 2026-07-27T14:00:23Z -> 2026-08-17T14:00:19Z
```

Both `evolution.darwin_curate` samples carry the identical envelope:

```
2026-08-18T03:30:00.183Z evolution.darwin_curate non_zero_exit builtin_not_ok {"ok": false, "reason": "data_dir_unavailable"}
```

**The split maps exactly onto the resolver used.** Every job that fails with
`data_dir_unavailable` uses a private two-probe copy; every job that succeeds uses either
`registry.resolve_data_dir` or `chat_driver.resolve_data_dir`. `system.update_check` — reported
in the brief as a co-victim — is in fact **100% successful (90/90)**; it never needed a data dir,
reading `app_state.corlinman_update_checker` instead.

### Corroboration: someone already hit and patched this exact bug

Confirming the coordinator's lead. In the out-of-repo private job plugins:

```
-rw-r----- 1 root corlinman-agent 6213 2026-07-28 17:47:11 JST briefing.py
-rw-r----- 1 root corlinman-agent 6171 2026-07-22 00:35:56 JST briefing.py.bak-pre-datadir-fix
```

The `.bak` predates the storage split (2026-07-22 JST); the fix was applied **2026-07-28 17:47
JST**, i.e. ~31 hours *after* the 07-27 cutover — the split is what made a latent bug bite. The
diff swaps a direct `getattr(app_state, "data_dir", None)` for
`chat_driver.resolve_data_dir(context.app_state)`.

To answer the coordinator's specific question: **`chat_driver.resolve_data_dir` and
`registry.resolve_data_dir` are two genuinely different implementations with different
signatures** (`chat_driver` takes a raw `app_state`; `registry` takes a `BuiltinContext`).
The operator's fix worked *specifically because* the `chat_driver` variant has the environment
-variable fallback — it never actually resolved anything off `app_state` in production; it fell
through to `os.environ["CORLINMAN_EXECUTION_STATE_DIR"]`. `persona_decay` was never on the
shared helper at all, which is why the patch to `briefing.py` did not help it.

---

## Q4 — Other box-health findings bearing on migration

### 4a. NapCat is fully connected to QQ — but two orphaned systemd units are melting the journal

**The Docker container is healthy and actively bridging.** `docker ps` shows
`corlinman-napcat | Up 3 weeks`, and it is receiving live group traffic at the moment of
investigation (23:28–23:30 JST today):

```
08-18 23:29:56 [info] Grantly | 接收 <- 群聊 [高认知且渴望存续的好人群(980927602)] [奇迹(532649249)] 真爆拉了
08-18 23:30:05 [info] Grantly | 接收 <- 群聊 [25计软聚集地(1016414937)] [牢菌(923357761)] 【每日词云】...
```

OneBot v11 WebSocket confirmed listening (read-only probe, no message injected):

```
$ curl -o /dev/null -w "%{http_code}" http://127.0.0.1:3001/   ->  426   (Upgrade Required = live WS endpoint)
$ curl -o /dev/null -w "%{http_code}" http://127.0.0.1:6099/   ->  301   (WebUI alive)
08-17 21:34:21 [info] [OneBot] [WebSocket Server] Server Started :::3001
```

**However, there are duplicate legacy systemd units for the same role, permanently failing:**

```
corlinman-napcat.service          NRestarts=309304  ActiveState=activating  Result=exit-code
corlinman-napcat-manager.service  NRestarts=605725  ActiveState=activating  Result=exit-code
```

Measured rate: **187 + 116 = 303 restarts per 10 minutes (~44,000/day)**. Failure reasons:

```
NapCat-v4.18.4-amd64.AppImage[2343082]: fuse: failed to exec fusermount: No such file or directory
NapCat-v4.18.4-amd64.AppImage[2343080]: Cannot mount AppImage, please check your FUSE setup.
corlinman-napcat.service: Main process exited, code=exited, status=127/n/a

(-manager)[2343087]: Failed to set up mount namespacing: /run/systemd/unit-root/opt/corlinman/data/.napcat/managed: No such file or directory
(-manager)[2343087]: Failed at step NAMESPACE spawning /opt/corlinman/repo/.venv/bin/corlinman-napcat-manager: No such file or directory
corlinman-napcat-manager.service: Main process exited, code=exited, status=226/NAMESPACE
```

These are leftovers from the pre-Docker native NapCat deployment. The manager unit's mount
namespace still references **`/opt/corlinman/data/.napcat/managed`** — the *old* data root,
another artefact of the 07-27 split. They are functionally harmless (the Docker container does
the real work) but they are the direct cause of the ~7-hour journal retention that destroyed the
forensic evidence for Q1, and they contribute continuous process-spawn and IO churn.

### 4b. Memory pressure: real but not acute; no observable OOM history

```
$ free -m
               total   used   free   shared  buff/cache   available
Mem:            1966   1522    119        4         542         444
Swap:           6143   4032   2111

$ cat /proc/pressure/memory
some avg10=0.38 avg60=0.93 avg300=1.16   full avg10=0.36 avg60=0.81 avg300=1.00
$ cat /proc/pressure/io
some avg10=7.46 avg60=13.71 avg300=17.72  full avg10=5.62 avg60=12.59 avg300=16.65
```

Memory PSI is low (~1% stall) — the box is swap-heavy but not thrashing. **IO pressure is
materially higher (~17% stalled at 5-min average)**, consistent with the restart loop above.

**The corlinman workload is not the memory consumer.** Top RSS:

```
    630 root      192200  9.5  chrome
3563633 root      156992  7.7  chrome
1629812 debian    101044  5.0  qq
    629 root       59688  2.9  uvicorn        (copy-sync, unrelated)
2581308 corlinman  26392  1.3  corlinman-gatew
```

The gateway is **26 MB RSS / 42 MB per systemd accounting**; the agent is **8.2 MB**. The 1.9 GB
is being consumed by co-tenant workloads: a 7-container `copytrader_*` stack, `tradingagents-web`,
Chrome instances, and a native `qq` process.

**OOM history: no evidence found, and this is a negative I cannot strengthen.** `dmesg -T | grep
-iE 'oom|killed'` and `journalctl -k | grep -iE 'oom|killed'` both return nothing — but the
kernel ring buffer and the journal only cover the retained ~7-hour window. **I cannot rule out
historical OOM kills; the evidence would have rotated away.** What I can say positively is that
both corlinman services show `Active: active (running) since Fri 2026-07-31 13:21:14` with no
intervening restart, so neither has been OOM-killed in the last 18 days.

Disk: `/dev/vda1 50G, 42G used, 8.1G avail, 84%`. Not urgent, worth watching.

### 4c. nginx is proxying correctly

```
$ nginx -t
nginx: configuration file /etc/nginx/nginx.conf test is successful
$ curl -s http://127.0.0.1:6005/health                      -> 200
$ curl -sk https://corlinman.cornna.xyz/health              -> 200
{"status":"ok","mode":"ok","version":"1.56.5"}
```

End-to-end through nginx to the gateway is healthy. Two cosmetic/adjacent issues:

- `nginx: [warn] conflicting server name "cornna.abrdns.com" on 127.0.0.1:10443, ignored` — a
  duplicate vhost, and `https://cornna.abrdns.com/health` returns **502**. This is a stale
  hostname, not the corlinman entrypoint.
- **`certbot.service` is in `failed` state**: `All renewals failed ... /etc/letsencrypt/live/cfp1.cornna.xyz/fullchain.pem (failure)`.
  The failing cert is `cfp1.cornna.xyz` (**expires 2026-09-03, 15 days**), an unrelated proxy
  domain. `corlinman.cornna.xyz` is valid for 60 days. But because certbot exits non-zero on the
  first failure, **the failing unit can block renewal of the corlinman cert when its window
  opens (~2026-09-18)**. This is a latent, dated, self-inflicting outage.

### 4d. Repo revision drift (checked, benign)

```
prod:  27bdf9c8 fix(deploy): restore split-process placeholder IPC     (working tree clean)
local: 389cb733 fix(deploy): 修复双进程 placeholder IPC，并发布 v1.56.5
```

Different SHAs, same change, and prod reports `version 1.56.5` matching local. The prod working
tree is clean (`git status --short` empty). All source quoted in this document was read from the
local checkout; the relevant files were confirmed present at the same paths on prod. Treat the
local checkout as representative.

---

## Implications for the migration

### (a) corlinman code bugs — must NOT be reproduced in the target framework

1. **Passing a framework namespace object as the job context (`app_state=app.state`).** This is
   the actual root cause of Q3. The scheduler receives Starlette's untyped `app.state` bag and
   every builtin `getattr`-gropes it for attributes by string name. There is no type checking,
   no failure on a missing handle, and no startup validation — a rename or a relocation silently
   converts a job into a permanent no-op. **The target framework must hand jobs a typed,
   explicitly-constructed context object**, and a job that cannot resolve a required dependency
   must fail *loudly at registration/boot*, not return `{"ok": false}` forever.

2. **Six divergent copies of `resolve_data_dir`.** The consolidation into
   `registry.resolve_data_dir` was written and documented but never applied; the private copies
   still shadow it and probe fewer locations. Do not port the copies. **Port exactly one
   resolver**, and delete the rest.

3. **`admin_state` populated on the manual path but not the cron path.** `_scheduler_lib.py:1306`
   passes `admin_state=state`; `runner.py:591` does not. Divergent context between "fire now"
   and scheduled execution is why this went unnoticed for over a year — an operator testing via
   the admin UI would see it work. **The target must use one identical code path for manual and
   scheduled firing.**

4. **Silent-failure envelope.** `{"ok": false, "reason": "data_dir_unavailable"}` returned 543
   consecutive times produced no alert, no log escalation, and no visible degradation. **The
   target needs consecutive-failure alerting on scheduled jobs.** A job that has failed 543 times
   running is not a job.

5. **`with suppress(AttributeError, TypeError)` around boot-critical wiring**
   (`app_factory.py:117-118`). Two assignments share one suppress block, so a failure on the
   first silently skips the second. Not the active root cause here, but the same
   swallow-everything pattern. **Do not port defensive suppression around wiring that must
   succeed.**

### (b) Environment/host problems — WILL recur on the target framework unless fixed

6. **Journal retention of ~7 hours.** This is a *host* condition caused by the crash-looping
   units, and it is entirely framework-independent. **If you cut over today, the target
   framework's own boot and error logs would be destroyed within 7 hours**, and you would be
   debugging it as blind as I was for Q1. This is the single most important item in this
   document for migration readiness.

7. **Orphaned `corlinman-napcat*.service` units (309k / 605k restarts).** These are host-level
   systemd leftovers. A new framework does not remove them; they will keep spawning ~44,000
   processes/day, keep the IO PSI at ~17%, and keep shredding the journal. Note the manager unit
   still points at the pre-split `/opt/corlinman/data/.napcat/managed`.

8. **1.9 GB RAM shared with a heavy co-tenant stack** (7-container copytrader, tradingagents-web,
   Chrome, native qq; 4 GB of 6 GB swap in use). corlinman itself is tiny (26 MB + 8 MB), so
   **the target framework inherits the headroom problem, not the cause**. If the target has a
   larger baseline footprint than ~35 MB combined, validate against actual free memory, not
   nominal total.

9. **`certbot.service` failed, with a dated fuse.** Framework-independent; will silently break
   TLS renewal for `corlinman.cornna.xyz` around 2026-09-18 regardless of what runs behind nginx.

10. **Disk at 84%.** Framework-independent; watch it.

### (c) Irrelevant to the migration

11. **The "22-day outage".** There was none. **Do not budget migration work for it, and do not
    treat "the scheduler stops silently" as a corlinman defect the target must design around.**
    The scheduler's reliability record here is actually good: continuous hourly ticking for 22
    days across a storage cutover.

12. **The 2026-07-31 restart.** Coincidental, not causal.

13. **`system.update_check` failures.** There are none — 90/90 success. Remove it from the
    defect list.

14. **The `cornna.abrdns.com` 502 / duplicate vhost warning.** Stale unrelated hostname.

15. **Prod vs local git SHA drift.** Same version, clean tree, benign.

> **Data-migration warning, and the most likely way this investigation's finding gets
> re-broken:** any migration tooling that reads "the corlinman database" from
> `/opt/corlinman/data/` will silently ingest the **abandoned 2026-07-27 snapshot** and lose
> ~3 weeks of production history (837 scheduler runs, 2313 memory observations). Both prior
> audits already made exactly this mistake. **Canonical live state is
> `/opt/corlinman/execution-state/`.** `/opt/corlinman/data/` remains live only for
> gateway-private control-plane files (`config.toml`, `evolution.sqlite`, `kb.sqlite`,
> `mcp_servers.sqlite`, `plugins.sqlite`, `tenants.sqlite`) — it is a genuine split, not a
> wholesale move, so neither directory alone is complete.

---

## Recommended remediation

I performed **no** remediation. All items below are proposals.

### Must be fixed BEFORE cutover

| # | Action | Why before |
|---|---|---|
| R1 | **Stop and mask the two orphaned units** (`corlinman-napcat.service`, `corlinman-napcat-manager.service`). The Docker container already provides the bridge. | Without this you have no usable logs during cutover — the single highest-value fix, and it is one command. |
| R2 | **Raise journal retention** (`SystemMaxUse=` / `MaxRetentionSec=` in `journald.conf`), after R1. | You cannot safely cut over a system whose logs survive 7 hours. |
| R3 | **Point every migration/ETL script at `/opt/corlinman/execution-state/`**, and audit any existing script that hardcodes `/opt/corlinman/data/`. Re-run the two prior audits against the correct files. | Prevents silently migrating a 3-week-stale snapshot. |
| R4 | **Re-baseline the migration's job inventory against the live DB.** Real current failures are `hermes.youtube_daily` (`chat_error`), `hermes.qzone_daily` (`model_not_found` / `404 page not found`), `hermes.analysis_digest` (`telegram_send_failed`), `hermes.competition_daily`, `hermes.diary_summary` — none of which appear in the brief. | The target framework must actually carry these jobs; several have integration bugs that are separate from the data-dir bug and were masked by the phantom-outage framing. |
| R5 | **Fix `certbot.service`** (repair or remove the `cfp1.cornna.xyz` lineage so the unit exits 0). | Cheap now; becomes a TLS outage around 2026-09-18, likely mid-migration. |

### Can be fixed AFTER cutover

| # | Action | Why after |
|---|---|---|
| R6 | **Fix `persona.decay` / `evolution.darwin_curate`.** Minimal fix: replace both private `_resolve_data_dir` with `registry.resolve_data_dir`, or add the `CORLINMAN_EXECUTION_STATE_DIR` env fallback to the shared helper. | If the target framework is taking over these jobs, fixing them in corlinman is throwaway work — implement them correctly in the target instead. Fix here only if corlinman will run in parallel. Note `persona.decay` has *never* succeeded in 14 months, so there is no regression risk and no urgency. |
| R7 | **Verify persona decay semantics before enabling it anywhere.** It has never run successfully against production data; personas carry ~14 months of un-decayed state. | Enabling a never-tested decay sweep against real data is its own risk — needs a dry run, not a hurried fix. |
| R8 | **Delete the four remaining private `_resolve_data_dir` copies** (`evolution_engine_run_once`, `evolution_shadow_test`, `persona_life_advance`, and any others). | Latent identical bugs in jobs not currently scheduled. |
| R9 | **Reclaim host memory / disk** (retire the copytrader + Chrome tenants or move corlinman to its own box). | Not blocking — corlinman uses 35 MB — but the box is at 84% disk and 4 GB swap. |
| R10 | **Fold the `data` / `execution-state` split into the target's storage model explicitly**, and remove the stale `/opt/corlinman/data/*.sqlite` snapshots once migration is verified (archive first). | Leaving both copies in place guarantees someone repeats the audits' mistake. |

### Explicitly NOT recommended

- **Do not restart `corlinman.service` to "fix" the scheduler.** It is working. A restart would
  destroy the current process state and prove nothing.
- **Do not treat `persona.decay` as a cutover blocker.** It has been failing since 2026-06-04 and
  nothing downstream depends on it.
