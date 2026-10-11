"""Real OneBot routing/event construction with short, real asyncio timers."""
import asyncio
import time

import pytest

from gateway.platforms.base import MessageType
from plugins.platforms.onebot import adapter as A, protocol as P, reply_batch as B
from tests.gateway.test_onebot_reply_context import make_adapter, group_event, private_event


@pytest.fixture
def ad():
    A._reset_module_state()
    adapter = make_adapter({"reply_debounce_seconds": 0.08, "group_sessions_per_user": False})
    adapter.turns.reply = ""
    return adapter


async def drain(ad):
    async with asyncio.timeout(5):
        while ad._reply_batcher.tasks:
            await asyncio.gather(*list(ad._reply_batcher.tasks), return_exceptions=True)
            # gather() over already-finished tasks completes without yielding,
            # so the tasks' discard callbacks would never get to run.
            await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [group_event, private_event])
async def test_every_turn_waits_until_sender_is_quiet(ad, factory):
    seen = []
    async def receive(event):
        seen.append((time.monotonic(), event))
    ad.handle_message = receive
    await ad._on_message_event(factory("格兰，帮我看看", message_id=1))
    await asyncio.sleep(0.05)
    last = time.monotonic()
    await ad._on_message_event(factory("补充条件：不要删除文件", message_id=2))
    await asyncio.sleep(0.05)
    assert not seen
    await drain(ad)
    assert len(seen) == 1
    when, event = seen[0]
    assert when >= last + 0.08
    assert event.text == "格兰，帮我看看\n补充条件：不要删除文件"
    assert event.message_id == event.metadata[A.REPLY_ANCHOR_META] == "2"
    assert event.metadata["onebot_burst_message_ids"] == ["1", "2"]
    assert "补充条件" not in (event.channel_context or "")
    assert not ad._reply_batcher.pending and not ad._reply_batcher.locks
    await ad._on_message_event(factory("格兰，再来一次", message_id=3))
    assert len(seen) == 1
    await drain(ad)
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_senders_and_groups_and_dms_never_share_a_burst(ad):
    await ad._on_message_event(group_event("格兰 A", uid=1, message_id=1))
    await ad._on_message_event(group_event("格兰 B", uid=2, message_id=2))
    await ad._on_message_event(private_event("DM A", uid=1, message_id=3))
    await ad._on_message_event(group_event("A续", uid=1, message_id=4))
    await ad._on_message_event(group_event("无关群", uid=1, gid=999, message_id=5))
    await drain(ad)
    events = ad.turns.events
    assert len(events) == 3
    assert {(e.source.chat_type, e.user_id, e.text) for e in events} == {
        ("group", "1", "格兰 A\nA续"), ("group", "2", "格兰 B"), ("dm", "1", "DM A"),
    }


@pytest.mark.asyncio
async def test_media_and_text_arriving_during_download_are_retained(ad, monkeypatch):
    downloading, release = asyncio.Event(), asyncio.Event()
    async def media(refs):
        if any(ref.kind == "image" for ref in refs):
            downloading.set()
            await release.wait()
            return ["/tmp/image.png"], ["image/png"]
        return [], []
    monkeypatch.setattr(ad, "_download_refs", media)
    first = private_event("看图", message_id=1)
    first.message.append(P.ImageSegment(url="https://img/x", file="image.png"))
    await ad._on_message_event(first)
    await asyncio.wait_for(downloading.wait(), timeout=1)
    await ad._on_message_event(private_event("还有这个条件", message_id=2))
    release.set()
    await drain(ad)
    event, = ad.turns.events
    assert "看图" in event.text and "还有这个条件" in event.text
    assert event.media_urls == ["/tmp/image.png"]
    assert event.media_types == ["image/png"]
    assert event.message_type == MessageType.PHOTO
    assert event.source.message_id == event.message_id == "2"


@pytest.mark.asyncio
async def test_shared_busy_session_keeps_each_senders_full_context(ad):
    release = asyncio.Event()
    async def receive(event):
        ad.turns.events.append(event)
        if len(ad.turns.events) == 1:
            key = ad._reply_batcher._session_key(event)
            ad._session_tasks[key] = asyncio.create_task(release.wait())
    ad.handle_message = receive
    await ad._on_message_event(group_event("格兰 A", uid=1, message_id=1))
    await asyncio.sleep(0.12)
    await ad._on_message_event(group_event("格兰 B", uid=2, message_id=2))
    await asyncio.sleep(0.12)
    await ad._on_message_event(group_event("B继续补充", uid=2, message_id=3))
    await ad._on_message_event(group_event("格兰 C", uid=3, message_id=4))
    assert len(ad.turns.events) == 1
    release.set()
    await drain(ad)
    assert [(e.user_id, e.text) for e in ad.turns.events] == [
        ("1", "格兰 A"), ("2", "格兰 B\nB继续补充"), ("3", "格兰 C"),
    ]


@pytest.mark.asyncio
async def test_continuations_still_obey_live_channel_gate(ad):
    await ad._on_message_event(group_event("格兰开始", message_id=1))
    ad.router.group_whitelist = frozenset()
    await ad._on_message_event(group_event("不应被带入", message_id=2))
    await drain(ad)
    assert "不应被带入" not in ad.turns.events[0].text


@pytest.mark.asyncio
async def test_stop_is_immediate_and_cancels_unsent_burst(ad):
    await ad._on_message_event(private_event("还没说完", message_id=1))
    await ad._on_message_event(private_event("/stop", message_id=2))
    await asyncio.sleep(0)
    assert [e.text for e in ad.turns.events] == ["/stop"]
    await drain(ad)
    assert len(ad.turns.events) == 1


@pytest.mark.asyncio
async def test_disconnect_cleans_timers_without_sending(ad):
    await ad._on_message_event(private_event("还没说完", message_id=1))
    await ad.disconnect()
    assert not ad.turns.events
    assert not ad._reply_batcher.pending
    assert not ad._reply_batcher.tasks


@pytest.mark.parametrize("value", [None, "bad", -1, 0, float("nan"), float("inf")])
def test_invalid_or_disabled_settings_use_immediate_path(value):
    assert B.quiet_seconds({"reply_debounce_seconds": value}) == 0


def test_yaml_three_second_setting_reaches_real_adapter():
    result = A._apply_yaml_config({}, {"reply_debounce_seconds": 3})
    assert B.quiet_seconds(result) == 3


@pytest.mark.asyncio
async def test_configured_three_seconds_is_real_elapsed_silence(ad):
    ad._extra["reply_debounce_seconds"] = 3
    ad.config.extra["reply_debounce_seconds"] = 3
    start = time.monotonic()
    await ad._on_message_event(private_event("你好", message_id=1))
    await asyncio.sleep(0.1)
    assert not ad.turns.events
    await drain(ad)
    assert time.monotonic() - start >= 3
    assert len(ad.turns.events) == 1


@pytest.mark.asyncio
async def test_real_base_handler_preserves_shared_sender_attribution(ad):
    first_started, release = asyncio.Event(), asyncio.Event()
    events = []
    async def handler(event):
        events.append(event)
        if len(events) == 1:
            first_started.set()
            await release.wait()
        return None
    del ad.handle_message  # Exercise the real BasePlatformAdapter path.
    ad.set_message_handler(handler)
    await ad._on_message_event(group_event("格兰 A", uid=1, message_id=1))
    await asyncio.wait_for(first_started.wait(), 1)
    await ad._on_message_event(group_event("格兰 B", uid=2, message_id=2))
    await ad._on_message_event(group_event("补充", uid=2, message_id=3))
    await asyncio.sleep(0.12)
    assert len(events) == 1
    release.set()
    await drain(ad)
    await asyncio.gather(*list(ad._session_tasks.values()))
    assert [(e.user_id, e.text) for e in events] == [("1", "格兰 A"), ("2", "格兰 B\n补充")]
    assert not ad._pending_messages


@pytest.mark.asyncio
async def test_clarify_reply_reaches_active_agent_without_deadlock(ad):
    from tools import clarify_gateway as clarify
    event = await ad._build_message_event(private_event("待回答"), "待回答")
    key = ad._reply_batcher._session_key(event)
    release = asyncio.Event()
    active = ad._session_tasks[key] = asyncio.create_task(release.wait())
    clarify.register("onebot-burst-clarify", key, "选择？", ["甲", "乙"])
    try:
        await ad._on_message_event(private_event("乙", message_id=2))
        await drain(ad)
        assert [e.text for e in ad.turns.events] == ["乙"]
        assert not active.done()
    finally:
        clarify.resolve_gateway_clarify("onebot-burst-clarify", "乙")
        release.set()
        await active


# ── fixed window, room scope, context images ────────────────────────────


@pytest.fixture
def room(monkeypatch):
    A._reset_module_state()
    adapter = make_adapter({
        "reply_debounce_seconds": 0.1,
        "reply_debounce_mode": "fixed",
        "reply_debounce_scope": "chat",
        "reply_image_lookback_seconds": 60,
        "group_sessions_per_user": False,
    })
    adapter.turns.reply = ""
    cached = []

    async def fake_cache(url, ext=".jpg"):
        cached.append(url)
        return f"/cache/{len(cached)}{ext}"

    monkeypatch.setattr(A, "cache_image_from_url", fake_cache)
    adapter.cached = cached
    return adapter


def image_event(url, *, uid, nickname, message_id, gid=None):
    event = group_event("", uid=uid, nickname=nickname, message_id=message_id, **({"gid": gid} if gid else {}))
    event.message = [P.ImageSegment(url=url, file=f"{message_id}.png")]
    event.raw_message = f"[CQ:image,file={message_id}.png]"
    return event


@pytest.mark.asyncio
async def test_fixed_window_does_not_slide_and_later_lines_start_a_new_turn(room):
    start = time.monotonic()
    await room._on_message_event(group_event("格兰 先听我说", uid=1, message_id=1))
    for i in range(2, 5):
        await asyncio.sleep(0.04)
        await room._on_message_event(group_event(f"补充{i}", uid=1, message_id=i))
    await drain(room)
    first, = room.turns.events
    # Messages kept arriving past 0.1s; a sliding window would still be open.
    assert time.monotonic() - start < 0.1 + 0.04 * 3
    assert "格兰 先听我说" in first.text and "补充2" in first.text
    assert "补充4" not in first.text
    assert not room._reply_batcher.pending


@pytest.mark.asyncio
async def test_chat_scope_collects_the_room_with_names_and_one_speech_unit(room):
    room.group_window_max = 1
    await room._on_message_event(group_event("格兰 你们觉得呢", uid=1, nickname="alice", message_id=1))
    await room._on_message_event(group_event("我觉得行", uid=2, nickname="bob", message_id=2))
    await room._on_message_event(group_event("我也是", uid=3, nickname="carol", message_id=3))
    await drain(room)
    event, = room.turns.events
    assert event.user_id == "1"
    assert event.text == "格兰 你们觉得呢\nbob：我觉得行\ncarol：我也是"
    assert event.metadata["onebot_burst_message_ids"] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_unaddressed_room_image_rides_with_the_next_question_once(room):
    await room._on_message_event(image_event("https://img/cake", uid=2, nickname="bob", message_id=1))
    await room._on_message_event(group_event("格兰你咋看待这个蛋糕", uid=1, message_id=2))
    await drain(room)
    event, = room.turns.events
    assert event.media_urls == ["/cache/1.png"]
    assert event.message_type == MessageType.PHOTO
    assert "第1张是bob 刚才在群里发的图片" in event.text
    await room._on_message_event(group_event("格兰 再看一眼", uid=1, message_id=3))
    await drain(room)
    assert room.turns.events[-1].media_urls == []
    assert room.cached == ["https://img/cake"]


@pytest.mark.asyncio
async def test_image_inside_the_window_is_not_attached_twice(room):
    await room._on_message_event(group_event("格兰 看图", uid=1, message_id=1))
    await room._on_message_event(image_event("https://img/a", uid=1, nickname="alice", message_id=2))
    await drain(room)
    event, = room.turns.events
    assert event.media_urls == ["/cache/1.png"]
    assert "附图说明" not in event.text


@pytest.mark.asyncio
async def test_quoted_message_supplies_text_and_images(room):
    class QuoteClient(type(room._client)):
        async def call_action(self, action, *, timeout=None):
            self.actions.append(action)
            if isinstance(action, P.RawAction) and action.action == "get_msg":
                return {"status": "ok", "retcode": 0, "data": {
                    "message_id": 77,
                    "sender": {"user_id": 9, "nickname": "dave"},
                    "message": [
                        {"type": "text", "data": {"text": "这是我做的"}},
                        {"type": "image", "data": {"url": "https://img/q", "file": "q.jpg"}},
                    ],
                }}
            return await super().call_action(action, timeout=timeout)

    room._client = QuoteClient()
    trigger = group_event("格兰 评价一下", uid=1, message_id=5)
    trigger.message.insert(0, P.ReplySegment(id="77"))
    await room._on_message_event(trigger)
    await drain(room)
    event, = room.turns.events
    assert event.reply_to_message_id == "77"
    assert event.reply_to_text == "这是我做的"
    assert event.reply_to_author_name == "dave"
    assert event.media_urls == ["/cache/1.jpg"]
    assert room.cached == ["https://img/q"]


def test_burst_session_key_follows_the_gateway_setting(ad):
    class Store:
        class config:
            group_sessions_per_user = True
            thread_sessions_per_user = False

        @staticmethod
        def _resolve_profile_for_key(source):
            return None

    async def build():
        return await ad._build_message_event(group_event("格兰", uid=7), "格兰")

    event = asyncio.run(build())
    shared = ad._reply_batcher._session_key(event)
    ad._session_store = Store()
    per_user = ad._reply_batcher._session_key(event)
    assert shared != per_user and per_user.endswith("7")


@pytest.mark.parametrize(("extra", "expected"), [
    ({}, ("quiet", "sender")),
    ({"reply_debounce_mode": "FIXED", "reply_debounce_scope": "chat"}, ("fixed", "chat")),
    ({"reply_debounce_mode": "bogus", "reply_debounce_scope": 3}, ("quiet", "sender")),
])
def test_window_settings_parse_defensively(extra, expected):
    assert (B.window_mode(extra), B.window_scope(extra)) == expected


def test_window_keys_survive_yaml_loading():
    result = A._apply_yaml_config({}, {
        "reply_debounce_mode": "fixed",
        "reply_debounce_scope": "chat",
        "reply_image_lookback_seconds": 60,
        "reply_image_lookback_max": 2,
    })
    assert result["reply_debounce_mode"] == "fixed"
    assert result["reply_image_lookback_max"] == 2


@pytest.mark.asyncio
async def test_lookback_is_measured_on_qq_time_from_the_trigger(room):
    room.config.extra["reply_image_lookback_seconds"] = 120
    stale = image_event("https://img/stale", uid=3, nickname="carol", message_id=1)
    stale.time = 1_700_000_000
    recent = image_event("https://img/recent", uid=2, nickname="bob", message_id=2)
    recent.time = stale.time + 137
    question = group_event("格兰这个好帅", uid=1, message_id=3)
    question.time = recent.time + 63  # the picture came a minute before the question
    for event in (stale, recent, question):
        await room._on_message_event(event)
    await drain(room)
    # All three arrived within milliseconds here; only QQ's clock tells them apart.
    assert room.cached == ["https://img/recent"]
    assert room.turns.events[0].media_urls == ["/cache/1.png"]


@pytest.mark.asyncio
async def test_room_transcript_never_shows_media_links(room):
    picture = image_event("https://img/x", uid=2, nickname="bob", message_id=1)
    picture.raw_message = "[CQ:image,file=a.png,url=https://multimedia.nt.qq.com.cn/download?appid=1407&amp;fileid=X&amp;rkey=Y]"
    await room._on_message_event(picture)
    await room._on_message_event(group_event("格兰 看", uid=1, message_id=2))
    await drain(room)
    rendered = room.turns.events[0].channel_context or ""
    assert "multimedia" not in rendered and "[图片]" in rendered


def test_humanize_cq_placeholders():
    raw = "[CQ:reply,id=5][CQ:at,qq=42] 看[CQ:image,file=a.png,url=https://x?a=1&amp;b=2][CQ:face,id=1][CQ:poke,qq=1]"
    assert P.humanize_cq(raw) == "@42  看[图片][表情][poke]"
