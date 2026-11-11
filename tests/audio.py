"""测试用：把文字合成为 16k 单声道 PCM，模拟与会者说话。"""
import asyncio
import subprocess
import tempfile
import wave
from pathlib import Path


def say_pcm(text: str, voice: str = "Tingting") -> bytes:
    """用系统语音合成（离线、稳定）。"""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "a.wav"
        subprocess.run(["say", "-v", voice, "--data-format=LEI16@16000", "-o", str(out), text], check=True)
        with wave.open(str(out)) as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1
            return w.readframes(w.getnframes())


async def edge_pcm(text: str, voice: str) -> bytes:
    """用 edge-tts 合成不同嗓音（模拟不同的人），经 ffmpeg 转 16k PCM。"""
    from avatar.config import Settings
    from avatar.mouth import Mouth
    s = Settings(); s.speed = 1.0
    mp3 = await Mouth(s)._online(text, voice)
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    out, _ = await p.communicate(mp3)
    return out


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(16000 * seconds)


def chunks(pcm: bytes, ms: int = 100):
    step = 16000 * 2 * ms // 1000
    for i in range(0, len(pcm), step):
        yield pcm[i:i + step]
