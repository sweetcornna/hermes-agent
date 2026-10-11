"""Native QQ backend settings within the existing OneBot platform.

Keeps conversation IDs, routing and persona configuration unchanged. These
are transport settings, not model tools or per-conversation prompt changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

WRITE_ACTIONS = frozenset({"send_private_msg", "send_group_msg", "send_msg", "delete_msg", "send_private_forward_msg", "send_group_forward_msg"})


@dataclass(frozen=True)
class NativeQQSettings:
    enabled: bool = False
    send_timeout_seconds: float = 35.0
    guard: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_extra(cls, extra: Mapping[str, Any]) -> "NativeQQSettings":
        raw = extra.get("native_qq", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError("platforms.onebot.native_qq must be a mapping")
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("native_qq.enabled must be a boolean")
        timeout = raw.get("send_timeout_seconds", 35)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 30 <= timeout <= 120:
            raise ValueError("native_qq.send_timeout_seconds must be between 30 and 120")
        guard = raw.get("guard", {})
        if not isinstance(guard, Mapping):
            raise ValueError("native_qq.guard must be a mapping")
        return cls(enabled, float(timeout), dict(guard))


def native_features(data: Any) -> frozenset[str] | None:
    if not isinstance(data, Mapping):
        return None
    native = data.get("native_qq")
    if not isinstance(native, Mapping) or type(native.get("schema")) is not int or native.get("schema") != 1:
        return None
    if native.get("delivery_confirmation") is not True or native.get("idempotency") is not True or native.get("guard") is not True:
        return None
    features = native.get("capabilities")
    if not isinstance(features, list) or not all(isinstance(value, str) for value in features):
        return None
    return frozenset(features)
