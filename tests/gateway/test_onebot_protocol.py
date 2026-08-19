"""Wire-protocol tests for the OneBot v11 (QQ / NapCat) platform plugin.

Pure functions only — no sockets, no gateway. These lock the decode/encode
contract the rest of the adapter is built on, including the two robustness
properties that exist because they were once real outages:

* a malformed numeric field must not raise (it would unwind the reader loop
  and drop the WebSocket → reconnect churn + message loss);
* merged-forward nodes must carry BOTH backend key dialects.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from plugins.platforms.onebot import protocol as P


# ---------------------------------------------------------------------------
# parse_event
# ---------------------------------------------------------------------------

class TestParseEvent:

    def test_group_message_event(self):
        raw: Dict[str, Any] = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "time": 1_700_000_000,
            "self_id": 100,
            "user_id": 200,
            "group_id": 300,
            "message_id": 1,
            "message": [
                {"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": "hello"}},
            ],
            "raw_message": "[CQ:at,qq=100] hello",
            "sender": {"user_id": 200, "nickname": "alice", "card": "Alice in QQ"},
        }
        ev = P.parse_event(raw)
        assert isinstance(ev, P.MessageEvent)
        assert ev.message_type == P.MessageType.GROUP
        assert ev.group_id == 300
        assert len(ev.message) == 2
        assert ev.sender is not None
        # The group card wins over the nickname for display.
        assert ev.sender.display_name() == "Alice in QQ"
        assert P.is_mentioned(ev.message, 100)

    def test_heartbeat_decodes_as_meta_event(self):
        ev = P.parse_event({
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "time": 1_700_000_000,
            "self_id": 100,
            "status": {},
        })
        assert isinstance(ev, P.MetaEvent)
        assert ev.meta_event_type == "heartbeat"

    def test_notice_and_request_events_decode(self):
        notice = P.parse_event({
            "post_type": "notice", "notice_type": "group_increase",
            "self_id": 1, "time": 2, "group_id": 3, "user_id": 4,
        })
        assert isinstance(notice, P.NoticeEvent) and notice.notice_type == "group_increase"
        req = P.parse_event({
            "post_type": "request", "request_type": "friend",
            "self_id": 1, "time": 2, "flag": "abc",
        })
        assert isinstance(req, P.RequestEvent) and req.flag == "abc"

    def test_unknown_post_type_maps_to_unknown_event(self):
        ev = P.parse_event({"post_type": "mystery", "time": 0, "self_id": 0})
        assert isinstance(ev, P.UnknownEvent)
        assert ev.raw["post_type"] == "mystery"

    def test_unknown_message_type_maps_to_unknown_event(self):
        ev = P.parse_event({
            "post_type": "message", "message_type": "guild",
            "self_id": 1, "user_id": 1, "message_id": 1, "message": [], "time": 0,
        })
        assert isinstance(ev, P.UnknownEvent)

    def test_cq_string_message_format_degrades_to_text(self):
        """A backend configured for CQ-string output still routes.

        ``messagePostFormat: "string"`` ships ``message`` as a CQ string
        instead of a segment array. Media/mention extraction degrades, but
        the text must survive so keyword routing keeps working.
        """
        ev = P.parse_event({
            "post_type": "message", "message_type": "private",
            "self_id": 1, "user_id": 2, "message_id": 3,
            "message": "[CQ:at,qq=1] 格兰在吗", "time": 0,
        })
        assert isinstance(ev, P.MessageEvent)
        assert "格兰在吗" in P.segments_to_text(ev.message)


class TestMalformedFieldsNeverRaise:
    """One bad frame must never take down the connection."""

    def test_non_numeric_self_id_does_not_raise(self):
        ev = P.parse_event({
            "post_type": "message", "message_type": "private",
            "self_id": "not-a-number", "user_id": 12345, "message_id": 7,
            "time": 1_700_000_000,
            "message": [{"type": "text", "data": {"text": "hi"}}],
            "raw_message": "hi",
        })
        assert isinstance(ev, P.MessageEvent)
        assert ev.self_id == 0
        assert ev.user_id == 12345

    def test_non_numeric_time_and_message_id_do_not_raise(self):
        ev = P.parse_event({
            "post_type": "message", "message_type": "group",
            "self_id": 100, "group_id": 9999, "user_id": "weird",
            "message_id": "abc", "time": "later",
            "message": [{"type": "text", "data": {"text": "yo"}}],
        })
        assert isinstance(ev, P.MessageEvent)
        assert (ev.self_id, ev.user_id, ev.message_id, ev.time) == (100, 0, 0, 0)

    def test_non_numeric_notice_self_id_does_not_raise(self):
        ev = P.parse_event({
            "post_type": "notice", "notice_type": "group_increase",
            "self_id": "bad", "time": "bad",
        })
        assert isinstance(ev, P.NoticeEvent)

    def test_bool_numeric_field_is_not_treated_as_int(self):
        # bool is an int subclass; True must not become self_id=1.
        ev = P.parse_event({
            "post_type": "message", "message_type": "private",
            "self_id": True, "user_id": 5, "message_id": 1, "message": [], "time": 0,
        })
        assert isinstance(ev, P.MessageEvent) and ev.self_id == 0

    def test_non_dict_segment_data_collapses_to_other(self):
        ev = P.parse_event({
            "post_type": "message", "message_type": "private",
            "self_id": 1, "user_id": 1, "message_id": 1,
            "message": [{"type": "text", "data": "oops"}], "time": 0,
        })
        assert isinstance(ev, P.MessageEvent)
        assert isinstance(ev.message[0], P.OtherSegment)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

class TestSegments:

    @pytest.mark.parametrize(("payload", "expected_cls"), [
        ({"type": "text", "data": {"text": "hi"}}, P.TextSegment),
        ({"type": "at", "data": {"qq": "1"}}, P.AtSegment),
        ({"type": "image", "data": {"url": "https://x", "file": "f"}}, P.ImageSegment),
        ({"type": "reply", "data": {"id": "42"}}, P.ReplySegment),
        ({"type": "face", "data": {"id": "1"}}, P.FaceSegment),
        ({"type": "record", "data": {"url": "https://y"}}, P.RecordSegment),
        ({"type": "video", "data": {"url": "https://v", "file": "v.mp4"}}, P.VideoSegment),
        ({"type": "file", "data": {"url": "https://f", "file": "doc.pdf"}}, P.FileSegment),
        ({"type": "forward", "data": {"id": "9"}}, P.ForwardSegment),
    ])
    def test_known_segment_types(self, payload, expected_cls):
        ev = P.parse_event({
            "post_type": "message", "message_type": "private",
            "self_id": 1, "user_id": 1, "message_id": 1,
            "message": [payload], "time": 0,
        })
        assert isinstance(ev, P.MessageEvent)
        assert isinstance(ev.message[0], expected_cls)

    def test_unknown_segment_collapses_to_other(self):
        ev = P.parse_event({
            "post_type": "message", "message_type": "private",
            "self_id": 1, "user_id": 1, "message_id": 1,
            "message": [{"type": "poke", "data": {"id": "x"}}], "time": 0,
        })
        assert isinstance(ev, P.MessageEvent)
        assert isinstance(ev.message[0], P.OtherSegment)
        assert ev.message[0].raw["type"] == "poke"


class TestSegmentHelpers:

    def test_text_extraction_flattens_segments(self):
        segs = [P.AtSegment(qq="100"), P.TextSegment(text="hello "),
                P.TextSegment(text="world"), P.FaceSegment(id="1")]
        text = P.segments_to_text(segs)
        assert "hello world" in text
        # The address stays visible so keyword routing can see it.
        assert "@100" in text

    def test_strip_self_mention_drops_only_the_bot(self):
        segs = [P.AtSegment(qq="100"), P.TextSegment(text=" hi "),
                P.AtSegment(qq="555"), P.TextSegment(text="look")]
        out = P.strip_self_mention(segs, 100)
        assert "@100" not in out
        assert "@555" in out   # other people's mentions are content
        assert out.startswith("hi")

    def test_media_covers_image_and_record(self):
        segs = [P.TextSegment(text="caption"),
                P.ImageSegment(url="https://cdn/img.jpg", file="img.jpg"),
                P.RecordSegment(url="https://cdn/voice.amr"),
                P.AtSegment(qq="100"), P.ReplySegment(id="42")]
        media = P.segments_to_media(segs)
        assert [m.kind for m in media] == ["image", "audio"]
        assert media[0].url == "https://cdn/img.jpg"
        assert media[0].file_name == "img.jpg"

    def test_media_covers_video_and_file(self):
        segs = [P.VideoSegment(url="https://cdn/clip.mp4", file="clip.mp4"),
                P.FileSegment(url="https://cdn/report.pdf", file="report.pdf")]
        media = P.segments_to_media(segs)
        assert [m.kind for m in media] == ["video", "document"]
        assert media[1].mime == "application/octet-stream"

    def test_media_skips_empty_urls(self):
        # gocq ships an empty url for offline media; NapCat ships name-only
        # file segments. Neither can be downloaded.
        segs = [P.ImageSegment(url="", file="x.jpg"),
                P.VideoSegment(url="", file="x.mp4"),
                P.FileSegment(url="", file="x.pdf")]
        assert P.segments_to_media(segs) == []

    def test_media_empty_for_text_only(self):
        assert P.segments_to_media([P.TextSegment(text="hi"), P.AtSegment(qq="1")]) == []

    def test_is_mentioned_handles_at_all(self):
        assert P.is_mentioned([P.AtSegment(qq="all")], 12345)

    def test_is_mentioned_false_when_unmentioned(self):
        assert not P.is_mentioned([P.TextSegment(text="hi there")], 100)
        assert not P.is_mentioned([P.AtSegment(qq="999")], 100)

    def test_reply_target(self):
        assert P.reply_target([P.ReplySegment(id="77"), P.TextSegment(text="x")]) == "77"
        assert P.reply_target([P.TextSegment(text="x")]) is None


# ---------------------------------------------------------------------------
# action_to_wire
# ---------------------------------------------------------------------------

class TestActionToWire:

    def test_send_group_msg_envelope(self):
        s = P.action_to_wire(P.SendGroupMsg(
            group_id=1, message=[P.ReplySegment(id="42"), P.TextSegment(text="hello")]))
        assert s["action"] == "send_group_msg"
        assert s["params"]["group_id"] == 1
        assert s["params"]["message"][0] == {"type": "reply", "data": {"id": "42"}}
        assert s["params"]["message"][1]["data"]["text"] == "hello"

    def test_send_private_msg_envelope(self):
        s = P.action_to_wire(P.SendPrivateMsg(user_id=7, message=[P.TextSegment(text="yo")]))
        assert s["action"] == "send_private_msg"
        assert s["params"]["user_id"] == 7

    def test_set_input_status_envelope(self):
        s = P.action_to_wire(P.SetInputStatus(user_id=9876, event_type=1))
        assert s["action"] == "set_input_status"
        assert s["params"] == {"user_id": 9876, "event_type": 1}

    def test_upload_private_file_envelope(self):
        s = P.action_to_wire(P.UploadPrivateFile(user_id=42, file="/tmp/a.html", name="a.html"))
        assert s["action"] == "upload_private_file"
        assert s["params"]["file"] == "/tmp/a.html"
        assert s["params"]["name"] == "a.html"

    def test_upload_group_file_envelope_omits_unset_folder(self):
        s = P.action_to_wire(P.UploadGroupFile(group_id=10, file="/tmp/x.pdf", name="x.pdf"))
        assert s["action"] == "upload_group_file"
        assert "folder" not in s["params"]

    def test_image_segment_serializes_inline_url_and_file(self):
        s = P.action_to_wire(P.SendGroupMsg(group_id=7, message=[
            P.TextSegment(text="here"),
            P.ImageSegment(url="https://cdn/pic.png", file="pic.png")]))
        img = s["params"]["message"][1]
        assert img["type"] == "image"
        assert img["data"] == {"url": "https://cdn/pic.png", "file": "pic.png"}

    def test_image_segment_serializes_file_without_url(self):
        s = P.action_to_wire(P.SendPrivateMsg(
            user_id=8, message=[P.ImageSegment(file="base64://ZmFrZQ==")]))
        img = s["params"]["message"][0]
        assert "url" not in img["data"]
        assert img["data"]["file"] == "base64://ZmFrZQ=="

    def test_record_segment_serializes_file_without_url(self):
        s = P.action_to_wire(P.SendPrivateMsg(
            user_id=8, message=[P.RecordSegment(file="base64://ZmFrZQ==")]))
        rec = s["params"]["message"][0]
        assert rec["type"] == "record"
        assert "url" not in rec["data"]

    def test_video_and_file_segments_serialize(self):
        s = P.action_to_wire(P.SendPrivateMsg(user_id=3, message=[
            P.VideoSegment(url="https://cdn/clip.mp4", file="clip.mp4"),
            P.FileSegment(url="https://cdn/doc.pdf")]))
        vid, fil = s["params"]["message"]
        assert vid["data"]["file"] == "clip.mp4"
        assert "file" not in fil["data"]

    def test_other_segment_round_trips_raw(self):
        raw = {"type": "poke", "data": {"id": "x"}}
        s = P.action_to_wire(P.SendPrivateMsg(user_id=1, message=[P.OtherSegment(raw=raw)]))
        assert s["params"]["message"][0] == raw

    def test_group_forward_msg_nodes_carry_both_key_dialects(self):
        """One wire shape must satisfy go-cqhttp AND NapCat.

        go-cqhttp reads ``name``/``uin``; NapCat reads ``nickname``/``user_id``.
        Emitting both means no backend sniffing.
        """
        s = P.action_to_wire(P.SendGroupForwardMsg(group_id=77, messages=[
            P.ForwardNode(name="bot", uin="10086", content=[P.TextSegment(text="part 1")])]))
        assert s["action"] == "send_group_forward_msg"
        node = s["params"]["messages"][0]
        assert node["type"] == "node"
        assert node["data"]["name"] == "bot" and node["data"]["uin"] == "10086"
        assert node["data"]["nickname"] == "bot" and node["data"]["user_id"] == "10086"
        assert node["data"]["content"][0]["data"]["text"] == "part 1"

    def test_send_private_forward_msg_envelope(self):
        s = P.action_to_wire(P.SendPrivateForwardMsg(user_id=555, messages=[
            P.ForwardNode(name="bot", uin="10086", content=[P.TextSegment(text="folded")])]))
        assert s["action"] == "send_private_forward_msg"
        assert s["params"]["user_id"] == 555

    def test_raw_action_is_the_escape_hatch_for_unmodelled_actions(self):
        """The Qzone tool family reaches the backend through RawAction."""
        s = P.action_to_wire(P.RawAction("get_cookies", {"domain": "user.qzone.qq.com"}))
        assert s == {"action": "get_cookies",
                     "params": {"domain": "user.qzone.qq.com"}}

    def test_raw_action_defaults_to_empty_params(self):
        assert P.action_to_wire(P.RawAction("get_login_info")) == {
            "action": "get_login_info", "params": {}}
