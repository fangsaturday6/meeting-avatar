"""形象页送来的声音：解帧、计时、落盘、看是不是一直是数字静音。"""
import struct

import numpy as np

from avatar.audio_in import AudioIn
from avatar.config import Settings
from avatar.meeting import Line, Meeting
from avatar.recorder import AudioStore

FRAME = 640          # 每包 40 毫秒


class FakeEars:
    def __init__(self):
        self.fed = []

    def feed(self, pcm, clock=None):
        self.fed.append((len(pcm) // 2, clock))


def frame(seq: int, loud: bool = True) -> bytes:
    body = (np.ones(FRAME) * (800 if loud else 0)).astype(np.int16).tobytes()
    return b"PCM1" + struct.pack("<I", seq) + body


def setup(tmp_path, scene="room"):
    s = Settings(); s.scene = scene
    m = Meeting(s)
    m.status, m.record_id = "listening", "2026-11-13_100000"
    store = AudioStore(tmp_path)
    ears = FakeEars()
    return m, ears, store, AudioIn(m, ears, store)


def test_frames_reach_the_ears_with_their_place_on_the_recording(tmp_path):
    m, ears, store, audio = setup(tmp_path)
    for seq in range(3):
        audio.feed(frame(seq))
    audio.feed(b"\0" * 1280)                      # 旧页面：没有帧头，整包都是声音
    assert ears.fed == [(FRAME, 0), (FRAME, FRAME), (FRAME, 2 * FRAME), (FRAME, 3 * FRAME)]
    audio.finish()
    assert len(store.pcm(m.record_id)) == 4 * FRAME * 2
    events = [r["event"] for r in store.rows(m.record_id, "timeline")]
    assert events == ["start", "end"]


def test_lost_frames_and_a_reloaded_page_are_written_down(tmp_path):
    m, ears, store, audio = setup(tmp_path)
    for seq in (0, 1, 5, 6, 0, 1):                # 2~4 号没送到；然后页面刷新，从 0 重新数
        audio.feed(frame(seq))
    audio.finish()
    rows = store.rows(m.record_id, "timeline")
    assert [(r["event"], r.get("frames")) for r in rows] == [
        ("start", None), ("gap", 3), ("restart", None), ("end", None)]
    assert rows[1]["at"] == 2 * FRAME / 16000     # 录音本身是连续的，丢的不补零
    assert len(store.pcm(m.record_id)) == 6 * FRAME * 2


def test_digital_silence_means_it_cannot_hear(tmp_path):
    m, ears, store, audio = setup(tmp_path)
    for seq in range(int(5 / 0.04)):              # 连续 5 秒一个采样都是 0：麦克风被关了或者没权限
        audio.feed(frame(seq, loud=False))
    assert m.mic_health == "silent"
    audio.feed(frame(999, loud=True))
    assert m.mic_health == "ok"


def test_online_meetings_are_allowed_to_be_silent(tmp_path):
    m, ears, store, audio = setup(tmp_path, scene="online")  # 虚拟声卡没人说话时本来就是全零
    for seq in range(200):
        audio.feed(frame(seq, loud=False))
    assert m.mic_health == "ok"


def test_a_new_meeting_closes_the_last_recording(tmp_path):
    m, ears, store, audio = setup(tmp_path)
    first = m.record_id
    audio.feed(frame(0))
    m.record_id = "2026-11-13_110000"
    audio.feed(frame(0))
    assert ears.fed[-1] == (FRAME, 0)             # 新的一场从 0 计时
    assert [r["event"] for r in store.rows(first, "timeline")] == ["start", "end"]
    audio.finish()
    assert store.has_audio(first) and store.has_audio(m.record_id)


def test_what_was_heard_is_kept_with_its_voice(tmp_path):
    m, ears, store, audio = setup(tmp_path)
    audio.feed(frame(0))
    line = Line(id=7, t=0.0, who="human", text="先做登录吧", speaker_id=2, speaker="说话人2")
    audio.heard("先做登录吧", np.array([0.5, -0.25], dtype=np.float32), 1.8, 0.04, line)
    audio.heard("先做登录吧", None, 1.0, 3.0, None)        # 被判成回声，没有进记录
    audio.finish()
    first, echo = store.rows(m.record_id, "heard")
    assert first["line_id"] == 7 and first["speaker_id"] == 2 and not first["echo"]
    assert np.frombuffer(AudioIn.voice(first["emb"]), dtype=np.float32).tolist() == [0.5, -0.25]
    assert echo["echo"] and echo["emb"] is None and echo["line_id"] is None


def test_never_keep_audio_deletes_the_sound_when_the_meeting_ends(tmp_path):
    m, ears, store, audio = setup(tmp_path)
    audio.feed(frame(0))
    m.settings.keep_audio = "never"                # 会开到一半改成「不留」
    audio.feed(frame(1))
    audio.finish()
    assert not store.has_audio(m.record_id)
    assert store.rows(m.record_id, "timeline")
