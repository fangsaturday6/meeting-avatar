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
