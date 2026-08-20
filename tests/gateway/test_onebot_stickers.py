"""Grantley's stickers: offered by dice, chosen by the model, sent inline.

The feature has two halves that meet only through a text marker, and almost
every test here exists because one half can silently stop matching the other.

**The offer.** A probability decides whether one turn's persona frame carries
the ``## 可用表情`` list at all.  It does *not* decide whether a sticker is
sent — the model does that, by writing ``[STICKER:<slug>]`` on its own line.
Suppression is therefore by omission: on most turns the model has no idea
stickers exist and writes plain text without being told not to, which is a
far stronger restraint than a "use these sparingly" sentence it renegotiates
every turn.

**The send.** The adapter lifts the marker out of the body, resolves the slug
against the catalogue, and appends one ``ImageSegment`` to the LAST chat
bubble.  Never a separate message: an extra notification is exactly the cost
the bubble cap next door exists to bound, and spending it on a cartoon would
be perverse.

What these tests pin, and the failure each one is hunting:

* the probability gate is absolute at both ends — ``0`` never offers and
  never honours (a model that saw the menu on an earlier turn WILL sometimes
  emit the marker on a turn where it was not offered, and an operator who
  switched the feature off must not get a sticker anyway), ``1`` always does;
* the marker never reaches a human, whatever it says.  A valid slug becomes
  an image; an invented one becomes nothing at all — the reply still goes,
  minus the syntax.  Printing ``[STICKER:happy]`` into a QQ window and
  raising over it are both worse than silence;
* the marker is lifted BEFORE the Tencent content gate, so the gate audits
  the sentence a person actually reads;
* the sticker is resolved AFTER the routing decision, so it can change
  neither the bubble count nor the card/chat verdict;
* the card lane carries no sticker, in the body or in any node;
* a reply that was *only* a marker sends the image alone — no empty text
  bubble beside it, which QQ renders as a blank grey box;
* the quote / @mention behaviour is untouched.

Everything runs against a fake client and the repo's own asset directory — no
sockets, no NapCat, no model call.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import persona_binding as PB
from plugins.platforms.onebot import protocol as P
from plugins.platforms.onebot import sticker as S
from plugins.qzone import policy as POLICY


GROUP = 183287894
DM_PEER = 536132102
SEP = A.BUBBLE_SEPARATOR

#: Shared with ``test_onebot_bubble_cap.py`` / ``test_onebot_content_gate.py``
#: so a rule-table revision fails loudly in all three rather than silently
#: testing nothing.
BLOCKED_TEXT = "QQ解冻教程"

#: Comfortably over ``FORWARD_TEXT_THRESHOLD`` — routes to the card lane.
LONG_BODY = "长" * 1200


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Records every outbound action; never touches a socket."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None) -> None:
        self.actions: List[Any] = []
        self._responses: List[Dict[str, Any]] = list(responses or [])
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0

    async def call_action(self, action, *, timeout=None):
        self.actions.append(action)
        if self._responses:
            return self._responses.pop(0)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

    async def send_action(self, action):
        self.actions.append(action)

    async def close(self):
        self.connected = False


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    client: Optional[FakeClient] = None,
) -> A.OneBotAdapter:
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
        # Stickers ON by default in this suite: nearly every test here is
        # about what happens once one is in play, and the probability gate
        # gets its own section rather than being an accident of the fixture.
        "sticker_probability": 1,
    }
    base.update(extra or {})
    # A caller passing ``None`` means "say nothing here", so the key is
    # removed rather than set: an explicit ``None`` in ``extra`` shadows the
    # environment under this adapter's ``extra.get(key, os.getenv(...))``
    # convention, which is not what such a test is trying to arrange.
    base = {k: v for k, v in base.items() if v is not None}
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad.router.group_replies_enabled = True
    return ad


def text_messages(ad: A.OneBotAdapter) -> List[str]:
    """Text of each outgoing message, one entry per message on the wire."""
    out: List[str] = []
    for action in ad._client.actions:
        segs = getattr(action, "message", None)
        if segs is None:
            continue
        out.append("".join(getattr(s, "text", "") or "" for s in segs))
    return out


def segments_of(ad: A.OneBotAdapter, index: int) -> List[Any]:
    return list(getattr(ad._client.actions[index], "message", []) or [])


def image_segments(ad: A.OneBotAdapter) -> List[Any]:
    """Every image segment the adapter emitted, across all messages."""
    out: List[Any] = []
    for action in ad._client.actions:
        for seg in getattr(action, "message", None) or []:
            if isinstance(seg, P.ImageSegment):
                out.append(seg)
    return out


def cards(ad: A.OneBotAdapter) -> List[Any]:
    return [
        a
        for a in ad._client.actions
        if isinstance(a, (P.SendPrivateForwardMsg, P.SendGroupForwardMsg))
    ]


def card_segments(ad: A.OneBotAdapter) -> List[Any]:
    """Every segment inside every node of every card that was sent."""
    out: List[Any] = []
    for card in cards(ad):
        for node in card.messages:
            out.extend(node.content)
    return out


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


@pytest.fixture(autouse=True)
def _no_bubble_gap(monkeypatch):
    monkeypatch.setattr(A, "BUBBLE_GAP_SECS", 0)


#: An ``extra`` that reads no real persona config: ``persona_channels``
#: present-but-empty means "no bindings", so ``channel_prompt`` returns
#: whatever the sticker menu contributes and nothing else.
NO_BINDINGS: Dict[str, Any] = {"persona_channels": {}}


# ---------------------------------------------------------------------------
# 0. The pure helpers
# ---------------------------------------------------------------------------


class TestMarkerExtraction:
    """``extract_markers`` lifts syntax out of prose without scarring it."""

    def test_a_marker_on_its_own_line_leaves_no_hole(self):
        body, slugs = S.extract_markers("第一句\n\n[STICKER:thumbs-up]\n\n第二句")
        assert slugs == ["thumbs-up"]
        assert body == "第一句\n\n第二句", "removal left a phantom paragraph break"

    def test_an_inline_marker_is_lifted_too(self):
        body, slugs = S.extract_markers("干得漂亮[STICKER:thumbs-up]")
        assert (body, slugs) == ("干得漂亮", ["thumbs-up"])

    def test_a_marker_only_body_becomes_empty(self):
        assert S.extract_markers("[STICKER:shrug]") == ("", ["shrug"])

    def test_an_unknown_slug_is_still_stripped(self):
        """Validity is the caller's question; syntax never reaches a reader."""
        assert S.extract_markers("在的[STICKER:nonexistent]") == (
            "在的",
            ["nonexistent"],
        )

    def test_text_without_markers_is_returned_untouched(self):
        body, slugs = S.extract_markers("就是一句普通的话\n\n带个空行")
        assert (body, slugs) == ("就是一句普通的话\n\n带个空行", [])

    def test_case_and_inner_spacing_are_forgiven(self):
        body, slugs = S.extract_markers("好[sticker: Thumbs-Up ]")
        assert (body, slugs) == ("好", ["thumbs-up"])

    def test_several_markers_are_all_reported_in_order(self):
        _, slugs = S.extract_markers("[STICKER:shrug]a[STICKER:angry]")
        assert slugs == ["shrug", "angry"]

    def test_empty_input_is_safe(self):
        assert S.extract_markers("") == ("", [])


class TestSlugResolution:
    """The catalogue is an allowlist, and it is checked before the disk is."""

    def test_every_catalogued_sticker_exists_on_disk(self):
        """The menu may never advertise a file the send path cannot ship."""
        missing = [
            e["slug"] for e in S.STICKER_CATALOG if not S.sticker_path(e["slug"])
        ]
        assert missing == [], f"catalogued but absent: {missing}"

    def test_all_nine_are_catalogued(self):
        assert len(S.STICKER_CATALOG) == 9

    def test_an_unknown_slug_resolves_to_nothing(self):
        assert S.sticker_path("nonexistent") is None

    def test_a_traversal_attempt_resolves_to_nothing(self):
        """Slugs come from model output, so they are never joined blindly."""
        assert S.sticker_path("../../../etc/passwd") is None
        assert S.sticker_path("../grantley/persona.py") is None

    def test_a_missing_directory_degrades_to_an_empty_menu(self, tmp_path):
        assert S.available_stickers(str(tmp_path / "nope")) == []

    def test_a_partial_directory_offers_only_what_is_there(self, tmp_path):
        (tmp_path / "shrug.jpg").write_bytes(b"\xff\xd8\xff")
        assert [e["slug"] for e in S.available_stickers(str(tmp_path))] == ["shrug"]


# ---------------------------------------------------------------------------
# 1. The probability gate — both ends are absolute
# ---------------------------------------------------------------------------


class TestTheOfferProbability:
    def test_zero_never_offers_the_menu(self):
        extra = {**NO_BINDINGS, "sticker_probability": 0}
        assert PB.channel_prompt(extra, chat_id=DM_PEER, is_group=False) is None

    def test_one_always_offers_the_full_menu(self):
        extra = {**NO_BINDINGS, "sticker_probability": 1}
        frame = PB.channel_prompt(extra, chat_id=DM_PEER, is_group=False)
        assert frame is not None
        assert "## 可用表情" in frame
        for entry in S.STICKER_CATALOG:
            assert f"`{entry['slug']}`" in frame, f"{entry['slug']} missing from menu"

    def test_the_menu_explains_the_marker_syntax(self):
        """A list of slugs the model cannot spend is a list of nothing."""
        frame = PB.channel_prompt(
            {**NO_BINDINGS, "sticker_probability": 1},
            chat_id=DM_PEER,
            is_group=False,
        )
        assert "[STICKER:" in frame

    def test_groups_are_offered_the_menu_too(self):
        frame = PB.channel_prompt(
            {**NO_BINDINGS, "sticker_probability": 1}, chat_id=GROUP, is_group=True
        )
        assert frame is not None and "## 可用表情" in frame

    def test_the_default_is_a_restrained_rate(self):
        assert 0.0 < S.DEFAULT_STICKER_PROBABILITY <= 0.25

    def test_the_gate_never_consults_the_rng_at_the_extremes(self):
        """0 and 1 are decisions, not very likely coin tosses."""

        class Exploding(random.Random):
            def random(self):  # noqa: D102
                raise AssertionError("the rng was consulted at an extreme")

        assert S.should_offer(0, Exploding()) is False
        assert S.should_offer(1, Exploding()) is True

    def test_a_mid_range_probability_is_reproducible_from_its_seed(self):
        """Pinned with a fixed seed — nothing here may flake."""
        draws = [S.should_offer(0.5, random.Random(7)) for _ in range(5)]
        assert len(set(draws)) == 1, "same seed must give the same answer"

        rng = random.Random(20240819)
        hits = sum(S.should_offer(0.25, rng) for _ in range(4000))
        assert hits == 1047, "seeded draw sequence changed"

    def test_out_of_range_settings_are_clamped_not_rejected(self):
        assert S.probability_from_extra({"sticker_probability": 5}) == 1.0
        assert S.probability_from_extra({"sticker_probability": -3}) == 0.0

    def test_a_garbage_setting_falls_back_to_the_default(self):
        assert (
            S.probability_from_extra({"sticker_probability": "banana"})
            == S.DEFAULT_STICKER_PROBABILITY
        )

    def test_it_reads_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_STICKER_PROBABILITY", "0")
        assert make_adapter({"sticker_probability": None}).sticker_probability == 0.0

    def test_extra_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_STICKER_PROBABILITY", "0")
        assert make_adapter({"sticker_probability": 1}).sticker_probability == 1.0

    def test_the_asset_directory_is_configurable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ONEBOT_STICKER_DIR", str(tmp_path))
        assert make_adapter().sticker_dir == str(tmp_path)


# ---------------------------------------------------------------------------
# 2. Zero is a hard off switch, not merely "never offered"
# ---------------------------------------------------------------------------


class TestZeroIsAbsolute:
    """A model that saw the menu once can emit the marker again later."""

    @pytest.mark.asyncio
    async def test_a_hallucinated_marker_sends_no_image(self):
        ad = make_adapter({"sticker_probability": 0})
        await ad.send(str(DM_PEER), "在的\n[STICKER:thumbs-up]")
        assert image_segments(ad) == [], "a switched-off feature sent a sticker"

    @pytest.mark.asyncio
    async def test_the_marker_is_still_stripped_when_switched_off(self):
        """Off must not mean "print the syntax instead"."""
        ad = make_adapter({"sticker_probability": 0})
        await ad.send(str(DM_PEER), "在的\n[STICKER:thumbs-up]")
        assert text_messages(ad) == ["在的"]

    @pytest.mark.asyncio
    async def test_a_marker_only_reply_when_off_sends_nothing_at_all(self):
        ad = make_adapter({"sticker_probability": 0})
        res = await ad.send(str(DM_PEER), "[STICKER:shrug]")
        assert res.success is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_live_probability_change_controls_the_outbound_sticker_too(self):
        """The menu and send path must resolve the same live config value."""
        ad = make_adapter({"sticker_probability": 1})
        ad.config.extra["sticker_probability"] = 0

        await ad.send(str(DM_PEER), "在的\n[STICKER:thumbs-up]")

        assert image_segments(ad) == []
        assert text_messages(ad) == ["在的"]

    @pytest.mark.asyncio
    async def test_a_live_probability_enable_allows_a_sticker(self):
        ad = make_adapter({"sticker_probability": 0})
        ad.config.extra["sticker_probability"] = 1

        await ad.send(str(DM_PEER), "在的\n[STICKER:thumbs-up]")

        assert len(image_segments(ad)) == 1


# ---------------------------------------------------------------------------
# 3. The happy path: marker in, image on the last bubble
# ---------------------------------------------------------------------------


class TestTheStickerRidesTheLastBubble:
    @pytest.mark.asyncio
    async def test_the_marker_is_replaced_by_an_image_segment(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), "干得漂亮\n[STICKER:thumbs-up]")
        assert res.success is True
        assert text_messages(ad) == ["干得漂亮"]
        assert len(image_segments(ad)) == 1

    @pytest.mark.asyncio
    async def test_it_lands_on_the_last_bubble_not_the_first(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), f"先说这个{SEP}再说那个\n[STICKER:laughing]")
        assert len(ad._client.actions) == 2, "the sticker fired an extra message"
        assert not any(isinstance(s, P.ImageSegment) for s in segments_of(ad, 0))
        assert any(isinstance(s, P.ImageSegment) for s in segments_of(ad, 1))

    @pytest.mark.asyncio
    async def test_the_image_follows_the_text_within_that_bubble(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), "好啊\n[STICKER:heart-hug]")
        kinds = [type(s).__name__ for s in segments_of(ad, 0)]
        assert kinds == ["TextSegment", "ImageSegment"]

    @pytest.mark.asyncio
    async def test_a_marker_at_the_top_still_rides_the_last_bubble(self):
        """Position in the body is syntax, not intent about ordering."""
        ad = make_adapter()
        await ad.send(str(DM_PEER), f"[STICKER:shrug]\n第一句{SEP}第二句")
        assert not any(isinstance(s, P.ImageSegment) for s in segments_of(ad, 0))
        assert any(isinstance(s, P.ImageSegment) for s in segments_of(ad, 1))

    @pytest.mark.asyncio
    async def test_the_image_is_shipped_inline_as_base64(self):
        """The backend runs in its own container and cannot read our disk."""
        ad = make_adapter()
        await ad.send(str(DM_PEER), "好\n[STICKER:thinking]")
        seg = image_segments(ad)[0]
        assert seg.file.startswith("base64://")
        assert len(seg.file) > len("base64://") + 100, "payload looks empty"

    @pytest.mark.asyncio
    async def test_only_one_image_goes_out_however_many_markers(self):
        """One flourish; a wall of stickers is the noise we exist to prevent."""
        ad = make_adapter()
        await ad.send(
            str(DM_PEER), "话\n[STICKER:shrug]\n[STICKER:angry]\n[STICKER:laughing]"
        )
        assert len(image_segments(ad)) == 1

    @pytest.mark.asyncio
    async def test_it_works_in_a_group_too(self):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", "收到\n[STICKER:fired-up]")
        assert res.success is True
        assert len(image_segments(ad)) == 1

    @pytest.mark.asyncio
    async def test_every_catalogued_slug_can_actually_be_sent(self):
        """Guards the menu against advertising something that then fails."""
        for entry in S.STICKER_CATALOG:
            ad = make_adapter()
            await ad.send(str(DM_PEER), f"话\n[STICKER:{entry['slug']}]")
            assert len(image_segments(ad)) == 1, f"{entry['slug']} did not send"


# ---------------------------------------------------------------------------
# 4. Bad input degrades to silence, never to an error or to raw syntax
# ---------------------------------------------------------------------------


class TestUnknownSlugsAreDroppedQuietly:
    @pytest.mark.asyncio
    async def test_an_invented_slug_sends_no_image_and_no_error(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), "在的\n[STICKER:nonexistent]")
        assert res.success is True
        assert image_segments(ad) == []
        assert text_messages(ad) == ["在的"]

    @pytest.mark.asyncio
    async def test_the_reader_never_sees_the_raw_marker(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), "在的\n[STICKER:happy]")
        for message in text_messages(ad):
            assert "STICKER" not in message

    @pytest.mark.asyncio
    async def test_a_missing_file_degrades_to_text_only(self, tmp_path):
        """The deployed box lost the assets; the answer still arrives."""
        ad = make_adapter({"sticker_dir": str(tmp_path)})
        res = await ad.send(str(DM_PEER), "在的\n[STICKER:thumbs-up]")
        assert res.success is True
        assert text_messages(ad) == ["在的"]
        assert image_segments(ad) == []

    @pytest.mark.asyncio
    async def test_a_rejected_sticker_retries_the_text_once_without_it(self):
        """A cosmetic image failure must not discard the actual answer."""
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 100, "message": "image unsupported"},
                {"status": "ok", "retcode": 0, "data": {"message_id": 1002}},
            ]
        )
        ad = make_adapter(client=client)

        res = await ad.send(str(DM_PEER), "在的\n[STICKER:thumbs-up]")

        assert res.success is True
        assert len(client.actions) == 2
        assert any(
            isinstance(segment, P.ImageSegment)
            for segment in getattr(client.actions[0], "message", [])
        )
        assert not any(
            isinstance(segment, P.ImageSegment)
            for segment in getattr(client.actions[1], "message", [])
        )
        assert "在的" in "".join(
            getattr(segment, "text", "") or ""
            for segment in getattr(client.actions[1], "message", [])
        )

    @pytest.mark.asyncio
    async def test_a_permanent_target_error_does_not_retry_without_the_sticker(self):
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 1404, "message": "unknown target"},
            ]
        )
        ad = make_adapter(client=client)

        res = await ad.send(str(DM_PEER), "在的\n[STICKER:thumbs-up]")

        assert res.success is False
        assert res.error_kind == "not_found"
        assert len(client.actions) == 1

    @pytest.mark.asyncio
    async def test_a_valid_slug_after_an_invalid_one_still_wins(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), "话\n[STICKER:bogus]\n[STICKER:angry]")
        assert len(image_segments(ad)) == 1


# ---------------------------------------------------------------------------
# 5. The sticker changes neither the bubble count nor the routing verdict
# ---------------------------------------------------------------------------


class TestTheStickerCannotReshapeTheReply:
    @pytest.mark.asyncio
    async def test_the_bubble_count_is_identical_with_and_without_a_marker(self):
        body = f"一{SEP}二{SEP}三"
        plain = make_adapter()
        await plain.send(str(DM_PEER), body)
        marked = make_adapter()
        await marked.send(str(DM_PEER), body + "\n[STICKER:thinking]")
        assert len(marked._client.actions) == len(plain._client.actions) == 3
        assert text_messages(marked) == text_messages(plain)

    @pytest.mark.asyncio
    async def test_a_marker_cannot_tip_a_chat_reply_into_a_card(self):
        """Routing measures the STRIPPED text, so the syntax cannot count."""
        # Just under the fold threshold once the marker is removed, but over
        # it if the marker's own characters were (wrongly) counted.
        body = "话" * (A.FORWARD_TEXT_THRESHOLD - 5) + "\n[STICKER:thumbs-up]"
        assert len(body) > A.FORWARD_TEXT_THRESHOLD
        ad = make_adapter()
        await ad.send(str(DM_PEER), body)
        assert cards(ad) == [], "the marker's characters were counted as content"
        assert len(image_segments(ad)) == 1

    @pytest.mark.asyncio
    async def test_a_marker_cannot_tip_a_reply_over_the_bubble_cap(self):
        """A marker on its own line is not a bubble."""
        ad = make_adapter()
        await ad.send(str(DM_PEER), f"一{SEP}二{SEP}三\n[STICKER:shrug]")
        assert cards(ad) == []
        assert len(ad._client.actions) == 3

    @pytest.mark.asyncio
    async def test_the_marker_is_not_archived_into_group_history(self):
        """The buffer is prompt input; syntax fed back becomes a habit."""
        ad = make_adapter()
        await ad.send(f"g{GROUP}", "收到\n[STICKER:fired-up]")
        buffered = "".join(
            text
            for _ts, _sender, text, _is_self in A._GROUP_RECENT[
                A.speech_key(ad.instance_id, GROUP)
            ]
        )
        assert "STICKER" not in buffered


# ---------------------------------------------------------------------------
# 6. The card lane never carries a sticker
# ---------------------------------------------------------------------------


class TestTheCardLaneIsStickerFree:
    @pytest.mark.asyncio
    async def test_a_long_reply_folds_without_an_image(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), LONG_BODY + "\n[STICKER:thumbs-up]")
        assert res.success is True
        assert len(cards(ad)) == 1
        assert image_segments(ad) == []

    @pytest.mark.asyncio
    async def test_no_card_node_contains_an_image_either(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), LONG_BODY + "\n[STICKER:laughing]")
        assert not any(isinstance(s, P.ImageSegment) for s in card_segments(ad))

    @pytest.mark.asyncio
    async def test_the_marker_does_not_leak_into_a_card_node(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), LONG_BODY + "\n[STICKER:laughing]")
        for seg in card_segments(ad):
            assert "STICKER" not in (getattr(seg, "text", "") or "")

    @pytest.mark.asyncio
    async def test_a_many_bubble_reply_becomes_a_sticker_free_card(self):
        """Count overflow follows the same no-sticker card contract as length."""
        body = SEP.join(f"第{i}句" for i in range(1, 9)) + "\n[STICKER:shrug]"
        ad = make_adapter()
        await ad.send(str(DM_PEER), body)
        assert len(cards(ad)) == 1
        assert image_segments(ad) == []
        assert not any(isinstance(s, P.ImageSegment) for s in card_segments(ad))
        assert not any(
            "STICKER" in (getattr(s, "text", "") or "") for s in card_segments(ad)
        )


# ---------------------------------------------------------------------------
# 7. A reply that is nothing but a sticker
# ---------------------------------------------------------------------------


class TestStickerOnlyReplies:
    @pytest.mark.asyncio
    async def test_one_message_carrying_only_the_image(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), "[STICKER:shrug]")
        assert res.success is True
        assert len(ad._client.actions) == 1
        kinds = [type(s).__name__ for s in segments_of(ad, 0)]
        assert kinds == ["ImageSegment"], "an empty text bubble went out beside it"

    @pytest.mark.asyncio
    async def test_no_empty_text_segment_is_emitted(self):
        """QQ renders a zero-length message as a blank grey box."""
        ad = make_adapter()
        await ad.send(str(DM_PEER), "[STICKER:angry]")
        assert text_messages(ad) == [""] or text_messages(ad) == []
        assert not any(isinstance(s, P.TextSegment) for s in segments_of(ad, 0))

    @pytest.mark.asyncio
    async def test_whitespace_around_the_marker_does_not_create_a_bubble(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), "\n\n  [STICKER:shrug]  \n\n")
        assert len(ad._client.actions) == 1
        assert not any(isinstance(s, P.TextSegment) for s in segments_of(ad, 0))

    @pytest.mark.asyncio
    async def test_an_invalid_marker_only_reply_sends_nothing(self):
        """Nothing to say and nothing to show — so say nothing."""
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), "[STICKER:nonexistent]")
        assert res.success is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_sticker_only_group_reply_still_carries_the_mention(self):
        ad = make_adapter()
        await ad.send(
            f"g{GROUP}", "[STICKER:heart-hug]", metadata={"onebot_at_user_id": "777"}
        )
        kinds = [type(s).__name__ for s in segments_of(ad, 0)]
        assert kinds == ["AtSegment", "ImageSegment"]


# ---------------------------------------------------------------------------
# 8. The content gate still runs, and runs on what a human reads
# ---------------------------------------------------------------------------


class TestTheContentGateIsUnbypassed:
    def test_the_probe_phrase_is_still_refused(self):
        """Pins the fixture: a rule-table revision must fail loudly here."""
        cfg = POLICY.resolve_config(None)
        assert POLICY.moderate_text(BLOCKED_TEXT, cfg).decision.allowed is False

    @pytest.mark.asyncio
    async def test_a_refused_reply_leaks_neither_text_nor_image(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), BLOCKED_TEXT + "\n[STICKER:thumbs-up]")
        assert res.success is False
        assert ad._client.actions == [], "refused content escaped via the sticker path"

    @pytest.mark.asyncio
    async def test_a_refused_marker_only_reply_sends_no_image(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), BLOCKED_TEXT + "[STICKER:angry]")
        assert res.success is False
        assert image_segments(ad) == []

    @pytest.mark.asyncio
    async def test_a_refused_long_reply_still_reaches_no_lane(self):
        body = BLOCKED_TEXT + "。" + LONG_BODY + "\n[STICKER:shrug]"
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), body)
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_gate_audits_the_text_without_the_marker(self):
        """The marker is machine syntax; it must not split an audited phrase.

        Spliced through the middle of the probe phrase, the marker would hide
        it from a gate that ran first — and the reader would still read the
        phrase, because by then the marker is gone.
        """
        ad = make_adapter()
        spliced = BLOCKED_TEXT[:2] + "[STICKER:shrug]" + BLOCKED_TEXT[2:]
        res = await ad.send(str(DM_PEER), spliced)
        assert res.success is False, "a marker was used to smuggle refused text"
        assert ad._client.actions == []


# ---------------------------------------------------------------------------
# 9. Regression guard: quote / @mention behaviour is unchanged
# ---------------------------------------------------------------------------


class TestQuoteAndMentionAreUntouched:
    @pytest.mark.asyncio
    async def test_the_quote_and_mention_stay_on_the_first_message(self):
        ad = make_adapter()
        await ad.send(
            f"g{GROUP}",
            f"先说这个{SEP}再说那个\n[STICKER:thumbs-up]",
            reply_to="9001",
            metadata={"onebot_at_user_id": "777"},
        )
        first = [type(s).__name__ for s in segments_of(ad, 0)]
        assert first[:2] == ["ReplySegment", "AtSegment"]
        last = [type(s).__name__ for s in segments_of(ad, 1)]
        assert "ReplySegment" not in last and "AtSegment" not in last

    @pytest.mark.asyncio
    async def test_the_sticker_does_not_displace_the_mention(self):
        """Single bubble: lead segments, text, then the image — in that order."""
        ad = make_adapter()
        await ad.send(
            f"g{GROUP}",
            "收到\n[STICKER:fired-up]",
            reply_to="9001",
            metadata={"onebot_at_user_id": "777"},
        )
        kinds = [type(s).__name__ for s in segments_of(ad, 0)]
        assert kinds == ["ReplySegment", "AtSegment", "TextSegment", "ImageSegment"]

    @pytest.mark.asyncio
    async def test_a_dm_gets_no_mention_but_still_gets_the_sticker(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), "收到\n[STICKER:fired-up]")
        kinds = [type(s).__name__ for s in segments_of(ad, 0)]
        assert kinds == ["TextSegment", "ImageSegment"]


# ---------------------------------------------------------------------------
# 10. The frame composes with the persona binding rather than replacing it
# ---------------------------------------------------------------------------


class TestFrameComposition:
    def test_the_menu_is_offered_even_without_a_channel_binding(self):
        """Stickers belong to the account, not to a configured channel."""
        frame = PB.channel_prompt(
            {**NO_BINDINGS, "sticker_probability": 1}, chat_id=999, is_group=False
        )
        assert frame is not None and "## 可用表情" in frame

    def test_no_menu_and_no_binding_still_means_no_frame(self):
        assert (
            PB.channel_prompt(
                {**NO_BINDINGS, "sticker_probability": 0}, chat_id=999, is_group=False
            )
            is None
        )

    def test_a_broken_menu_never_costs_the_channel_frame(self, monkeypatch):
        """A flourish that explodes must not take the persona down with it."""

        def boom(*a, **kw):
            raise RuntimeError("catalogue on fire")

        monkeypatch.setattr(S, "offer_menu", boom)
        assert (
            PB.channel_prompt(
                {**NO_BINDINGS, "sticker_probability": 1},
                chat_id=DM_PEER,
                is_group=False,
            )
            is None
        )

    def test_the_menu_is_rerolled_per_turn_not_frozen_for_the_day(self):
        """``_channel_prompt`` is day-cached; the roll must sit outside it."""
        extra = {**NO_BINDINGS, "sticker_probability": 1}
        assert PB.channel_prompt(extra, chat_id=DM_PEER, is_group=False) is not None
        off = {**NO_BINDINGS, "sticker_probability": 0}
        assert PB.channel_prompt(off, chat_id=DM_PEER, is_group=False) is None
