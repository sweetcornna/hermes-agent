"""Inbound screening — keep the room's risk out of the bot's own mouth.

The outbound gates (:mod:`.agent_gate` + :mod:`plugins.qzone.policy`) decide
what leaves the process. This module decides what is allowed *in*, and it
exists because the outbound gates alone leave one thing open: a bot cannot be
frozen for what it reads, but it can very easily be *led* into saying
something it will be frozen for.

Three distinct places take inbound text, and a gate on only the first would be
a gate with two holes behind it:

1. **The turn trigger** — the message being answered. Blocked ⇒ no turn runs.
2. **The group context buffer** (``record_group_message``) — the last 30 lines
   of the room, replayed into every later prompt in that group.
3. **The persistent archive** (``_record_group_history``) — the 24h window
   that three cron monitors read back and *summarise*.

(2) and (3) are the ones that actually matter, and they are the reason this
module redacts rather than merely dropping. If a group spends twenty messages
on a banned topic and the bot simply declines to answer each one, those twenty
messages are still sitting in the buffer and the archive. The next innocuous
"在吗" replays all of them into the prompt, and the summariser will happily
restate them in a message the bot sends itself. Declining to answer does not
prevent that; refusing to *store* it does.

So blocked inbound text is replaced with :data:`REDACTION_PLACEHOLDER` at
every one of those three points — the same treatment, and the same string,
corlinman's ``_redact_feeds`` applies on the QZone lane.

Why this stage is deterministic-only
------------------------------------
There is no LLM stage here, deliberately. The outbound agent gate costs one
model call and measured ~3.3s median in production; adding a second one
inbound would roughly double every reply's latency, and it would buy very
little — the thing that freezes an account is the message the bot *sends*,
and that is already read by a model on the way out. Here the keyword table is
the right tool: it is free, it cannot time out, and blatant bait is exactly
the shape a keyword table is good at. Subtlety is caught on the outbound side,
where it counts.

Consequence: this gate is cheap enough to run on every inbound message,
including ones nobody will answer, which is what makes (2) and (3) coverable
at all.

Group silence is a feature
--------------------------
A blocked group message produces **no reply at all**, not a refusal. Answering
"这个话题不适合在 QQ 上讨论" in a room where someone just posted political bait
is the bot publicly attaching itself to that message — visible to every member
and to anything reading the group. Silence is both safer and indistinguishable
from the bot simply not being addressed. DMs do get one notice, rate-limited,
because there a silent bot just looks broken to the one person watching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional

from plugins.qzone import policy as content_policy

logger = logging.getLogger(__name__)

__all__ = [
    "INBOUND_GATE_VERSION",
    "REDACTION_PLACEHOLDER",
    "InboundGateConfig",
    "InboundVerdict",
    "NoticeCooldown",
    "resolve_inbound_gate_config",
    "screen_inbound",
]

INBOUND_GATE_VERSION: Final[str] = "onebot-inbound-gate-2026-08-28.1"

#: Byte-identical to corlinman's ``_redact_feeds`` placeholder. Kept the same
#: on purpose: both lanes now leave the same trace in a prompt, so one grep
#: finds every redaction regardless of which side it came from.
REDACTION_PLACEHOLDER: Final[str] = "[内容已按 QQ 风控策略隐藏]"

#: Long enough that a user spamming bait cannot turn the bot into a refusal
#: metronome, short enough that a genuine misunderstanding gets an explanation
#: rather than silence.
_DEFAULT_NOTICE_COOLDOWN_SECS: Final[float] = 300.0


@dataclass(frozen=True, slots=True)
class InboundGateConfig:
    enabled: bool = True
    #: Send a one-line notice in DMs when a message is blocked. Never applies
    #: to groups — see the module docstring.
    dm_notice: bool = True
    notice_cooldown_secs: float = _DEFAULT_NOTICE_COOLDOWN_SECS

    @classmethod
    def from_mapping(cls, value: object) -> InboundGateConfig:
        """Parse a config snapshot. Only an explicit ``False`` disables."""
        if not isinstance(value, Mapping):
            return cls()
        try:
            cooldown = float(
                value.get("notice_cooldown_secs", _DEFAULT_NOTICE_COOLDOWN_SECS)
            )
        except (TypeError, ValueError):
            cooldown = _DEFAULT_NOTICE_COOLDOWN_SECS
        return cls(
            enabled=value.get("enabled") is not False,
            dm_notice=value.get("dm_notice") is not False,
            notice_cooldown_secs=(
                cooldown if cooldown >= 0 else _DEFAULT_NOTICE_COOLDOWN_SECS
            ),
        )


@dataclass(frozen=True, slots=True)
class InboundVerdict:
    """The screening result.

    ``text`` is always the string safe to use downstream: the original when
    allowed, :data:`REDACTION_PLACEHOLDER` when not. Callers should use it
    unconditionally rather than branching on ``allowed`` — that way a caller
    that forgets the branch still stores the redacted form, which is the
    failure mode we can live with.
    """

    allowed: bool
    text: str
    category_codes: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()

    def audit_fields(self) -> dict[str, object]:
        """Loggable fields. Deliberately excludes the message text."""
        return {
            "gate": "inbound",
            "gate_version": INBOUND_GATE_VERSION,
            "allowed": self.allowed,
            "category_codes": list(self.category_codes),
            "rule_ids": list(self.rule_ids),
        }


def resolve_inbound_gate_config(snapshot: object = None) -> InboundGateConfig:
    """Resolve config, fail-safe to enabled-with-defaults."""
    try:
        return InboundGateConfig.from_mapping(snapshot)
    except Exception:  # noqa: BLE001
        return InboundGateConfig()


def screen_inbound(
    text: str, config: InboundGateConfig | None = None
) -> InboundVerdict:
    """Screen one inbound message against the deterministic tables.

    Fails **closed**, like every other use of this table: a classifier that
    raises redacts rather than passes. The cost of a false positive here is
    one message the bot does not see; the cost of a false negative is content
    the bot may later repeat.
    """
    cfg = config or InboundGateConfig()
    body = text if isinstance(text, str) else str(text or "")
    if not cfg.enabled or not body.strip():
        return InboundVerdict(allowed=True, text=body)

    try:
        decision = content_policy.classify_text_all(body)
    except Exception:  # noqa: BLE001 — fail closed, never fail open
        decision = content_policy.classifier_failure_decision(body)

    if decision.allowed:
        return InboundVerdict(allowed=True, text=body)
    return InboundVerdict(
        allowed=False,
        text=REDACTION_PLACEHOLDER,
        category_codes=decision.category_codes,
        rule_ids=decision.rule_ids,
    )


class NoticeCooldown:
    """Per-conversation rate limiter for the DM notice.

    Bounded: a gateway that runs for months must not accumulate one timestamp
    per person who ever tripped the gate. Eviction is by oldest-stamp because
    the value *is* the recency — no separate LRU bookkeeping needed.
    """

    __slots__ = ("_seen", "_limit")

    def __init__(self, limit: int = 512) -> None:
        self._seen: dict[str, float] = {}
        self._limit = max(1, limit)

    def should_notify(self, key: str, now: float, cooldown_secs: float) -> bool:
        """True at most once per *cooldown_secs* for *key*; records the stamp."""
        last = self._seen.get(key)
        if last is not None and now - last < cooldown_secs:
            return False
        self._seen[key] = now
        while len(self._seen) > self._limit:
            oldest = min(self._seen, key=self._seen.__getitem__)
            self._seen.pop(oldest, None)
        return True

    def clear(self) -> None:
        self._seen.clear()


def log_blocked(verdict: InboundVerdict, *, is_group: bool, where: Any) -> None:
    """One INFO line per blocked inbound message. Never logs the text.

    INFO rather than WARNING: a blocked inbound is the gate working as
    designed, not an operational problem. The outbound fall-throughs are the
    ones that need to stand out in the journal.
    """
    logger.info(
        "OneBot inbound gate: redacted a %s message in %s (categories=%s rules=%s)",
        "group" if is_group else "direct",
        where,
        ",".join(verdict.category_codes) or "-",
        ",".join(verdict.rule_ids) or "-",
    )
