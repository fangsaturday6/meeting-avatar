"""重放评测台：拿存下来的录音重新听一遍，和现场听到的、和另一套参数比。"""
import asyncio

import numpy as np

from avatar import replay
from avatar.recorder import AudioStore, Recorder

RATE = 16000


class FakeEars:
    """每段连续的非零声音算一句话，文字是它的长度。"""
    lead_in = 0.2

    def __init__(self, *_):
        self.on_utterance = None
        self.fed = bytearray()

    def load(self):
        self.loaded = True

    def feed(self, pcm, clock=None):
        self.fed += pcm

    def flush(self):
        x = np.frombuffer(bytes(self.fed), dtype=np.int16)
        loud = np.concatenate([[0], (x != 0).astype(int), [0]])
        edges = np.flatnonzero(np.diff(loud))
        self._out = [(a / RATE, (b - a) / RATE) for a, b in zip(edges[::2], edges[1::2])]

    async def drain(self):
        for at, seconds in getattr(self, "_out", []):
            await self.on_utterance(f"说了{seconds:.1f}秒", np.ones(3, dtype=np.float32) * at, seconds=seconds, at=at)
        self._out = []

    def close(self):
        pass


def record(store, mid, segments):
    """segments：[(开始秒, 长度秒), …]，其余是静音。"""
    total = max(a + n for a, n in segments) + 1
    x = np.zeros(int(total * RATE), dtype=np.int16)
    for a, n in segments:
        x[int(a * RATE):int((a + n) * RATE)] = 500
    r = Recorder(store.folder(mid), keep=lambda: "always")
    r.write(x.tobytes())
    r.heard({"at": 1.0, "seconds": 2.0, "text": "说了2.0秒", "speaker_id": 1, "line_id": 1, "echo": False, "emb": None})
    r.close()


def test_replay_hears_the_recording_again(tmp_path):
    store = AudioStore(tmp_path)
    record(store, "m1", [(1, 2), (5, 1.5)])
    ears = FakeEars()
    rows = asyncio.run(replay.replay(store.pcm("m1"), ears))
    assert [(r["at"], r["seconds"], r["text"]) for r in rows] == [(1.0, 2.0, "说了2.0秒"), (5.0, 1.5, "说了1.5秒")]


def test_command_line_saves_and_compares(tmp_path, capsys):
    store = AudioStore(tmp_path)
    record(store, "2026-11-13_090000", [(1, 2), (5, 1.5)])
    record(store, "2026-11-13_100000", [(1, 2)])
    made = []

    def make(**params):
        e = FakeEars()
        for k, v in params.items():
            setattr(e, k, v)
        made.append(e)
        return e

    assert replay.main(["latest", "--set", "lead_in=0.3", "--save", "a", "--diff", "heard"], store=store, make_ears=make) == 0
    out = capsys.readouterr().out
    assert made[0].lead_in == 0.3 and made[0].loaded
    assert "2026-11-13_100000" in out and "1 句" in out and "和 heard 一样" in out
    assert store.rows("2026-11-13_100000", "replay-a")[0]["text"] == "说了2.0秒"

    assert replay.main(["2026-11-13_090000", "--diff", "heard"], store=store, make_ears=make) == 0
    out = capsys.readouterr().out
    assert "+ [00:05.0]" in out and "说了1.5秒" in out


def test_unknown_parameters_and_meetings_are_refused(tmp_path, capsys):
    store = AudioStore(tmp_path)
    assert replay.main(["nothing"], store=store, make_ears=FakeEars) == 1
    assert "没有这场会的录音" in capsys.readouterr().err
    record(store, "m1", [(1, 1)])
    assert replay.main(["m1", "--set", "rm_rf=1"], store=store, make_ears=FakeEars) == 1
    assert "不能调的参数" in capsys.readouterr().err
