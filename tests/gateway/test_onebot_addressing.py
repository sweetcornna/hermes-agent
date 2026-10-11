"""Being spoken to without an @: quotes of the bot's posts, and the implicit-address judge.

Also the picture labelling that keeps stickers from reading as photos.  The
judge's model call is replaced by a fake ``async_call_llm``; nothing here
reaches a provider or QQ.
"""
import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent import auxiliary_client
from plugins.platforms.onebot import adapter as A, addressee as AD, protocol as P, turn_media as TM
from tests.gateway.test_onebot_reply_batch import drain
from tests.gateway.test_onebot_reply_context import GROUP, group_event, make_adapter

FAST = {
    "implicit_address_enabled": True,
    "implicit_address_quiet_seconds": 0.02,
    "implicit_address_max_wait_seconds": 0.2,
    "implicit_address_min_interval_seconds": 0,
    "implicit_address_recent_self_minutes": 0,
}


@pytest.fixture(autouse=True)
def _clean_module_state():
    A._reset_module_state()
    yield
    A._reset_module_state()


def _response(text: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


@pytest.fixture
def llm(monkeypatch) -> Dict[str, Any]:
    state: Dict[str, Any] = {"answer": '{"to_him": false, "index": null, "reason": "群友互聊"}', "calls": []}

    async def _call(task=None, **kwargs):  # noqa: ANN001
        state["calls"].append({"task": task, **kwargs})
        answer = state["answer"]
        if isinstance(answer, BaseException):
            raise answer
        return _response(answer)

    monkeypatch.setattr(auxiliary_client, "async_call_llm", _call)
    return state


async def settle(ad, seconds: float = 0.3) -> None:
    """Let the watcher's lull pass, then let any reply window finish."""
    await asyncio.sleep(seconds)
    await drain(ad)


def quote_event(text, quoted_id, *, uid=555, message_id=50):
    event = group_event(text, uid=uid, message_id=message_id)
    event.message = [P.ReplySegment(id=str(quoted_id)), P.TextSegment(text=text)]
    return event


# ---------------------------------------------------------------------------
# Quotes of the bot's own posts
# ---------------------------------------------------------------------------


class TestQuoteOfSelf:
    @pytest.mark.asyncio
    async def test_quoting_a_recent_bot_post_is_an_address(self):
        ad = make_adapter()
        ad._remember_own_message("777")
        await ad._on_message_event(quote_event("那你明天去吗", 777))
        assert [e.text for e in ad.turns.events] == ["那你明天去吗"]

    @pytest.mark.asyncio
    async def test_the_bots_send_results_are_remembered(self):
        ad = make_adapter()
        await ad.send(A.format_chat_id(True, GROUP), "我先说一句")
        assert "1001" in ad._own_message_ids
        await ad._on_message_event(quote_event("真的假的", 1001))
        assert len(ad.turns.events) == 1

    @pytest.mark.asyncio
    async def test_older_posts_are_resolved_with_get_msg(self, monkeypatch):
        ad = make_adapter()

        async def call(action, timeout=None):  # noqa: ANN001
            if getattr(action, "action", "") == "get_msg":
                return {"data": {"sender": {"user_id": 100, "nickname": "格兰"}, "message": []}}
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

        monkeypatch.setattr(ad, "_call", call)
        await ad._on_message_event(quote_event("你上次说的那个", 4242))
        assert len(ad.turns.events) == 1

    @pytest.mark.asyncio
    async def test_quoting_someone_else_is_not_an_address(self, monkeypatch):
        ad = make_adapter()

        async def call(action, timeout=None):  # noqa: ANN001
            if getattr(action, "action", "") == "get_msg":
                return {"data": {"sender": {"user_id": 9, "nickname": "bob"}, "message": []}}
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

        monkeypatch.setattr(ad, "_call", call)
        await ad._on_message_event(quote_event("哈哈哈", 4242))
        assert ad.turns.events == []

    @pytest.mark.asyncio
    async def test_can_be_switched_off(self):
        ad = make_adapter({"quote_reply_addresses_bot": False})
        ad._remember_own_message("777")
        await ad._on_message_event(quote_event("那你明天去吗", 777))
        assert ad.turns.events == []

    @pytest.mark.asyncio
    async def test_the_answer_quotes_the_message_that_quoted_the_bot(self):
        ad = make_adapter()
        ad._remember_own_message("777")
        await ad._on_message_event(quote_event("那你明天去吗", 777, message_id=88))
        event = ad.turns.events[0]
        assert event.metadata[A.REPLY_ANCHOR_META] == "88"

    def test_own_id_memory_is_bounded(self):
        ad = make_adapter()
        for i in range(A._OWN_MESSAGE_IDS_MAX + 5):
            ad._remember_own_message(str(i))
        assert len(ad._own_message_ids) == A._OWN_MESSAGE_IDS_MAX
        assert "0" not in ad._own_message_ids


# ---------------------------------------------------------------------------
# The implicit-address judge
# ---------------------------------------------------------------------------


class TestVerdictParsing:
    def test_yes_needs_a_valid_index(self):
        assert AD.parse_verdict('{"to_him": true, "index": 2, "reason": "x"}', 3).index == 2
        assert AD.parse_verdict('{"to_him": true, "index": 4}', 3) is None
        assert AD.parse_verdict('{"to_him": true, "index": null}', 3) is None
        assert AD.parse_verdict('{"to_him": true, "index": true}', 3) is None

    def test_no_and_garbage(self):
        assert AD.parse_verdict('{"to_him": false, "index": null}', 3).to_him is False
        assert AD.parse_verdict('{"to_him": "true", "index": 1}', 3) is None
        assert AD.parse_verdict("不知道", 3) is None

    def test_settings(self):
        assert AD.resolve_settings({}).enabled is False
        cfg = AD.resolve_settings(
            {"implicit_address_enabled": "on", "proactive_judge_persona": "虎", "implicit_address_quiet_seconds": "1.5"}
        )
        assert cfg.enabled and cfg.persona == "虎" and cfg.quiet_seconds == 1.5
        assert AD.resolve_settings({"implicit_address_persona": "猫", "proactive_judge_persona": "虎"}).persona == "猫"

    def test_media_only_messages_are_not_judged(self):
        assert not AD.worth_judging("[图片][表情包]")
        assert AD.worth_judging("[图片] 你看这个")

    def test_yaml_keys(self):
        extras = A._apply_yaml_config(
            {}, {"implicit_address_enabled": True, "quote_reply_addresses_bot": False}
        )
        assert extras["implicit_address_enabled"] is True
        assert extras["quote_reply_addresses_bot"] is False


class TestWatcher:
    @pytest.mark.asyncio
    async def test_off_by_default(self, llm):
        ad = make_adapter()
        await ad._on_message_event(group_event("老虎今天怎么不说话", message_id=1))
        await settle(ad)
        assert llm["calls"] == [] and ad.turns.events == []

    @pytest.mark.asyncio
    async def test_a_lull_is_judged_once_and_a_yes_answers_that_message(self, llm):
        llm["answer"] = '{"to_him": true, "index": 2, "reason": "在问他"}'
        ad = make_adapter(FAST)
        await ad._on_message_event(group_event("今天好热", uid=1, nickname="bob", message_id=1))
        await ad._on_message_event(group_event("那只老虎你觉得呢", uid=2, nickname="carol", message_id=2))
        await settle(ad)
        assert len(llm["calls"]) == 1
        call = llm["calls"][0]
        assert call["task"] == "onebot_address_judge"
        user = call["messages"][1]["content"]
        assert "1. " in user and "2. " in user and "那只老虎你觉得呢" in user
        event, = ad.turns.events
        assert event.text == "那只老虎你觉得呢"
        assert event.user_id == "2"

    @pytest.mark.asyncio
    async def test_a_no_stays_silent(self, llm):
        ad = make_adapter(FAST)
        await ad._on_message_event(group_event("你吃了吗", uid=1, message_id=1))
        await settle(ad)
        assert len(llm["calls"]) == 1 and ad.turns.events == []

    @pytest.mark.asyncio
    async def test_failures_stay_silent(self, llm):
        llm["answer"] = RuntimeError("502")
        ad = make_adapter(FAST)
        await ad._on_message_event(group_event("你觉得呢", uid=1, message_id=1))
        await settle(ad)
        assert ad.turns.events == []

    @pytest.mark.asyncio
    async def test_an_explicit_address_clears_what_was_waiting(self, llm):
        llm["answer"] = '{"to_him": true, "index": 1, "reason": "x"}'
        ad = make_adapter(dict(FAST, implicit_address_quiet_seconds=0.15, implicit_address_max_wait_seconds=1))
        await ad._on_message_event(group_event("老虎在吗", uid=1, message_id=1))
        await ad._on_message_event(group_event("格兰 出来", uid=1, message_id=2))
        await settle(ad, 0.4)
        assert llm["calls"] == []
        assert [e.text for e in ad.turns.events] == ["格兰 出来"]

    @pytest.mark.asyncio
    async def test_muted_rooms_are_not_watched(self, llm):
        ad = make_adapter(dict(FAST, group_replies_enabled=False))
        ad.router.group_replies_enabled = False
        await ad._on_message_event(group_event("你觉得呢", uid=1, message_id=1))
        await settle(ad)
        assert llm["calls"] == []

    @pytest.mark.asyncio
    async def test_only_judged_while_the_bot_is_in_the_conversation(self, llm):
        ad = make_adapter(dict(FAST, implicit_address_recent_self_minutes=10))
        await ad._on_message_event(group_event("那只老虎呢", uid=1, message_id=1))
        await settle(ad)
        assert llm["calls"] == []  # never spoke here: a cold call needs an @

        A.record_group_message(ad.instance_id, GROUP, "格兰", "我先去吃饭了", True)
        await ad._on_message_event(group_event("吃啥", uid=1, message_id=2))
        await settle(ad)
        assert len(llm["calls"]) == 1

        A._LAST_SELF_POST[A.speech_key(ad.instance_id, GROUP)] -= 11 * 60
        await ad._on_message_event(group_event("还在吗", uid=1, message_id=3))
        await settle(ad)
        assert len(llm["calls"]) == 1  # his last post is 11 minutes old

    def test_recent_self_window_setting(self):
        defaults = AD.resolve_settings({})
        assert defaults.recent_self_minutes == 10.0
        assert (defaults.quiet_seconds, defaults.max_wait_seconds) == (8.0, 20.0)
        assert AD.resolve_settings({"implicit_address_recent_self_minutes": 0}).recent_self_minutes == 0.0
        assert A._apply_yaml_config({}, {"implicit_address_recent_self_minutes": 5})[
            "implicit_address_recent_self_minutes"
        ] == 5

    @pytest.mark.asyncio
    async def test_the_bots_sends_mark_it_present(self):
        ad = make_adapter()
        assert A.last_self_post_at(ad.instance_id, GROUP) is None
        await ad.send(A.format_chat_id(True, GROUP), "来了")
        assert A.last_self_post_at(ad.instance_id, GROUP) is not None

    @pytest.mark.asyncio
    async def test_daily_ceiling(self, llm):
        ad = make_adapter(dict(FAST, implicit_address_daily_max_checks=1))
        await ad._on_message_event(group_event("一", uid=1, message_id=1))
        await settle(ad)
        await ad._on_message_event(group_event("二", uid=1, message_id=2))
        await settle(ad)
        assert len(llm["calls"]) == 1

    @pytest.mark.asyncio
    async def test_bot_posts_are_labelled_in_the_judges_transcript(self, llm):
        ad = make_adapter(FAST)
        A.record_group_message(ad.instance_id, GROUP, "格兰", "我明天去训练场", True)
        await ad._on_message_event(group_event("带我一个", uid=1, message_id=1))
        await settle(ad)
        user = llm["calls"][0]["messages"][1]["content"]
        assert f"{AD.SELF_LABEL}: 我明天去训练场" in user


# ---------------------------------------------------------------------------
# Stickers and pictures
# ---------------------------------------------------------------------------


def sticker_event(*, uid, nickname, message_id, sticker=True):
    event = group_event("", uid=uid, nickname=nickname, message_id=message_id)
    event.message = [
        P.ImageSegment(
            url=f"https://img/{message_id}",
            file=f"{message_id}.gif",
            sub_type=1 if sticker else 0,
            summary="[动画表情]" if sticker else "",
        )
    ]
    event.raw_message = f"[CQ:image,file={message_id}.gif,url=x]"
    return event


@pytest.fixture
def cached(monkeypatch) -> List[str]:
    urls: List[str] = []

    async def fake_cache(url, ext=".jpg"):
        urls.append(url)
        return f"/cache/{len(urls)}{ext}"

    monkeypatch.setattr(A, "cache_image_from_url", fake_cache)
    return urls


class TestStickers:
    def test_parsed_from_the_wire(self):
        seg, = P.parse_segments([{"type": "image", "data": {"url": "u", "sub_type": "1", "summary": "[动画表情]"}}])
        assert seg.is_sticker
        assert P.segments_to_media([seg])[0].sticker is True
        assert P._segment_to_wire(P.ImageSegment(url="u")) == {"type": "image", "data": {"url": "u"}}

    def test_room_transcript_says_sticker(self):
        assert P.humanize_cq("[CQ:image,file=a]看[CQ:image,file=b]", [True, False]) == "[表情包]看[图片]"
        assert P.humanize_cq("[CQ:image,file=a]") == "[图片]"

    @pytest.mark.asyncio
    async def test_stickers_never_ride_the_lookback(self, cached):
        ad = make_adapter({"reply_image_lookback_seconds": 60})
        await ad._on_message_event(sticker_event(uid=2, nickname="bob", message_id=1))
        await ad._on_message_event(group_event("格兰 你看", uid=1, message_id=2))
        assert ad.turns.events[0].media_urls == []
        assert cached == []
        buffered = A.recent_group_messages(ad.instance_id, GROUP)
        assert any(text == "[表情包]" for _, _, text, _ in buffered)

    @pytest.mark.asyncio
    async def test_a_sticker_in_the_turn_is_labelled_as_one(self, cached):
        ad = make_adapter()
        event = sticker_event(uid=1, nickname="alice", message_id=3)
        event.message.insert(0, P.TextSegment(text="格兰 "))
        event.raw_message = "格兰 " + event.raw_message
        await ad._on_message_event(event)
        turn = ad.turns.events[0]
        assert turn.media_urls == ["/cache/1.gif"]
        assert "第1张是alice 发的表情包" in turn.text
        assert "不是发图人本人的照片或头像" in turn.text

    def test_note_for_mixed_sources(self):
        note = TM.attachment_note(
            [
                TM.ImageLabel("alice", False, "posted"),
                TM.ImageLabel("bob", True, "recent"),
                TM.ImageLabel("carol", False, "quoted"),
            ]
        )
        assert "第1张是alice 发的图片" in note
        assert "第2张是bob 刚才在群里发的表情包" in note
        assert "第3张是carol 被引用的那条消息里的图片" in note
        assert TM.attachment_note([TM.ImageLabel("alice", False, "posted")]) == ""


class TestStillFrame:
    @pytest.mark.asyncio
    async def test_animated_gif_becomes_its_middle_frame(self, tmp_path):
        Image = pytest.importorskip("PIL.Image")
        frames = [Image.new("RGB", (8, 8), c) for c in ("white", "red", "blue")]
        path = tmp_path / "s.gif"
        frames[0].save(path, save_all=True, append_images=frames[1:])
        still = await A._still_frame(str(path))
        assert still.endswith("_still.png")
        with Image.open(still) as image:
            assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)

    @pytest.mark.asyncio
    async def test_still_images_are_left_alone(self, tmp_path):
        Image = pytest.importorskip("PIL.Image")
        path = tmp_path / "p.png"
        Image.new("RGB", (8, 8), "white").save(path)
        assert await A._still_frame(str(path)) == str(path)
        assert await A._still_frame(str(tmp_path / "missing.gif")) == str(tmp_path / "missing.gif")


def test_judge_clock_uses_configured_timezone_not_host(monkeypatch):
    """The judge reads the room on the bot's clock (Shanghai), not the US host's."""
    import hermes_time

    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Shanghai")
    hermes_time.reset_cache()
    try:
        # 2026-10-05 00:39:41 UTC == 08:39 in Shanghai, 17:39 in Los Angeles.
        assert AD._clock(1791160781.0) == "08:39"
    finally:
        hermes_time.reset_cache()
