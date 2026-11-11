"""嘴：一句话 → 一段可播放的音频。

三种来源，按顺序兜底：
  本地合成（MeloTTS，不联网，延迟稳定）→ 在线合成（edge-tts，更自然但看网络）→ 系统自带语音
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import models
from .config import Settings, proxy_url

log = logging.getLogger("avatar.mouth")


class Mouth:
    online_timeout = 6.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self._local = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mouth")
        self._online_failures = 0

    def load(self) -> None:
        """加载本地合成模型（约两秒，放线程里调）。没有模型就跳过。"""
        if self._local is not None or not models.tts_ready():
            return
        import sherpa_onnx as so

        vits = so.OfflineTtsVitsModelConfig(
            model=str(models.TTS_MODEL.path), lexicon=str(models.TTS_LEXICON.path),
            tokens=str(models.TTS_TOKENS.path),
        )
        cfg = so.OfflineTtsConfig(
            model=so.OfflineTtsModelConfig(vits=vits, num_threads=4, provider="cpu"),
            rule_fsts=",".join(str(f.path) for f in models.TTS_FSTS), max_num_sentences=1,
        )
        self._local = so.OfflineTts(cfg)

    @property
    def local_ready(self) -> bool:
        return self._local is not None

    async def synth(self, text: str) -> tuple[bytes, str]:
        want_online = self.settings.voice != "local" or not self.local_ready
        # 在线合成连续失败几次后先不再尝试，免得每句话都白等一次超时
        if want_online and self._online_failures < 3:
            try:
                audio = await asyncio.wait_for(self._online(text), self.online_timeout)
                self._online_failures = 0
                return audio, "audio/mpeg"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._online_failures += 1
                log.warning("在线语音合成失败（第 %d 次）：%s", self._online_failures, e)
        if self.local_ready:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._pool, self._synth_local, text), "audio/wav"
        return await self._system(text), "audio/wav"

    def reset_online(self) -> None:
        self._online_failures = 0

    # ---- 本地 ----
    def _synth_local(self, text: str) -> bytes:
        a = self._local.generate(text, sid=0, speed=self.settings.speed)
        pcm = (np.clip(np.asarray(a.samples, dtype=np.float32), -1, 1) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(a.sample_rate)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()

    # ---- 在线 ----
    async def _online(self, text: str, voice: str | None = None) -> bytes:
        import edge_tts

        voice = voice or (self.settings.voice if self.settings.voice != "local" else "zh-CN-YunxiNeural")
        rate = f"{round((self.settings.speed - 1) * 100):+d}%"
        proxy = proxy_url()
        buf = bytearray()
        async for chunk in edge_tts.Communicate(text, voice, rate=rate, proxy=proxy).stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        if not buf:
            raise RuntimeError("没有收到音频")
        return bytes(buf)

    # ---- 系统 ----
    async def _system(self, text: str) -> bytes:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.wav"
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", "Tingting", "--data-format=LEI16@22050", "-o", str(out), text,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return out.read_bytes()

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
