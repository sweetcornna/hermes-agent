"""The corlinman scheduler jobs, expressed as hermes-native cron jobs.

This module is the single source of truth for the migration. Everything else
— the installer, the dry-run planner, the generated ``$HERMES_HOME/scripts/``
entry points and the tests — reads these specs and never re-states a cron
expression, a chat id or a parameter value of its own.

Three properties are load-bearing and are asserted by the test suite:

1. **Every spec declares its timezone explicitly** (D8). hermes cron has no
   per-job timezone — ``cron/jobs.py`` evaluates every schedule against
   ``hermes_time.now()``, i.e. one process-wide zone from ``HERMES_TIMEZONE``
   / ``config.yaml: timezone``. So the declaration here is a *contract*:
   :mod:`plugins.corlinman_jobs.preflight` refuses to install when the
   configured zone does not match, rather than letting a job silently fall
   back to the host's zone (the production host is ``Asia/Tokyo``, one hour
   off every schedule below).
2. **Nothing is enabled.** :data:`JobSpec.install_enabled` is ``False`` for
   all of them and the installer pauses each job immediately after creating
   it. ``hermes.daily_agenda`` additionally carries
   ``source_enabled=False`` — it was already off in production, which is a
   different fact from "off because we are mid-migration".
3. **The schedules are staggered off the hour** (P1). The target host runs
   SQLite 3.40.1, too old for hermes's WAL guard, so the store falls back to
   DELETE mode where every writer serialises behind an fsync;
   ``cron.max_parallel_jobs`` is 2 and must not be raised. Each spec records
   both its production cron and why its new minute was chosen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

#: Every migrated job declares this zone, verbatim from A1 §2. The three
#: in-code corlinman builtins used UTC; only ``persona.decay`` survives the
#: migration and it is hourly, which is zone-invariant for whole-hour
#: offsets — it is pinned to the same zone as everything else so the install
#: has exactly one timezone contract to check.
TIMEZONE = "Asia/Shanghai"

#: Telegram destination shared by the four Telegram-targeted jobs. Preserved
#: verbatim (D16): when the migration was written ``getChat`` answered
#: ``Bad Request: chat not found`` because neither bot was a member of the
#: chat, and the decision was to port the destination unchanged rather than
#: invent a working one. That decision paid off — on 2026-08-19 the user
#: added ``@sweetcornna2_bot`` to the chat and it was re-verified reachable:
#: ``getChat`` → ok, ``Corn Agents``, ``type=supergroup``, ``is_forum=True``,
#: the bot an ``administrator`` with ``can_manage_topics``, and all four
#: forum topics (11 / 12 / 13 / 680) valid. Had the migration "fixed" the id,
#: it would now have to be reverted.
TELEGRAM_CHAT_ID = -1003990634877

#: The QQ account grantley posts from, verbatim from A1 §2.
QQ_ACCOUNT = "1010679324"

#: Persona slug used by the QQ jobs and by the qzone sidecar files.
PERSONA_ID = "grantley"


def telegram_deliver(topic_id: int) -> str:
    """hermes ``deliver`` string for a Telegram forum topic.

    ``<platform>:<chat_id>:<thread_id>`` — parsed by
    ``tools/send_message_tool._TELEGRAM_TOPIC_TARGET_RE``.
    """
    return f"telegram:{TELEGRAM_CHAT_ID}:{topic_id}"


@dataclass(frozen=True)
class JobSpec:
    """One migrated job.

    ``source_*`` fields are the corlinman definition, reproduced so the
    old→new mapping can be regenerated from code instead of from prose.
    """

    #: hermes cron job name. Kept byte-identical to the corlinman job name so
    #: the mapping is 1:1 and ``hermes cron <verb> <name>`` works.
    name: str
    #: corlinman ``source_job_id`` (its idempotency key), or None for the
    #: in-code builtin defaults which had no id.
    source_job_id: Optional[str]
    #: corlinman ``action_type``.
    source_action_type: str
    #: corlinman ``cron`` field, verbatim.
    source_cron: str
    #: corlinman ``timezone`` field, verbatim.
    source_timezone: str
    #: corlinman ``enabled`` flag as it stands in production.
    source_enabled: bool

    #: The hermes schedule actually installed (staggered — see P1).
    schedule: str
    #: Why this minute.
    stagger_reason: str

    #: Job prompt (None for ``no_agent`` script jobs).
    prompt: Optional[str]
    #: Filename under ``$HERMES_HOME/scripts/``. Context script for agent
    #: jobs, or the job itself when ``no_agent``.
    script: Optional[str] = None
    no_agent: bool = False
    #: hermes ``deliver`` string.
    deliver: str = "local"
    #: Toolset allowlist. ``None`` means "hermes default", which no migrated
    #: job wants — every agent job here names its toolsets.
    enabled_toolsets: Optional[Sequence[str]] = None

    #: The verbatim corlinman parameter bag (``metadata``), carried so the
    #: generated scripts and the mapping table read from one place.
    params: Mapping[str, Any] = field(default_factory=dict)

    #: True when a successful run publishes to the real public QQ feed. Gates
    #: the extra confirmation in ``enable`` and forbids agent dry-runs (D17).
    writes_public_feed: bool = False

    #: Free-form per-job behavioural note for the migration document.
    notes: str = ""

    # -- invariants ---------------------------------------------------------

    #: Declared timezone. Always :data:`TIMEZONE`; kept as a field so the
    #: preflight check reads a per-job value rather than a global constant.
    timezone: str = TIMEZONE

    #: Never true. Present so the property is greppable and testable rather
    #: than implicit in installer code.
    install_enabled: bool = False

    @property
    def disabled_reason(self) -> str:
        """Why this job lands paused — the two cases are different facts."""
        if not self.source_enabled:
            return "disabled in production before the migration (source enabled=false)"
        return "disabled by the migration; enable explicitly at cutover"

    @property
    def dry_run_agent_safe(self) -> bool:
        """Whether ``--with-agent`` may drive a real model turn for this job.

        False for anything holding the qzone write tools: a dry run can
        suppress *delivery*, but it cannot stop the agent from calling
        ``qzone_publish`` / ``qzone_post_comment``, which write to a real
        public feed with no undo.
        """
        return not self.writes_public_feed


# ---------------------------------------------------------------------------
# The nine migrated jobs, in schedule order.
# ---------------------------------------------------------------------------

JOB_SPECS: tuple[JobSpec, ...] = (
    JobSpec(
        name="hermes.daily_agenda",
        source_job_id="fc6c8be7d0cb",
        source_action_type="personal.daily_agenda",
        source_cron="0 7 * * *",
        source_timezone="Asia/Shanghai",
        source_enabled=False,
        schedule="7 7 * * *",
        stagger_reason=(
            "07:00 → 07:07. Off the hour so it never queues behind the hourly "
            "decay tick; alone in its hour."
        ),
        prompt=None,
        script="corlinman_daily_agenda.py",
        no_agent=True,
        deliver=telegram_deliver(12),
        params={
            "telegram_chat_id": TELEGRAM_CHAT_ID,
            "telegram_topic_id": 12,
            "agenda_path": "scheduler_data/class_schedule.yaml",
        },
        notes=(
            "Fully deterministic in the source (no model call), so it ports to "
            "a no_agent script. The SVG→PNG card is preserved: the script emits "
            "a MEDIA: tag when rsvg-convert renders one and falls back to the "
            "text agenda otherwise, exactly like the source."
        ),
    ),
    JobSpec(
        name="hermes.competition_daily",
        source_job_id="ead0ccfdbd38",
        source_action_type="briefing.competition_daily",
        source_cron="0 9 * * *",
        source_timezone="Asia/Shanghai",
        source_enabled=True,
        schedule="9 9 * * *",
        stagger_reason=(
            "09:00 → 09:09. Two jobs share the 09:00 hour; this one takes the "
            "earlier slot and sits 15 minutes clear of hermes.qzone_reply."
        ),
        prompt=None,  # filled in below from prompts.py
        deliver=telegram_deliver(13),
        enabled_toolsets=("web",),
        params={"telegram_chat_id": TELEGRAM_CHAT_ID, "telegram_topic_id": 13},
        notes=(
            "Pure research turn — no state, no watermark. The only port change "
            "is that the source's system_prompt is folded into the job prompt."
        ),
    ),
    JobSpec(
        name="hermes.qzone_reply",
        source_job_id="3d43e796bdc4",
        source_action_type="qzone.reply_comments",
        source_cron="0 9,21 * * *",
        source_timezone="Asia/Shanghai",
        source_enabled=True,
        schedule="24 9,21 * * *",
        stagger_reason=(
            "09:00/21:00 → 09:24/21:24. 15 minutes after competition_daily in "
            "the morning, alone in the evening; both slots clear of the :17 "
            "decay tick."
        ),
        prompt=None,
        deliver="local",
        enabled_toolsets=("onebot",),
        params={
            "persona_id": PERSONA_ID,
            "qq_account": QQ_ACCOUNT,
            "max_replies": 3,
            "lookback_posts": 15,
            "qq_instance_id": "default",
        },
        writes_public_feed=True,
        notes=(
            "Prompt is RECONSTRUCTED from A1 §3 / A5 §3.12 — corlinman's "
            "qzone_reply.py was not exported. Dedup is no longer a prompt hint: "
            "plugins/qzone/state.py enforces it inside qzone_post_comment."
        ),
    ),
    JobSpec(
        name="hermes.qzone_friends",
        source_job_id="63c47a8759a3",
        source_action_type="qzone.comment_friends",
        source_cron="30 13 * * *",
        source_timezone="Asia/Shanghai",
        source_enabled=True,
        schedule="33 13 * * *",
        stagger_reason=(
            "13:30 → 13:33. :30 is the other minute operators reach for by "
            "reflex; :33 keeps the slot recognisable while leaving it unshared."
        ),
        prompt=None,
        deliver="local",
        enabled_toolsets=("onebot",),
        params={
            "persona_id": PERSONA_ID,
            "owner_uin": "2104743984",
            "qq_instance_id": "default",
        },
        writes_public_feed=True,
        notes=(
            "Behaviour read out of the private plugin's qzone_friends.py, which "
            "does exist even though A2/A5 found no builtin in corlinman's own "
            "repo. The on_mission skip is NOT ported — see D1 notes."
        ),
    ),
    JobSpec(
        name="hermes.analysis_digest",
        source_job_id="43f40d8e09f3",
        source_action_type="personal.analysis_digest",
        source_cron="0 15 * * *",
        source_timezone="Asia/Shanghai",
        source_enabled=True,
        schedule="12 15 * * *",
        stagger_reason="15:00 → 15:12. Off the hour; alone in its hour.",
        prompt=None,
        script="corlinman_analysis_material.py",
        deliver=telegram_deliver(680),
        enabled_toolsets=("web",),
        params={
            "telegram_chat_id": TELEGRAM_CHAT_ID,
            "telegram_topic_id": 680,
            "user_id": "1114483029",
            "channels": ["telegram", "gateway"],
        },
        notes=(
            "The 24h keyword filter over the journal is deterministic and moves "
            "into the context script; the model only summarises. Unlike the "
            "source, the no-material path still costs one model call."
        ),
    ),
    JobSpec(
        name="hermes.qzone_daily",
        source_job_id="1d116b77bed7",
        source_action_type="qzone.daily_publish",
        source_cron="0 22 * * *",
        source_timezone="Asia/Shanghai",
        source_enabled=True,
        schedule="21 22 * * *",
        stagger_reason="22:00 → 22:21. Off the hour; alone in its hour.",
        prompt=None,
        script="corlinman_qzone_recent_posts.py",
        deliver="local",
        enabled_toolsets=("onebot",),
        params={
            "persona_id": PERSONA_ID,
            "qq_account": QQ_ACCOUNT,
            "qq_instance_id": "default",
            "prompt_template": (
                "用今日的视角写一条 200 字以内的 QQ 空间说说。语气轻松自然，"
                "结合此刻生活状态，避免重复近期内容；结尾调用 qzone_publish 发布。"
            ),
        },
        writes_public_feed=True,
        notes=(
            "The anti-repeat corpus (the last publishes) is injected by a "
            "context script reading plugins/qzone/state.py's public "
            "post_log_entries(). If that script fails the prompt orders a "
            "[SILENT] no-publish, because a cron script failure does not abort "
            "the run."
        ),
    ),
    JobSpec(
        name="hermes.youtube_daily",
        source_job_id="03e42ec536f8",
        source_action_type="briefing.youtube_daily",
        source_cron="0 23 * * *",
        source_timezone="Asia/Shanghai",
        source_enabled=True,
        schedule="6 23 * * *",
        stagger_reason=(
            "23:00 → 23:06. Shares the 23:00 hour with diary_summary; taking "
            "the early slot leaves 35 minutes for the long research turn."
        ),
        prompt=None,
        script="corlinman_youtube_state.py",
        deliver=telegram_deliver(680),
        enabled_toolsets=("web",),
        params={
            "telegram_chat_id": TELEGRAM_CHAT_ID,
            "telegram_topic_id": 680,
            "youtube_channels": [
                "https://www.youtube.com/@tiabtc",
                "https://www.youtube.com/@CakeBaBa",
            ],
            "state_file": "scheduler_state/youtube_daily.json",
        },
        notes=(
            "Watermark job. The context script both emits the seen-id block and "
            "harvests the PREVIOUS run's ids — and only when that run delivered "
            "successfully, mirroring the source's "
            "`if delivery.ok and not shadow` rule."
        ),
    ),
    JobSpec(
        name="hermes.diary_summary",
        source_job_id="5a2aa0aaa7de",
        source_action_type="personal.diary_summary",
        source_cron="30 23 * * *",
        source_timezone="Asia/Shanghai",
        source_enabled=True,
        schedule="41 23 * * *",
        stagger_reason=(
            "23:30 → 23:41. 35 minutes after youtube_daily, which is the "
            "longest-running job in the set."
        ),
        prompt=None,
        script="corlinman_diary_material.py",
        deliver=telegram_deliver(11),
        enabled_toolsets=(),  # source ran this turn with tools_enabled=False
        params={
            "telegram_chat_id": TELEGRAM_CHAT_ID,
            "telegram_topic_id": 11,
            "user_id": "1114483029",
        },
        notes=(
            "Tool-free turn, matching the source's tools_enabled=False. Secret "
            "redaction, the noise-prefix filter and the per-message / total "
            "character caps all move into the context script."
        ),
    ),
    JobSpec(
        name="persona.decay",
        source_job_id=None,
        source_action_type="persona.decay",
        source_cron="0 0 */1 * * * *",
        source_timezone="UTC",
        source_enabled=True,
        schedule="17 * * * *",
        stagger_reason=(
            "Hourly at :00 → hourly at :17. :00 collided with five other jobs; "
            ":17 is at least 4 minutes clear of every other minute in the set. "
            "The tick is a sub-second no_agent script, so a small margin is "
            "enough."
        ),
        prompt=None,
        script="corlinman_grantley_decay.py",
        no_agent=True,
        deliver="local",
        params={},
        notes=(
            "Re-pointed at plugins/grantley's decay, which resolves its store "
            "explicitly (D15). corlinman's version failed 1803/1803 times with "
            "data_dir_unavailable and has never once decayed a row."
        ),
    ),
)


#: Names of the three migrated QQ group-digest monitors (D2), as a frozenset
#: other modules can test membership against without importing MONITOR_SPECS.
MONITOR_NAMES: frozenset[str] = frozenset({"qunjlu", "sanhu", "jlu"})


# ---------------------------------------------------------------------------
# The three QQ monitors (D2) — daily group-chat digests, a different
# corlinman subsystem from the twelve scheduler jobs above (config lives in
# ``[[channels.qq.instances.default.monitors]]``, not
# ``scheduler_runtime_jobs.json``; A1 §4). Kept in a separate tuple rather
# than folded into JOB_SPECS: the "12 source jobs = 9 installed + 3 dropped"
# accounting above is a real, tested count of a real, separate corlinman
# list, and merging a different list into it would make that count lie.
# ``ALL_SPECS`` below is what the installer actually iterates.
#
# All three share four properties, verbatim from A1 §4 / the exported
# config.toml (L59-L110): schedule_type="daily", window_minutes=1440,
# send_when_empty=false, timezone="" (unset — see the timezone note below).
# ---------------------------------------------------------------------------

MONITOR_SPECS: tuple[JobSpec, ...] = (
    JobSpec(
        name="qunjlu",
        source_job_id=None,
        source_action_type="qq.monitor_digest",
        source_cron='schedule_type="daily", daily_time="09:00"',
        source_timezone="",
        source_enabled=True,
        schedule="5 8 * * *",
        stagger_reason=(
            "Nominal 09:00, but no monitor declares its own timezone and "
            "none is set on the instance either, so all three evaluated "
            "against the process-local zone — Asia/Tokyo in production "
            "(A1 §4). 09:00 JST = 08:00 China time is what actually fired "
            "(D25). 08:00 → 08:05: nothing else runs in the 08:00 hour, so "
            "the only thing to clear is persona.decay's hourly :17 tick and "
            "the :00 mark itself."
        ),
        prompt=None,  # filled in below
        script="corlinman_qq_monitor_qunjlu.py",
        deliver="local",
        enabled_toolsets=(),
        params={
            "qq_instance_id": "default",
            "group_id": "183287894",
            "watch_user_ids": ("1076712858",),
            "focus_user_ids": (),
            "target_type": "group",
            "target_id": "183287894",
            "window_minutes": 1440,
            "style_extra": "",
            "send_when_empty": False,
            "daily_time": "09:00",
        },
        writes_public_feed=False,
        notes=(
            "D26: production's [channels.qq.instances.default] carries "
            "group_replies_enabled=false, an emergency mute that "
            "_qq_monitor_run_once checks BEFORE fetching any history or "
            "generating any text for a group-type target — qunjlu's digest "
            "has never once reached the group. The migrated job must stay "
            "equally unable to post there, and not by re-checking that same "
            "config flag: this port's OneBot adapter only reads "
            "group_replies_enabled inside router.py (passive replies) and "
            "proactive.py (proactive speech) — adapter.send() itself, which "
            "is what a cron deliver=onebot:g... target actually calls, does "
            "not consult it at all. Copying the flag's name into this job "
            "would therefore recreate exactly the failure class this "
            "migration has already hit five times ('config says off, "
            "behaviour says on') in the opposite direction. Suppression "
            "here is structural instead: deliver='local' means "
            "cron._resolve_delivery_targets returns no target no matter "
            "what the model writes, and enabled_toolsets=() means the "
            "model has no tool capable of sending anything on its own — two "
            "checks that do not depend on any runtime config value. The "
            "script still runs and still produces a real digest (visible "
            "in `hermes cron logs qunjlu`), so an operator can see what "
            "would have been sent before deciding to send it. Lifting the "
            "suppression means editing this spec's deliver to "
            "'onebot:g183287894' and reinstalling (D1's installer never "
            "updates an existing job, so that means `hermes cron rm "
            "qunjlu` first) — a reviewed code change, not a config flip."
        ),
    ),
    JobSpec(
        name="sanhu",
        source_job_id=None,
        source_action_type="qq.monitor_digest",
        source_cron='schedule_type="daily", daily_time="10:00"',
        source_timezone="",
        source_enabled=True,
        schedule="5 9 * * *",
        stagger_reason=(
            "Nominal 10:00 JST = 09:00 China time (D25, same reasoning as "
            "qunjlu). 09:00 → 09:05: the 09:00 hour also carries "
            "hermes.competition_daily (:09) and hermes.qzone_reply (:24); "
            "05 sits ahead of both with >=4 minutes of clearance and is "
            "clear of the :17 decay tick."
        ),
        prompt=None,
        script="corlinman_qq_monitor_sanhu.py",
        deliver="onebot:2104743984",
        enabled_toolsets=(),
        params={
            "qq_instance_id": "default",
            "group_id": "980927602",
            "watch_user_ids": (),
            "focus_user_ids": (),
            "target_type": "user",
            "target_id": "2104743984",
            "window_minutes": 1440,
            "style_extra": "",
            "send_when_empty": False,
            "daily_time": "10:00",
        },
        writes_public_feed=False,
        notes=(
            "Everyone in group 980927602, no focus filter. Delivery target "
            "2104743984 is a private chat, unaffected by "
            "group_replies_enabled either in the source or in this port. "
            "980927602 was the busiest tracked group (45,578 of the "
            "52,649-row export) — most days this monitor's window exceeds "
            "the single-turn message cap; see "
            "corlinman_jobs_lib.QQ_MONITOR_PROMPT_MESSAGE_CAP for why the "
            "source's parallel map-reduce summarisation is not reproduced "
            "and what happens instead."
        ),
    ),
    JobSpec(
        name="jlu",
        source_job_id=None,
        source_action_type="qq.monitor_digest",
        source_cron='schedule_type="daily", daily_time="11:00"',
        source_timezone="",
        source_enabled=True,
        schedule="5 10 * * *",
        stagger_reason=(
            "Nominal 11:00 JST = 10:00 China time (D25, same reasoning as "
            "qunjlu). 10:00 → 10:05: nothing else runs in the 10:00 hour, "
            "so the only thing to clear is the :17 decay tick and :00 "
            "itself."
        ),
        prompt=None,
        script="corlinman_qq_monitor_jlu.py",
        deliver="onebot:2104743984",
        enabled_toolsets=(),
        params={
            "qq_instance_id": "default",
            "group_id": "183287894",
            "watch_user_ids": (),
            "focus_user_ids": ("1076712858",),
            "target_type": "user",
            "target_id": "2104743984",
            "window_minutes": 1440,
            "style_extra": "",
            "send_when_empty": False,
            "daily_time": "11:00",
        },
        writes_public_feed=False,
        notes=(
            "Everyone in group 183287894 (same group qunjlu reads, "
            "different filter: jlu has no watch_user_ids, so it collects "
            "everyone — 1076712858 is only *focus*-marked, not filtered "
            "to), delivered privately to 2104743984. focus_user_ids never "
            "narrows the collected rows (source "
            "_QqMonitorSource.collection_ids: watch_user_ids empty means "
            "'everyone', independent of focus); it only ★-marks that "
            "sender's lines and earns them a dedicated closing paragraph "
            "in the prompt. Unaffected by group_replies_enabled — the "
            "target is a private chat, not group 183287894 itself."
        ),
    ),
)


def _with_monitor_prompts(specs: tuple[JobSpec, ...]) -> tuple[JobSpec, ...]:
    from dataclasses import replace

    from . import prompts

    out = []
    for spec in specs:
        out.append(
            replace(
                spec,
                prompt=prompts.qq_monitor_digest(
                    focus_user_ids=tuple(spec.params["focus_user_ids"]),
                    style_extra=str(spec.params["style_extra"]),
                ),
            )
        )
    return tuple(out)


MONITOR_SPECS = _with_monitor_prompts(MONITOR_SPECS)


@dataclass(frozen=True)
class DroppedJob:
    """A source job deliberately not migrated."""

    name: str
    source_action_type: str
    source_cron: str
    reason: str


DROPPED_JOBS: tuple[DroppedJob, ...] = (
    DroppedJob(
        name="system.update_check",
        source_action_type="system.update_check",
        source_cron="0 0 */6 * * * *",
        reason=(
            "hermes already does this. hermes_cli/banner.py::check_for_updates() "
            "polls upstream and caches the verdict for 6 hours — the same "
            "interval corlinman's job used — and it is surfaced by the banner, "
            "`hermes update --check` and the dashboard's "
            "/api/hermes/update/check. A cron job would be a second poller "
            "against the same cache file for no added signal. This is the one "
            "dropped job that was healthy in production (90/90)."
        ),
    ),
    DroppedJob(
        name="evolution.darwin_curate",
        source_action_type="evolution.darwin_curate",
        source_cron="0 30 3 * * * *",
        reason=(
            "No hermes equivalent worth carrying. It scored SKILL.md files and "
            "emitted quality signals into corlinman's evolution engine, which "
            "was not ported (A3 lists the evolution driver as gap G4, and "
            "plugins/grantley implements only decay and life_advance). It also "
            "has no behaviour to preserve: 79/79 recorded firings across both "
            "history files returned data_dir_unavailable, so it has never "
            "scanned a single skill. Migrating it would mean writing a new "
            "curator and inventing its consumer."
        ),
    ),
    DroppedJob(
        name="grantley.qzone_reply",
        source_action_type="qzone.reply_comments",
        source_cron="unrecoverable",
        reason=(
            "Decision D9. Its cron/timezone/enabled are unrecoverable, it has "
            "fired zero times since the 2026-07-27 storage split, and "
            "hermes.qzone_reply covers the same action with the same persona."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Prompt wiring — kept out of the table above so the table stays readable.
# ---------------------------------------------------------------------------

def _with_prompts(specs: tuple[JobSpec, ...]) -> tuple[JobSpec, ...]:
    from dataclasses import replace

    from . import prompts

    bodies = {
        "hermes.competition_daily": prompts.COMPETITION_DAILY,
        "hermes.youtube_daily": prompts.YOUTUBE_DAILY,
        "hermes.diary_summary": prompts.DIARY_SUMMARY,
        "hermes.analysis_digest": prompts.ANALYSIS_DIGEST,
    }
    out = []
    for spec in specs:
        if spec.name in bodies:
            out.append(replace(spec, prompt=bodies[spec.name]))
        elif spec.name == "hermes.qzone_friends":
            out.append(
                replace(
                    spec,
                    prompt=prompts.qzone_comment_friends(str(spec.params["owner_uin"])),
                )
            )
        elif spec.name == "hermes.qzone_daily":
            out.append(
                replace(
                    spec,
                    prompt=prompts.qzone_daily_publish(str(spec.params["prompt_template"])),
                )
            )
        elif spec.name == "hermes.qzone_reply":
            out.append(
                replace(
                    spec,
                    prompt=prompts.qzone_reply_comments(
                        int(spec.params["max_replies"]),
                        int(spec.params["lookback_posts"]),
                    ),
                )
            )
        else:
            out.append(spec)
    return tuple(out)


JOB_SPECS = _with_prompts(JOB_SPECS)

#: Everything this plugin can plan/install/status: the nine scheduler jobs
#: (schedule order) followed by the three monitors. The single iterable the
#: installer and preflight actually default to. Built here, after both
#: ``_with_prompts`` rebindings above, so it holds the prompt-wired specs —
#: not the ``prompt=None`` placeholders those tuples started as.
ALL_SPECS: tuple[JobSpec, ...] = JOB_SPECS + MONITOR_SPECS

#: Name → spec, across both the nine scheduler jobs and the three monitors.
SPECS_BY_NAME: dict[str, JobSpec] = {spec.name: spec for spec in ALL_SPECS}


def spec_by_name(name: str) -> JobSpec:
    """Look up one spec, raising a listing error rather than KeyError."""
    try:
        return SPECS_BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"unknown job {name!r}; known jobs: {', '.join(sorted(SPECS_BY_NAME))}"
        ) from None


__all__ = [
    "ALL_SPECS",
    "DROPPED_JOBS",
    "JOB_SPECS",
    "MONITOR_NAMES",
    "MONITOR_SPECS",
    "PERSONA_ID",
    "QQ_ACCOUNT",
    "SPECS_BY_NAME",
    "TELEGRAM_CHAT_ID",
    "TIMEZONE",
    "DroppedJob",
    "JobSpec",
    "spec_by_name",
    "telegram_deliver",
]
