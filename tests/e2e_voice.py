"""语音端到端测试：模拟一个形象页 + 两位与会者，走真实的后台、模型和本机 Claude。

运行：uv run python -m tests.e2e_voice
它会自己在 8766 端口起一个后台，测完关掉。需要模型已下载、本机 claude 已登录。
"""
import asyncio
import json
import os
import struct
import subprocess
import sys
import time

import httpx
import websockets

from tests.audio import chunks, say_pcm, silence

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
T0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - T0:6.2f}s] {msg}", flush=True)


class FakeStage:
    """假形象页：实时送麦克风音频，按真实时长「播放」收到的语音片段。"""

    def __init__(self, ws):
        self.ws = ws
        self.mic: asyncio.Queue[bytes] = asyncio.Queue()
        self.clips: asyncio.Queue = asyncio.Queue()
        self.events: list[tuple[float, str, str]] = []
        self.playing = False
        self.generation = 0
        self.last_voice_sent = 0.0
        self.tasks = [asyncio.create_task(c) for c in (self._mic_loop(), self._recv_loop(), self._play_loop())]

    def mark(self, kind: str, text: str = "") -> None:
        self.events.append((time.monotonic(), kind, text))
        log(f"{kind} {text}")

    def speak(self, pcm: bytes) -> None:
        for c in chunks(pcm, 40):
            self.mic.put_nowait(c)

    async def wait_mic_empty(self) -> None:
        while not self.mic.empty():
            await asyncio.sleep(0.02)

    async def _mic_loop(self) -> None:
        quiet = silence(0.04)
        nxt = time.monotonic()
        while True:
            try:
                chunk = self.mic.get_nowait()
                self.last_voice_sent = time.monotonic()
            except asyncio.QueueEmpty:
                chunk = quiet
            await self.ws.send(chunk)
            nxt += 0.04
            await asyncio.sleep(max(0, nxt - time.monotonic()))

    async def _recv_loop(self) -> None:
        async for msg in self.ws:
            if isinstance(msg, str):
                if json.loads(msg).get("type") == "stop":
                    self.generation += 1
                    while not self.clips.empty():
                        self.clips.get_nowait()
                    self.mark("收到停止指令")
                continue
            n = struct.unpack(">I", msg[:4])[0]
            head = json.loads(msg[4:4 + n])
            audio = msg[4 + n:]
            # WAV：44 字节头，16 位单声道；采样率在第 24 字节
            rate = struct.unpack("<I", audio[24:28])[0] if audio[:4] == b"RIFF" else 24000
            head["seconds"] = max(0.2, (len(audio) - 44) / (rate * 2))
            self.mark("收到片段", f"{head['seconds']:.1f}s「{head['text']}」")
            self.clips.put_nowait((self.generation, head))

    async def _play_loop(self) -> None:
        while True:
            gen, head = await self.clips.get()
            if gen != self.generation:
                continue
            self.playing = True
            await self.ws.send(json.dumps({"type": "clip_started", "id": head["id"]}))
            self.mark("开始播放", head["text"])
            end = time.monotonic() + head["seconds"]
            while time.monotonic() < end and gen == self.generation:
                await asyncio.sleep(0.02)
            self.playing = False
            if gen == self.generation:
                await self.ws.send(json.dumps({"type": "clip_ended", "id": head["id"]}))

    def first(self, kind: str, after: float) -> float | None:
        return next((t for t, k, _ in self.events if k == kind and t >= after), None)

    def close(self) -> None:
        for t in self.tasks:
            t.cancel()


async def main() -> int:
    env = dict(os.environ)
    server = subprocess.Popen([sys.executable, "-m", "avatar", "--port", str(PORT)], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("  ✓ " if ok else "  ✗ ") + what, flush=True)
        if not ok:
            failures.append(what)

    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=60, trust_env=False) as http:
            for _ in range(100):
                try:
                    st = (await http.get("/api/state")).json()
                    if st["ready"]["ears"] and st["ready"]["local_voice"]:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.3)
            else:
                print("后台没起来，或模型没装好"); return 2
            log(f"后台就绪 {st['ready']}")
            await http.patch("/api/settings", json={"topic": "讨论新版应用的登录和支付排期", "silence_nudge": 0})
            log("合成两位与会者的语音…")
            a1 = say_pcm("大家好，我们今天讨论一下新版应用的排期。", "Tingting")
            b1 = say_pcm("小克，你觉得我们应该先做登录，还是先做支付？", "Meijia")
            a2 = say_pcm("等一下，我先插一句。预算其实还没有批下来，这个要先跟财务确认。", "Tingting")
            await http.post("/api/meeting/start")
            log("会议开始")

            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws/audio", max_size=None) as ws:
                stage = FakeStage(ws)
                await asyncio.sleep(1.0)

                # ① 张三开场（不是对数字人说的）
                stage.speak(a1); await stage.wait_mic_empty(); await asyncio.sleep(3.0)
                st = (await http.get("/api/state")).json()
                check(len(st["lines"]) >= 1 and "排期" in st["lines"][0]["text"], f"听清开场白：{st['lines'][0]['text'] if st['lines'] else '（没听到）'}")

                # ② 李四点名提问 → 应当开口回答
                stage.speak(b1); await stage.wait_mic_empty()
                asked = stage.last_voice_sent
                for _ in range(400):
                    if stage.first("开始播放", asked): break
                    await asyncio.sleep(0.02)
                t_clip, t_play = stage.first("收到片段", asked), stage.first("开始播放", asked)
                check(t_play is not None, "被点名后开口回答")
                if t_play:
                    log(f"★ 接话延迟：问完到出声 {t_play - asked:.2f}s（其中到拿到第一段语音 {t_clip - asked:.2f}s）")
                    check(t_play - asked < 4.0, f"接话延迟在 4 秒内（实际 {t_play - asked:.2f}s）")

                # ③ 它说话时张三插话 → 应当立刻停下
                await asyncio.sleep(1.2)
                check(stage.playing, "插话前它正在说话")
                barge = time.monotonic()
                stage.speak(a2)
                for _ in range(200):
                    if stage.first("收到停止指令", barge): break
                    await asyncio.sleep(0.02)
                t_stop = stage.first("收到停止指令", barge)
                check(t_stop is not None, "被插话后停下")
                if t_stop:
                    log(f"★ 让话延迟：对方开口到它停下 {t_stop - barge:.2f}s")
                    check(t_stop - barge < 1.5, f"让话延迟在 1.5 秒内（实际 {t_stop - barge:.2f}s）")
                await stage.wait_mic_empty(); await asyncio.sleep(7.0)

                st = (await http.get("/api/state")).json()
                print("\n----- 字幕 -----")
                for l in st["lines"]:
                    print(f"  {l['speaker']}：{l['text']}{' ［被打断］' if l['cut'] else ''}")
                print("----- 说话人 -----", [(s["name"], s["count"]) for s in st["speakers"]])
                humans = [l for l in st["lines"] if l["who"] == "human"]
                check(len(humans) >= 3, f"三段人声都进了字幕（实际 {len(humans)} 段）")
                check(any(l["cut"] for l in st["lines"] if l["who"] == "bot"), "被打断的那句话有标记")
                check(any("预算" in l["text"] for l in humans), "插话内容被听清")
                if st["ready"]["speakers"]:
                    check(len(st["speakers"]) == 2, f"两种嗓音被区分成两位说话人（实际 {len(st['speakers'])} 位）")
                    ids = [l["speaker_id"] for l in humans[:3]]
                    check(len(ids) == 3 and ids[0] == ids[2] != ids[1], f"第一句和插话是同一个人，提问是另一个人（{ids}）")
                else:
                    print("  - 声纹模型还没装好，跳过说话人区分的检查")
                stage.close()
            await http.post("/api/meeting/stop")
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
        err = server.stderr.read().decode(errors="replace")
        if "Traceback" in err:
            print("----- 后台报错 -----\n" + err[-3000:])
            failures.append("后台有报错")
    print("\n结果：" + ("全部通过" if not failures else f"{len(failures)} 项没过：" + "；".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
