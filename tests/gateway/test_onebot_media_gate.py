"""The media gate: the bytes nobody was looking at.

Captions were audited from the start. The pictures they were attached to were
not — ``adapter.send`` carried a ``TODO(media)`` saying so. This closes it.

Two things here are easy to get wrong and are pinned hard:

**The fail posture is inverted relative to the text agent gate.** That gate
fails *open* because a deterministic table sits behind it. Nothing sits behind
this one — it *is* the classifier, and the ported ``policy.moderate_media``
denies anything unclassified. So a vision timeout blocks the picture. That is
only safe because of how the caller degrades, which is the second thing:

**A blocked attachment does not fail the message.** It drops to a text-only
send, the same degradation the source system performs when an attachment is
refused but the body is not. The visible worst case for a vision outage is a
bot that talks but stops posting pictures — not a bot that stops talking.

The provenance shortcut is pinned too: the repo's own sticker pack is trusted
without a model call (a human committed those files; anyone who can change
them already has code execution), and a path that tries to *look* like it
lives there must not inherit that trust.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import media_gate as MG
from plugins.platforms.onebot.media_gate import (
    MediaGateConfig,
    MediaVerdict,
    is_trusted_asset,
    judge_media_file,
    resolve_media_gate_config,
)

GROUP = 183287894

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c630001000005000"
    "10d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def png(tmp_path: Path) -> Path:
    target = tmp_path / "probe.png"
    target.write_bytes(PNG_BYTES)
    return target


@pytest.fixture(autouse=True)
def _clear_cache():
    MG._CACHE.clear()
    yield
    MG._CACHE.clear()


def stub_llm(monkeypatch, answer: str | None = None, *, raises: Exception | None = None):
    """Replace the vision call. Returns the list of calls actually made."""
    calls: List[Dict[str, Any]] = []

    async def _call(**kwargs):
        calls.append(kwargs)
        if raises is not None:
            raise raises
        return answer

    monkeypatch.setattr(
        "agent.auxiliary_client.async_call_llm", _call, raising=False
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.extract_content_or_reasoning",
        lambda r: r if isinstance(r, str) else "",
        raising=False,
    )
    return calls


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


class TestTheVerdict:
    @pytest.mark.asyncio
    async def test_a_safe_answer_allows(self, monkeypatch, png):
        stub_llm(monkeypatch, '{"safe": true, "reason": "风景照", "category": ""}')
        verdict = await judge_media_file(png)
        assert verdict.safe is True
        assert verdict.consulted is True

    @pytest.mark.asyncio
    async def test_an_unsafe_answer_blocks(self, monkeypatch, png):
        stub_llm(
            monkeypatch, '{"safe": false, "reason": "二维码引流", "category": "qrcode"}'
        )
        verdict = await judge_media_file(png)
        assert verdict.safe is False
        assert verdict.consulted is True
        assert verdict.category == "qrcode"

    @pytest.mark.asyncio
    async def test_prose_around_the_json_is_tolerated(self, monkeypatch, png):
        stub_llm(monkeypatch, '好的，我的判断是：\n{"safe": true, "reason": "食物"}\n')
        assert (await judge_media_file(png)).safe is True

    @pytest.mark.asyncio
    async def test_a_truthy_string_is_not_an_approval(self, monkeypatch, png):
        """``"safe": "true"`` is not an answer in the shape we asked for.

        Reading it as approval is exactly how a gate silently becomes a no-op,
        so it is treated as unparseable — which, here, means blocked.
        """
        stub_llm(monkeypatch, '{"safe": "true", "reason": "x"}')
        verdict = await judge_media_file(png)
        assert verdict.safe is False
        assert verdict.consulted is False
        assert verdict.fallthrough_cause == "unparseable"


class TestItFailsClosed:
    """No deterministic table sits behind this gate, so it must not fail open."""

    @pytest.mark.asyncio
    async def test_a_timeout_blocks(self, monkeypatch, png):
        async def _hang(**_kw):
            await asyncio.sleep(10)

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _hang, raising=False
        )
        verdict = await judge_media_file(png, config=MediaGateConfig(timeout_secs=0.05))
        assert verdict.safe is False
        assert verdict.fallthrough_cause == "timeout"

    @pytest.mark.asyncio
    async def test_a_provider_error_blocks(self, monkeypatch, png):
        stub_llm(monkeypatch, raises=RuntimeError("no vision provider"))
        verdict = await judge_media_file(png)
        assert verdict.safe is False
        assert verdict.fallthrough_cause == "call_failed"

    @pytest.mark.asyncio
    async def test_an_unreadable_file_blocks(self, tmp_path):
        verdict = await judge_media_file(tmp_path / "does-not-exist.png")
        assert verdict.safe is False
        assert verdict.fallthrough_cause == "unreadable"

    @pytest.mark.asyncio
    async def test_garbage_blocks(self, monkeypatch, png):
        stub_llm(monkeypatch, "I'm afraid I can't help with that.")
        verdict = await judge_media_file(png)
        assert verdict.safe is False
        assert verdict.fallthrough_cause == "unparseable"

    @pytest.mark.asyncio
    async def test_an_unshrinkable_oversized_image_blocks(self, monkeypatch, png):
        monkeypatch.setattr(MG, "_downscale", lambda _raw: None)
        verdict = await judge_media_file(png, config=MediaGateConfig(max_bytes=1))
        assert verdict.safe is False
        assert verdict.fallthrough_cause == "too_large"

    @pytest.mark.asyncio
    async def test_the_master_switch_allows(self, png):
        verdict = await judge_media_file(png, config=MediaGateConfig(enabled=False))
        assert verdict.safe is True
        assert verdict.consulted is False


class TestProvenance:
    def test_the_repo_sticker_pack_is_trusted(self):
        root = MG._ASSET_ROOTS[0]
        assert is_trusted_asset(root / "stickers" / "thumbs-up.png") is True

    def test_an_outside_path_is_not(self, png):
        assert is_trusted_asset(png) is False

    def test_traversal_cannot_fake_trust(self):
        """``assets/../../etc/passwd`` must not read as a repo asset."""
        root = MG._ASSET_ROOTS[0]
        assert is_trusted_asset(root / ".." / ".." / ".." / "etc" / "passwd") is False

    @pytest.mark.asyncio
    async def test_a_trusted_asset_costs_no_model_call(self, monkeypatch):
        calls = stub_llm(monkeypatch, '{"safe": true}')
        root = MG._ASSET_ROOTS[0]
        verdict = await judge_media_file(root / "stickers" / "any.png")
        assert verdict.safe is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_trust_can_be_switched_off(self, monkeypatch):
        calls = stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        root = MG._ASSET_ROOTS[0]
        await judge_media_file(
            root / "stickers" / "any.png",
            config=MediaGateConfig(trust_repo_assets=False),
        )
        # It tried; whether the file exists is beside the point.
        assert len(calls) <= 1


class TestTheCache:
    @pytest.mark.asyncio
    async def test_the_same_image_is_judged_once(self, monkeypatch, png):
        calls = stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        for _ in range(4):
            assert (await judge_media_file(png)).safe is True
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_identical_bytes_at_two_paths_share_a_verdict(
        self, monkeypatch, tmp_path
    ):
        """Keyed by content, not by path — the same picture is one call."""
        calls = stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        a.write_bytes(PNG_BYTES)
        b.write_bytes(PNG_BYTES)
        await judge_media_file(a)
        await judge_media_file(b)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_failure_is_never_cached(self, monkeypatch, png):
        """A transient outage must not be pinned onto that image forever."""
        stub_llm(monkeypatch, raises=RuntimeError("down"))
        assert (await judge_media_file(png)).safe is False
        calls = stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        assert (await judge_media_file(png)).safe is True
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_it_is_bounded(self, monkeypatch, tmp_path):
        stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        for i in range(30):
            probe = tmp_path / f"i{i}.png"
            probe.write_bytes(PNG_BYTES + bytes([i]))
            await judge_media_file(probe, config=MediaGateConfig(cache_size=8))
        assert len(MG._CACHE) <= 8


class TestScreeningProfiles:
    """QZone screens on a different risk model than chat.

    The account-threatening categories are shared verbatim; only 色情 differs,
    because the 说说 lane publishes the persona's own reference-anchored art
    and the operator asked for shirtless / wet-clothes framing. Live on
    2026-08-28 the chat prompt read one shirtless frame as
    ``pornography_vulgarity`` and took all three pictures down with it.
    """

    @pytest.mark.asyncio
    async def test_chat_is_the_default(self, monkeypatch, png):
        calls = stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        await judge_media_file(png)
        assert calls[0]["messages"][0]["content"] == MG._SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_qzone_selects_the_looser_prompt(self, monkeypatch, png):
        calls = stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        await judge_media_file(png, profile="qzone")
        sent = calls[0]["messages"][0]["content"]
        assert sent == MG._QZONE_SYSTEM_PROMPT
        assert sent != MG._SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_an_unknown_profile_falls_back_to_the_stricter_one(
        self, monkeypatch, png
    ):
        """A typo must tighten the gate, never open it."""
        calls = stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        await judge_media_file(png, profile="qzoen")
        assert calls[0]["messages"][0]["content"] == MG._SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_profiles_do_not_share_a_cache_slot(self, monkeypatch, png):
        """Same bytes, different prompt — whichever ran first must not decide."""
        calls = stub_llm(monkeypatch, '{"safe": true, "reason": "ok"}')
        await judge_media_file(png, profile="chat")
        await judge_media_file(png, profile="qzone")
        assert len(calls) == 2

    def test_qzone_keeps_every_account_risk_category(self):
        """Only 色情 was relaxed. Losing 涉政 or 二维码 here is the real danger."""
        for clause in ("涉政", "血腥暴力", "违法交易", "二维码", "翻墙推广"):
            assert clause in MG._QZONE_SYSTEM_PROMPT

    def test_qzone_still_refuses_hard_pornography(self):
        for clause in ("生殖器", "性行为", "未成年"):
            assert clause in MG._QZONE_SYSTEM_PROMPT


class TestConfig:
    def test_it_fails_safe_to_on(self):
        for junk in (None, "nonsense", 42, object()):
            assert resolve_media_gate_config(junk).enabled is True

    def test_junk_values_fall_back_to_defaults(self):
        cfg = resolve_media_gate_config(
            {"timeout_secs": "soon", "max_tokens": None, "cache_size": -5}
        )
        assert cfg.timeout_secs > 0 and cfg.max_tokens > 0 and cfg.cache_size > 0

    def test_only_explicit_false_disables(self):
        assert resolve_media_gate_config({"enabled": "false"}).enabled is True
        assert resolve_media_gate_config({"enabled": False}).enabled is False

    def test_audit_fields_carry_no_path_or_bytes(self, tmp_path):
        fields = MediaVerdict(
            safe=False, consulted=True, reason="x", category="c"
        ).audit_fields()
        assert "reason" not in fields  # the model's prose is not an audit field
        assert fields["category"] == "c"


# ---------------------------------------------------------------------------
# Wired into the send path
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.actions: List[Any] = []
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0

    async def call_action(self, action, *, timeout=None):
        self.actions.append(action)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

    async def send_action(self, action):
        self.actions.append(action)

    async def close(self):
        self.connected = False


def make_adapter(extra: Optional[Dict[str, Any]] = None) -> A.OneBotAdapter:
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad.router.group_replies_enabled = True
    return ad


def sent_kinds(ad: A.OneBotAdapter) -> List[str]:
    out: List[str] = []
    for action in ad._client.actions:
        for seg in getattr(action, "message", []) or []:
            out.append(type(seg).__name__)
    return out


def wire_text(ad: A.OneBotAdapter) -> str:
    out: List[str] = []
    for action in ad._client.actions:
        for seg in getattr(action, "message", []) or []:
            text = getattr(seg, "text", None)
            if text:
                out.append(text)
    return "".join(out)


@pytest.fixture(autouse=True)
def _clean_module_state():
    A._reset_module_state()
    yield
    A._reset_module_state()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ONEBOT_"):
            monkeypatch.delenv(key, raising=False)


def block_media(monkeypatch):
    async def _blocked(_path, **_kw):
        return MediaVerdict(safe=False, consulted=True, category="qrcode")

    monkeypatch.setattr(MG, "judge_media_file", _blocked)


def allow_media(monkeypatch):
    async def _ok(_path, **_kw):
        return MediaVerdict(safe=True, consulted=True)

    monkeypatch.setattr(MG, "judge_media_file", _ok)


class TestTheSendPathDegrades:
    @pytest.mark.asyncio
    async def test_a_safe_image_goes_out(self, monkeypatch, png):
        allow_media(monkeypatch)
        ad = make_adapter()
        res = await ad.send_image_file(f"g{GROUP}", str(png), caption="看这个")
        assert res.success is True
        assert "ImageSegment" in sent_kinds(ad)

    @pytest.mark.asyncio
    async def test_a_blocked_image_still_sends_the_caption(self, monkeypatch, png):
        """Degrade to text-only, exactly as the source system does."""
        block_media(monkeypatch)
        ad = make_adapter()
        res = await ad.send_image_file(f"g{GROUP}", str(png), caption="看这个")
        assert res.success is True
        assert "ImageSegment" not in sent_kinds(ad)
        assert "看这个" in wire_text(ad)

    @pytest.mark.asyncio
    async def test_a_blocked_image_with_no_caption_sends_nothing(
        self, monkeypatch, png
    ):
        """Nothing left to degrade to, so the refusal stands."""
        block_media(monkeypatch)
        ad = make_adapter()
        res = await ad.send_image_file(f"g{GROUP}", str(png))
        assert res.success is False
        assert A.is_media_gate_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_vision_outage_does_not_silence_the_bot(self, monkeypatch, png):
        """The whole justification for failing closed: text still flows."""
        # Opt back in to the real gate: conftest stubs it offline for every
        # other suite, and this test is specifically about what the real one
        # does when the provider is gone. The module-level import above still
        # holds the original function, which is what makes this possible.
        monkeypatch.setattr(MG, "judge_media_file", judge_media_file)
        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm",
            _raise_down,
            raising=False,
        )
        monkeypatch.setattr(MG, "_CACHE", type(MG._CACHE)())
        ad = make_adapter()
        assert (await ad.send(f"g{GROUP}", "今天天气不错")).success is True
        res = await ad.send_image_file(f"g{GROUP}", str(png), caption="配文")
        assert res.success is True
        assert "ImageSegment" not in sent_kinds(ad)
        assert "配文" in wire_text(ad)

    @pytest.mark.asyncio
    async def test_the_refusal_carries_its_own_marker(self, monkeypatch, png):
        """Distinct from the text markers: the outcome differs."""
        block_media(monkeypatch)
        ad = make_adapter()
        res = await ad.send_image_file(f"g{GROUP}", str(png))
        assert A.is_media_gate_blocked_send_result(res) is True
        assert A.is_policy_blocked_send_result(res) is False
        assert A.is_agent_gate_blocked_send_result(res) is False


async def _raise_down(**_kw):
    raise RuntimeError("vision provider down")
