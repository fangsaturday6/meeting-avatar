"""接口层：REST + WebSocket + 静态页面。界面、测试和 AI Agent 走同一套接口。"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import models
from .config import DESIGN_DIR, SCENES, VOICES, Settings
from .ears import Ears
from .fast_brain import FastBrain
from .floor import Floor
from .meeting import Meeting
from .mouth import Mouth
from .slow_brain import SlowBrain
from .textutil import TASK_KINDS

log = logging.getLogger("avatar.api")
WEB = Path(__file__).parent / "web"


class StagePlayer:
    """把语音片段送到形象页播放。没有形象页连着时，当作瞬间播完（纯文字模式）。"""

    def __init__(self) -> None:
        self.ws: WebSocket | None = None
        self.floor: Floor | None = None

    async def play(self, clip_id: int, audio: bytes, mime: str, text: str) -> None:
        ws = self.ws
        if ws is not None:
            head = json.dumps({"id": clip_id, "mime": mime, "text": text}, ensure_ascii=False).encode()
            try:
                # 头和音频放在同一帧里，避免和「停止」指令交错
                await ws.send_bytes(struct.pack(">I", len(head)) + head + audio)
                return
            except Exception:  # noqa: BLE001
                self.ws = None
        self.floor.on_clip_started(clip_id)
        self.floor.on_clip_ended(clip_id)

    async def stop(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.send_json({"type": "stop"})
            except Exception:  # noqa: BLE001
                self.ws = None


class Hub:
    """把所有部件装到一起。测试时可以换成假部件。"""

    def __init__(self, settings: Settings | None = None, brain=None, mouth=None, worker=None,
                 save_settings: bool = True):
        self.settings = settings or Settings.load()
        self.save_settings = save_settings
        self.meeting = Meeting(self.settings)
        self.player = StagePlayer()
        self.mouth = mouth or Mouth(self.settings)
        self.brain = brain or FastBrain(self.settings, recap=self._recap)
        self.worker = worker or SlowBrain(self.settings)
        self.floor = Floor(self.meeting, self.brain, self.mouth, self.player, self.worker)
        self.player.floor = self.floor
        self.ears = Ears(self.floor.on_speech_start, self.floor.on_speech_end, self.floor.on_utterance)
        self.ready = {"ears": False, "speakers": False, "local_voice": False}

    def _recap(self) -> str:
        return "\n".join(f"[{l.speaker}] {l.text}" for l in self.meeting.lines[-60:] if l.who != "note")

    async def load(self) -> None:
        """加载本地模型。缺模型不报错，只是少一项能力。"""
        try:
            if not models.missing(include_optional=False):
                await asyncio.to_thread(self.ears.load)
                self.ready["ears"] = True
                self.ready["speakers"] = self.ears.has_speaker_model
            if hasattr(self.mouth, "load"):
                await asyncio.to_thread(self.mouth.load)
                self.ready["local_voice"] = bool(getattr(self.mouth, "local_ready", False))
        except Exception:  # noqa: BLE001
            log.exception("本地模型加载失败")
        self.meeting.emit("ready", ready=self.ready)

    def state(self) -> dict:
        return {**self.meeting.snapshot(), "ready": self.ready,
                "voices": VOICES, "scenes": SCENES, "task_kinds": list(TASK_KINDS)}

    async def close(self) -> None:
        await self.floor.stop()
        self.ears.close()
        if hasattr(self.mouth, "close"):
            self.mouth.close()


# ---------- 请求体 ----------
class UtteranceIn(BaseModel):
    speaker: str = Field(min_length=1, max_length=30)
    text: str = Field(min_length=1, max_length=2000)


class SayIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class MuteIn(BaseModel):
    muted: bool


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)


class TaskIn(BaseModel):
    kind: str
    brief: str = Field(min_length=2, max_length=1000)


class BoardIn(BaseModel):
    task_id: int | None = None


def create_app(hub: Hub | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hub = hub or Hub()
        loader = asyncio.create_task(app.state.hub.load())
        yield
        loader.cancel()
        await app.state.hub.close()

    app = FastAPI(title="会议数字人", lifespan=lifespan)

    def h() -> Hub:
        return app.state.hub

    def need_meeting() -> None:
        if not h().meeting.active:
            raise HTTPException(409, "会议还没开始")

    # ---------- 页面 ----------
    @app.get("/", include_in_schema=False)
    async def console_page():
        return FileResponse(WEB / "console.html")

    @app.get("/stage", include_in_schema=False)
    async def stage_page():
        return FileResponse(WEB / "stage.html")

    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/designs", StaticFiles(directory=DESIGN_DIR, html=True), name="designs")
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    # ---------- 状态 ----------
    @app.get("/api/state")
    async def get_state():
        return h().state()

    # ---------- 会议 ----------
    @app.post("/api/meeting/start")
    async def meeting_start():
        hub = h()
        hub.ears.reset() if hub.ready["ears"] else None
        await hub.floor.start()
        return hub.state()

    @app.post("/api/meeting/stop")
    async def meeting_stop():
        await h().floor.stop()
        return h().state()

    @app.post("/api/utterance")
    async def utterance(body: UtteranceIn):
        """以文字方式注入一句与会者发言（没有麦克风时、测试时、AI Agent 参会时用）。"""
        need_meeting()
        line = await h().floor.on_utterance(body.text, speaker_name=body.speaker.strip())
        if line is None:
            raise HTTPException(400, "这句话是空的")
        return line.to_dict()

    @app.post("/api/say")
    async def say(body: SayIn):
        need_meeting()
        h().floor.say(body.text)
        return {"ok": True}

    @app.post("/api/hush")
    async def hush():
        need_meeting()
        await h().floor.hush()
        return {"ok": True}

    @app.post("/api/mute")
    async def mute(body: MuteIn):
        await h().floor.set_muted(body.muted)
        return {"muted": h().meeting.muted}

    @app.patch("/api/speakers/{speaker_id}")
    async def rename_speaker(speaker_id: int, body: RenameIn):
        if speaker_id not in h().meeting.speakers.speakers:
            raise HTTPException(404, "没有这位说话人")
        return h().meeting.rename_speaker(speaker_id, body.name)

    @app.post("/api/tasks")
    async def create_task(body: TaskIn):
        need_meeting()
        if body.kind not in TASK_KINDS:
            raise HTTPException(400, "任务类型只能是：" + "、".join(TASK_KINDS))
        return h().floor.dispatch(body.kind, body.brief.strip()).to_dict()

    @app.post("/api/board")
    async def board(body: BoardIn):
        if body.task_id is not None and h().meeting.get_task(body.task_id) is None:
            raise HTTPException(404, "没有这个任务")
        h().meeting.show_on_board(body.task_id)
        return {"task_id": body.task_id}

    @app.patch("/api/settings")
    async def patch_settings(body: dict):
        hub = h()
        try:
            changed = hub.settings.update(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if changed:
            if hub.save_settings:
                hub.settings.save()
            if "voice" in changed and hasattr(hub.mouth, "reset_online"):
                hub.mouth.reset_online()
            if hub.meeting.active and {"name", "topic"} & set(changed):
                # 会议中途改了名字或背景：下次说话前告诉大脑
                hub.floor.carry.append(
                    f"（设置更新：你的名字是「{hub.settings.name}」；会议背景：{hub.settings.topic.strip() or '暂无'}）")
            hub.meeting.emit("settings", settings=hub.settings.to_dict(), changed=changed)
        return {"settings": hub.settings.to_dict(), "changed": changed}

    # ---------- 事件流 ----------
    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        await ws.accept()
        hub = h()
        q = hub.meeting.subscribe()
        try:
            await ws.send_json({"type": "snapshot", "state": hub.state()})
            while True:
                await ws.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.meeting.unsubscribe(q)

    # ---------- 形象页音频通道 ----------
    @app.websocket("/ws/audio")
    async def ws_audio(ws: WebSocket):
        await ws.accept()
        hub = h()
        old, hub.player.ws = hub.player.ws, ws
        if old is not None:      # 只允许一个页面负责收音和播放，新开的顶替旧的
            try:
                await old.send_json({"type": "replaced"})
                await old.close()
            except Exception:  # noqa: BLE001
                pass
        hub.meeting.set_flag("stage_online", True)
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    if hub.meeting.active:
                        hub.ears.feed(msg["bytes"])
                elif msg.get("text"):
                    try:
                        ev = json.loads(msg["text"])
                    except ValueError:
                        continue
                    if ev.get("type") == "clip_started":
                        hub.floor.on_clip_started(int(ev.get("id", 0)))
                    elif ev.get("type") == "clip_ended":
                        hub.floor.on_clip_ended(int(ev.get("id", 0)))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if hub.player.ws is ws:
                hub.player.ws = None
                hub.meeting.set_flag("stage_online", False)
                if hub.ready["ears"]:
                    hub.ears.reset()

    return app
