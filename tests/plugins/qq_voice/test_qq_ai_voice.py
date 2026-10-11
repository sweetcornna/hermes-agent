"""``qq_ai_voice``: discovery-before-guessing, group-only, NapCat-only.

Three things this tool has to get right, and they are all failure modes the
model cannot recover from on its own:

* it must **list** the group's AI characters rather than invent a
  ``character_id``, because an invented one comes back as a bare retcode;
* it must refuse a private-chat target by name, because QQ has no
  ``send_private_ai_record`` and the analogous ``qq_send_voice`` call does
  work — the model needs to be pointed at it;
* a backend rejection must name the likely cause. ``send_group_ai_record``
  and ``get_ai_characters`` are NapCat extensions; Lagrange and go-cqhttp
  reject them outright, and "retcode 1200" does not say so.

Plus the same live-vs-direct rule as ``qq_send_voice``: a published adapter
means ``voice.speak_ai_character`` runs and the raw transport does not.

Fully offline — the transport and the live-adapter accessor are patched.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

import plugins.qq_voice.ai_voice as AV
import tools.onebot_client as OBC
from gateway.platforms.base import SendResult
from plugins.platforms.onebot import voice as V

GROUP = 183287894
PEER = 536132102

_CHARACTERS = [
    {
        "type": "推荐",
        "characters": [
            {
                "character_id": "lucy-voice-guangdong-f1",
                "character_name": "小何",
                "preview_url": "http://x",
            },
            {"character_id": "lucy-voice-lizeyan", "character_name": "李泽言"},
        ],
    },
    {
        "type": "古风",
        "characters": [
            {"character_id": "lucy-voice-guanxiaofang", "character_name": "관샤오팡"}
        ],
    },
]


class _Transport:
    def __init__(self, responses=None, error=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.error = error

    def __call__(self, action, params=None, **kw):
        self.calls.append((action, dict(params or {})))
        if self.error is not None:
            raise self.error
        return self.responses.get(action, {"message_id": 5150})

    @property
    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]


@pytest.fixture
def transport(monkeypatch):
    spy = _Transport(responses={"get_ai_characters": _CHARACTERS})
    monkeypatch.setattr(OBC, "onebot_call", spy)
    return spy


@pytest.fixture
def no_gateway(monkeypatch):
    monkeypatch.setattr(AV, "live_adapter", lambda: (None, None))


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


@pytest.fixture
def gateway(monkeypatch, loop_thread):
    adapter = object()
    monkeypatch.setattr(AV, "live_adapter", lambda: (adapter, loop_thread))
    return adapter


def _err(raw: str) -> dict:
    payload = json.loads(raw)
    assert "error" in payload, f"expected a tool error, got {payload}"
    return payload


def _ok(raw: str) -> dict:
    payload = json.loads(raw)
    assert payload.get("success") is True, f"expected success, got {payload}"
    return payload


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


class TestArguments:
    def test_a_private_target_is_refused_with_the_working_alternative(
        self, transport, no_gateway
    ):
        payload = _err(
            AV.handle_qq_ai_voice({"user_id": PEER, "text": "hi", "character": "c"})
        )
        assert "group-only" in payload["error"]
        assert "qq_send_voice" in payload["error"]
        assert transport.calls == []

    def test_a_private_target_is_refused_even_alongside_a_group(
        self, transport, no_gateway
    ):
        payload = _err(
            AV.handle_qq_ai_voice({
                "user_id": PEER,
                "group_id": GROUP,
                "text": "hi",
                "character": "c",
            })
        )
        assert "group-only" in payload["error"]
        assert transport.calls == []

    def test_a_missing_group_id_is_refused(self, transport, no_gateway):
        payload = _err(AV.handle_qq_ai_voice({"text": "hi", "character": "c"}))
        assert "'group_id'" in payload["error"]
        assert transport.calls == []

    def test_a_junk_group_id_is_refused(self, transport, no_gateway):
        payload = _err(AV.handle_qq_ai_voice({"group_id": "abc", "character": "c"}))
        assert "numeric QQ id" in payload["error"]
        assert transport.calls == []

    def test_a_character_without_text_is_refused(self, transport, no_gateway):
        payload = _err(AV.handle_qq_ai_voice({"group_id": GROUP, "character": "c"}))
        assert "'text'" in payload["error"]
        assert transport.calls == []

    def test_a_numeric_string_group_id_is_accepted(self, transport, no_gateway):
        _ok(
            AV.handle_qq_ai_voice({
                "group_id": str(GROUP),
                "character": "c",
                "text": "hi",
            })
        )
        _, params = transport.calls[0]
        assert params["group_id"] == GROUP


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestFlattenCharacters:
    def test_folds_the_style_in_and_drops_the_preview_url(self):
        out = AV._flatten_characters(_CHARACTERS)
        assert out[0] == {
            "character_id": "lucy-voice-guangdong-f1",
            "character_name": "小何",
            "style": "推荐",
        }
        assert len(out) == 3
        assert all("preview_url" not in entry for entry in out)

    @pytest.mark.parametrize("junk", [None, {}, "nope", 7])
    def test_a_shape_we_did_not_expect_is_empty_not_an_exception(self, junk):
        assert AV._flatten_characters(junk) == []

    def test_entries_without_an_id_are_skipped(self):
        out = AV._flatten_characters([{"type": "现代", "characters": [{"x": 1}]}])
        assert out == []


class TestDiscovery:
    def test_no_character_lists_instead_of_sending(self, transport, no_gateway):
        payload = _ok(AV.handle_qq_ai_voice({"group_id": GROUP, "text": "早上好"}))

        assert payload["sent"] is False
        assert payload["count"] == 3
        assert payload["characters"][0]["character_id"] == "lucy-voice-guangdong-f1"
        assert payload["pending_text"] == "早上好"
        # The whole point: nothing was posted with a guessed character.
        assert transport.actions == ["get_ai_characters"]

    def test_listing_needs_no_text(self, transport, no_gateway):
        payload = _ok(AV.handle_qq_ai_voice({"group_id": GROUP}))
        assert payload["sent"] is False
        assert payload["pending_text"] is None

    def test_listing_does_not_need_the_live_adapter_seam(self, transport, monkeypatch):
        """A read has no mute, no policy and nothing to record.

        ``onebot_call`` already reuses the gateway's live socket by itself, so
        the listing path must not depend on the adapter being published.
        """

        def explode():  # pragma: no cover — must never run
            raise AssertionError("listing must not consult the live adapter")

        monkeypatch.setattr(AV, "live_adapter", explode)
        _ok(AV.handle_qq_ai_voice({"group_id": GROUP}))

    def test_a_backend_that_cannot_list_names_the_napcat_constraint(
        self, no_gateway, monkeypatch
    ):
        spy = _Transport(error=RuntimeError("OneBot action 'get_ai_characters' failed"))
        monkeypatch.setattr(OBC, "onebot_call", spy)

        payload = _err(AV.handle_qq_ai_voice({"group_id": GROUP}))
        assert "NapCat" in payload["error"]
        assert "get_ai_characters" in payload["error"]
        assert len(spy.calls) == 1

    def test_an_empty_list_names_the_napcat_constraint(self, no_gateway, monkeypatch):
        spy = _Transport(responses={"get_ai_characters": []})
        monkeypatch.setattr(OBC, "onebot_call", spy)

        payload = _err(AV.handle_qq_ai_voice({"group_id": GROUP}))
        assert "NapCat" in payload["error"]


# ---------------------------------------------------------------------------
# The direct (no-gateway) path
# ---------------------------------------------------------------------------


class TestDirectPath:
    def test_posts_send_group_ai_record_with_both_field_spellings(
        self, transport, no_gateway
    ):
        payload = _ok(
            AV.handle_qq_ai_voice({
                "group_id": GROUP,
                "character": "lucy-voice-lizeyan",
                "text": "早上好",
            })
        )
        assert transport.actions == ["send_group_ai_record"]
        _, params = transport.calls[0]
        assert params["group_id"] == GROUP
        assert params["character"] == "lucy-voice-lizeyan"
        # NapCat's docs disagree with themselves about "text" vs "test";
        # ``protocol.action_to_wire`` sends both and this hand-built params
        # dict matches it deliberately.
        assert params["text"] == "早上好"
        assert params["test"] == "早上好"
        assert payload["delivery_path"] == "direct"
        assert payload["message_id"] == 5150
        assert payload["character"] == "lucy-voice-lizeyan"

    def test_a_rejection_names_the_napcat_constraint_and_is_never_retried(
        self, no_gateway, monkeypatch
    ):
        spy = _Transport(
            error=RuntimeError(
                "OneBot action 'send_group_ai_record' failed: unknown action "
                "(retcode=1404)"
            )
        )
        monkeypatch.setattr(OBC, "onebot_call", spy)

        payload = _err(
            AV.handle_qq_ai_voice({"group_id": GROUP, "character": "c", "text": "hi"})
        )
        assert "NapCat" in payload["error"]
        assert "Lagrange" in payload["error"]
        assert "retcode=1404" in payload["error"]  # verbatim, not swallowed
        assert payload["code"] == "onebot_failed"
        assert payload["delivery_path"] == "direct"
        assert len(spy.calls) == 1


# ---------------------------------------------------------------------------
# The gateway path
# ---------------------------------------------------------------------------


class TestGatewayPathIsPreferred:
    def test_speak_ai_character_runs_and_the_raw_transport_does_not(
        self, transport, gateway, monkeypatch
    ):
        seen: list[tuple] = []

        async def fake_speak_ai(adapter, chat_id, text, character, **kw):
            seen.append((adapter, chat_id, text, character))
            return V.SpeakOutcome(spoke=True, message_ids=("31337",))

        monkeypatch.setattr(V, "speak_ai_character", fake_speak_ai)

        payload = _ok(
            AV.handle_qq_ai_voice({
                "group_id": GROUP,
                "character": "lucy-voice-lizeyan",
                "text": "早上好",
            })
        )
        assert len(seen) == 1
        adapter, chat_id, text, character = seen[0]
        assert adapter is gateway
        assert chat_id == f"g{GROUP}"
        assert text == "早上好"
        assert character == "lucy-voice-lizeyan"
        assert transport.calls == [], "the guarded path must not touch the transport"
        assert payload["delivery_path"] == "gateway"
        assert payload["message_id"] == "31337"

    def test_a_muted_group_is_an_error_and_is_not_blamed_on_napcat(
        self, transport, gateway, monkeypatch
    ):
        """A local refusal is not a backend problem; saying so would misdirect."""
        failure = SendResult(
            success=False, error="group speech is muted", error_kind="forbidden"
        )

        async def fake_speak_ai(adapter, chat_id, text, character, **kw):
            return V.SpeakOutcome(
                spoke=False, reason="group speech is muted", failure=failure
            )

        monkeypatch.setattr(V, "speak_ai_character", fake_speak_ai)

        payload = _err(
            AV.handle_qq_ai_voice({"group_id": GROUP, "character": "c", "text": "hi"})
        )
        assert "muted" in payload["error"]
        assert "NapCat" not in payload["error"]
        assert payload["code"] == "forbidden"
        assert transport.calls == []

    def test_a_backend_rejection_through_the_gateway_names_napcat(
        self, transport, gateway, monkeypatch
    ):
        failure = SendResult(
            success=False,
            error="OneBot rejected the AI voice message: unknown action",
            error_kind="permanent",
            raw_response={"status": "failed", "retcode": 1404},
        )

        async def fake_speak_ai(adapter, chat_id, text, character, **kw):
            return V.SpeakOutcome(spoke=False, reason=failure.error, failure=failure)

        monkeypatch.setattr(V, "speak_ai_character", fake_speak_ai)

        payload = _err(
            AV.handle_qq_ai_voice({"group_id": GROUP, "character": "c", "text": "hi"})
        )
        assert "NapCat" in payload["error"]
        assert "unknown action" in payload["error"]
        assert transport.calls == []

    def test_the_coroutine_raising_is_one_clear_error(
        self, transport, gateway, monkeypatch
    ):
        async def fake_speak_ai(adapter, chat_id, text, character, **kw):
            raise RuntimeError("adapter exploded")

        monkeypatch.setattr(V, "speak_ai_character", fake_speak_ai)
        payload = _err(
            AV.handle_qq_ai_voice({"group_id": GROUP, "character": "c", "text": "hi"})
        )
        assert "adapter exploded" in payload["error"]
        assert payload["code"] == "gateway_failed"
        assert transport.calls == []


class TestFallbackPathIsUsedWhenNothingIsPublished:
    def test_no_adapter_means_the_direct_transport_and_the_result_says_so(
        self, transport, monkeypatch
    ):
        from plugins.platforms.onebot import client as C

        monkeypatch.setattr(C, "get_live_adapter", lambda: (None, None))

        def explode(*a, **kw):  # pragma: no cover — must never run
            raise AssertionError("speak_ai_character must not run without an adapter")

        monkeypatch.setattr(V, "speak_ai_character", explode)

        payload = _ok(
            AV.handle_qq_ai_voice({"group_id": GROUP, "character": "c", "text": "hi"})
        )
        assert payload["delivery_path"] == "direct"
        assert "best-effort" in payload["delivery_note"]
        assert transport.actions == ["send_group_ai_record"]
