"""耳朵：16k 单声道 PCM → 「有人开始说话 / 说完了 / 说了什么、是谁的声纹」。

断句（silero VAD）很轻，直接在事件循环里跑；识别（SenseVoice）和声纹放到单线程池里，
保证顺序且不卡住会议。
"""
from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable

import numpy as np

from . import models

log = logging.getLogger("avatar.ears")
SAMPLE_RATE = 16000


class Ears:
    min_silence = 0.5      # 停顿多久算一句话说完
    min_embed_seconds = 1.0  # 短于这个长度的片段声纹不可靠，不提取
    live_windows = 190       # 中途分辨说话人时最多看最近约 6 秒

    def __init__(
        self,
        on_speech_start: Callable[[], None],
        on_speech_end: Callable[[float], None],
        on_utterance: Callable[..., Awaitable[object]],   # (text, embedding, seconds=…)
    ):
        self.on_speech_start = on_speech_start
        self.on_speech_end = on_speech_end
        self.on_utterance = on_utterance
        self.ready = False
        self.has_speaker_model = False
        self._vad = None
        self._asr = None
        self._spk = None
        self._window = 512
        self._buf = np.zeros(0, dtype=np.float32)
        self._speaking = False
        self._live: list[np.ndarray] = []   # 当前这段人声到目前为止的采样（用来中途分辨是谁在说）
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ears")
        self._results: asyncio.Queue | None = None
        self._consumer: asyncio.Task | None = None

    # ---- 加载（耗时一两秒，放线程里） ----
    def load(self) -> None:
        import sherpa_onnx as so

        cfg = so.VadModelConfig()
        cfg.silero_vad.model = str(models.VAD.path)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = self.min_silence
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = 20
        cfg.sample_rate = SAMPLE_RATE
        self._window = cfg.silero_vad.window_size
        self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
        self._asr = so.OfflineRecognizer.from_sense_voice(
            model=str(models.ASR.path), tokens=str(models.ASR_TOKENS.path),
            num_threads=4, use_itn=True, language="auto",
        )
        if models.SPEAKER.ready:
            self._spk = so.SpeakerEmbeddingExtractor(
                so.SpeakerEmbeddingExtractorConfig(model=str(models.SPEAKER.path), num_threads=2)
            )
            self.has_speaker_model = True
        self.ready = True

    def reset(self) -> None:
        """换一场会议或换了音频来源时清掉残留状态。"""
        self._buf = np.zeros(0, dtype=np.float32)
        if self._vad is not None:
            self._vad.reset()
        if self._speaking:
            self._speaking = False
            self.on_speech_end(0.0)

    # ---- 喂音频（事件循环里调用） ----
    def feed(self, pcm16: bytes) -> None:
        if not self.ready:
            return
        if self._results is None:
            self._results = asyncio.Queue()
            self._consumer = asyncio.create_task(self._consume())
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, samples])
        w = self._window
        while len(self._buf) >= w:
            win = self._buf[:w]
            self._vad.accept_waveform(win)
            self._buf = self._buf[w:]
            speaking = self._vad.is_speech_detected()
            if speaking:
                if not self._speaking:
                    self._live = []
                self._live.append(win)
                if len(self._live) > self.live_windows:
                    del self._live[: len(self._live) - self.live_windows]
            if speaking and not self._speaking:
                self._speaking = True
                self.on_speech_start()
            elif not speaking and self._speaking:
                self._speaking = False
                self.on_speech_end(self.min_silence)
            while not self._vad.empty():
                seg = np.array(self._vad.front.samples, dtype=np.float32)
                self._vad.pop()
                loop = asyncio.get_running_loop()
                self._results.put_nowait(loop.run_in_executor(self._pool, self._recognize, seg))

    async def _consume(self) -> None:
        while True:
            fut = await self._results.get()
            try:
                text, emb, seconds = await fut
                if text:
                    await self.on_utterance(text, emb, seconds=seconds)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("这段话没识别出来")

    # ---- 识别（线程池里） ----
    def _recognize(self, samples: np.ndarray) -> tuple[str, np.ndarray | None, float]:
        seconds = len(samples) / SAMPLE_RATE
        st = self._asr.create_stream()
        st.accept_waveform(SAMPLE_RATE, samples)
        self._asr.decode_stream(st)
        text = st.result.text.strip()
        if not re.search(r"[\w一-鿿]", text):
            return "", None, seconds
        emb = None
        if self._spk is not None and len(samples) >= SAMPLE_RATE * self.min_embed_seconds:
            emb = self._embed(samples)
        return text, emb, seconds

    async def who(self) -> np.ndarray | None:
        """现在正在说话的人的声纹。没人在说、说得太短或没有声纹模型时返回 None。"""
        if self._spk is None or not self._speaking:
            return None
        samples = np.concatenate(self._live) if self._live else np.zeros(0, dtype=np.float32)
        if len(samples) < SAMPLE_RATE * 1.2:
            return None
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._embed, samples)

    def _embed(self, samples: np.ndarray) -> np.ndarray:
        s = self._spk.create_stream()
        s.accept_waveform(SAMPLE_RATE, samples)
        s.input_finished()
        return np.asarray(self._spk.compute(s), dtype=np.float32)

    def close(self) -> None:
        if self._consumer:
            self._consumer.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
