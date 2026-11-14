"""录音存档：每场会一个目录，放在 workspace/audio/<会议编号>/。

- 原始声音：16k 单声道 16 位 WAV，每 60 秒一块（mic-0000.wav …）。正在写的一块叫 .wav.part，
  头部的长度还没填；进程被杀也读得出来，下次启动时补封。
- heard.jsonl：每句识别结果（在录音里的位置、时长、文字、归给了谁、声纹）。会后重聚类、重放比对都靠它。
- timeline.jsonl：录音时钟和墙钟怎么对应、哪里丢过声音。

写盘全在一个后台线程里做，事件循环只攒数据；写不上只记日志，绝不影响开会。
"""
from __future__ import annotations

import json
import logging
import os
import re
import struct
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .config import WORKSPACE

log = logging.getLogger("avatar.recorder")

RATE = 16000
WIDTH = 2                               # 16 位
AUDIO_DIR = WORKSPACE / "audio"
_ID = re.compile(r"[\w-]+")
CHANNELS = ("mic", "system")    # 麦克风；这台电脑放出来的声音（你在电脑上开线上会时，线上的人在这一路）
_HEAD = 44


def _wav_header(data_bytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * WIDTH, WIDTH, 16)
            + b"data" + struct.pack("<I", data_bytes))


def _seal(part: Path) -> None:
    """把一块写完的 .part 补上头部长度，改名成 .wav。"""
    size = part.stat().st_size
    with part.open("r+b") as f:
        f.seek(0)
        f.write(_wav_header(max(0, size - _HEAD)))
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, part.with_suffix(""))         # mic-0003.wav.part → mic-0003.wav


class AudioStore:
    def __init__(self, root: Path = AUDIO_DIR):
        self.root = Path(root)

    def folder(self, meeting_id: str) -> Path:
        if not _ID.fullmatch(meeting_id):
            raise ValueError("会议编号不对")
        path = self.root / meeting_id
        path.mkdir(parents=True, exist_ok=True)
        for p in (self.root, path):
            os.chmod(p, 0o700)                      # 录音和声纹只给本机的你看
        return path

    def _existing(self, meeting_id: str) -> Path | None:
        if not _ID.fullmatch(meeting_id):
            return None
        path = self.root / meeting_id
        return path if path.is_dir() else None

    def _chunks(self, folder: Path, channel: str | None = None) -> list[Path]:
        """录音块，按块序。channel：mic（麦克风）/ system（这台电脑放出的声音）；不给就是两路都要。"""
        heads = (f"{channel}-",) if channel else tuple(f"{c}-" for c in CHANNELS)
        return sorted(p for p in folder.iterdir() if p.name.startswith(heads) and p.name.endswith((".wav", ".wav.part")))

    def has_audio(self, meeting_id: str) -> bool:
        folder = self._existing(meeting_id)
        return bool(folder and self._chunks(folder))

    def pcm(self, meeting_id: str, channel: str = "mic") -> bytes:
        """整场会这一路的声音（按块的顺序接起来，含没封口的那块）。"""
        folder = self._existing(meeting_id)
        if not folder:
            return b""
        return b"".join(p.read_bytes()[_HEAD:] for p in self._chunks(folder, channel))

    def seal_all(self) -> int:
        """补封上次没正常收尾的录音块。返回补了几块。"""
        n = 0
        for part in self.root.glob("*/*-*.wav.part"):
            try:
                _seal(part)
                n += 1
            except OSError:
                log.exception("录音块没补封上：%s", part)
        return n

    def ended_at(self, meeting_id: str) -> float:
        """这场会什么时候散的：录音收尾时记下的时刻；没正常收尾的，按最后写盘的时间算。"""
        ends = [r["wall"] for r in self.rows(meeting_id, "timeline") if r.get("event") == "end" and r.get("wall")]
        if ends:
            return float(ends[-1])
        folder = self._existing(meeting_id)
        return max((p.stat().st_mtime for p in folder.iterdir()), default=0.0) if folder else 0.0

    def sweep(self, max_age: float, now: float, skip: str = "") -> list[str]:
        """散会超过 max_age 秒的录音删掉声音（文字和声纹留着）。skip 是正在开的那场。返回删了哪几场。"""
        if not self.root.is_dir():
            return []
        dropped = []
        for folder in sorted(self.root.iterdir()):
            mid = folder.name
            if mid == skip or not folder.is_dir() or not self.has_audio(mid):
                continue
            if now - self.ended_at(mid) > max_age:
                self.drop_audio(mid)
                dropped.append(mid)
        return dropped

    def drop_audio(self, meeting_id: str) -> None:
        """删掉声音，留下文字和声纹。"""
        folder = self._existing(meeting_id)
        for p in self._chunks(folder) if folder else []:
            p.unlink(missing_ok=True)

    def rows(self, meeting_id: str, name: str) -> list[dict]:
        folder = self._existing(meeting_id)
        path = folder / f"{name}.jsonl" if folder else None
        if not path or not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass                                # 进程被杀时写到一半的最后一行
        return out


class Recorder:
    """一场会的录音。事件循环里调 write / heard / event，真正写盘在后台线程。"""

    chunk_seconds = 60.0
    flush_seconds = 1.0

    def __init__(self, folder: Path, keep: Callable[[], str], channel: str = "mic"):
        self.folder = folder
        self.channel = channel
        self._rows = "" if channel == "mic" else f"{channel}-"    # 麦克风那一路的文件名不带前缀（和以前一样）
        self.keep = keep                            # 每次写都问一下：会开到一半改成「不留」，后面的就不写了
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recorder")
        self._pending = bytearray()
        self._chunk = 0                             # 正在写第几块
        self._in_chunk = 0                          # 这一块已经交出去多少字节
        self._last: Future | None = None
        self._failed = False

    # ---- 事件循环里调 ----
    def write(self, pcm: bytes) -> None:
        if self.keep() == "never":
            return
        self._pending += pcm
        if len(self._pending) >= self.flush_seconds * RATE * WIDTH:
            self._flush()

    def heard(self, row: dict) -> None:
        self._submit(self._append_row, self._rows + "heard", row)

    def event(self, **row) -> None:
        self._submit(self._append_row, self._rows + "timeline", row)

    def sync(self) -> None:
        """把攒下的都写下去并等写完（不收尾）。"""
        self._flush()
        if self._last:
            self._last.result()

    def close(self) -> None:
        """收尾：写完、把最后一块封口、等线程结束。"""
        self._flush()
        self._submit(self._seal_current)
        self._pool.shutdown(wait=True)

    # ---- 内部 ----
    def _flush(self) -> None:
        size = int(self.chunk_seconds * RATE) * WIDTH
        while self._pending:
            room = size - self._in_chunk
            piece, self._pending = bytes(self._pending[:room]), self._pending[room:]
            self._submit(self._append_audio, self._chunk, piece)
            self._in_chunk += len(piece)
            if self._in_chunk >= size:
                self._submit(self._seal_chunk, self._chunk)
                self._chunk += 1
                self._in_chunk = 0

    def _submit(self, fn, *args) -> None:
        self._last = self._pool.submit(self._guard, fn, *args)

    def _guard(self, fn, *args) -> None:
        try:
            fn(*args)
        except OSError:
            if not self._failed:                    # 磁盘满、目录被删：记一次就够，会照常开
                log.exception("录音没写上")
            self._failed = True

    def _part(self, index: int) -> Path:
        return self.folder / f"{self.channel}-{index:04d}.wav.part"

    def _append_audio(self, index: int, data: bytes) -> None:
        part = self._part(index)
        new = not part.exists()
        with part.open("ab") as f:
            if new:
                f.write(_wav_header(0))
            f.write(data)

    def _seal_chunk(self, index: int) -> None:
        if self._part(index).exists():
            _seal(self._part(index))

    def _seal_current(self) -> None:
        self._seal_chunk(self._chunk)

    def _append_row(self, name: str, row: dict) -> None:
        with (self.folder / f"{name}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
