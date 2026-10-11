"""Validation for provider-scoped context-compression policy.

Memory providers may tune the existing compressor for one initialized
session, but the returned mapping is untrusted plugin input.  Keep the
contract narrow and normalize every field independently so one malformed
override cannot discard the provider's other valid fields or break agent
construction.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


MIN_COMPRESSION_THRESHOLD_RATIO = 0.10
MAX_COMPRESSION_THRESHOLD_RATIO = 0.95
MIN_COMPRESSION_THRESHOLD_TOKENS = 4_096
MAX_COMPRESSION_THRESHOLD_TOKENS = 2_000_000
MIN_COMPRESSION_TAIL_TOKENS = 256
MAX_COMPRESSION_TAIL_TOKENS = 1_000_000
MAX_MIN_TAIL_USER_MESSAGES = 32

_BOOLEAN_POLICY_FIELDS = frozenset(
    {
        "enabled",
        "abort_on_summary_failure",
        "in_place",
        "micro_compact",
    }
)
_POLICY_FIELDS = _BOOLEAN_POLICY_FIELDS | {
    "threshold",
    "threshold_tokens",
    "tail_tokens",
    "min_tail_user_messages",
}


def _finite_number(value: Any, *, allow_string: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif allow_string and isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def normalize_threshold_ratio(
    value: Any,
    *,
    allow_string: bool = False,
) -> float | None:
    """Return a safe compression ratio, or ``None`` for invalid input."""
    number = _finite_number(value, allow_string=allow_string)
    if number is None:
        return None
    if not MIN_COMPRESSION_THRESHOLD_RATIO <= number <= MAX_COMPRESSION_THRESHOLD_RATIO:
        return None
    return number


def _positive_integral_number(value: Any) -> int | None:
    """Accept integers and finite integral floats, never bools or strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        parsed = int(value)
    else:
        return None
    return parsed if parsed > 0 else None


def normalize_threshold_tokens(value: Any) -> int | None:
    """Normalize an optional absolute trigger with anti-thrash clamps."""
    if value is None:
        return None
    parsed = _positive_integral_number(value)
    if parsed is None:
        return None
    return max(
        MIN_COMPRESSION_THRESHOLD_TOKENS,
        min(MAX_COMPRESSION_THRESHOLD_TOKENS, parsed),
    )


def normalize_tail_tokens(value: Any) -> int | None:
    """Normalize an optional absolute verbatim-tail budget."""
    if value is None:
        return None
    parsed = _positive_integral_number(value)
    if parsed is None:
        return None
    return max(
        MIN_COMPRESSION_TAIL_TOKENS,
        min(MAX_COMPRESSION_TAIL_TOKENS, parsed),
    )


def normalize_provider_compression_policy(
    raw_policy: Any,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Validate a provider policy field-by-field.

    Invalid and unknown fields are omitted so the corresponding global
    ``compression`` value remains authoritative.  ``threshold_tokens=None``
    is a valid explicit override: it disables an inherited absolute cap while
    preserving the ratio trigger.
    """
    if not isinstance(raw_policy, Mapping):
        return {}, ("<policy>",)

    normalized: dict[str, Any] = {}
    rejected: list[str] = []
    for key, value in raw_policy.items():
        if key not in _POLICY_FIELDS:
            rejected.append(str(key))
            continue
        if key in _BOOLEAN_POLICY_FIELDS:
            if isinstance(value, bool):
                normalized[key] = value
            else:
                rejected.append(key)
            continue
        if key == "threshold":
            parsed_ratio = normalize_threshold_ratio(value)
            if parsed_ratio is None:
                rejected.append(key)
            else:
                normalized[key] = parsed_ratio
            continue
        if key == "threshold_tokens":
            if value is None:
                normalized[key] = None
                continue
            parsed_threshold = normalize_threshold_tokens(value)
            if parsed_threshold is None:
                rejected.append(key)
            else:
                normalized[key] = parsed_threshold
            continue
        if key == "tail_tokens":
            parsed_tail = normalize_tail_tokens(value)
            if parsed_tail is None:
                rejected.append(key)
            else:
                normalized[key] = parsed_tail
            continue
        if key == "min_tail_user_messages":
            parsed_min_tail = _positive_integral_number(value)
            if parsed_min_tail is None:
                rejected.append(key)
            else:
                normalized[key] = min(
                    MAX_MIN_TAIL_USER_MESSAGES,
                    parsed_min_tail,
                )

    return normalized, tuple(rejected)


def merge_provider_compression_policy(
    base_config: Any,
    raw_policy: Any,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Copy global config and overlay only validated provider fields."""
    merged = dict(base_config) if isinstance(base_config, Mapping) else {}
    normalized, rejected = normalize_provider_compression_policy(raw_policy)
    merged.update(normalized)
    return merged, rejected
