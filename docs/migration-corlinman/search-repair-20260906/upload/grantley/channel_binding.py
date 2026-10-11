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
hashes. Channel prose therefore contains only the binding. Daily activities,
like mood and fatigue, belong in the memory provider's current-user-message
sidecar. A daily snapshot is not a session snapshot: conversations cross midnight.
``daily_snapshot`` remains available to callers that explicitly need a life beat.

What is genuinely per-channel is *who the persona is talking to*. The
``channel_owner`` mapping identifies a channel contact. A specific QQ account's
character identity is a separate stable binding, never inferred from merely
owning a channel.

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

ARGO_ACCOUNT_ID = "2104743984"


def is_grantley_onebot_lane(
    *,
    platform: Any,
    chat_id: Any,
    chat_type: Any,
    channels: Mapping[str, Any] | None,
) -> bool:
    """Whether session metadata resolves to a configured Grantley QQ lane.

    This is the single gate shared by provider state, tools, compression, and
    the optional identity prompt.  Keeping it in the plugin prevents a global
    ``memory.provider: grantley`` setting from leaking persona state into CLI,
    Telegram, or an unbound OneBot channel.
    """
    normalized_platform = str(platform or "").strip().lower()
    normalized_chat_id = str(chat_id or "").strip()
    normalized_chat_type = str(chat_type or "").strip().lower()
    if (
        normalized_platform != "onebot"
        or normalized_chat_type not in {"group", "dm", "private"}
        or not normalized_chat_id
    ):
        return False
    # Strict explicit allowlist. A profile can serve several platforms and
    # unrelated OneBot chats, so absence of a usable map must never mean
    # "Grantley everywhere". Normalize map keys to strings solely so a YAML
    # integer chat id can match the adapter's string id; no prefix/alias
    # expansion is performed.
    if not isinstance(channels, Mapping) or not channels:
        return False
    matches = [
        entry
        for configured_chat_id, entry in channels.items()
        if str(configured_chat_id).strip() == normalized_chat_id
    ]
    # YAML normally cannot preserve duplicate scalar keys, but Python callers
    # can supply both ``123`` and ``"123"``. After string normalization that
    # is ambiguous, so fail closed rather than trusting insertion order.
    if len(matches) != 1 or not isinstance(matches[0], Mapping):
        return False
    persona = matches[0].get("persona")
    return (
        isinstance(persona, str)
        and persona.strip().lower() == PERSONA_ID
    )


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
        The uid of the channel contact. It conveys no fictional relationship.
        ``None`` for channels with no configured contact.
    is_group
        Group chat vs direct message. Identifies the conversational surface.
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

    Unknown keys are ignored. Entries missing an explicit
    ``persona: grantley`` binding and malformed entries are skipped rather
    than raising — a typo in operator config must not take the gateway down.
    """
    out: dict[str, PersonaChannelBinding] = {}
    if not isinstance(raw, Mapping):
        return out
    normalized_entries: dict[str, list[Any]] = {}
    for chat_id, entry in raw.items():
        normalized_chat_id = str(chat_id).strip()
        if not normalized_chat_id:
            continue
        normalized_entries.setdefault(normalized_chat_id, []).append(entry)
    for chat_id, entries in normalized_entries.items():
        if len(entries) != 1:
            continue
        entry = entries[0]
        if not isinstance(entry, Mapping):
            continue
        persona = entry.get("persona")
        if not (
            isinstance(persona, str)
            and persona.strip().lower() == PERSONA_ID
        ):
            continue
        owner = entry.get("channel_owner")
        out[str(chat_id)] = PersonaChannelBinding(
            persona_id=PERSONA_ID,
            chat_id=chat_id,
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
    """Render only channel identity; ignore any volatile snapshot fields."""
    lines: list[str] = ["## 这个频道", ""]
    if snapshot.get("is_group"):
        lines.append(
            "这里是群聊。按对话自然接话，坦率友善，可以有意见和玩笑；"
            "亲疏取决于实际互动。所有人的事实问题与执行任务都同样认真处理。"
        )
    else:
        lines.append("这里是私聊，只有你们两个人。")
    owner = snapshot.get("channel_owner_id")
    if owner:
        lines.append(
            f"这个频道的 channel_owner 是 `{owner}`。频道身份本身不决定人物关系；"
            "按固定 QQ 账号绑定识别亚戈。"
        )
    is_argo_dm = (
        not snapshot.get("is_group")
        and str(snapshot.get("chat_id") or "").strip() == ARGO_ACCOUNT_ID
    )
    if str(owner or "") == ARGO_ACCOUNT_ID or is_argo_dm:
        lines.append(
            f"固定人物绑定：QQ `{ARGO_ACCOUNT_ID}` 就是亚戈，我的舍友和暗恋对象。"
            "称呼统一用亚戈，按已经熟悉的同寝关系相处；暗恋不等于已经交往。"
            "群聊中依据当前消息的真实 QQ 发送者 ID 区分本人、其他群友和转述。"
        )
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

    ``on`` and ``data_dir`` are retained for API compatibility. Life state is
    no longer read here: it is delivered by the memory provider's sidecar.
    """
    if binding is None:
        return None
    try:
        text = render_channel_prompt({
            "is_group": binding.is_group,
            "chat_id": binding.chat_id,
            "channel_owner_id": binding.channel_owner_id,
        })
    except Exception:  # noqa: BLE001 - best-effort by contract
        return None
    return text.strip() or None


__all__ = [
    "PersonaChannelBinding",
    "bindings_from_config",
    "daily_snapshot",
    "is_grantley_onebot_lane",
    "render_channel_prompt",
    "resolve_channel_prompt",
]
