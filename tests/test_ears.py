"""耳朵里不需要模型的部分。"""
import numpy as np

from avatar.ears import Tape


def ramp(a: int, b: int) -> np.ndarray:
    return np.arange(a, b, dtype=np.float32)


def test_tape_gives_back_the_audio_just_before_a_position():
    tape = Tape(100)
    tape.write(ramp(0, 30)); tape.write(ramp(30, 60))
    assert tape.before(40, 10).tolist() == ramp(30, 40).tolist()
    assert tape.before(5, 10).tolist() == ramp(0, 5).tolist()        # 开头不够长：有多少给多少
    assert tape.before(0, 10).size == 0


def test_tape_wraps_around_and_forgets_what_it_overwrote():
    tape = Tape(100)
    for i in range(0, 250, 50):
        tape.write(ramp(i, i + 50))                                  # 一共 250 个采样，只留最近 100 个
    assert tape.before(240, 20).tolist() == ramp(220, 240).tolist()
    assert tape.before(160, 20).tolist() == ramp(150, 160).tolist()  # 150 之前的已经被盖掉了
    assert tape.before(120, 20).size == 0


def test_tape_starts_over_after_reset():
    tape = Tape(100)
    tape.write(ramp(0, 80))
    tape.reset()
    tape.write(ramp(500, 520))
    assert tape.before(20, 10).tolist() == ramp(510, 520).tolist()


def test_tape_survives_a_write_longer_than_itself():
    tape = Tape(100)
    tape.write(ramp(0, 250))
    assert tape.end == 250 and tape.before(250, 30).tolist() == ramp(220, 250).tolist()
    assert tape.before(140, 30).size == 0


# ---------- 每句话在录音里的位置（假断句、假识别，不用模型） ----------
import asyncio
from types import SimpleNamespace

from avatar.ears import SAMPLE_RATE, Ears


class FakeVad:
    """收到第 speech_at 个采样之后的第一个窗口时，交出一段从 speech_at 开始、长 1 秒的话。"""

    def __init__(self, speech_at: int):
        self.speech_at, self.fed, self.out = speech_at, 0, []

    def accept_waveform(self, win):
        before, self.fed = self.fed, self.fed + len(win)
        if before <= self.speech_at + SAMPLE_RATE < self.fed:
            self.out.append(SimpleNamespace(start=self.speech_at, samples=[0.1] * SAMPLE_RATE))

    def is_speech_detected(self):
        return False

    def empty(self):
        return not self.out

    @property
    def front(self):
        return self.out[0]

    def pop(self):
        self.out.pop(0)

    def reset(self):
        self.fed = 0

    def flush(self):
        if self.fed > self.speech_at and not self.out:
            self.out.append(SimpleNamespace(start=self.speech_at, samples=[0.1] * (self.fed - self.speech_at)))


def fake_ears(speech_at: int):
    got = []

    async def heard(text, emb, seconds=0.0, at=None):
        got.append((text, round(seconds, 2), at))

    ears = Ears(lambda: None, lambda ago: None, heard)
    ears._vad = FakeVad(speech_at)
    ears._recognize = lambda samples, lead: ("你好", None, len(samples) / SAMPLE_RATE)
    ears.ready = True
    return ears, got


def silence(seconds: float) -> bytes:
    return b"\0" * int(seconds * SAMPLE_RATE) * 2


def test_each_utterance_knows_where_it_is_on_the_recording():
    async def go():
        ears, got = fake_ears(speech_at=SAMPLE_RATE)        # 断句说：重置之后第 1 秒开始有人说话
        ears.feed(silence(1), clock=10 * SAMPLE_RATE)        # 这一帧在整场录音的第 10 秒
        ears.feed(silence(2), clock=11 * SAMPLE_RATE)
        await ears.drain()
        assert got == [("你好", 1.0, 11.0)]

        ears.reset()                                         # 麦克风断过：断句从头数，位置要重新对上
        ears._vad.speech_at = 0
        ears.feed(silence(2), clock=30 * SAMPLE_RATE)
        await ears.drain()
        assert got[-1] == ("你好", 1.0, 30.0)
        ears.close()
    asyncio.run(go())


def test_without_a_clock_positions_count_from_the_first_sample_fed():
    async def go():
        ears, got = fake_ears(speech_at=SAMPLE_RATE // 2)
        ears.feed(silence(2))
        await ears.drain()
        assert got == [("你好", 1.0, 0.5)]
        ears.close()
    asyncio.run(go())


def test_flush_hands_over_the_last_unfinished_utterance():
    async def go():
        ears, got = fake_ears(speech_at=SAMPLE_RATE)
        ears.feed(silence(1.5))                              # 录音在这句话说到一半时结束了
        await ears.drain()
        assert got == []
        ears.flush()
        await ears.drain()
        assert got == [("你好", 0.47, 1.0)]                   # 不满一个断句窗口（512 个采样）的尾巴不算
        ears.close()
    asyncio.run(go())
