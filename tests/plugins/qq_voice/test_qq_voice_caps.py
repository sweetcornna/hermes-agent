"""The two caps on the model-callable voice tools: script length, daily budget.

These tools are the only unbounded voice path in the install — every
autonomous lane already has a daily maximum and a length ceiling — and each
``qq_send_voice`` call runs a synthesis through a provider the operator pays
per call. The contracts pinned here are therefore about *money and
listenability*, not about wire shapes:

* the length cap gates **before** the synthesizer is touched, so a refused
  message never costs anything;
* the day's budget moves only for a message QQ has, or may have — a refusal, a
  mute, a synthesis failure and a backend rejection all leave it alone;
* a damaged counter file degrades the budget rather than silencing the bot;
* the caps are ordinary ``config.yaml`` thresholds — declared in
  ``plugin.yaml``'s ``config_schema``, read from the native
  ``plugins.entries.qq_voice.settings`` path, defaulted, hot-reloaded, and
  switchable off with ``0``.

Fully offline: the clock, the TTS backend, the transport and the live-adapter
seam are all patched, and the counter lives under the per-test ``HERMES_HOME``
the root conftest already redirects into ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

import plugins.qq_voice.ai_voice as AV
import plugins.qq_voice.quota as Q
import plugins.qq_voice.send_voice as SV
import tools.onebot_client as OBC
import tools.tts_tool as TTS
from gateway.platforms.base import SendResult
from plugins.platforms.onebot import voice as V

GROUP = 183287894
PEER = 536132102

#: A fixed instant to hang the clock on. Mid-afternoon UTC, so a host in any
#: plausible timezone is still inside one calendar day rather than sitting on
#: its own midnight.
NOON_UTC = float(calendar.timegm((2026, 8, 22, 15, 30, 0, 0, 0, 0)))


def _manifest() -> Dict[str, Any]:
    """The plugin's own ``plugin.yaml``, read from beside ``quota.py``.

    Located relative to the module under test rather than to the repo root so
    the manifest that is asserted about is the one the loader would find for
    this package.
    """
    path = Path(Q.__file__).with_name("plugin.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class _Transport:
    """Stands in for ``onebot_call``. Records; never raises unless told to."""

    def __init__(self, error: Optional[Exception] = None) -> None:
        self.calls: List[Tuple[str, dict]] = []
        self.error = error

    def __call__(self, action, params=None, **kw):
        self.calls.append((action, dict(params or {})))
        if self.error is not None:
            raise self.error
        return {"message_id": 4242}

    @property
    def actions(self) -> List[str]:
        return [a for a, _ in self.calls]


class _Adapter:
    """The one coroutine method the file-send path reaches for."""

    def __init__(self, result: Optional[SendResult] = None) -> None:
        self.voice_sends: List[Tuple[str, str]] = []
        self.result = result or SendResult(success=True, message_id="99")

    async def send_voice(self, chat_id, audio_path, *a, **kw):
        self.voice_sends.append((chat_id, audio_path))
        return self.result


class _Synth:
    """A TTS backend that counts how often it was asked to speak.

    The count is the point: a message refused by either cap must reach zero
    calls, because the whole reason the caps exist is that each call bills.
    """

    def __init__(self, tmp_path: Path, *, files: int = 1, error: str = "") -> None:
        self._tmp = tmp_path
        self._files = files
        self._error = error
        self.calls: List[str] = []

    def __call__(self, text=None, **kw):
        self.calls.append(text)
        if self._error:
            return json.dumps({"success": False, "error": self._error})
        paths = []
        for index in range(self._files):
            path = self._tmp / f"speech{len(self.calls)}_{index}.mp3"
            path.write_bytes(b"ID3" + bytes([index]) * 32)
            paths.append(str(path))
        return json.dumps({"success": True, "file_paths": paths})


@pytest.fixture
def hermes_home() -> Path:
    """The per-test Hermes home the root conftest points at ``tmp_path``."""
    return Path(os.environ["HERMES_HOME"])


def settings(**values: Any) -> str:
    """A ``config.yaml`` body putting ``values`` on the plugin's own path.

    Spelled out as literal YAML rather than ``yaml.dump``'d from a dict so
    every test writes the file an operator would write, nesting and all:
    ``plugins.entries.qq_voice.settings`` is the claim under test, and a
    dumper would let a wrong path still round-trip.
    """
    body = "".join(f"        {key}: {value}\n" for key, value in values.items())
    return "plugins:\n  entries:\n    qq_voice:\n      settings:\n" + body


@pytest.fixture
def configure(hermes_home):
    """Write a real ``config.yaml`` and let the real loader read it.

    Exercising ``hermes_cli.config.load_config`` rather than patching
    ``quota._settings`` is deliberate: the claim under test is that settings
    on the native plugin path resolve without anything being registered in
    ``DEFAULT_CONFIG``, which a patched loader would hide.
    """

    def _write(body: str) -> Path:
        path = hermes_home / "config.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def clock(monkeypatch):
    """Freeze (and later wind) the quota's wall clock."""

    holder = {"now": NOON_UTC}
    monkeypatch.setattr(Q, "_now", lambda: holder["now"])

    def _set(value: float) -> None:
        holder["now"] = value

    return _set


@pytest.fixture
def transport(monkeypatch):
    spy = _Transport()
    monkeypatch.setattr(OBC, "onebot_call", spy)
    return spy


@pytest.fixture
def synth(monkeypatch, tmp_path):
    spy = _Synth(tmp_path)
    monkeypatch.setattr(TTS, "text_to_speech_tool", spy)
    return spy


@pytest.fixture
def no_gateway(monkeypatch):
    """The CLI / cron case: nothing has published an adapter."""
    monkeypatch.setattr(SV, "live_adapter", lambda: (None, None))
    monkeypatch.setattr(AV, "live_adapter", lambda: (None, None))


@pytest.fixture
def loop_thread():
    """A real event loop on another thread — the shape a live gateway has."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


@pytest.fixture
def gateway(monkeypatch, loop_thread):
    adapter = _Adapter()
    monkeypatch.setattr(SV, "live_adapter", lambda: (adapter, loop_thread))
    monkeypatch.setattr(AV, "live_adapter", lambda: (adapter, loop_thread))
    return adapter


def _err(raw: str) -> Dict[str, Any]:
    payload = json.loads(raw)
    assert "error" in payload, f"expected a tool error, got {payload}"
    return payload


def _ok(raw: str) -> Dict[str, Any]:
    payload = json.loads(raw)
    assert payload.get("success") is True, f"expected success, got {payload}"
    return payload


def _counter() -> Optional[Dict[str, Any]]:
    """The raw counter file, or ``None`` when nothing has been written."""
    path = Q.quota_path()
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _used() -> int:
    """Today's recorded count, 0 when the file is absent or from another day."""
    raw = _counter()
    if not raw or raw.get("date") != Q.local_day():
        return 0
    return int(raw.get("count") or 0)


def _last_second_of_local_day(ts: float) -> float:
    """23:59:59 on the local calendar day containing ``ts``.

    Built from ``time.localtime`` / ``time.mktime`` so the boundary is the
    host's own midnight, whatever zone that is. That is exactly the boundary
    the counter claims to reset on, so the test is the contract rather than a
    restatement of one particular timezone.
    """
    local = time.localtime(ts)
    return time.mktime((
        local.tm_year,
        local.tm_mon,
        local.tm_mday,
        23,
        59,
        59,
        0,
        0,
        local.tm_isdst,
    ))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestLimitsResolution:
    def test_defaults_apply_with_no_settings_block(self, hermes_home):
        """The common case: an operator who never heard of this plugin."""
        assert not (hermes_home / "config.yaml").exists()
        limits = Q.resolve_limits()
        assert limits.max_text_chars == Q.DEFAULT_MAX_TEXT_CHARS == 200
        assert limits.daily_max == Q.DEFAULT_DAILY_MAX == 30

    def test_defaults_survive_a_config_file_without_the_block(self, configure):
        configure("tts:\n  provider: fish\n")
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=200, daily_max=30)

    def test_the_native_settings_path_is_the_one_that_is_read(self, configure):
        """The whole point of G2: ``plugins.entries.<id>.settings``."""
        assert Q.CONFIG_PATH == ("plugins", "entries", "qq_voice", "settings")
        configure(settings(max_text_chars=42, daily_max=7))
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=42, daily_max=7)

    def test_one_key_overrides_without_disturbing_the_other(self, configure):
        configure(settings(daily_max=5))
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=200, daily_max=5)

    def test_a_junk_value_falls_back_to_the_default(self, configure):
        """A typo in config.yaml must not crash a tool call."""
        configure(settings(max_text_chars="soon", daily_max="[]"))
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=200, daily_max=30)

    def test_a_non_mapping_settings_block_falls_back_to_the_defaults(self, configure):
        configure("plugins:\n  entries:\n    qq_voice:\n      settings: 200\n")
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=200, daily_max=30)

    def test_a_non_mapping_entry_falls_back_to_the_defaults(self, configure):
        """``cfg_get`` walks through a non-dict without raising."""
        configure("plugins:\n  entries:\n    qq_voice: 200\n")
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=200, daily_max=30)

    def test_another_plugins_settings_are_not_read(self, configure):
        """Plugin-relative means plugin-relative — no reaching sideways."""
        configure(
            "plugins:\n  entries:\n    grantley:\n      settings:\n"
            "        max_text_chars: 9\n        daily_max: 9\n"
        )
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=200, daily_max=30)

    def test_an_edit_applies_to_the_next_call_not_the_next_restart(self, configure):
        configure(settings(daily_max=500))
        assert Q.resolve_limits().daily_max == 500
        configure(settings(daily_max=3))
        assert Q.resolve_limits().daily_max == 3

    def test_the_refusals_name_the_full_config_path(self):
        """What an operator is told is what they can grep for in config.yaml.

        A shorter spelling would be a path that does not exist, which is
        worse than a long one.
        """
        assert Q.CONFIG_SECTION == "plugins.entries.qq_voice.settings"
        assert "plugins.entries.qq_voice.settings.max_text_chars" in Q.text_cap_refusal(
            500, 200
        )
        assert "plugins.entries.qq_voice.settings.daily_max" in Q.quota_refusal(
            Q.QuotaSnapshot(used_today=30, limit=30)
        )


class TestTheOldTopLevelSectionIsGone:
    """A top-level ``qq_voice:`` key is ignored, on purpose and with no shim.

    The bespoke section this plugin briefly read never shipped in a release
    (this branch is unmerged), so there is no installed config to migrate.
    Keeping a fallback would leave two sources of truth for the same cap, and
    only one of them — the native path — is covered by ``config_schema``
    validation, so the top-level spelling would silently outrank a declared,
    type-checked setting. Ignoring it keeps the defaults in force, which is
    the visible failure rather than the quiet one.
    """

    def test_a_top_level_section_does_not_configure_anything(self, configure):
        configure("qq_voice:\n  max_text_chars: 5\n  daily_max: 1\n")
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=200, daily_max=30)

    def test_the_native_path_wins_when_both_are_present(self, configure):
        configure(
            "qq_voice:\n  max_text_chars: 5\n  daily_max: 1\n"
            + settings(max_text_chars=42, daily_max=7)
        )
        assert Q.resolve_limits() == Q.VoiceLimits(max_text_chars=42, daily_max=7)

    def test_a_top_level_section_never_blocks_a_send(
        self, configure, clock, transport, synth, no_gateway
    ):
        """The end-to-end consequence: the defaults apply, nothing refuses."""
        configure("qq_voice:\n  daily_max: 0\n  max_text_chars: 1\n")
        payload = _ok(SV.handle_qq_send_voice({"text": "hi", "group_id": str(GROUP)}))
        assert payload["voice_quota"] == {
            "used_today": 1,
            "limit": 30,
            "remaining": 29,
        }


class TestTheManifestDeclaresTheCaps:
    """``plugin.yaml``'s ``config_schema`` is the declaration of these keys.

    Declared settings are what makes them type-checked at load and visible to
    an operator who did not read the source. These tests assert the manifest
    and ``quota.py`` *agree* rather than snapshotting either: a default
    changed on one side alone is the failure being guarded against.
    """

    def test_both_caps_are_declared_and_nothing_else_is(self):
        assert set(_manifest()["config_schema"]) == {"max_text_chars", "daily_max"}

    def test_the_declared_defaults_are_the_resolvers_defaults(self):
        schema = _manifest()["config_schema"]
        assert schema["max_text_chars"]["default"] == Q.DEFAULT_MAX_TEXT_CHARS
        assert schema["daily_max"]["default"] == Q.DEFAULT_DAILY_MAX

    def test_an_unconfigured_install_lands_on_the_declared_defaults(self, hermes_home):
        """The same invariant end to end, through the real loader."""
        assert not (hermes_home / "config.yaml").exists()
        schema = _manifest()["config_schema"]
        limits = Q.resolve_limits()
        assert limits == Q.VoiceLimits(
            max_text_chars=schema["max_text_chars"]["default"],
            daily_max=schema["daily_max"]["default"],
        )

    @pytest.mark.parametrize("key", ["max_text_chars", "daily_max"])
    def test_every_declared_key_is_actually_honoured_under_that_name(
        self, configure, key
    ):
        """Pins the spelling on both sides: a rename here breaks loudly."""
        assert key in _manifest()["config_schema"]
        configure(settings(**{key: 7}))
        assert getattr(Q.resolve_limits(), key) == 7

    def test_every_declared_key_carries_a_description(self):
        """An undescribed setting is one nobody can discover."""
        for key, spec in _manifest()["config_schema"].items():
            assert spec.get("description", "").strip(), key

    def test_the_declared_types_are_ones_the_loader_understands(self):
        """An unknown type is accepted but silently skips the type check."""
        from hermes_cli.plugins import _CONFIG_SCHEMA_TYPES

        declared = {
            str(spec["type"]).lower() for spec in _manifest()["config_schema"].values()
        }
        assert declared <= set(_CONFIG_SCHEMA_TYPES)
        assert declared == {"int"}

    def test_the_manifest_parses_without_a_single_loader_warning(self, caplog):
        """Through the real parser, not a re-implementation of it."""
        from hermes_cli.plugins import _parse_manifest_v2_fields

        with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
            parsed = _parse_manifest_v2_fields(_manifest(), Q.PLUGIN_ID)
        assert not caplog.records
        assert set(parsed["config_schema"]) == {"max_text_chars", "daily_max"}


class TestSettingsValidateAgainstTheDeclaredSchema:
    """``validate_config_schema`` is what an operator's typo runs into."""

    @staticmethod
    def _validate(block: Dict[str, Any]) -> List[str]:
        from hermes_cli.plugins import validate_config_schema

        return validate_config_schema(Q.PLUGIN_ID, _manifest()["config_schema"], block)

    def test_a_wellformed_block_is_accepted(self):
        assert self._validate({"max_text_chars": 200, "daily_max": 30}) == []

    def test_an_empty_block_is_accepted(self):
        """Neither key is ``required``; both have a default."""
        assert self._validate({}) == []

    @pytest.mark.parametrize("value", [0, -1])
    def test_turning_a_cap_off_is_a_valid_declared_value(self, value):
        """``0`` disables a cap, so it must not read as a schema violation."""
        assert self._validate({"daily_max": value}) == []

    def test_a_type_mismatch_is_reported_by_its_full_config_path(self):
        warnings = self._validate({"max_text_chars": "soon"})
        assert warnings == [
            "plugins.entries.qq_voice.settings.max_text_chars should be int (got str)"
        ]

    def test_a_bool_does_not_pass_for_an_int(self, configure):
        """``True`` is an ``int`` in Python and a mistake in config.yaml.

        The loader warns; the resolver would coerce it to ``1`` and cap every
        message at one character, so the warning is the only thing standing
        between the operator and a mute bot.
        """
        assert self._validate({"daily_max": True}) == [
            "plugins.entries.qq_voice.settings.daily_max should be int (got bool)"
        ]

    def test_validation_warns_but_the_resolver_still_answers(self, configure):
        """A schema mismatch is a warning, never a load failure (#64165).

        The two halves have to agree on that: the loader reports the bad key
        and ``resolve_limits`` falls back to the default rather than raising
        into a tool call.
        """
        assert self._validate({"max_text_chars": "soon"})
        configure(settings(max_text_chars="soon"))
        assert Q.resolve_limits().max_text_chars == Q.DEFAULT_MAX_TEXT_CHARS


class TestDisablingACap:
    """``0`` (or negative) means "no cap" — the repo's own spelling.

    ``SlidingWindowCounter.allow`` treats ``max_count <= 0`` the same way; a
    ``null`` sentinel would be a second convention for the same idea.
    """

    @pytest.mark.parametrize("value", [0, -1])
    def test_text_cap_off(self, configure, value):
        configure(settings(max_text_chars=value))
        assert Q.resolve_limits().text_over_cap(10_000) is False

    @pytest.mark.parametrize("value", [0, -1])
    def test_daily_cap_off_is_never_exhausted(self, configure, clock, value):
        configure(settings(daily_max=value))
        limits = Q.resolve_limits()
        for _ in range(50):
            Q.charge_one_send(limits)
        assert Q.quota_snapshot(limits).exhausted is False

    def test_a_disabled_cap_writes_no_counter_file(self, configure, clock):
        """Nothing recorded when the cap is off, matching the counter class."""
        configure(settings(daily_max=0))
        Q.charge_one_send(Q.resolve_limits())
        assert _counter() is None

    def test_a_disabled_cap_reports_no_remaining_rather_than_a_number(
        self, configure, clock
    ):
        configure(settings(daily_max=0))
        assert Q.quota_snapshot(Q.resolve_limits()).as_dict() == {
            "used_today": 0,
            "limit": 0,
            "remaining": None,
        }


# ---------------------------------------------------------------------------
# Perceived length
# ---------------------------------------------------------------------------


class TestSpokenLength:
    def test_plain_text_counts_as_written(self):
        assert Q.spoken_length("hello") == 5

    def test_han_characters_count_one_each(self):
        assert Q.spoken_length("早上好") == 3

    def test_a_decomposed_accent_is_one_character_not_two(self):
        """``len()`` says 2; a reader — and a listener — say 1."""
        assert Q.spoken_length("é") == 1
        assert len("é") == 2

    def test_a_variation_selector_does_not_inflate_the_count(self):
        assert Q.spoken_length("❤️") == 1

    def test_empty_text_is_zero(self):
        assert Q.spoken_length("") == 0


# ---------------------------------------------------------------------------
# The text cap on qq_send_voice
# ---------------------------------------------------------------------------


class TestTextCap:
    def test_over_the_cap_is_refused_and_never_reaches_the_synthesizer(
        self, configure, clock, synth, transport, no_gateway
    ):
        """The load-bearing assertion: the cap gates before the money.

        A cap that refused *after* synthesis would still bill for every
        oversized message, which is the entire failure it exists to prevent.
        """
        configure(settings(max_text_chars=8))
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "x" * 9}))
        assert payload["code"] == "text_too_long"
        assert payload["text_chars"] == 9
        assert payload["max_text_chars"] == 8
        assert synth.calls == [], "the cap must gate before the expensive work"
        assert transport.calls == []

    def test_exactly_at_the_cap_is_allowed(
        self, configure, clock, synth, transport, no_gateway
    ):
        """Pin the boundary: the cap is a maximum, not a strict ceiling."""
        configure(settings(max_text_chars=8))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "x" * 8}))
        assert len(synth.calls) == 1
        assert transport.actions == ["send_msg"]

    def test_the_refusal_tells_the_model_what_to_do_instead(
        self, configure, clock, synth, no_gateway
    ):
        """Guidance, not a wall — sending voice was the persona's own call."""
        configure(settings(max_text_chars=4))
        payload = _err(
            SV.handle_qq_send_voice({"group_id": GROUP, "text": "far too long"})
        )
        assert "text message" in payload["error"]
        assert "your call" in payload["error"]
        assert "max_text_chars" in payload["error"]

    def test_a_cap_refusal_names_no_delivery_path(
        self, configure, clock, synth, no_gateway
    ):
        """There is no path to name: the cap gates before one is chosen.

        Reporting one anyway would tell the reader a send was attempted when
        none was. Pins the claim ``plugin.yaml`` and the README make about
        which results carry ``delivery_path``.
        """
        configure(settings(max_text_chars=4))
        payload = _err(
            SV.handle_qq_send_voice({"group_id": GROUP, "text": "far too long"})
        )
        assert "delivery_path" not in payload

    def test_the_cap_is_measured_in_perceived_characters(
        self, configure, clock, synth, transport, no_gateway
    ):
        """8 decomposed accents are 16 code points and 8 characters."""
        configure(settings(max_text_chars=8))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "é" * 8}))
        assert len(synth.calls) == 1

    def test_no_cap_means_a_very_long_script_goes_through(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(max_text_chars=0))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "x" * 5000}))
        assert len(synth.calls) == 1

    def test_an_audio_file_send_has_no_script_to_cap(
        self, configure, clock, transport, no_gateway, tmp_path
    ):
        """Its size is already bounded by ``MAX_AUDIO_BYTES``, not by this."""
        configure(settings(max_text_chars=1))
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"ID3" + b"z" * 64)
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)}))
        assert transport.actions == ["send_msg"]


# ---------------------------------------------------------------------------
# The daily budget: what charges it
# ---------------------------------------------------------------------------


class TestQuotaChargesOnlyDelivery:
    def test_a_successful_direct_send_charges_exactly_one(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(daily_max=5))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert _used() == 1

    def test_a_chunked_send_still_charges_one_message_not_one_per_chunk(
        self, configure, clock, monkeypatch, tmp_path, transport, no_gateway
    ):
        """The operator's budget is stated in messages, not in audio files."""
        configure(settings(daily_max=5))
        monkeypatch.setattr(TTS, "text_to_speech_tool", _Synth(tmp_path, files=3))
        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "long"}))
        assert len(payload["message_ids"]) == 3
        assert _used() == 1

    def test_an_audio_file_send_is_charged_too(
        self, configure, clock, transport, no_gateway, tmp_path
    ):
        """No free branch: otherwise the model synthesizes elsewhere and posts.

        ``text_to_speech`` bills identically, so exempting the file branch
        would leave the cap trivially bypassable rather than merely leaky.
        """
        configure(settings(daily_max=5))
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"ID3" + b"z" * 64)
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)}))
        assert _used() == 1

    def test_a_synthesis_failure_leaves_the_counter_untouched(
        self, configure, clock, monkeypatch, tmp_path, transport, no_gateway
    ):
        configure(settings(daily_max=5))
        monkeypatch.setattr(
            TTS, "text_to_speech_tool", _Synth(tmp_path, error="no voice configured")
        )
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["code"] == "tts_failed"
        assert _used() == 0

    def test_a_backend_rejection_leaves_the_counter_untouched(
        self, configure, clock, synth, monkeypatch, no_gateway
    ):
        configure(settings(daily_max=5))
        monkeypatch.setattr(
            OBC, "onebot_call", _Transport(error=RuntimeError("OneBot HTTP 502"))
        )
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["code"] == "onebot_failed"
        assert _used() == 0

    def test_a_muted_group_leaves_the_counter_untouched(
        self, configure, clock, synth, transport, gateway, monkeypatch
    ):
        """A mute is a decision taken before synthesis; it costs nothing."""
        configure(settings(daily_max=5))
        failure = SendResult(
            success=False, error="group speech is muted", error_kind="forbidden"
        )

        async def fake_speak(adapter, chat_id, text, **kw):
            return V.SpeakOutcome(
                spoke=False, reason="group speech is muted", failure=failure
            )

        monkeypatch.setattr(V, "speak", fake_speak)
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["code"] == "forbidden"
        assert _used() == 0

    def test_a_policy_refusal_leaves_the_counter_untouched(
        self, configure, clock, synth, transport, gateway, monkeypatch
    ):
        configure(settings(daily_max=5))

        async def fake_speak(adapter, chat_id, text, **kw):
            return V.SpeakOutcome(spoke=False, reason="refused by the content policy")

        monkeypatch.setattr(V, "speak", fake_speak)
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["code"] == "not_spoken"
        assert _used() == 0

    def test_an_exploding_adapter_leaves_the_counter_untouched(
        self, configure, clock, synth, transport, gateway, monkeypatch
    ):
        configure(settings(daily_max=5))

        async def fake_speak(adapter, chat_id, text, **kw):
            raise RuntimeError("adapter exploded")

        monkeypatch.setattr(V, "speak", fake_speak)
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["code"] == "gateway_failed"
        assert _used() == 0

    def test_a_gateway_refused_file_send_leaves_the_counter_untouched(
        self, configure, clock, transport, monkeypatch, loop_thread, tmp_path
    ):
        configure(settings(daily_max=5))
        adapter = _Adapter(
            SendResult(success=False, error="muted", error_kind="forbidden")
        )
        monkeypatch.setattr(SV, "live_adapter", lambda: (adapter, loop_thread))
        audio = tmp_path / "clip.ogg"
        audio.write_bytes(b"OggS" + b"q" * 40)
        _err(SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)}))
        assert _used() == 0

    def test_an_argument_error_never_reaches_the_counter(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(daily_max=5))
        _err(SV.handle_qq_send_voice({"text": "hi"}))
        assert _used() == 0

    def test_a_partly_delivered_chunked_send_is_charged_once(
        self, configure, clock, monkeypatch, tmp_path, no_gateway
    ):
        """Two bubbles are audible; the third failed. It cost, so it counts.

        Charging here is not the unknown-outcome rule — these two chunks were
        acknowledged by the backend. It is the ordinary "QQ has it" rule
        applied to a message that only partly arrived.
        """
        configure(settings(daily_max=5))
        monkeypatch.setattr(TTS, "text_to_speech_tool", _Synth(tmp_path, files=3))

        calls: List[str] = []

        def flaky(action, params=None, **kw):
            calls.append(action)
            if len(calls) == 3:
                raise RuntimeError("OneBot HTTP 502")
            return {"message_id": len(calls)}

        monkeypatch.setattr(OBC, "onebot_call", flaky)
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "long"}))
        assert payload["partial_message_ids"] == [1, 2]
        assert _used() == 1


class TestUnknownOutcome:
    """The gateway timeout — the one error that still costs budget.

    Deliberate, and the reasoning is stated in ``send_voice._timed_out_mid_send``:
    the coroutine may be mid-send, and the failure mode that produces timeouts
    is the one that repeats, so a counter that ignored them would let a
    retrying model post an unbounded run of paid voice bubbles while reading
    zero. Burning one send for a message that may not have arrived is the
    smaller, bounded, visible error.
    """

    @pytest.fixture
    def stalled_gateway(self, monkeypatch, gateway):
        async def never_returns(adapter, chat_id, text, **kw):
            await asyncio.sleep(30)
            raise AssertionError("the handler should have given up first")

        monkeypatch.setattr(V, "speak", never_returns)
        # A real timeout, just a short one — the handler's own hand-off is
        # what is under test, not a stubbed exception.
        monkeypatch.setattr(SV, "run_on_adapter_loop", _timeout_after_a_moment)
        return gateway

    def test_a_timeout_charges_the_budget(
        self, configure, clock, transport, stalled_gateway
    ):
        configure(settings(daily_max=5))
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["code"] == "timeout"
        assert _used() == 1

    def test_the_timeout_error_says_the_budget_moved(
        self, configure, clock, transport, stalled_gateway
    ):
        """The model must not have to guess; the number is in the error."""
        configure(settings(daily_max=5))
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert "may still have been delivered" in payload["error"]
        assert "voice budget" in payload["error"]
        assert payload["voice_quota"] == {
            "used_today": 1,
            "limit": 5,
            "remaining": 4,
        }


def _timeout_after_a_moment(coro, loop, **kw):
    """Stand-in for ``run_on_adapter_loop`` that always times out.

    Closes the coroutine so the event loop is not left holding it; the real
    function leaves it running, which is exactly why the outcome is unknown.
    """
    coro.close()
    raise TimeoutError("gave up waiting for the gateway")


# ---------------------------------------------------------------------------
# The daily budget: refusing, resetting, reporting
# ---------------------------------------------------------------------------


class TestQuotaExhaustion:
    def test_over_the_budget_is_refused_and_never_reaches_the_synthesizer(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(daily_max=2))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "one"}))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "two"}))
        before = len(synth.calls)

        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "three"}))
        assert payload["code"] == "quota_exhausted"
        assert len(synth.calls) == before, "an over-budget call must not synthesize"
        assert transport.actions == ["send_msg", "send_msg"]
        assert _used() == 2, "a refused call must not charge either"

    def test_the_refusal_names_the_setting_and_the_reset(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(daily_max=1))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "one"}))
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "two"}))
        assert "daily_max" in payload["error"]
        assert "local midnight" in payload["error"]
        assert payload["voice_quota"] == {
            "used_today": 1,
            "limit": 1,
            "remaining": 0,
        }

    def test_the_budget_is_shared_across_both_branches_of_the_tool(
        self, configure, clock, synth, transport, no_gateway, tmp_path
    ):
        """One budget for the tool, not one per argument shape."""
        configure(settings(daily_max=2))
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"ID3" + b"z" * 64)
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)}))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "spoken"}))
        payload = _err(
            SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)})
        )
        assert payload["code"] == "quota_exhausted"

    def test_the_budget_is_not_per_chat(
        self, configure, clock, synth, transport, no_gateway
    ):
        """The bill is per synthesis, not per recipient, so the cap is global.

        A per-chat budget would multiply the ceiling by the number of chats
        the bot is in, which is the number the operator has least control over.
        """
        configure(settings(daily_max=1))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "one"}))
        payload = _err(SV.handle_qq_send_voice({"user_id": PEER, "text": "two"}))
        assert payload["code"] == "quota_exhausted"


class TestLocalMidnight:
    def test_the_day_is_the_hosts_local_day_not_utc(self):
        """``time.localtime``, deliberately — see ``quota.local_day``."""
        assert Q.local_day(NOON_UTC) == time.strftime(
            "%Y-%m-%d", time.localtime(NOON_UTC)
        )

    def test_the_count_survives_later_the_same_local_day(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(daily_max=5))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "morning"}))
        clock(_last_second_of_local_day(NOON_UTC))
        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "night"}))
        assert payload["voice_quota"]["used_today"] == 2

    def test_crossing_local_midnight_resets_the_count(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(daily_max=1))
        last_second = _last_second_of_local_day(NOON_UTC)
        clock(last_second)
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "just in time"}))
        _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "too many"}))

        clock(last_second + 2)  # one local day later, by two seconds
        assert Q.local_day(last_second) != Q.local_day(last_second + 2)
        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "new day"}))
        assert payload["voice_quota"] == {
            "used_today": 1,
            "limit": 1,
            "remaining": 0,
        }

    def test_a_stale_dated_file_is_not_carried_forward(
        self, configure, clock, hermes_home
    ):
        configure(settings(daily_max=3))
        path = Q.quota_path()
        path.write_text(
            json.dumps({"date": "1999-12-31", "count": 3}), encoding="utf-8"
        )
        assert Q.quota_snapshot(Q.resolve_limits()).used_today == 0


class TestQuotaInTheResult:
    def test_a_successful_result_carries_the_block(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(daily_max=4))
        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["voice_quota"] == {
            "used_today": 1,
            "limit": 4,
            "remaining": 3,
        }

    def test_remaining_counts_down_across_calls(
        self, configure, clock, synth, transport, no_gateway
    ):
        configure(settings(daily_max=3))
        seen = [
            _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": f"m{i}"}))[
                "voice_quota"
            ]["remaining"]
            for i in range(3)
        ]
        assert seen == [2, 1, 0]

    def test_a_gateway_send_reports_it_too(
        self, configure, clock, transport, gateway, monkeypatch
    ):
        configure(settings(daily_max=9))

        async def fake_speak(adapter, chat_id, text, **kw):
            return V.SpeakOutcome(spoke=True, message_ids=("777",), audio_bytes=12)

        monkeypatch.setattr(V, "speak", fake_speak)
        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["voice_quota"]["used_today"] == 1
        assert payload["voice_quota"]["limit"] == 9

    def test_a_failure_reports_the_unchanged_budget(
        self, configure, clock, synth, monkeypatch, no_gateway
    ):
        """So the model can tell an error that cost something from one that did not."""
        configure(settings(daily_max=6))
        monkeypatch.setattr(
            OBC, "onebot_call", _Transport(error=RuntimeError("OneBot HTTP 502"))
        )
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["voice_quota"] == {
            "used_today": 0,
            "limit": 6,
            "remaining": 6,
        }


# ---------------------------------------------------------------------------
# The counter file itself
# ---------------------------------------------------------------------------


class TestCounterFile:
    def test_it_lives_under_the_plugin_data_dir_for_the_active_home(self, hermes_home):
        assert Q.quota_path() == (
            hermes_home / "plugin-data" / "qq_voice" / "voice_quota.json"
        )

    def test_it_keeps_the_two_fields_the_reference_deployment_wrote(
        self, configure, clock, synth, transport, no_gateway
    ):
        """``{date, count}`` — so an existing production file can be copied in."""
        configure(settings(daily_max=5))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert _counter() == {"date": Q.local_day(), "count": 1}

    @pytest.mark.parametrize(
        "damage",
        [
            "",
            "   ",
            "{",
            '{"date": "2026-08-22", "coun',
            "[]",
            "null",
            '{"date": 20260822, "count": 4}',
            '{"date": "2026-08-22"}',
            '{"date": "2026-08-22", "count": -3}',
            '{"date": "2026-08-22", "count": true}',
        ],
        ids=[
            "empty",
            "blank",
            "truncated-brace",
            "truncated-json",
            "array",
            "null",
            "numeric-date",
            "no-count",
            "negative",
            "bool-count",
        ],
    )
    def test_a_damaged_counter_does_not_block_a_send(
        self, configure, clock, synth, transport, no_gateway, damage
    ):
        """Fail open. A corrupt *counter* must not silence the bot.

        The cap protects a bill; refusing every voice message because the
        bookkeeping is damaged is a strictly worse outcome than briefly
        over-spending a soft budget, and the file is rewritten whole here.
        """
        configure(settings(daily_max=5))
        path = Q.quota_path()
        path.write_text(damage, encoding="utf-8")

        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert _counter() == {"date": Q.local_day(), "count": 1}

    def test_a_damaged_counter_warns_once_rather_than_raising(
        self, configure, clock, caplog
    ):
        configure(settings(daily_max=5))
        Q.quota_path().write_text("{oh no", encoding="utf-8")
        with caplog.at_level("WARNING", logger=Q.logger.name):
            assert Q.quota_snapshot(Q.resolve_limits()).used_today == 0
        assert len(caplog.records) == 1
        assert "quota" in caplog.records[0].getMessage()

    def test_a_missing_file_is_silent(self, configure, clock, caplog):
        """First run is the normal case, not something to warn about."""
        configure(settings(daily_max=5))
        with caplog.at_level("WARNING", logger=Q.logger.name):
            assert Q.quota_snapshot(Q.resolve_limits()).used_today == 0
        assert caplog.records == []

    def test_overlapping_increments_do_not_lose_a_count(self, clock):
        """Two tool calls can be in flight at once; neither may vanish.

        The increment is a read-modify-write, so without the lock the losing
        thread's count is silently dropped and the budget over-delivers.
        """
        limits = Q.VoiceLimits(max_text_chars=0, daily_max=1000)
        workers = 16
        start = threading.Barrier(workers)

        def charge() -> None:
            start.wait(timeout=10)
            Q.charge_one_send(limits)

        threads = [threading.Thread(target=charge) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert _counter() == {"date": Q.local_day(), "count": workers}


# ---------------------------------------------------------------------------
# qq_ai_voice: capped on length, not on budget
# ---------------------------------------------------------------------------


class TestAiVoiceIsCappedOnLengthOnly:
    """The two caps have two reasons, so they cover two different sets.

    ``max_text_chars`` is a promise to the listener — a five-minute voice
    bubble is equally unlistenable in one of Tencent's AI voices — and leaving
    it off here would hand the model a free way round the rule. ``daily_max``
    is a cost control on a paid per-synthesis provider, and 群 AI 声聊 is
    synthesized on Tencent's side, so charging it would make the model ration
    the wrong resource and let a free tool starve the paid one.
    """

    def test_over_the_text_cap_is_refused_before_the_backend_is_called(
        self, configure, clock, transport, no_gateway
    ):
        configure(settings(max_text_chars=8))
        payload = _err(
            AV.handle_qq_ai_voice({
                "group_id": GROUP,
                "character": "lucy-voice-xiaoyu",
                "text": "x" * 9,
            })
        )
        assert payload["code"] == "text_too_long"
        assert transport.calls == []

    def test_exactly_at_the_text_cap_is_allowed(
        self, configure, clock, transport, no_gateway
    ):
        configure(settings(max_text_chars=8))
        _ok(
            AV.handle_qq_ai_voice({
                "group_id": GROUP,
                "character": "lucy-voice-xiaoyu",
                "text": "x" * 8,
            })
        )
        assert transport.actions == ["send_group_ai_record"]

    def test_the_text_cap_can_be_disabled_here_too(
        self, configure, clock, transport, no_gateway
    ):
        configure(settings(max_text_chars=0))
        _ok(
            AV.handle_qq_ai_voice({
                "group_id": GROUP,
                "character": "lucy-voice-xiaoyu",
                "text": "x" * 5000,
            })
        )
        assert transport.actions == ["send_group_ai_record"]

    def test_a_send_does_not_charge_the_daily_budget(
        self, configure, clock, transport, no_gateway
    ):
        configure(settings(daily_max=5))
        for _ in range(3):
            _ok(
                AV.handle_qq_ai_voice({
                    "group_id": GROUP,
                    "character": "lucy-voice-xiaoyu",
                    "text": "hi",
                })
            )
        assert _counter() is None
        assert _used() == 0

    def test_an_exhausted_budget_does_not_silence_it(
        self, configure, clock, synth, transport, no_gateway
    ):
        """The paid tool is out of budget; the free one is unaffected."""
        configure(settings(daily_max=1))
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "the last one"}))
        _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "over budget"}))

        _ok(
            AV.handle_qq_ai_voice({
                "group_id": GROUP,
                "character": "lucy-voice-xiaoyu",
                "text": "still fine",
            })
        )
        assert _used() == 1

    def test_its_result_carries_no_quota_block(
        self, configure, clock, transport, no_gateway
    ):
        """There is no budget here, so reporting one would be a lie."""
        configure(settings(daily_max=5))
        payload = _ok(
            AV.handle_qq_ai_voice({
                "group_id": GROUP,
                "character": "lucy-voice-xiaoyu",
                "text": "hi",
            })
        )
        assert "voice_quota" not in payload

    def test_listing_characters_is_never_capped(
        self, configure, clock, monkeypatch, no_gateway
    ):
        """Discovery sends nothing, so neither cap has anything to protect."""
        configure(settings(max_text_chars=4, daily_max=0))

        def fake(action, params=None, **kw):
            assert action == "get_ai_characters"
            return [{"type": "推荐", "characters": [{"character_id": "lucy"}]}]

        monkeypatch.setattr(OBC, "onebot_call", fake)
        payload = _ok(
            AV.handle_qq_ai_voice({"group_id": GROUP, "text": "far too long to speak"})
        )
        assert payload["sent"] is False
        assert payload["count"] == 1
