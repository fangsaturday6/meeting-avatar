"""接口层：REST + WebSocket + 静态页面。界面、测试和 AI Agent 走同一套接口。"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import knowledge, models
from .archive import NO_CONTENT, Archive, dialogue_of, split_minutes
from .config import DESIGN_DIR, ENGAGEMENTS, ROLES, SCENES, VOICES, Settings
from .ears import Ears
from .fast_brain import FastBrain
from .floor import Floor
from .meeting import Meeting
from .mouth import Mouth
from .slow_brain import SlowBrain
from .textutil import TASK_KINDS

log = logging.getLogger("avatar.api")
MINUTES_MIN_LINES = 3     # 别人至少说了这么多句，结束时才自动整理纪要
BACKFILL = 10             # 启动时最多给这么多场没有纪要的旧会补纪要
WEB = Path(__file__).parent / "web"


FRESH = {"Cache-Control": "no-cache"}   # 每次都向后台确认有没有变（没变只回 304）


class FreshStatic(StaticFiles):
    """不让浏览器凭经验缓存页面脚本：后台更新之后，界面和脚本必须是同一版。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.update(FRESH)
        return resp


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
                 save_settings: bool = True, archive: Archive | None = None):
        self.settings = settings or Settings.load()
        self.save_settings = save_settings
        self.archive = archive or Archive()
        self.meeting = Meeting(self.settings, self.archive)
        self.player = StagePlayer()
        self.mouth = mouth or Mouth(self.settings)
        self.brain = brain or FastBrain(self.settings, recap=self._recap, briefing=self.briefing)
        self.worker = worker or SlowBrain(self.settings)
        self.floor = Floor(self.meeting, self.brain, self.mouth, self.player, self.worker)
        self.player.floor = self.floor
        self.meeting.on_settings = self._settings_changed
        self.minuting: dict[str, asyncio.Task] = {}     # 正在整理纪要的会议
        self._bg: set[asyncio.Task] = set()
        self._projects: list[knowledge.Project] = []    # 它认识的项目（扫盘要一两秒，只在后台线程里刷新）
        self.ears = Ears(self.floor.on_speech_start, self.floor.on_speech_end, self.floor.on_utterance)
        self.floor.voice_probe = self.ears.who
        self.ready = {"ears": False, "speakers": False, "local_voice": False}

    def _recap(self) -> str:
        return "\n".join(f"[{l.speaker}] {l.text}" for l in self.meeting.lines[-60:] if l.who != "note")

    # ---------- 它对你的了解 ----------
    def refresh_projects(self) -> list[knowledge.Project]:
        """重新认一遍项目。要读盘，只能在线程里调，别在事件循环里调（会卡住正在开的会）。"""
        self._projects = knowledge.everything(self.settings)
        return self._projects

    async def _refresh_projects(self) -> None:
        try:
            await asyncio.to_thread(self.refresh_projects)
        except Exception:  # noqa: BLE001
            log.exception("项目清单没刷新成")
        self.meeting.emit("projects", projects=self.projects_state())

    def briefing(self) -> str:
        """开会前告诉大脑的背景：今天几号、你有哪些项目、上次会议定了什么。"""
        s = self.settings
        week = "一二三四五六日"[time.localtime().tm_wday]
        parts = [f"【现在】{time.strftime('%Y年%m月%d日 %H:%M')}，星期{week}"]
        who = s.owner.strip() or "主人"
        projects = self.refresh_projects()        # briefing 是在线程里被调用的，可以读盘
        brief = knowledge.project_brief(s.project_dir) if s.project_dir else ""
        if brief:
            parts.append("【这场会聊的项目是什么】会上说“这个项目”“我们这个”，指的就是它。下面是它的简介：\n" + brief)
        if s.guarded:
            # 替主人开会且没放开：只带这场会的项目。别的项目、Claude 的记忆和会话、别的会的纪要一概不带，
            # 它不知道，会上的人也就问不出来
            main = next((p for p in projects if p.path == str(Path(s.project_dir).expanduser())), None) if s.project_dir else None
            if main:
                parts.append(f"【这场会聊的项目】{main.name}" + (f"：{main.about}" if main.about else "")
                             + "。细节你并不清楚，要答细节就派工查代码。别的项目和别的会议你不了解，有人问就说要问他本人。")
            return "\n\n".join(parts)
        if projects:
            main = knowledge.main_project(s, projects)
            parts.append(
                "【你了解的项目】" + (f"这场会主要讨论「{main}」。" if main else "")
                + f"下面是{who}电脑上的项目（最近动过的在前）。"
                "别人提到某个系统或功能时，先对一对是哪个项目；细节你并不清楚，要答细节就派工查代码。\n"
                + knowledge.for_brain(projects))
            sessions = knowledge.recent_sessions(projects)
            if sessions:
                parts.append(
                    f"【{who}最近让 Claude 做的事】每行是一次会话：日期、项目、标题。会上有人提到的事和哪条对得上，"
                    "它就在那个项目里（派工查代码时写上这个项目）；你只知道标题，不知道细节。\n"
                    + knowledge.sessions_text(sessions))
        rules = knowledge.owner_rules()
        if rules:
            parts.append(f"【{who}给他的 AI 定的规矩】下面是他写给 Claude 的长期要求。你是同一个 AI 的分身，"
                         "其中适用于开会的（语言、做事原则、偏好）你也照做；有人问起他的做事原则，可以据此回答。\n" + rules)
        memory = knowledge.memory_index(s.project_dir) if s.project_dir else ""
        if memory:
            parts.append(f"【{who}的 Claude 在这个项目里记下的事】每行是一条记忆的标题和提要，细节要派工查代码才看得到。\n" + memory)
        last = self.archive.last_minutes()     # 正在开的这场还没有纪要，不会取到自己
        if last:
            parts.append("【上一次会议的纪要】\n" + last + "\n有人说“上次说的那个”，多半指这些；要更早或更细的，派工查会议。")
        return "\n\n".join(parts)

    def _settings_changed(self, changed: list[str], source: str) -> None:
        if self.save_settings:
            self.settings.save()
        if "voice" in changed and hasattr(self.mouth, "reset_online"):
            self.mouth.reset_online()
        if {"project_dir", "projects_root"} & set(changed):
            self._spawn(self._refresh_projects())
        if not self.meeting.active or source == "brain":
            return
        # 会议中途改了设置：下次说话前告诉大脑（它的系统提示是开会时定下的）
        s = self.settings
        if {"name", "topic", "owner", "role"} & set(changed):
            who = f"你是{s.owner.strip()}的 AI 分身，" if s.owner.strip() else ""
            role = "他本人不在，你替他开会；" if s.role == "delegate" and s.owner.strip() else ""
            self.floor.carry.append(
                f"（设置更新：你的名字是「{s.name}」；{who}{role}会议背景：{s.topic.strip() or '暂无'}）")
        if "engagement" in changed:
            self.floor.note_engagement()
        if {"role", "open_book"} & set(changed) and s.guarded:
            # 它开会时拿到的背景已经在脑子里了，收不回来，只能明确告诉它从现在起闭口
            self.floor.carry.append("（从现在起，这场会之外的事——主人的其他项目、他最近在忙什么、别的会议上说过什么——"
                                    "一概不谈，也不去查；有人问就说要问他本人。）")

    def projects_state(self) -> dict:
        root = knowledge.root_of(self.settings)
        return {"root": str(root) if root else "", "names": [p.name for p in self._projects]}

    # ---------- 会后纪要 ----------
    def make_minutes(self, meeting_id: str) -> bool:
        """后台整理一场会的纪要，写回会议记录。已经在整理就不重复。返回有没有这场会。"""
        if meeting_id in self.minuting:
            return True
        text = self.archive.read(meeting_id)
        if text is None:
            return False
        task = asyncio.create_task(self._minutes(meeting_id, dialogue_of(text)))
        self.minuting[meeting_id] = task
        self.meeting.emit("minutes", id=meeting_id, state="running")
        return True

    async def _minutes(self, meeting_id: str, dialogue: str) -> None:
        state = "failed"
        try:
            res = await self.worker.summarize(dialogue)
            if res.ok:
                title, summary = split_minutes(res.text)
                state = "done" if summary else "empty"
                m = self.meeting
                if m.record_id == meeting_id and not m.active:
                    m.set_minutes(title, summary)      # 刚结束的这一场：之后再改名、重存也不会丢
                else:
                    self.archive.set_minutes(meeting_id, title, summary)
            else:
                log.warning("纪要没整理成：%s", res.text[:100])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("整理纪要出错")
        finally:
            if self.minuting.get(meeting_id) is asyncio.current_task():
                del self.minuting[meeting_id]       # 只摘自己的登记：别把紧接着发起的下一次整理摘掉
            self.meeting.emit("minutes", id=meeting_id, state=state)
            self.meeting.emit("meetings", meetings=self.archive.recent())

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
        await self._refresh_projects()       # 顺便把项目认一遍，开会时就不用等
        self._spawn(self._backfill_minutes())

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _backfill_minutes(self) -> None:
        """给还没有纪要的旧会补上。

        纪要平时是点「结束会议」时整理的；后台被重启、电脑合盖、当时网络不通的会就没有纪要。
        每次启动把最近这样的会补一遍，一场一场来；这次没整理成的（比如还是没网）下次启动再试。
        """
        if not hasattr(self.worker, "summarize"):
            return
        todo = [r["id"] for r in self.archive.recent(30)
                if not r["minutes"] and r["lines"] > MINUTES_MIN_LINES and r["id"] != self.meeting.record_id]
        for meeting_id in todo[:BACKFILL]:
            if self.make_minutes(meeting_id) and (task := self.minuting.get(meeting_id)):
                await asyncio.wait({task})
        self.meeting.emit("ready", ready=self.ready)

    def state(self) -> dict:
        return {**self.meeting.snapshot(), "ready": self.ready, "meetings": self.archive.recent(),
                "minuting": list(self.minuting), "projects": self.projects_state(),
                "voices": VOICES, "scenes": SCENES, "roles": ROLES, "engagements": ENGAGEMENTS,
                "task_kinds": list(TASK_KINDS)}

    async def close(self) -> None:
        for t in [*self.minuting.values(), *self._bg]:
            t.cancel()
        await self.floor.stop()
        self.meeting.save()
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
    @app.get("/stage", include_in_schema=False)      # 旧地址，保留
    async def stage_page():
        """主窗口：只有形象。"""
        return FileResponse(WEB / "stage.html", headers=FRESH)

    @app.get("/panel", include_in_schema=False)
    async def panel_page():
        """设置窗口：记录和设置。"""
        return FileResponse(WEB / "panel.html", headers=FRESH)

    DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/designs", FreshStatic(directory=DESIGN_DIR, html=True), name="designs")
    app.mount("/static", FreshStatic(directory=WEB), name="static")

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
        hub = h()
        was_active = hub.meeting.active
        await hub.floor.stop()
        # 说得够多才值得整理纪要；三两句的测试不整理（想要可以手动点）
        if was_active and sum(l.who == "human" for l in hub.meeting.lines) >= MINUTES_MIN_LINES:
            hub.make_minutes(hub.meeting.record_id)
        return hub.state()

    # ---------- 会议记录 ----------
    @app.get("/api/meetings")
    async def list_meetings():
        return h().archive.recent()

    @app.get("/api/meetings/{meeting_id}", response_class=PlainTextResponse)
    async def read_meeting(meeting_id: str):
        """一场会的完整对话（Markdown 文本）。meeting_id 写 latest 就是最近一场。"""
        hub = h()
        if meeting_id == "latest":
            meeting_id = next((m["id"] for m in hub.archive.recent(1)), "")
        if meeting_id == hub.meeting.record_id:
            hub.meeting.save()      # 正在开的这场：先把还没落盘的写下去，读到的就是最新的
        text = hub.archive.read(meeting_id)
        if text is None:
            raise HTTPException(404, "没有这场会议的记录")
        return text

    @app.post("/api/meetings/{meeting_id}/minutes", status_code=202)
    async def minutes(meeting_id: str):
        """整理（或重新整理）一场会的纪要。后台进行，完成后会议记录里多出「纪要」一节。"""
        hub = h()
        if meeting_id == "latest":
            meeting_id = next((m["id"] for m in hub.archive.recent(1)), "")
        if meeting_id == hub.meeting.record_id and hub.meeting.active:
            raise HTTPException(409, "这场会还没结束，结束后再整理纪要")
        if not hasattr(hub.worker, "summarize"):
            raise HTTPException(409, "这台机器上没有配置整理纪要的能力")
        if not hub.make_minutes(meeting_id):
            raise HTTPException(404, "没有这场会议的记录")
        return {"id": meeting_id, "state": "running"}

    @app.post("/api/utterance")
    async def utterance(body: UtteranceIn):
        """以文字方式注入一句与会者发言（没有麦克风时、测试时、AI Agent 参会时用）。"""
        need_meeting()
        speaker = " ".join(body.speaker.split())
        if not speaker:
            raise HTTPException(400, "要写上是谁说的")
        line = await h().floor.on_utterance(body.text, speaker_name=speaker)
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
            changed = hub.meeting.change_settings(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
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
