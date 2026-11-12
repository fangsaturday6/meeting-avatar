from avatar.textutil import (
    ReplyStream, SentenceSplitter, clean_for_speech, echo_score, fix_name, parse_reply, similar,
)


def test_splitter_cuts_at_full_stop_and_keeps_tail():
    sp = SentenceSplitter()
    assert sp.feed("你好，我是小克。今天") == ["你好，我是小克。"]
    assert sp.flush() == ["今天"]


def test_splitter_first_segment_cuts_at_comma_for_speed():
    sp = SentenceSplitter()
    out = sp.feed("嗯，这个问题我觉得可以这样看，先做登录")
    assert out == ["嗯，这个问题我觉得可以这样看，"]
    assert len(out[0]) >= 4
    # 第一段之后，短逗号不再切
    assert sp.feed("比较稳，然后再做支付。") == ["先做登录比较稳，然后再做支付。"]


def test_splitter_english_period_and_decimal():
    sp = SentenceSplitter()
    assert sp.feed("版本是 3.5 左右") == []
    assert sp.feed("。OK. Next") == ["版本是 3.5 左右。", "OK."]


def test_splitter_stream_char_by_char():
    sp = SentenceSplitter()
    got = []
    for ch in "好的！我查一下。":
        got += sp.feed(ch)
    assert got == ["好的！", "我查一下。"]


def test_splitter_drops_punctuation_only():
    sp = SentenceSplitter()
    assert sp.feed("对。。。好。") == ["对。。。", "好。"]


def test_parse_reply_silent():
    assert parse_reply("[静]").silent
    assert parse_reply("  [静] ").silent
    assert parse_reply("").silent


def test_parse_reply_with_dispatch():
    r = parse_reply("好的我查一下。[[派工 查资料:飞书会议机器人接口]]")
    assert not r.silent
    assert r.speech == "好的我查一下。"
    assert r.dispatch == ("查资料", "飞书会议机器人接口")


def test_reply_stream_silent_emits_nothing():
    rs = ReplyStream()
    out = []
    for ch in "[静]":
        out += rs.feed(ch)
    rest, reply = rs.finish()
    assert out == [] and rest == [] and reply.silent


def test_reply_stream_hides_dispatch_from_speech():
    rs = ReplyStream()
    out = []
    for ch in "我去查一下代码。[[派工 查代码：登录接口在哪里实现]]":
        out += rs.feed(ch)
    rest, reply = rs.finish()
    assert out + rest == ["我去查一下代码。"]
    assert reply.dispatch == ("查代码", "登录接口在哪里实现")


def test_reply_stream_tail_without_punctuation():
    rs = ReplyStream()
    out = rs.feed("先做登录吧。支付往后放")
    rest, reply = rs.finish()
    assert out + rest == ["先做登录吧。", "支付往后放"]
    assert reply.speech == "先做登录吧。支付往后放"


def test_clean_for_speech():
    assert clean_for_speech("**登录**是基础 👍（笑）") == "登录是基础"


def test_similar_and_echo():
    assert similar("我觉得先做登录", "我觉得先做登陆") > 0.8
    assert echo_score("先做登录吧", "我觉得还是先做登录吧，支付依赖它") > 0.9
    assert echo_score("那排期怎么定", "我觉得还是先做登录吧，支付依赖它") < 0.4


def test_fix_name_corrects_homophones_only():
    assert fix_name("小课，你咋不说话", "小克") == "小克，你咋不说话"
    assert fix_name("他们还有青年小柯。", "小克") == "他们还有青年小克。"
    assert fix_name("小克你好", "小克") == "小克你好"
    assert fix_name("我们上课吧", "小克") == "我们上课吧"
    assert fix_name("OK", "小克") == "OK"


def test_backchannel_is_only_short_agreement():
    from avatar.textutil import is_backchannel
    for yes in ["嗯", "嗯嗯。", "对对对", "好的", "是的，", "OK", "嗯，你继续", "然后呢？"]:
        assert is_backchannel(yes), yes
    for no in ["不对", "好了好了", "等一下", "先做支付", "嗯，但是排期呢", "小克", ""]:
        assert not is_backchannel(no), no


def test_reply_can_carry_an_engagement_change():
    from avatar.textutil import ReplyStream, parse_reply
    r = parse_reply("行，我少说。\n[[参与 少说]]")
    assert r.speech == "行，我少说。" and r.engagement == "quiet" and r.dispatch is None
    assert parse_reply("好。").engagement is None
    rs = ReplyStream()
    out = []
    for ch in "好，我多说点。\n[[参与 多说]]":
        out += rs.feed(ch)
    rest, reply = rs.finish()
    assert "".join(out + rest) == "好，我多说点。" and reply.engagement == "active"


def test_reply_can_name_speakers():
    from avatar.textutil import parse_reply
    r = parse_reply("[静]\n[[称呼 说话人2=张三]]\n[[称呼：说话人3 ＝ 李四]]")
    assert r.silent and r.speech == "" and r.names == (("说话人2", "张三"), ("说话人3", "李四"))
    r = parse_reply("你好王五。\n[[称呼 说话人1=王五]]")
    assert not r.silent and r.speech == "你好王五。" and r.names == (("说话人1", "王五"),)
    assert parse_reply("好。").names == ()


def test_marker_before_the_words_does_not_swallow_them():
    from avatar.textutil import ReplyStream
    rs, out = ReplyStream(), []
    for ch in "[[称呼 说话人2=王五]]\n欢迎王五，我们开始吧。\n[[参与 适中]]":
        out += rs.feed(ch)
    rest, reply = rs.finish()
    assert "".join(out + rest) == "欢迎王五，我们开始吧。"
    assert reply.names == (("说话人2", "王五"),) and reply.engagement == "normal" and not reply.silent
    rs, out = ReplyStream(), []
    for ch in "[[称呼 说话人2=王五]]\n[静]":
        out += rs.feed(ch)
    rest, reply = rs.finish()
    assert out + rest == [] and reply.silent and reply.names == (("说话人2", "王五"),)


def test_speaker_prefix_copied_from_the_transcript_is_not_spoken():
    from avatar.textutil import clean_for_speech
    assert clean_for_speech("[小克] 在，你说。") == "在，你说。"
    assert clean_for_speech("[说话人1] 好。") == "好。"
    assert clean_for_speech("用 [0, 1] 这个区间。") == "用 [0, 1] 这个区间。"
