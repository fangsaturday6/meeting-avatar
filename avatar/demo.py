"""演示后台：不需要模型、网络和本机 Claude，数据放临时目录，不碰真实设置。

改界面、截图、给 AI Agent 练手用。接口和真实后台完全一样。

    uv run python -m avatar.demo              # 8767 端口，等你操作
    uv run python -m avatar.demo --autoplay   # 自动演一场会，各种状态轮流出现
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import tempfile
import wave

os.environ.setdefault("AVATAR_WORKSPACE", tempfile.mkdtemp(prefix="avatar-demo-"))

import numpy as np  # noqa: E402
import uvicorn  # noqa: E402

from . import connect  # noqa: E402
from .api import Hub, create_app  # noqa: E402
from .config import DESIGN_DIR, Settings  # noqa: E402
from .floor import WorkResult  # noqa: E402

ANSWERS = [
    "先做手机号登录，微信登录要等资质批下来。",
    "我建议这周先把接口定下来，下周再联调。",
    "可以，不过要先确认一下数据从哪来。",
]
RESULTS = {
    "查资料": "## 结论\n\n开放平台有这个接口，在**第三章 · 消息与群组**。\n\n- 需要企业自建应用权限\n- 每分钟限 100 次\n- 返回里带 `message_id`，可以用来撤回\n",
    "查代码": "## 登录逻辑在哪\n\n- `auth/login.py`：校验验证码、签发令牌\n- `auth/session.py`：会话续期\n\n```python\ndef login(phone, code):\n    verify(phone, code)\n    return issue_token(phone)\n```\n",
    "做设计": "登录页做好了：手机号加验证码，一个主按钮。",
}
DESIGN_HTML = """<!doctype html><meta charset="utf-8"><title>登录页</title>
<body style="margin:0;height:100vh;display:grid;place-items:center;font-family:-apple-system,'PingFang SC',sans-serif;background:#f4f4f2">
<form style="width:300px;display:grid;gap:12px;background:#fff;padding:28px;border-radius:16px">
<b style="font-size:20px">登录</b><input placeholder="手机号" style="padding:12px;border:1px solid #ddd;border-radius:10px;font-size:15px">
<input placeholder="验证码" style="padding:12px;border:1px solid #ddd;border-radius:10px;font-size:15px">
<button type="button" style="padding:12px;border:0;border-radius:10px;background:#111;color:#fff;font-size:15px">登录</button></form>"""
PACE = 1.0   # 演示节奏的倍数；测试里调快
SCRIPT = [  # (等几秒, 谁, 说什么)
    (1.5, "张总", "登录先做手机号还是微信？"),
    (2.0, "李工", "小克你怎么看"),
    (6.0, "李工", "查一下飞书有没有发消息的接口"),
    (9.0, "张总", "那出一版登录页看看"),
    (10.0, "王芳", "登录逻辑现在在哪个文件，查一下代码"),
]


class DemoBrain:
    """按关键词回话的假大脑。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.on_retry = None
        self._n = 0

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def cancel(self) -> None: ...

    def _reply(self, prompt: str) -> str:
        last = prompt.strip().splitlines()[-1] if prompt.strip() else ""
        if "设计" in last or "出一版" in last:
            return "好，我出一版。[[派工 做设计：登录页]]"
        if "查" in last and any(k in last for k in ("代码", "文件", "函数")):
            return "我看一下代码。[[派工 查代码：登录逻辑在哪个文件]]"
        if "查" in last:
            return "我查一下。[[派工 查资料：飞书发消息的接口]]"
        if self.settings.name in last or last.endswith(("？", "?", "吗", "呢")):
            self._n += 1
            return ANSWERS[self._n % len(ANSWERS)]
        return "[静]"

    async def ask(self, prompt: str):
        reply = self._reply(prompt)

        async def gen():
            await asyncio.sleep(1.0 * PACE)     # 让「在想」看得见
            for i in range(0, len(reply), 3):
                await asyncio.sleep(0.04 * PACE)
                yield reply[i:i + 3]
        return gen()


class DemoMouth:
    """不合成真话，只发一段有音节起伏的轻声，让嘴型和字幕能跟着动。"""

    rate = 24000

    async def synth(self, text: str) -> tuple[bytes, str]:
        n = max(1, len(text))
        t = np.arange(int(self.rate * 0.19 * n)) / self.rate
        syllable = np.sin(np.pi * ((t / 0.19) % 1.0)) ** 2
        pitch = 130 + 25 * np.sin(2 * np.pi * 0.7 * t)
        phase = 2 * np.pi * np.cumsum(pitch) / self.rate
        wave_ = (np.sin(phase) + 0.4 * np.sin(2 * phase) + 0.2 * np.sin(3 * phase)) * syllable * 0.1
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.rate)
            w.writeframes((wave_ * 32767).astype("<i2").tobytes())
        return buf.getvalue(), "audio/wav"


class DemoWorker:
    async def run(self, task, on_progress) -> WorkResult:
        for step in ("正在搜索", "正在读第一份文档", "正在整理结论"):
            on_progress(step)
            await asyncio.sleep(1.6 * PACE)
        if task.kind == "做设计":
            folder = DESIGN_DIR / f"demo-{task.id}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "index.html").write_text(DESIGN_HTML, encoding="utf-8")
            return WorkResult(True, RESULTS[task.kind], artifact=f"/designs/demo-{task.id}/index.html")
        return WorkResult(True, RESULTS.get(task.kind, "做完了。"))


class _NoEars:
    """演示时不做语音识别：麦克风的声音只用来让形象有反应。"""

    def feed(self, pcm: bytes) -> None: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...


class DemoConnector:
    """假的登记器：开关照常能拨，但不碰你真实的 Claude Code / Codex 配置。Codex 演的是「没装」。"""

    def __init__(self) -> None:
        self.states = {"claude": "off", "codex": "missing"}

    def clients(self) -> list[dict]:
        return [{"id": cid, "name": c["name"], "state": self.states[cid]} for cid, c in connect.CLIENTS.items()]

    def set(self, client_id: str, connected: bool) -> None:
        if why := connect.problem(client_id, self.states[client_id]):
            raise ValueError(why)
        self.states[client_id] = "on" if connected else "off"


class DemoHub(Hub):
    def __init__(self, autoplay: bool = False):
        s = Settings()
        s.silence_nudge = 0.0
        super().__init__(settings=s, brain=DemoBrain(s), mouth=DemoMouth(), worker=DemoWorker(), save_settings=False,
                         agents=DemoConnector())
        self.ears = _NoEars()
        self.floor.voice_probe = None
        self.ready = {"ears": True, "speakers": True, "local_voice": True}
        self.autoplay = autoplay

    async def load(self) -> None:
        self.meeting.emit("ready", ready=self.ready)
        while self.autoplay:
            await self._play()

    async def _play(self) -> None:
        await asyncio.sleep(2)
        await self.floor.start()
        for wait, who, text in SCRIPT:
            await asyncio.sleep(wait)
            self.floor.on_speech_start()
            await asyncio.sleep(min(2.0, 0.12 * len(text)))
            self.floor.on_speech_end()
            await self.floor.on_utterance(text, speaker_name=who)
        await asyncio.sleep(14)
        await self.floor.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="会议数字人 · 演示后台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--autoplay", action="store_true", help="自动演一场会")
    args = ap.parse_args()
    print(f"\n  演示后台  http://{args.host}:{args.port}/\n")
    uvicorn.run(create_app(DemoHub(autoplay=args.autoplay)), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
