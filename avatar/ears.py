"""耳朵：16k 单声道 PCM → 「有人开始说话 / 说完了 / 说了什么、是谁的声纹」。

断句（silero VAD）很轻，直接在事件循环里跑；识别（SenseVoice）和声纹放到单线程池里，
保证顺序且不卡住会议。
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable

import numpy as np

from . import models
from .textutil import strip_noise

log = logging.getLogger("avatar.ears")
SAMPLE_RATE = 16000


class Tape:
    """最近一段音频的循环录音带。断句只给片段本身，要往前多带一点就得自己留着。"""

    def __init__(self, size: int):
        self._ring = np.zeros(size, dtype=np.float32)
        self.end = 0               # 一共写进来多少采样；位置都按这个计数，和断句给的片段起点是同一把尺

    def write(self, samples: np.ndarray) -> None:
        n = len(self._ring)
        skipped = max(0, len(samples) - n)      # 一次写进来的比整盘带子还长：只留得下最后那一盘
        samples = samples[skipped:]
        i = (self.end + skipped) % n
        k = min(len(samples), n - i)
        self._ring[i:i + k] = samples[:k]
        self._ring[:len(samples) - k] = samples[k:]
        self.end += skipped + len(samples)

    def before(self, pos: int, count: int) -> np.ndarray:
        """pos 之前的 count 个采样；开头不够、或者已经被盖掉的部分就不给。"""
        first = max(pos - count, self.end - len(self._ring), 0)
        if first >= pos:
            return np.zeros(0, dtype=np.float32)
        return self._ring[np.arange(first, pos) % len(self._ring)]

    def reset(self) -> None:
        self.end = 0


class Ears:
    min_silence = 0.5      # 停顿多久算一句话说完
    # 交给识别的音频从片段起点再往前带这么多秒。断句要等确认是人声才算开始，片段起点常比真实起音晚几十毫秒
    # （实测最晚 141 毫秒），句首的声母被削掉，第一个字就认错：「查完」成了「卡完」，「差不多」成了「不多」。
    # 176 句合成语音加底噪实测：不带时错 110 字，带 0.1~0.2 秒错 95~98 字（不断句整段识别是 93），
    # 带到 0.5 秒反而错 123 字（前面的底噪太长）。
    lead_in = 0.2
    min_embed_seconds = 1.0  # 短于这个长度的片段声纹不可靠，不提取
    live_windows = 190       # 中途分辨说话人时最多看最近约 6 秒
    # 断句和识别的参数。改它们之前先用 `python -m avatar.replay` 拿录下来的会比一比
    vad_threshold = 0.5
    min_speech = 0.25
    max_speech = 20
    threads = 4

    def __init__(
        self,
        on_speech_start: Callable[[], None],
        on_speech_end: Callable[[float], None],
        on_utterance: Callable[..., Awaitable[object]],   # (text, embedding, seconds=…, at=…)
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
        self._tape = Tape(SAMPLE_RATE * 24)   # 比最长的一段话（20 秒）再多留几秒
        self._speaking = False
        self._fed = 0                         # 一共喂进来多少个采样（没给录音时钟时，用它定位）
        self._base: int | None = None         # 断句重置后的第 0 个采样，在录音里是第几个
        self._live: list[np.ndarray] = []   # 当前这段人声到目前为止的采样（用来中途分辨是谁在说）
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ears")
        self._results: asyncio.Queue | None = None
        self._inflight = 0                    # 交出去还没识别完、回调完的片段
        self._consumer: asyncio.Task | None = None

    # ---- 加载（耗时一两秒，放线程里） ----
    def load(self) -> None:
        import sherpa_onnx as so

        self._make_vad(so)
        self._asr = so.OfflineRecognizer.from_sense_voice(
            model=str(models.ASR.path), tokens=str(models.ASR_TOKENS.path),
            # 语言锁中文：让它自己猜的话，咳嗽、键盘声会被猜成日语、韩语、英语，记录里就多出「ち」「Yeah.」。
            # 锁了之后整句英文照样认得出来。
            num_threads=self.threads, use_itn=True, language="zh",
        )
        if models.SPEAKER.ready:
            self._spk = so.SpeakerEmbeddingExtractor(
                so.SpeakerEmbeddingExtractorConfig(model=str(models.SPEAKER.path), num_threads=2)
            )
            self.has_speaker_model = True
        self.ready = True

    def borrow(self, other: "Ears") -> None:
        """另一路声音用的耳朵：断句自己一份，识别和声纹借 other 已经加载好的，也在同一个线程里排队。"""
        import sherpa_onnx as so

        self._make_vad(so)
        self._asr, self._spk, self.has_speaker_model = other._asr, other._spk, other.has_speaker_model
        self._pool.shutdown(wait=False)
        self._pool = other._pool
        self.ready = other.ready

    def _make_vad(self, so) -> None:
        cfg = so.VadModelConfig()
        cfg.silero_vad.model = str((models.VAD if models.VAD.ready else models.VAD_OLD).path)
        cfg.silero_vad.threshold = self.vad_threshold
        cfg.silero_vad.min_silence_duration = self.min_silence
        cfg.silero_vad.min_speech_duration = self.min_speech
        cfg.silero_vad.max_speech_duration = self.max_speech
        cfg.sample_rate = SAMPLE_RATE
        self._window = cfg.silero_vad.window_size
        self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)

    def reset(self) -> None:
        """换一场会议或换了音频来源时清掉残留状态。"""
        self._buf = np.zeros(0, dtype=np.float32)
        self._tape.reset()
        self._base = None                    # 断句从头数了：下一帧来时重新对上录音里的位置
        if self._vad is not None:
            self._vad.reset()
        if self._speaking:
            self._speaking = False
            self.on_speech_end(0.0)

    # ---- 喂音频（事件循环里调用） ----
    def feed(self, pcm16: bytes, clock: int | None = None) -> None:
        """clock：这一帧第一个采样在整场录音里是第几个。给了它，每句话才能对回录音。"""
        if not self.ready:
            return
        if self._results is None:
            self._results = asyncio.Queue()
            self._consumer = asyncio.create_task(self._consume())
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if self._base is None:
            self._base = self._fed if clock is None else clock
        self._fed += len(samples)
        self._buf = np.concatenate([self._buf, samples])
        w = self._window
        while len(self._buf) >= w:
            win = self._buf[:w]
            self._vad.accept_waveform(win)
            self._tape.write(win)
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
            self._take()

    def _take(self) -> None:
        """把断句已经切好的片段交去识别。"""
        while not self._vad.empty():
            seg = np.array(self._vad.front.samples, dtype=np.float32)
            start = self._vad.front.start
            lead = self._tape.before(start, int(self.lead_in * SAMPLE_RATE))
            self._vad.pop()
            at = round(((self._base or 0) + start) / SAMPLE_RATE, 3)
            fut = asyncio.get_running_loop().run_in_executor(self._pool, self._recognize, seg, lead)
            self._inflight += 1
            self._results.put_nowait((fut, at))

    def flush(self) -> None:
        """声音到头了（重放结束）：说到一半的那句也交出去。"""
        if self._vad is None or self._results is None:
            return
        self._vad.flush()
        self._take()

    async def drain(self) -> None:
        """等已经交出去的片段都识别完、回调完。"""
        if self._results is not None:
            await self._results.join()

    async def _consume(self) -> None:
        while True:
            fut, at = await self._results.get()
            try:
                text, emb, seconds = await fut
                if text:
                    await self.on_utterance(text, emb, seconds=seconds, at=at)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("这段话没识别出来")
            finally:
                self._inflight -= 1
                self._results.task_done()

    # ---- 识别（线程池里） ----
    def _recognize(self, samples: np.ndarray, lead: np.ndarray) -> tuple[str, np.ndarray | None, float]:
        """samples 是断句给的片段，lead 是它前面多带的一小段：只给识别用，时长和声纹仍按片段本身算。"""
        seconds = len(samples) / SAMPLE_RATE
        text = self.recognize(samples, lead, note=True)
        if not text:
            return "", None, seconds
        emb = None
        if self._spk is not None and len(samples) >= SAMPLE_RATE * self.min_embed_seconds:
            emb = self._embed(samples)
        return text, emb, seconds

    def recognize(self, samples: np.ndarray, lead: np.ndarray, note: bool = False) -> str:
        """只认字，不取声纹（在线程池里调）。note：被当成杂音挡掉的、模型说不是人声的，记一行日志，
        以后查「真话被挡了 / 杂音漏进来了」时有据可查（挡杂音的规则攒够这些再改）。"""
        st = self._asr.create_stream()
        st.accept_waveform(SAMPLE_RATE, np.concatenate([lead, samples]))
        self._asr.decode_stream(st)
        raw, event = st.result.text, getattr(st.result, "event", "")
        text = strip_noise(raw, event)
        if note and raw.strip() and (not text or (event and "Speech" not in event)):
            log.info("%s %.1f秒 [%s]｜%s", "挡掉" if not text else "不像人声但留下", len(samples) / SAMPLE_RATE,
                     event.strip("<|>") or "?", raw)
        return text

    @property
    def busy(self) -> bool:
        """有人正在说，或者有一句还在识别：它的文字马上就到。"""
        return self._speaking or self._inflight > 0

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
