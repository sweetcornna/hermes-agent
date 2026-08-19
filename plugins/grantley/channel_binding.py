"""Per-channel persona binding (A3 G3) — the integration point for the adapter.

The problem A3 G3 states: ``channel_overrides.system_prompt`` is a static YAML
map keyed by ``chat_id``, which is perfect for "fixed persona on fixed
channel" and useless when the persona text has to be *computed* at runtime.
And ``register_system_prompt_section`` cannot help, because the session-info
mapping it receives carries no ``chat_id``
(``agent/system_prompt.py`` passes only ``session_id / model / provider /
platform / profile_name / cwd``).

The idiomatic answer, and the one this module implements, is the one every
other adapter already uses: **the adapter computes it.** A platform adapter
sets ``MessageEvent.channel_prompt`` (``gateway/platforms/base.py:2363``) on
each inbound message; the gateway folds it into ``combined_ephemeral``
(``gateway/run.py:5211-5213``), which is injected at API-call time and is
explicitly excluded from the cached system prompt.

Cache contract
--------------
Ephemeral is *legal* to vary between conversations but **must be byte-stable
within one**, because it is still part of the system message the provider
hashes. So this module returns a **daily frozen snapshot**:

* it reads today's life beat, which is deterministic per ``(persona, date)``
  (see :func:`plugins.grantley.life.daily_rng`) — not the live row, which a
  decay tick can change at any moment;
* it never reads ``fatigue``, ``mood`` or ``recent_topics``. Those decay
  continuously and belong in the per-turn sidecar, not here;
* it is a pure function of ``(binding, date)``, so two calls on the same day
  for the same channel are byte-identical — asserted by test.

What is genuinely per-channel is *who the persona is talking to*: the
``channel_owner`` (群主) that the character's 单相思 dynamic is written
around. In corlinman production that is QQ uid ``2104743984``. That mapping
cannot live in the shared identity prompt because it differs per group, which
is exactly why this layer exists.

Integration point for the OneBot adapter
----------------------------------------
The OneBot adapter (built separately, under ``plugins/platforms/onebot/``)
should, for each inbound message::

    from plugins.grantley.channel_binding import (
        PersonaChannelBinding, resolve_channel_prompt,
    )

    binding = PersonaChannelBinding(
        persona_id="grantley",
        chat_id=str(group_id or user_id),
        channel_owner_id=owner_map.get(str(group_id)),
        is_group=bool(group_id),
    )
    event.channel_prompt = resolve_channel_prompt(binding)

Nothing here imports anything from the adapter, and the adapter needs nothing
from hermes core to call it. If ``resolve_channel_prompt`` returns ``None``
the adapter simply leaves ``channel_prompt`` unset — the persona still works,
it just loses the per-channel owner framing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from . import life
from .persona import PERSONA_ID


@dataclass(frozen=True)
class PersonaChannelBinding:
    """How one persona attaches to one channel.

    Attributes
    ----------
    persona_id
        Persona slug — selects the seed pack and the state row.
    chat_id
        Platform-native channel id (QQ group id, or the peer uid for a DM).
    channel_owner_id
        The uid the character's ``channel_owner`` / 群主 dynamic points at.
        ``None`` for channels with no such relationship.
    is_group
        Group chat vs direct message. Changes the tone guidance the character
        prompt already specifies ("普通群友：随意痞帅、保持距离").
    display_name
        Optional human-readable channel name, for the operator's benefit.
    """

    persona_id: str = PERSONA_ID
    chat_id: str = ""
    channel_owner_id: str | None = None
    is_group: bool = False
    display_name: str = ""

    def key(self) -> str:
        """Stable identity for this binding — used as a snapshot cache key."""
        return f"{self.persona_id}:{self.chat_id}"


def bindings_from_config(raw: Mapping[str, Any] | None) -> dict[str, PersonaChannelBinding]:
    """Build bindings from a plain config mapping.

    Shape (``plugins.entries.grantley.settings.channels`` in ``config.yaml``,
    or the adapter's own ``extra``)::

        channels:
          "183287894":
            persona: grantley
            channel_owner: "2104743984"
            group: true
            name: "群聊-JLU"
          "536132102":
            persona: grantley
            group: false

    Unknown keys are ignored and a malformed entry is skipped rather than
    raising — a typo in operator config must not take the gateway down.
    """
    out: dict[str, PersonaChannelBinding] = {}
    if not isinstance(raw, Mapping):
        return out
    for chat_id, entry in raw.items():
        if not isinstance(entry, Mapping):
            continue
        owner = entry.get("channel_owner")
        out[str(chat_id)] = PersonaChannelBinding(
            persona_id=str(entry.get("persona") or PERSONA_ID),
            chat_id=str(chat_id),
            channel_owner_id=str(owner) if owner not in (None, "") else None,
            is_group=bool(entry.get("group", False)),
            display_name=str(entry.get("name") or ""),
        )
    return out


def daily_snapshot(
    binding: PersonaChannelBinding,
    *,
    on: date | datetime | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Return today's frozen persona snapshot for *binding*.

    Pure: derived only from the binding, the calendar date, and the seed
    pack. Contains **no** decaying value. Two calls on the same day return
    equal dicts.
    """
    if on is None:
        moment: datetime | date = life.now_dt()
    else:
        moment = on
    day = moment.date() if isinstance(moment, datetime) else moment

    seed_lib = life.resolve_seed_library(binding.persona_id, data_dir)
    rng = life.daily_rng(binding.persona_id, datetime.combine(day, datetime.min.time()))
    beat = life.draw_life_beat(seed_lib, rng)
    return {
        "persona_id": binding.persona_id,
        "chat_id": binding.chat_id,
        "date": day.isoformat(),
        "channel_owner_id": binding.channel_owner_id,
        "is_group": binding.is_group,
        "life_state": beat["life_state"],
        "activity": beat["activity"],
        "location": beat["location"],
        "companions": list(beat["companions"]),
    }


def render_channel_prompt(snapshot: Mapping[str, Any]) -> str:
    """Render the ephemeral per-channel prompt text from a frozen snapshot."""
    lines: list[str] = ["## 这个频道", ""]
    if snapshot.get("is_group"):
        lines.append("这里是群聊。对普通群友随意痞帅、保持距离，可以吐槽。")
    else:
        lines.append("这里是私聊，只有你们两个人。")
    owner = snapshot.get("channel_owner_id")
    if owner:
        lines.append(
            f"这个频道的 channel_owner / 群主是 `{owner}`——"
            "对他必须走「嘴硬 + 行动」双层，"
            "不能直接说出口，紧张感藏在别扭的关心里。"
        )
    activity = str(snapshot.get("activity") or "").strip()
    if activity:
        location = str(snapshot.get("location") or "").strip()
        companions = [str(c) for c in (snapshot.get("companions") or [])]
        today = f"今天你在{activity}"
        if location:
            today += f"（{location}）"
        today += "，" + (f"和{'、'.join(companions)}一起。" if companions else "一个人。")
        lines.append(today)
    return "\n".join(lines)


def resolve_channel_prompt(
    binding: PersonaChannelBinding | None,
    *,
    on: date | datetime | None = None,
    data_dir: Path | None = None,
) -> str | None:
    """The one function a platform adapter calls.

    Returns the ephemeral per-channel prompt, or ``None`` when there is
    nothing to say (no binding, or an empty render). Never raises: a persona
    is decorative, and chat must keep working when it breaks.
    """
    if binding is None:
        return None
    try:
        text = render_channel_prompt(daily_snapshot(binding, on=on, data_dir=data_dir))
    except Exception:  # noqa: BLE001 - best-effort by contract
        return None
    return text.strip() or None


__all__ = [
    "PersonaChannelBinding",
    "bindings_from_config",
    "daily_snapshot",
    "render_channel_prompt",
    "resolve_channel_prompt",
]
