"""形象页送来的声音的唯一入口：解帧、计录音时钟、看健康、落盘，再交给耳朵。

帧格式：b"PCM1" + 4 字节小端序号 + 16k 单声道 16 位 PCM。序号在页面里每采到一包就加一，
发不出去的包也占号，所以跳号说明中间有声音没送到。没有帧头的包（旧页面）整包当声音。
"""
from __future__ import annotations

import base64
import logging
import struct
import time
from typing import TYPE_CHECKING

import numpy as np

from .recorder import RATE, AudioStore, Recorder

if TYPE_CHECKING:
    from .ears import Ears
    from .meeting import Line, Meeting

log = logging.getLogger("avatar.audio")
MAGIC = b"PCM1"


class AudioIn:
    silent_after = 5.0       # 当面开会时连续这么多秒一个采样都是 0，就是麦克风被关了、没权限或者音轨死了

    def __init__(self, meeting: "Meeting", ears: "Ears", store: AudioStore):
        self.m, self.ears, self.store = meeting, ears, store
        self._rec: Recorder | None = None
        self._rec_id = ""
        self.samples = 0                 # 这场会录到第几个采样（录音时钟）
        self._seq: int | None = None
        self._zeros = 0                  # 连续多少个采样是 0
        self._on = True                  # 在不在录（「这段别记」时不录）

    # ---- 声音 ----
    def feed(self, data: bytes) -> None:
        seq, pcm = self._split(data)
        if self.m.record_id != self._rec_id:
            self._begin()
        if seq is not None:
            self._check_seq(seq)
        self._watch(pcm)
        if self.m.recording != self._on:
            self._on = self.m.recording
            self._event("on" if self._on else "off")      # 「这段别记」：照样听，只是不录
        if self._rec and self._on:
            self._rec.write(pcm)
        self.ears.feed(pcm, clock=self.samples)
        self.samples += len(pcm) // 2

    @staticmethod
    def _split(data: bytes) -> tuple[int | None, bytes]:
        if len(data) >= 8 and data[:4] == MAGIC:
            return struct.unpack_from("<I", data, 4)[0], data[8:]
        return None, data

    def _check_seq(self, seq: int) -> None:
        last, self._seq = self._seq, seq
        if last is None or seq == last + 1:
            return
        if seq > last + 1:
            lost = seq - last - 1
            log.info("麦克风的声音中间丢了 %d 包（约 %.1f 秒）", lost, lost * 0.04)
            self._event("gap", frames=lost)
        else:
            self._event("restart")        # 页面刷新或换了窗口，序号从头数

    def _watch(self, pcm: bytes) -> None:
        if self.m.settings.scene != "room":
            return                         # 线上会议的声音来自虚拟声卡，没人说话时本来就是全零
        if pcm.count(0) == len(pcm):
            self._zeros += len(pcm) // 2
            if self._zeros >= self.silent_after * RATE and self.m.mic_health != "silent":
                log.info("麦克风送来的一直是数字静音，它现在听不到")
                self.m.set_flag("mic_health", "silent")
        else:
            self._zeros = 0
            if self.m.mic_health != "ok":
                self.m.set_flag("mic_health", "ok")

    # ---- 一场会的开始和结束 ----
    def _begin(self) -> None:
        self.finish()
        self._rec_id = self.m.record_id
        if self._rec_id:
            try:
                self._rec = Recorder(self.store.folder(self._rec_id), keep=lambda: self.m.settings.keep_audio)
            except OSError:
                log.exception("录音目录建不了，这场会只记文字")
            self._event("start", wall=time.time())

    def finish(self) -> None:
        """会议结束（或换了一场）：录音收尾。「不留」的话把声音删掉。"""
        if self._rec:
            self._event("end", wall=time.time())
            self._rec.close()
            if self.m.settings.keep_audio == "never":
                self.store.drop_audio(self._rec_id)
        self._rec, self._rec_id = None, ""
        self.samples, self._seq, self._zeros, self._on = 0, None, 0, True
        if self.m.mic_health != "ok":
            self.m.set_flag("mic_health", "ok")

    def _event(self, name: str, **extra) -> None:
        if self._rec:
            self._rec.event(event=name, at=round(self.samples / RATE, 3), **extra)

    # ---- 听到的话 ----
    def heard(self, text: str, emb, seconds: float, at: float | None, line: "Line | None") -> None:
        """记一句识别结果：在录音里的位置、文字、归给了谁、声纹。line 为空说明被判成了回声。"""
        if not self._rec or not self.m.recording or (line is not None and line.off):
            return
        self._rec.heard({
            "at": None if at is None else round(at, 3),
            "seconds": round(seconds, 3),
            "text": text,
            "line_id": line.id if line else None,
            "speaker_id": line.speaker_id if line else None,
            "echo": line is None,
            "emb": None if emb is None else base64.b64encode(np.asarray(emb, dtype=np.float32).tobytes()).decode(),
        })

    @staticmethod
    def voice(b64: str) -> bytes:
        return base64.b64decode(b64)
