# D3 — the QQ group-history writer

Closes the gap D2 declared and §19 accepted: **this port had no writer for
`qq_group_history.sqlite`.** The whole repository contained reads and
validation and zero `INSERT`s; only corlinman's own dispatch loop populated
`group_messages`. Pointing `QQ_GROUP_HISTORY_DB` at corlinman's live file
works for the coexistence window, and then all three monitors (`sanhu`, `jlu`,
`qunjlu`) go quiet within that store's ~3-day retention the moment corlinman
is retired — **silently**, because `send_when_empty=false`.

Implemented exactly to D46-①…⑦. Nothing is enabled.

---

## 1. What landed

| Path | Lines | What it is |
|---|---:|---|
| `plugins/platforms/onebot/group_history.py` | **913** | new — config resolution, the writer thread, the store, retention |
| `plugins/platforms/onebot/group_history_backfill.py` | **380** | new — the one-shot idempotent import, runnable as `python -m` |
| `tests/gateway/test_onebot_group_history.py` | **1376** | new — 81 tests |
| `plugins/platforms/onebot/adapter.py` | 2111 (+128) | modified — mount point, lifecycle, YAML keys |
| `plugins/platforms/onebot/plugin.yaml` | 186 (+54) | modified — seven `ONEBOT_GROUP_HISTORY_*` entries |
| `plugins/platforms/onebot/README.md` | 439 (+68/-1) | modified — the archive section |

All six files are this migration's own (B2/B4 built `plugins/platforms/onebot/`).
**Zero upstream hermes files were touched** — see §10.

`plugins/corlinman_jobs/` is **not** modified. That is the point of D46-①: the
read path already speaks this schema, so cutover changes a variable, not code.

---

## 2. Schema — corlinman's, and verified to be

`SCHEMA_SQL` is a character-level copy of
`corlinman_server/qq_group_history.py::_SCHEMA`, read off the production
checkout over read-only SSH during this task. It carries the `monitor_state`
table too, even though nothing in this port writes it: the file is supposed to
be interchangeable with corlinman's, and a store missing a table corlinman's
own module creates is not interchangeable.

**How compatibility was actually verified** (not "it looks the same"):

1. **DDL diff against the real exported snapshot.** A file created by
   `connect_store()` vs `.migration-export/sqlite/qq_group_history.sqlite`
   (52,649 real production rows):

   ```
   IDENTICAL: True     diff: set()
   ```

   for all of `group_messages`, `idx_group_messages_window`, `monitor_state`,
   plus an exact column-order check via `PRAGMA table_info`.

2. **D2's own reader, unmodified, against a file this writer produced.**
   `corlinman_jobs_lib._qq_monitor_query` — loaded by path, not reimplemented
   — returns our rows in the 5-tuple shape its formatter consumes, and
   `_qq_monitor_format_lines` renders them with ★ focus marking intact.
   `sender_ids` narrowing (which *is* `qunjlu`'s entire mechanism) also works.

3. **Round trip through the backfill.** 52,649 real rows imported into a fresh
   store, then read back through the same reader.

Both snapshot-dependent tests `skipif` the export is absent, so a clean
checkout still runs green; the transcribed-DDL test always runs.

Two constants are pinned to corlinman's: `TEXT_CAP = 2000`, and the
"`sender_name` is blank when it is merely the uin repeated" convention (the
digest renders `name(id)` and would otherwise print `123(123)`).

---

## 3. Never corlinman's file (D46-②)

Two variables, and they are different on purpose:

| Variable | Names | Owner |
|---|---|---|
| `QQ_GROUP_HISTORY_DB` | the file the monitors **read** | D2, unchanged |
| `ONEBOT_GROUP_HISTORY_DB` | the file this gateway **writes** | D3, new |

During coexistence the reader points at
`/opt/corlinman/execution-state/qq_group_history.sqlite`. Had the writer
reused that variable it would have followed it there — the two-writer
configuration D46-② exists to prevent. Both default to the *same* path
(`$HERMES_HOME/plugin-data/corlinman_jobs/qq_group_history.sqlite`), so after
cutover the two converge, and a test asserts `group_history.default_db_path()
== installer.qq_group_history_db_path()` so that duplication cannot drift.

On top of the naming, there is a **mechanical** backstop. corlinman opens its
store with `PRAGMA journal_mode = WAL`; this module never does. So
`foreign_wal_reason()` refuses to start on a target that is in WAL mode or has
a `-wal` sidecar, with an error naming the variable to fix. It is not a proof,
but it catches the specific mistake — an operator copying the reader's path
into the writer's variable — that D46-② is about. Probing is read-only
(`mode=ro`, or just a sidecar stat).

**Why DELETE journal mode for our own file.** Not inertia: `hermes_state.py`
L655-660 documents that SQLite's WAL-reset bug can corrupt **multi-process**
WAL databases below 3.51.3 / 3.50.7 / 3.44.6, and refuses to enable WAL on
such builds. The target host has 3.40.1, and this store is exactly that
multi-process case — this gateway writes it, a `hermes cron` subprocess reads
it. `synchronous` is likewise left at `FULL`: batching already cut commits to
a few thousand a day, so there is nothing left to buy by weakening durability,
and a corrupt archive is worse than a slow one.

---

## 4. The numbers, and why these numbers

### Batch thresholds: **N = 200 rows, T = 30 seconds** (whichever first)

Basis is D2's measured traffic, not a round number: group `980927602` produces
~15,000 rows/day and `183287894` ~1,500, i.e. **~0.2 rows/s** averaged and
**~0.35 rows/s** across waking hours.

* **T = 30 s.** At that rate a 30 s window batches ~10 rows, so commits fall to
  **≤2,880/day** — about 10× fewer fsyncs than committing per message, which
  is the resource DELETE mode actually spends. Shorter values regress toward
  one-commit-per-message: at T = 5 s most windows would hold ~1.7 rows and
  batching would buy almost nothing.
  The cost is the loss window: **≤30 s** of messages on an *unclean* kill,
  out of a 24 h / ~16,500-message digest. A graceful `disconnect()` flushes,
  so the exposure exists only for SIGKILL / power loss.
* **N = 200.** Bounds the burst case so a flood commits by size rather than
  sitting in RAM for 30 s. One transaction is then ≤200 × ≤2 KB of text — a
  bounded blip and a bounded single fsync on a 1.9 GB box.

### Queue bound: **2,000 rows**, drop-newest on overflow

2,000 rows is **~100 minutes** of the busiest monitored group's traffic. The
queue can only approach it if the writer thread has been wedged for over an
hour, at which point dropping is the correct answer and the digest is already
compromised.

**On overflow the *new* row is dropped and counted** (`put_nowait` →
`queue.Full` → counter + rate-limited warning, one line per 1,000 drops so a
wedged writer cannot flood journald, which §6/§20 already had to widen once).
Dropping newest rather than evicting oldest keeps the backlog contiguous, so
whatever does get written is a coherent stretch of conversation rather than a
shuffled sample.

**`batch_rows` is clamped to `queue_max`.** This was found by measurement, not
by review: the worker holds un-committed rows in a plain list bounded only by
`batch_rows`, so a configured batch above the queue bound would have left
total in-flight memory unbounded — reopening the hole from the other side. The
clamp lives in `GroupHistoryWriter.__init__` so every construction path is
covered, which makes the guarantee **"at most `queue_max + batch_rows` rows in
flight, ever"**.

**Measured, with the worker deliberately wedged and every message a distinct
string** (an earlier measurement was wrong because CPython returns the same
object for a full-length slice, so 2,500 "rows" shared one string):

```
worst case (every message 2000 CJK chars, the TEXT_CAP):
    in-flight=2200  dropped=3800  depth=2000  bound=2000+200   RSS delta = 9.50 MB
typical case (~18 chars, real chatter):
    in-flight=2200  dropped=3800  depth=2000  bound=2000+200   RSS delta = 0.70 MB
```

Worst case ≈ 9.5 MB against `MemoryHigh=384M` / ~105 MB steady RSS; the
realistic case is under a megabyte.

### Retention: **7 days**, DELETE only

Floor of **2 days** enforced with a warning — the monitors read a 24 h window,
and a shorter retention would delete rows the next digest still needs, again
invisibly. Pruning runs hourly on the writer thread (first pass 60 s after
start), in **5,000-row chunks** with a 1,000,000-row stop, so one prune never
holds the single write lock long enough to stall a reading cron job (in DELETE
mode a write blocks readers). **No `VACUUM`** — freed pages are reused, and
rewriting the whole file to reclaim ~20 MB against 7.6 GB free is not a trade
worth making on 2 vCPUs. A test asserts `freelist_count > 0` after a large
prune, i.e. that nothing reclaimed the space.

Measured file size: the 52,649-row production snapshot imports to **11.6 MB**,
so 7 days at ~16,500 rows/day ≈ **25 MB**. In line with §21's ~20 MB estimate.

---

## 5. Not blocking the event loop (D46-③), and how that was verified

`record()` runs on the asyncio loop and does exactly two things that cost
anything: a `frozenset` membership test and a `put_nowait`. No I/O, no lock
held across a syscall, no `await`. A single daemon thread owns the
`sqlite3.Connection` — created *in that thread*, since sqlite3 objects are
thread-bound, which also makes the property structurally checkable.

Verification, three ways:

1. **Structural.** `writer._conn_thread_ident != threading.get_ident()` after
   a flush, and `record` is asserted not to be a coroutine function. If the
   connection belonged to the caller's thread, every fsync would land on
   whatever thread called `record` — the loop.
2. **Under load, in a test.** 60 inbound events through `_on_message_event`
   against a store patched to take **2 s per commit** must complete in < 1 s.
3. **Measured directly** (`asyncio` heartbeat probe, commits stalled at 0.5 s
   each, 500 real inbound events through the adapter):

```
500 inbound events, commits artificially 0.5s each
  total inbound wall time : 8.6 ms
  per-event max           : 0.051 ms
  per-event p99           : 0.010 ms
  max event-loop lag      : 0.43 ms
  writer stats            : queued=500 dropped=0 depth=499
```

`disconnect()` stops the writer through `asyncio.to_thread` with a 15 s cap,
so even the shutdown join never blocks the loop; the thread is a daemon, so a
wedged disk cannot delay process exit either.

---

## 6. Fail-open (D46-④)

Nothing in this module reaches the adapter as an exception.

| Failure | Behaviour |
|---|---|
| store cannot be opened at start | `start()` returns `False`, logs, archiving stays off, adapter unaffected |
| target is a WAL store | refused with a pointed error, archiving off |
| `INSERT` batch raises | batch counted in `failed`, dropped (not retried — a retry loop against a broken store spends the box's I/O re-failing), connection reopened next batch |
| prune raises | counted, next hourly pass tries again |
| queue full | row dropped and counted |
| anything unexpected in `record()` | caught, counted, `False` returned |
| writer thread dies | logged with traceback; `record()` then returns `False` forever |
| `resolve_config` / import explodes | `_start_group_history` catches it; the channel connects without an archive |

The adapter-level test does not assert this about the writer — it asserts the
**bot still answered** three messages while every commit was failing.

---

## 7. Backfill idempotency (D46-⑥), and how it was verified

Dedup key is **message identity**, not row identity:

* `(instance_id, group_id, message_id)` when the source row has a message id;
* `(instance_id, group_id, sender_user_id, event_time_ms, blake2b(text))` when
  it does not.

**Deliberately not keyed on `received_at_ms`.** The overlap window is the whole
reason the tool exists: the same QQ message captured by corlinman and by our
own live writer carries two different receive timestamps, so a timestamp key
would double every message in the coexistence period. The destination's
existing keys are read into a set first — bounded by retention (7 days ≈ 115k
rows) in a short-lived CLI process — which keeps the check exact without adding
an index to, and therefore changing the schema of, a file whose schema
compatibility is the entire point.

`received_at_ms` / `event_time_ms` are copied **verbatim**; the monitors window
on `received_at_ms`, so re-stamping would fold a week of history onto one
instant. The source is opened `mode=ro` throughout.

**Verified against the real 52,649-row production snapshot, through the actual
CLI:**

```
--dry-run          : scanned 52649  inserted 52649  duplicates 0      (nothing written)
run 1              : scanned 52649  inserted 52649  duplicates 0
run 2              : scanned 52649  inserted 0      duplicates 52649
run 3 (--days 7)   : scanned 52649  inserted 0      duplicates 52649
dest rows          : 52649   (== source rows)
per group          : 183287894 → 4583 ,  980927602 → 48066
source sha256 after: 96782a2b12fbab9c4da9e2684ba5fe17ecfabaa8bd24134ecdcb6334802cfab8 (unchanged)
```

Plus unit coverage for: duplicates *within* one source, NULL-`message_id` rows,
the same message already present under a different `received_at_ms`, identical
message ids in different groups (OneBot ids are only per-conversation unique),
`--days` / `--groups` / `--instance-id` filters, `--dry-run` writing nothing,
source == dest refused, and a byte-comparison proving the source is untouched.

---

## 8. Scope: whitelisted groups only, no DMs (D46-⑦)

* **Off by default.** `group_history_enabled` (or
  `ONEBOT_GROUP_HISTORY_ENABLED`) must be set. Recording real people's chat to
  disk on a host where corlinman is still the live service does not begin
  because a config file was merged.
* **Direct messages have no code path here.** The adapter calls the writer only
  from the group branch of `_on_message_event`; a test drives a private event
  through and asserts zero rows.
* **Capture set = explicit list ∩ `group_whitelist`**, else `group_whitelist`.
  An explicit list that filters down to nothing stays **off** rather than
  falling back to the whole whitelist — the same fix B4 made for
  `proactive_groups` (a mistyped id must not widen the target).
* **`group_whitelist = None`** ("no whitelist, any group may talk to the bot")
  leaves archiving **off** with a warning. An absent whitelist is not
  permission to archive every group the account is in.
* Capture sits **before** the router gate, matching corlinman's own capture
  point (`service.py` `_qq_dispatch_loop`, L2694-2716) — a digest of "what did
  the room say" that only saw answered messages would be worthless. It sits
  *after* the self-echo and dedup filters, so the bot's own posts and repeated
  events are not archived.
* **Privacy.** Counts and error strings only, in logs and in the CLI's output.
  No message text, no sender name, and no new outbound path for either.

---

## 9. Relationship to the 30-row in-memory buffer (B4)

They are complements, and both are fed at the same point:

| | `_GROUP_RECENT` (B4, `adapter.py`) | `group_history` (D3) |
|---|---|---|
| lives in | RAM, process lifetime | SQLite, 7 days |
| holds | 30 rows/group, 200-char cap | everything, 2000-char cap |
| includes the bot's own posts | yes | no |
| consumer | `proactive.py`'s prompt context | the three cron monitors |

Neither replaces the other and neither was changed to accommodate the other. A
test asserts the proactive buffer still receives messages after this change,
so "we broke B4's context window" fails a test rather than degrading quietly.

---

## 10. Verification performed

```
.venv/bin/python -m pytest tests/gateway/test_onebot_group_history.py -q
    → 81 passed

.venv/bin/python -m pytest tests/gateway/test_onebot_plugin.py \
    tests/gateway/test_onebot_proactive.py tests/gateway/test_onebot_router.py \
    tests/gateway/test_onebot_protocol.py tests/gateway/test_onebot_transport.py \
    tests/gateway/test_onebot_rate_limit.py tests/gateway/test_onebot_group_history.py \
    tests/tools/test_onebot_client.py tests/plugins/corlinman_jobs -q
    → 757 passed

# D2's own wide regression, to show this task changed nothing there
.venv/bin/python -m pytest tests/plugins/corlinman_jobs tests/plugins/qzone \
                          tests/plugins/grantley tests/cron -q
    → 1452 passed, 1 skipped     (identical to D2's §9 baseline)
```

Entirely offline: temporary directories, real SQLite files, a fake OneBot
client. No socket, no NapCat, no QQ session, no model call. The
`.migration-export` snapshot reads use the already-exported, gitignored local
copy. The only SSH in this task was read-only reconnaissance of corlinman's
source, and nothing on the production host was modified.

Zero upstream changes:

```
git diff --diff-filter=MDRT --stat 8911e2e0e..HEAD
    → .gitignore | 4 ++++   (C4's, not this task's)
```

---

## 11. Not done, known defects, residual risk

### 11.1 Not done, on purpose

* **`monitor_state` is created but never written.** corlinman's digest loop
  used it for last-fire bookkeeping; hermes cron owns scheduling here, so
  there is nothing to record. The table exists only so our file stays
  interchangeable with corlinman's.
* **No staleness check.** D2 §7.2's open item — `check_qq_group_history()`
  still reports OK for a store whose newest row is four days old. This writer
  makes staleness *less* likely, not detectable. A `MAX(received_at_ms)` check
  belongs in `preflight.py`, which is `corlinman_jobs`' file, and adding it
  here would have widened this task into D2's deliverable.
* **No `hermes` CLI subcommand for the backfill.** It runs as
  `python -m plugins.platforms.onebot.group_history_backfill`. Adding a
  subcommand would mean touching `hermes_cli/`, an upstream tree this
  migration has kept at zero modifications.
* **The archive is not surfaced in `health_snapshot()`.** Ops visibility is an
  hourly INFO heartbeat (written / queued / dropped / failed / depth) from the
  writer thread instead, to keep the blast radius off a structure other
  gateway code consumes.
* **No retry of a failed batch.** Deliberate; see §6.

### 11.2 Known defects and real risks

* **Up to `T` (30 s) of messages are lost on an unclean kill.** Bounded and
  documented, but real. `systemctl restart` runs `disconnect()` and flushes;
  SIGKILL, OOM-kill, and power loss do not. Given `OOMScoreAdjust=500` on this
  unit, OOM-kill is not hypothetical.
* **The WAL guard is a heuristic, not a proof.** It catches "the writer was
  aimed at corlinman's live file", because corlinman uses WAL and we do not.
  It would *not* catch two hermes instances configured onto the same file
  (both DELETE mode), and it *would* refuse a legitimate file somebody
  converted to WAL by hand. Both are logged clearly.
* **`synchronous=FULL` in DELETE mode is durable, not invulnerable.** Power
  loss mid-commit can still corrupt a rollback-journal database. The archive
  is regenerable from corlinman during coexistence and expendable after, but
  after cutover a corrupt file means the monitors go quiet until someone
  notices — which is the staleness gap above.
* **The backfill's key set is proportional to destination size.** ~115k keys
  (7 days) is tens of MB in a short-lived process. Importing into a very large
  destination on a 1.9 GB box would want the `--days` window rather than
  `--days 0`; the cutover runbook below uses `--days 7`.
* **Group ids are compared as text everywhere** (`str(group_id)`), matching
  D2's reader and corlinman's rows. A future caller passing an int-typed
  whitelist would silently match nothing. `_parse_groups` stringifies, so all
  supported config shapes are safe; a direct `GroupHistoryConfig(groups={123})`
  is not.
* **Two capture paths now read the message text**, and they disagree on
  ordering: the in-memory buffer prefers `raw_message`, the archive prefers
  `segments_to_text(...)`. That is intentional (each matches its own source's
  behaviour) but it means a backend whose `raw_message` differs from its
  segments produces two slightly different renderings. Documented in
  `_record_group_history`'s docstring.
* **Untested on the target host.** Everything here ran on macOS / SQLite
  3.53.1. The DELETE-mode reasoning, the fsync budget and the memory numbers
  are derived from the host's documented constraints, not measured on it.
  First real exercise is the cutover window.

---

## 12. Cutover runbook

Extends D2 §8. **Nothing below is done yet** — corlinman is still live.

### 12.1 Before cutover — start collecting, in parallel, harmlessly

Our writer and corlinman's write **different files**, so this can be switched
on well before cutover and costs corlinman nothing.

```yaml
# config.yaml — platforms.onebot.extra
group_whitelist: ["183287894", "980927602"]
group_history_enabled: true
group_history_groups: ["183287894", "980927602"]   # corlinman's monitored set
group_history_retention_days: 7
```

Leave `QQ_GROUP_HISTORY_DB` **pointing at corlinman's live file** — the
monitors keep reading fresh corlinman data throughout. Confirm the log line:

```
OneBot: group history archiving ON — db=... groups=['183287894', '980927602'] ...
```

and that rows are accumulating:

```bash
sqlite3 "$HERMES_HOME/plugin-data/corlinman_jobs/qq_group_history.sqlite" \
  "SELECT group_id, COUNT(*), MAX(received_at_ms) FROM group_messages GROUP BY 1;"
```

Let it run at least one full day before step 12.2, so the switchover is a
no-op rather than a leap.

### 12.2 At cutover — backfill, then move the reader

1. **Backfill** (dry run first; it is idempotent, so running it twice is fine
   and running it late is fine):

   ```bash
   cd /opt/hermes/repo
   /opt/hermes/venv/bin/python -m plugins.platforms.onebot.group_history_backfill \
       --source /opt/corlinman/execution-state/qq_group_history.sqlite \
       --days 7 --dry-run
   # then, without --dry-run
   ```

   Expect `inserted` > 0 on the first real run and `duplicates` ≈ everything
   already collected in 12.1. Run it **again** and expect `inserted: 0` — that
   is the idempotency check, on the real data, before it matters.

2. **Move the reader.** This is the one variable D2 built for:

   ```
   QQ_GROUP_HISTORY_DB=/var/lib/hermes/plugin-data/corlinman_jobs/qq_group_history.sqlite
   ```

   or simply **unset it** — reader and writer share that default path.

3. **Confirm** before trusting a scheduled run:

   ```bash
   hermes corlinman-jobs status        # → "qq_group_history.sqlite reachable, N row(s)"
   hermes cron trigger sanhu           # fire once, then: hermes cron logs sanhu
   ```

4. Only then retire corlinman.

### 12.3 Order, and why

Backfill **before** moving the reader. In that order the reader is never
pointed at a store that has not been filled yet; the reverse order gives the
monitors a window in which they read a nearly empty file and, with
`send_when_empty=false`, say nothing about it.

### 12.4 Rollback

Each step reverses independently, and none of them touches corlinman:

| Undo | How | Effect |
|---|---|---|
| the reader move | set `QQ_GROUP_HISTORY_DB` back to corlinman's file | monitors read corlinman again; requires corlinman still running |
| the backfill | it only ever inserts into **our** file; delete that file, or `DELETE FROM group_messages WHERE received_at_ms < <cutover ms>` | corlinman's file is opened `mode=ro` and provably unchanged |
| the writer | `group_history_enabled: false`, restart the channel | archiving stops; the file stays where it is |

The one-way door is retiring corlinman — after that, rolling the reader back
points it at a file nobody writes. Keep corlinman's store around (it is small)
until at least one full retention period of our own data has accumulated.
