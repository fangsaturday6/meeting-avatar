"""散会后重听：同一个人接连说的碎句拼起来再认一遍。"""
from avatar.meeting import Line, Meeting
from avatar.config import Settings
from avatar.rehear import better, pieces


def heard(id, at, seconds, speaker=1, text="", who="human"):
    return Line(id=id, t=0.0, who=who, text=text or f"第{id}句", speaker_id=speaker, at=at, seconds=seconds)


def test_one_persons_back_to_back_lines_are_joined():
    lines = [heard(1, 0.0, 2.0), heard(2, 2.8, 1.5), heard(3, 4.5, 3.0),    # 同一个人，停顿都不到 1.5 秒
             heard(4, 8.0, 2.0, speaker=2), heard(5, 10.2, 1.0, speaker=2)]
    ps = pieces(lines, {i: "mic" for i in range(1, 6)})
    assert [(p.line_ids, p.start, p.end, p.live) for p in ps] == [
        ([1, 2, 3], 0.0, 7.5, "第1句第2句第3句"), ([4, 5], 8.0, 11.2, "第4句第5句")]


def test_what_breaks_a_piece():
    lines = [
        heard(1, 0.0, 2.0), heard(2, 4.0, 2.0),                 # 停了两秒：不算接着说
        Line(id=3, t=0, who="bot", text="好的"), heard(4, 6.5, 2.0),     # 中间它说了话
        heard(5, 11.0, 8.0), heard(6, 19.5, 8.0),               # 拼起来超过 15 秒
        heard(7, 30.0, 2.0), heard(8, 32.5, 2.0),               # 一句麦克风、一句系统声音
        heard(9, 40.0, 2.0), heard(10, None, 0.0),              # 打字的没有录音位置
    ]
    channel = {i: "mic" for i in range(1, 11)}
    channel[8] = "system"
    assert pieces(lines, channel) == []


def test_off_the_record_and_unrecorded_lines_are_left_alone():
    off = heard(2, 2.5, 2.0)
    off.off = True
    assert pieces([heard(1, 0.0, 2.0), off, heard(3, 5.0, 2.0)], {1: "mic", 2: "mic", 3: "mic"}) == []
    assert pieces([heard(1, 0.0, 2.0), heard(2, 2.5, 2.0)], {1: "mic"}) == []      # 没录下来的那句不拼


def test_a_rehearing_that_loses_words_is_not_used():
    assert better("自动回家逻辑", "自动回价逻辑")
    assert better("对。问到了。", "对，问到了")
    assert not better("这个也可以。在。当就是我这。", "概但就。")
    assert not better("先做登录", "")


def test_joined_lines_keep_the_first_line_and_its_place():
    m = Meeting(Settings())
    sid = m.speakers.assign(None)
    for i, (at, sec) in enumerate([(0.0, 2.0), (2.5, 1.5), (4.5, 2.0)], 1):
        m.lines.append(Line(id=i, t=0.0, who="human", text=f"第{i}句", speaker_id=sid, at=at, seconds=sec))
    m.lines.append(Line(id=4, t=0.0, who="bot", text="好"))
    m.speakers.speakers[sid].count = 3
    assert m.rejoin_lines([([1, 2, 3], "先做登录再做注册", 6.5)]) == 1
    assert [(l.id, l.text, l.seconds, l.at) for l in m.lines] == [(1, "先做登录再做注册", 6.5, 0.0), (4, "好", 0.0, None)]
    assert m.speakers.speakers[sid].count == 1
