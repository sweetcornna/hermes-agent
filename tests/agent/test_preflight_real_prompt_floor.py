"""Turn-prologue preflight honors the last real provider prompt count.

A turn that ends without tool calls never reaches the loop's real-usage
compression gate, so the next turn's preflight is the only chance to compact.
When the rough estimate undercounts (provider tokenizers on CJK + terminal
output), a session sat at 144k real tokens under a 128k threshold forever.
The previous request's real count is a floor for the next request, and a
rebuilt agent can be seeded with the session's persisted count.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from tests.agent.test_engine_preflight_wire import _build, _make_agent


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def _compressor(cap=8_000):
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=200_000
    ):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.75,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
            threshold_tokens_cap=cap,
        )


def test_rough_estimate_alone_stays_under_threshold():
    compressor = _compressor()
    agent = _make_agent(compressor)

    _build(agent)

    agent._compress_context.assert_not_called()


def test_real_count_over_threshold_triggers_preflight_compaction():
    compressor = _compressor()
    compressor.update_from_response({"prompt_tokens": 9_000, "completion_tokens": 10})
    agent = _make_agent(compressor)

    _build(agent)

    agent._compress_context.assert_called()


def test_seeded_count_triggers_compaction_on_a_rebuilt_agent():
    compressor = _compressor()
    compressor.seed_real_prompt_tokens(9_000)
    agent = _make_agent(compressor)

    _build(agent)

    agent._compress_context.assert_called()


def test_a_count_from_before_the_last_compaction_is_ignored():
    compressor = _compressor()
    compressor.update_from_response({"prompt_tokens": 9_000, "completion_tokens": 10})
    compressor.awaiting_real_usage_after_compression = True
    compressor.last_prompt_tokens = -1

    assert compressor.real_prompt_floor() == 0
    agent = _make_agent(compressor)
    _build(agent)
    agent._compress_context.assert_not_called()


def test_seed_never_overrides_a_live_reading():
    compressor = _compressor()
    compressor.update_from_response({"prompt_tokens": 3_000, "completion_tokens": 10})

    compressor.seed_real_prompt_tokens(9_000)

    assert compressor.real_prompt_floor() == 3_000


@pytest.mark.parametrize("value", [0, -5, None, "x"])
def test_seed_ignores_unusable_values(value):
    compressor = _compressor()

    compressor.seed_real_prompt_tokens(value)

    assert compressor.real_prompt_floor() == 0
