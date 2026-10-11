"""Tests for tools.tool_output_limits.

Covers:
1. Default values when no config is provided.
2. Config override picks up user-supplied max_bytes / max_lines /
   max_line_length.
3. Malformed values (None, negative, wrong type) fall back to defaults
   rather than raising.
4. Integration: the helpers return what the terminal_tool and
   file_operations call paths will actually consume.

Port-tracking: anomalyco/opencode PR #23770
(feat(truncate): allow configuring tool output truncation limits).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools import tool_output_limits as tol


@pytest.fixture(autouse=True)
def _reset_limits_cache():
    """get_tool_output_limits() now memoizes its result for the process
    lifetime, so each test must start from a clean cache to observe the
    config value it patches in."""
    tol._reset_tool_output_limits_cache()
    yield
    tol._reset_tool_output_limits_cache()


class TestDefaults:
    def test_defaults_match_previous_hardcoded_values(self):
        assert tol.DEFAULT_MAX_BYTES == 50_000
        assert tol.DEFAULT_MAX_LINES == 2000
        assert tol.DEFAULT_MAX_LINE_LENGTH == 2000


    def test_get_limits_returns_defaults_when_load_config_raises(self):
        def _boom():
            raise RuntimeError("boom")

        with patch("hermes_cli.config.load_config", side_effect=_boom):
            limits = tol.get_tool_output_limits()
        assert limits["max_lines"] == tol.DEFAULT_MAX_LINES


class TestOverrides:
    def test_user_config_overrides_all_three(self):
        cfg = {
            "tool_output": {
                "max_bytes": 100_000,
                "max_lines": 5000,
                "max_line_length": 4096,
            }
        }
        with patch("hermes_cli.config.load_config", return_value=cfg):
            limits = tol.get_tool_output_limits()
        assert limits == {
            "max_bytes": 100_000,
            "max_lines": 5000,
            "max_line_length": 4096,
            "max_result_chars": None,
        }


    def test_section_not_a_dict_falls_back(self):
        cfg = {"tool_output": "nonsense"}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            limits = tol.get_tool_output_limits()
        assert limits["max_bytes"] == tol.DEFAULT_MAX_BYTES


class TestCoercion:
    @pytest.mark.parametrize("bad", [None, "not a number", -1, 0, [], {}])
    def test_invalid_values_fall_back_to_defaults(self, bad):
        cfg = {"tool_output": {"max_bytes": bad, "max_lines": bad, "max_line_length": bad}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            limits = tol.get_tool_output_limits()
        assert limits["max_bytes"] == tol.DEFAULT_MAX_BYTES
        assert limits["max_lines"] == tol.DEFAULT_MAX_LINES
        assert limits["max_line_length"] == tol.DEFAULT_MAX_LINE_LENGTH

    def test_string_integer_is_coerced(self):
        cfg = {"tool_output": {"max_bytes": "75000"}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            limits = tol.get_tool_output_limits()
        assert limits["max_bytes"] == 75_000


class TestShortcuts:
    def test_individual_accessors_delegate_to_get_tool_output_limits(self):
        cfg = {
            "tool_output": {
                "max_bytes": 111,
                "max_lines": 222,
                "max_line_length": 333,
            }
        }
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert tol.get_max_bytes() == 111
            assert tol.get_max_lines() == 222
            assert tol.get_max_line_length() == 333


class TestDefaultConfigHasSection:
    """The DEFAULT_CONFIG in hermes_cli.config must expose tool_output so
    that ``hermes setup`` and default installs stay in sync with the
    helpers here."""

    def test_default_config_contains_tool_output_section(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert "tool_output" in DEFAULT_CONFIG
        section = DEFAULT_CONFIG["tool_output"]
        assert isinstance(section, dict)
        assert section["max_bytes"] == tol.DEFAULT_MAX_BYTES
        assert section["max_lines"] == tol.DEFAULT_MAX_LINES
        assert section["max_line_length"] == tol.DEFAULT_MAX_LINE_LENGTH


class TestIntegrationReadPagination:
    """normalize_read_pagination uses get_max_lines() — verify the plumbing."""

    def test_pagination_limit_clamped_by_config_value(self):
        from tools.file_operations import normalize_read_pagination
        cfg = {"tool_output": {"max_lines": 50}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            offset, limit = normalize_read_pagination(offset=1, limit=1000)
        # limit should have been clamped to 50 (the configured max_lines)
        assert limit == 50
        assert offset == 1

    def test_pagination_default_when_config_missing(self):
        from tools.file_operations import normalize_read_pagination
        with patch("hermes_cli.config.load_config", return_value={}):
            offset, limit = normalize_read_pagination(offset=10, limit=100000)
        # Clamped to default MAX_LINES (2000).
        assert limit == tol.DEFAULT_MAX_LINES
        assert offset == 10


class TestMaxResultChars:
    """``max_result_chars`` lowers the per-result spill threshold (opt-in)."""

    @staticmethod
    def _budget(cfg, context_length=200_000):
        import types

        from agent.tool_executor import _budget_for_agent

        agent = types.SimpleNamespace(
            context_compressor=types.SimpleNamespace(context_length=context_length)
        )
        with patch("hermes_cli.config.load_config", return_value=cfg):
            return _budget_for_agent(agent)

    def test_absent_keeps_the_context_scaled_threshold(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            assert tol.get_tool_output_limits()["max_result_chars"] is None
        tol._reset_tool_output_limits_cache()
        assert self._budget({}).default_result_size == 100_000

    def test_cap_lowers_the_per_result_threshold(self):
        budget = self._budget({"tool_output": {"max_result_chars": 16_000}})
        assert budget.default_result_size == 16_000
        assert budget.resolve_threshold("terminal") == 16_000
        assert budget.resolve_threshold("read_file") == float("inf")

    def test_cap_never_raises_a_smaller_scaled_threshold(self):
        budget = self._budget(
            {"tool_output": {"max_result_chars": 90_000}}, context_length=20_000
        )
        assert budget.default_result_size < 90_000

    @pytest.mark.parametrize("bad", [0, -1, "x", None])
    def test_invalid_cap_is_ignored(self, bad):
        budget = self._budget({"tool_output": {"max_result_chars": bad}})
        assert budget.default_result_size == 100_000
