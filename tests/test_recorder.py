"""录音落盘：分块、崩溃后还能读、保留策略。"""
import json
import stat
import wave

import numpy as np

from avatar.recorder import AudioStore, Recorder

RATE = 16000


def pcm(seconds: float, start: int = 0) -> bytes:
    n = int(seconds * RATE)
    return (np.arange(start, start + n) % 30000).astype(np.int16).tobytes()


def rec(store, mid="m1", keep="always", chunk=1.0) -> Recorder:
    r = Recorder(store.folder(mid), keep=lambda: keep)
    r.chunk_seconds = chunk
    return r


def test_audio_comes_back_exactly_as_written_across_chunks(tmp_path):
    store = AudioStore(tmp_path)
    r = rec(store)
    data = pcm(2.5)
    for i in range(0, len(data), 1280):           # 和形象页一样，每包 40 毫秒
        r.write(data[i:i + 1280])
    r.close()
    names = sorted(p.name for p in store.folder("m1").iterdir() if "mic" in p.name)
    assert names == ["mic-0000.wav", "mic-0001.wav", "mic-0002.wav"]
    with wave.open(str(store.folder("m1") / "mic-0000.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (RATE, 1, 2, RATE)
    assert store.pcm("m1") == data


def test_a_crash_leaves_a_readable_part_that_can_be_sealed(tmp_path):
    store = AudioStore(tmp_path)
    r = rec(store, chunk=60)
    data = pcm(1.5)
    r.write(data)
    r.sync()                                      # 写盘线程把攒下的写完，但没正常收尾（进程被杀）
    parts = list(store.folder("m1").glob("*.part"))
    assert len(parts) == 1
    assert store.pcm("m1") == data                # 没封口的也读得出来
    assert store.seal_all() == 1
    with wave.open(str(store.folder("m1") / "mic-0000.wav")) as w:
        assert w.getnframes() == len(data) // 2
    assert store.pcm("m1") == data


def test_never_keeps_no_sound_but_still_keeps_what_was_heard(tmp_path):
    store = AudioStore(tmp_path)
    r = rec(store, keep="never")
    r.write(pcm(2))
    r.heard({"at": 0.5, "text": "你好"})
    r.event(event="start", at=0.0, wall=1.0)
    r.close()
    folder = store.folder("m1")
    assert not list(folder.glob("mic-*"))
    assert store.rows("m1", "heard") == [{"at": 0.5, "text": "你好"}]
    assert store.rows("m1", "timeline")[0]["event"] == "start"


def test_dropping_the_audio_keeps_the_text_and_voices(tmp_path):
    store = AudioStore(tmp_path)
    r = rec(store)
    r.write(pcm(1.2))
    r.heard({"at": 0.1, "text": "开会"})
    r.close()
    assert store.has_audio("m1")
    store.drop_audio("m1")
    assert not store.has_audio("m1") and store.pcm("m1") == b""
    assert store.rows("m1", "heard") == [{"at": 0.1, "text": "开会"}]


def test_folders_are_private_and_ids_cannot_escape(tmp_path):
    store = AudioStore(tmp_path / "audio")
    folder = store.folder("2026-11-13_101010")
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert folder.parent == store.root
    for bad in ("../x", "a/b", ".."):
        try:
            store.folder(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_rows_skip_a_line_cut_off_by_a_crash(tmp_path):
    store = AudioStore(tmp_path)
    folder = store.folder("m1")
    (folder / "heard.jsonl").write_text(json.dumps({"text": "完整"}) + "\n" + '{"text": "写到一半', encoding="utf-8")
    assert store.rows("m1", "heard") == [{"text": "完整"}]


def test_old_recordings_are_swept_after_the_time_is_up(tmp_path):
    store = AudioStore(tmp_path)
    for mid, ended in (("old", 1000.0), ("new", 9000.0)):
        r = rec(store, mid)
        r.write(pcm(1.2))
        r.event(event="end", at=1.2, wall=ended)
        r.close()
    r = rec(store, "crashed")                     # 没正常收尾的：按最后写盘的时间算
    r.write(pcm(1.2))
    r.close()
    assert store.ended_at("old") == 1000.0 and store.ended_at("crashed") > 9000
    dropped = store.sweep(max_age=2 * 3600, now=1000.0 + 2 * 3600 + 1, skip="new")
    assert dropped == ["old"]
    assert not store.has_audio("old") and store.has_audio("new") and store.has_audio("crashed")
    assert store.rows("old", "timeline")          # 文字那几份留着
