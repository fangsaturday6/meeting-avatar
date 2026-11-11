"""需要真实模型、网络和本机 Claude 的测试。运行：uv run pytest -m live -s"""
import asyncio
import time

import pytest

from avatar import models
from avatar.config import Settings
from avatar.ears import Ears
from avatar.fast_brain import FastBrain
from avatar.mouth import Mouth
from tests.audio import chunks, say_pcm, silence

pytestmark = pytest.mark.live


async def test_ears_hear_one_sentence():
    if models.missing(include_optional=False):
        pytest.skip("模型还没下载")
    events, got = [], []

    async def on_utt(text, emb):
        got.append((text, emb))

    ears = Ears(lambda: events.append("start"), lambda ago: events.append("end"), on_utt)
    t0 = time.time(); ears.load(); print(f"\n模型加载 {time.time()-t0:.1f}s 声纹模型={ears.has_speaker_model}")
    pcm = silence(0.3) + say_pcm("大家好，我们今天讨论登录功能怎么做。") + silence(1.2)
    t0 = time.time()
    for c in chunks(pcm):
        ears.feed(c)
        await asyncio.sleep(0)
    for _ in range(200):
        if got: break
        await asyncio.sleep(0.02)
    print(f"音频 {len(pcm)/32000:.1f}s，处理用时 {time.time()-t0:.2f}s，识别结果：{got[0][0] if got else None}")
    assert events[:2] == ["start", "end"]
    assert len(got) == 1 and "登录" in got[0][0]
    ears.close()


async def test_mouth_speaks_locally_and_falls_back():
    m = Mouth(Settings())
    await asyncio.to_thread(m.load)
    if not m.local_ready:
        pytest.skip("本地合成模型还没下载")
    for text in ["好的，", "我查一下飞书的 API 文档。"]:
        t0 = time.time()
        audio, mime = await m.synth(text)
        print(f"\n本地合成 {time.time()-t0:.2f}s {len(audio)} 字节 {mime} ｜{text}")
        assert mime == "audio/wav" and audio[:4] == b"RIFF" and len(audio) > 20000
    wav = await m._system("好的。")
    assert wav[:4] == b"RIFF"


async def test_fast_brain_answers_stays_quiet_and_can_be_interrupted():
    s = Settings(); s.topic = "讨论新版 App 的登录和支付排期"
    brain = FastBrain(s)
    t0 = time.time(); await brain.start(); print(f"\n预热 {time.time()-t0:.1f}s")

    async def ask(prompt, cut_after=None):
        t0 = time.time(); first = None; text = ""
        async for d in await brain.ask(prompt):
            first = first or time.time() - t0
            text += d
            if cut_after and len(text) >= cut_after:
                await brain.cancel(); cut_after = None
        print(f"首字 {first or -1:.2f}s 总 {time.time()-t0:.2f}s ｜{prompt[:28]} → {text[:60]!r}")
        return first, text

    first, text = await ask("[张三] 小克，你觉得我们先做登录还是先做支付？")
    assert first < 3 and "[静]" not in text and len(text) > 4
    _, text = await ask("[张三] 李四，你那边后端人手够吗？")
    assert text.strip().startswith("[静")
    _, text = await ask("[李四] 小克，飞书最新版本有没有开放会议机器人的接口，你帮忙查一下")
    assert "[[派工 查资料" in text
    _, text = await ask("[张三] 小克你详细讲讲 OAuth 授权码流程的每一步", cut_after=20)
    _, text = await ask("（你说到一半被人打断了。）\n[李四] 不用讲那么细，小克你一句话说结论")
    assert len(text) > 2
    await brain.stop()
    assert not brain.alive
