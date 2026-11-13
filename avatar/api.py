"""接口层：REST + WebSocket + 静态页面。界面、测试和 AI Agent 走同一套接口。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import numpy as np
from pydantic import BaseModel, Field

from . import connect, glossary, knowledge, mcp_server, models
from .archive import NO_CONTENT, Archive, checked_citations, dialogue_of, split_minutes
from .audio_in import AudioIn
from .config import DESIGN_DIR, ENGAGEMENTS, KEEP_AUDIO, KEEP_HOURS, ROLES, SCENES, VOICES, Settings
from .ears import Ears
from .fast_brain import FastBrain
from .floor import Floor
from .meeting import Meeting
from .mouth import Mouth
from .recluster import Item, recluster
from .recorder import AudioStore
from .slow_brain import SlowBrain
from .textutil import TASK_KINDS

log = logging.getLogger("avatar.api")
MINUTES_MIN_LINES = 3     # 别人至少说了这么多句，结束时才自动整理纪要
BACKFILL = 10             # 启动时最多给这么多场没有纪要的旧会补纪要
MIC_SILENT_AFTER = 3.0    # 形象页连着、麦克风的声音却这么多秒没送来，就算它听不到了（正常每 40 毫秒一包）
WEB = Path(__file__).parent / "web"
FIGURES = WEB / "models"  # 3D 形象文件：<名字>.glb，另可带 <名字>.<状态>.glb 作为那种状态的动作
POSES = ("off", "starting", "listening", "thinking", "waiting", "speaking")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def from_another_site(origin: str | None) -> bool:
    """这个请求是不是别的网站的页面发来的。

    浏览器发请求会带上页面所在的网站（Origin）；命令行和智能体不带。会议记录只给本机的程序读，
    不给你碰巧打开的某个网页读——包括把自己的域名指到 127.0.0.1 来冒充本机的那种。
    """
    if origin is None:
        return False
    return (urlsplit(origin).hostname or "") not in LOCAL_HOSTS


def list_figures(folder: Path) -> list[dict]:
    """目录里有哪些形象，各带了哪些状态的动作文件。"""
    figures: dict[str, dict] = {}
    files = sorted(folder.glob("*.glb")) if folder.is_dir() else []
    for f in files:                                   # 先认形象本体，再把动作文件归到它名下
        if "." not in f.stem:
            figures[f.stem] = {"name": f.stem, "url": f"/static/models/{f.name}", "motions": {}}
    for f in files:
        name, _, pose = f.stem.partition(".")
        if pose in POSES and name in figures:
            figures[name]["motions"][pose] = f"/static/models/{f.name}"
    return list(figures.values())


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
                 save_settings: bool = True, archive: Archive | None = None, agents=None,
                 audio_store: AudioStore | None = None):
        self.settings = settings or Settings.load()
        self.save_settings = save_settings
        self.archive = archive or Archive()
        self.agents = agents or connect.Connector()     # 把会议记录登记给本机的 Claude Code / Codex
        self.meeting = Meeting(self.settings, self.archive)
        self.player = StagePlayer()
        self.mouth = mouth or Mouth(self.settings)
        self.brain = brain or FastBrain(self.settings, recap=self._recap, briefing=self.briefing)
        self.worker = worker or SlowBrain(self.settings)
        self.floor = Floor(self.meeting, self.brain, self.mouth, self.player, self.worker)
        self.player.floor = self.floor
        self.meeting.on_settings = self._settings_changed
        self.minuting: dict[str, asyncio.Task] = {}     # 正在整理纪要的会议
        self.polishing: dict[str, asyncio.Task] = {}    # 纪要已经有了、整理稿还在写的会议
        self._bg: set[asyncio.Task] = set()
        self._projects: list[knowledge.Project] = []    # 它认识的项目（扫盘要一两秒，只在后台线程里刷新）
        self.ears = Ears(self.floor.on_speech_start, self.floor.on_speech_end, self._heard)
        self.floor.voice_probe = self.ears.who
        self.audio_store = audio_store or AudioStore()
        self.audio = AudioIn(self.meeting, self.ears, self.audio_store)    # 形象页送来的声音都从这里进
        self.ready = {"ears": False, "speakers": False, "local_voice": False}

    async def _heard(self, text: str, embedding=None, seconds: float = 10.0, at: float | None = None):
        """耳朵认出一句话：交给接话逻辑，再把识别结果连同声纹记进这场会的录音目录。"""
        line = await self.floor.on_utterance(text, embedding, seconds=seconds, at=at)
        if self.meeting.active:
            self.audio.heard(text, embedding, seconds, at, line)
        return line

    async def recheck_speakers(self) -> int:
        """散会后拿整场的声纹重新分一次是谁说的（recluster.py），改完再整理纪要。返回改了几句。"""
        m = self.meeting
        if not m.record_id or m.active:
            return 0
        rows = await asyncio.to_thread(self.audio_store.rows, m.record_id, "heard")
        heard = {r["line_id"]: r for r in rows if r.get("line_id") and r.get("emb")}
        items = []
        for line in m.lines:
            row = heard.get(line.id)
            if line.who != "human" or row is None:
                continue
            sp = m.speakers.speakers.get(line.speaker_id)
            items.append(Item(line.id, float(row.get("seconds") or line.seconds),
                              np.frombuffer(base64.b64decode(row["emb"]), dtype=np.float32), line.speaker_id,
                              fixed=line.pinned or bool(sp and sp.locked)))
        try:
            changes = await asyncio.to_thread(recluster, items)
        except Exception:  # noqa: BLE001
            log.exception("会后重新核对说话人出错，保持会上的结果")
            return 0
        before = {l.id: l.speaker for l in m.lines}
        n = m.apply_speakers(changes)
        if n:
            moves = Counter(f"{before[l.id]}→{l.speaker}" for l in m.lines if l.id in changes and before[l.id] != l.speaker)
            log.info("会后重新核对说话人：改了 %d 句（%s）", n, "、".join(f"{k} {v}句" for k, v in moves.items()))
            m.save()
        return n

    def end_meeting_audio(self) -> None:
        """会开完了：录音收尾。"""
        self.audio.finish()

    def _after_minutes(self, meeting_id: str, written: bool) -> None:
        """「整理完就删」：整理稿写进记录了（或者这场会没什么可整理的），录音就不留了。"""
        if written and self.settings.keep_audio == "until_polished" and meeting_id != self.audio._rec_id:
            self.audio_store.drop_audio(meeting_id)
            log.info("整理完了，按设置删掉这场会的录音：%s", meeting_id)

    def sweep_audio(self) -> list[str]:
        """「留 2 小时」：散会超过 2 小时的录音删掉声音。要读盘，在线程里调。"""
        if self.settings.keep_audio != "two_hours":
            return []
        dropped = self.audio_store.sweep(KEEP_HOURS * 3600, time.time(), skip=self.audio._rec_id)
        if dropped:
            log.info("录音留满 %.0f 小时，删掉声音：%s", KEEP_HOURS, "、".join(dropped))
        return dropped

    async def _sweep_forever(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.sweep_audio)
            except Exception:  # noqa: BLE001
                log.exception("清理过期录音出错")
            await asyncio.sleep(600)

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

    def _store_minutes(self, meeting_id: str, title: str, summary: str, polished: str = "") -> None:
        m = self.meeting
        if m.record_id == meeting_id and not m.active:
            m.set_minutes(title, summary, polished)     # 刚结束的这一场：之后再改名、重存也不会丢
        else:
            self.archive.set_minutes(meeting_id, title, summary, polished)

    async def _minutes(self, meeting_id: str, dialogue: str) -> None:
        """纪要和整理稿（书面化的对话）同时开工。纪要快，写完就算整理好了；整理稿慢，好了再补进同一份记录。"""
        state = "failed"
        if stale := self.polishing.pop(meeting_id, None):
            stale.cancel()          # 重新整理：上一次还没写完的整理稿不要了
        polish = getattr(self.worker, "polish", None)
        draft = asyncio.create_task(polish(dialogue)) if polish else None
        try:
            res = await self.worker.summarize(dialogue)
            if res.ok:
                title, summary = split_minutes(res.text)
                summary = checked_citations(summary, dialogue)
                state = "done" if summary else "empty"
                self._store_minutes(meeting_id, title, summary)
                if not summary or not draft:
                    self._after_minutes(meeting_id, True)      # 没有整理稿可等了
                if draft and summary:      # 没有纪要（整场都是杂音）就不要整理稿了
                    self.polishing[meeting_id] = asyncio.create_task(self._polished(meeting_id, title, summary, draft))
                    draft = None
            else:
                log.warning("纪要没整理成：%s", res.text[:100])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("整理纪要出错")
        finally:
            if draft:
                draft.cancel()
            if self.minuting.get(meeting_id) is asyncio.current_task():
                del self.minuting[meeting_id]       # 只摘自己的登记：别把紧接着发起的下一次整理摘掉
            self.meeting.emit("minutes", id=meeting_id, state=state)
            self.meeting.emit("meetings", meetings=self.archive.recent())

    async def _polished(self, meeting_id: str, title: str, summary: str, draft: asyncio.Task) -> None:
        """等整理稿写完，补进会议记录。没写成就算了：记录里还有逐句的对话。"""
        try:
            if polished := await draft:
                self._store_minutes(meeting_id, title, summary, polished)
                self.meeting.emit("meetings", meetings=self.archive.recent())    # 开着这场会的阅读页会重新读一遍
                self._after_minutes(meeting_id, True)
        except asyncio.CancelledError:
            draft.cancel()
            raise
        except Exception:  # noqa: BLE001
            log.exception("整理稿没写成")
        finally:
            if self.polishing.get(meeting_id) is asyncio.current_task():
                del self.polishing[meeting_id]

    async def load(self) -> None:
        """加载本地模型。缺模型不报错，只是少一项能力。"""
        if n := await asyncio.to_thread(self.audio_store.seal_all):
            log.info("上次没正常收尾的录音补封了 %d 块", n)
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
        self._spawn(self._sweep_forever())

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

    # ---------- 常用词 ----------
    @staticmethod
    def glossary_state() -> dict:
        """词表原文和里面有几个词。要读盘，在线程里调。"""
        text = glossary.read()
        return {"text": text, "count": len(glossary.parse(text))}

    async def glossary_changed(self) -> dict:
        """词表变了：告诉开着的窗口，返回新的词表。"""
        state = await asyncio.to_thread(self.glossary_state)
        self.meeting.emit("glossary", **state)
        return state

    async def remember_name(self, name: str) -> None:
        """主人亲手给说话人写的名字是对的写法，记进词表；会后整理时别人嘴里的同音字就能对上。
        声音里自己报的名字不走这里——那是识别出来的，字不一定对。"""
        if re.fullmatch(r"说话人\d+", name):
            return
        try:
            if await asyncio.to_thread(glossary.add, {name: ""}, "会上的人"):
                await self.glossary_changed()
        except OSError:
            log.exception("名字没记进常用词表")

    def state(self) -> dict:
        return {**self.meeting.snapshot(), "ready": self.ready, "meetings": self.archive.recent(),
                "minuting": list(self.minuting), "projects": self.projects_state(),
                "voices": VOICES, "scenes": SCENES, "roles": ROLES, "engagements": ENGAGEMENTS,
                "keep_audio_options": KEEP_AUDIO,
                "task_kinds": list(TASK_KINDS)}

    async def close(self) -> None:
        for t in [*self.minuting.values(), *self.polishing.values(), *self._bg]:
            t.cancel()
        await self.floor.stop()
        self.end_meeting_audio()
        self.meeting.save()
        self.ears.close()
        if hasattr(self.mouth, "close"):
            self.mouth.close()


class MicWatch:
    """盯着一个形象页的麦克风声音有没有在送来。

    耳机断开、麦克风被系统收走时，页面和后台的连接还在，只是不再有声音数据。这时要把状态里的
    mic_online 改成假（界面和 Agent 靠它知道「连着但听不到」），并清掉耳朵里说到一半的残留。
    """

    def __init__(self, hub: Hub, ws: WebSocket):
        self.hub, self.ws = hub, ws
        self.last = time.monotonic()        # 刚接通时页面还在开麦克风，先当它有声音
        self._set(True)
        self._task = asyncio.create_task(self._watch())

    def _mine(self) -> bool:
        return self.hub.player.ws is self.ws    # 被别的窗口顶替之后，就不归这个连接说了算

    def _set(self, online: bool) -> None:
        m = self.hub.meeting
        if not self._mine() or m.mic_online == online:
            return
        m.set_flag("mic_online", online)
        if m.active:
            log.info("麦克风的声音又送来了" if online else "麦克风没有声音送来了，它现在听不到")
        if not online and self.hub.ready["ears"]:
            self.hub.ears.reset()

    def heard(self) -> None:
        self.last = time.monotonic()
        self._set(True)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(MIC_SILENT_AFTER / 3)
            if time.monotonic() - self.last > MIC_SILENT_AFTER:
                self._set(False)

    def close(self) -> None:
        self._task.cancel()
        self._set(False)


# ---------- 请求体 ----------
class UtteranceIn(BaseModel):
    speaker: str = Field(min_length=1, max_length=30)
    text: str = Field(min_length=1, max_length=2000)


class SayIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class MuteIn(BaseModel):
    muted: bool


class RecordIn(BaseModel):
    on: bool


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)


class MeetingIn(BaseModel):
    private: bool


class LineSpeakerIn(BaseModel):
    speaker_id: int


class TaskIn(BaseModel):
    kind: str
    brief: str = Field(min_length=2, max_length=1000)


class BoardIn(BaseModel):
    task_id: int | None = None


class AgentIn(BaseModel):
    connected: bool


class GlossaryIn(BaseModel):
    text: str = Field(max_length=glossary.TEXT_LIMIT)


class GlossaryImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=glossary.IMPORT_LIMIT)
    name: str = Field(default="", max_length=200)


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
        was_active = hub.meeting.active
        await hub.floor.start()
        if not was_active and hub.settings.announce:
            hub.floor.announce()
        return hub.state()

    @app.post("/api/meeting/stop")
    async def meeting_stop():
        hub = h()
        was_active = hub.meeting.active
        await hub.floor.stop()
        hub.end_meeting_audio()
        if was_active:
            await hub.recheck_speakers()
        # 说得够多才值得整理纪要；三两句的测试不整理（想要可以手动点）
        if was_active and sum(l.who == "human" for l in hub.meeting.lines) >= MINUTES_MIN_LINES:
            hub.make_minutes(hub.meeting.record_id)
        return hub.state()

    # ---------- 形象 ----------
    @app.get("/api/figures")
    def get_figures():
        return {"figures": list_figures(FIGURES)}

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

    @app.patch("/api/meetings/{meeting_id}")
    async def patch_meeting(meeting_id: str, body: MeetingIn):
        """给一场会设「不给智能体看」：MCP、下一场会的开场背景、会上的「查会议」都读不到它。"""
        hub = h()
        if meeting_id == "latest":
            meeting_id = next((m["id"] for m in hub.archive.recent(1)), "")
        if meeting_id and meeting_id == hub.meeting.record_id:
            hub.meeting.set_private(body.private)       # 正在开（或刚开完）的这场：之后重存也带着
        elif not hub.archive.set_private(meeting_id, body.private):
            raise HTTPException(404, "没有这场会议的记录")
        rows = hub.archive.recent()
        hub.meeting.emit("meetings", meetings=rows)
        return next((r for r in rows if r["id"] == meeting_id), {"id": meeting_id, "private": body.private})

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

    # ---------- 接给我的智能体 ----------
    @app.get("/api/agents")
    async def get_agents(request: Request):
        """会议记录接给了哪些智能体，以及别的智能体怎么接（guide 是写给智能体看的一段话）。"""
        base = str(request.base_url).rstrip("/")
        clients = await asyncio.to_thread(h().agents.clients)      # 要起子进程去问，不能卡住正在开的会
        return {"clients": clients, "guide": connect.guide(base),
                "mcp": {"url": f"{base}/mcp", "command": connect.SERVER}, "api": f"{base}/api/meetings"}

    @app.put("/api/agents/{client_id}")
    async def set_agent(client_id: str, body: AgentIn):
        """把会议记录接给这个智能体（Claude Code、Codex），或者取消。"""
        hub = h()
        if client_id not in connect.CLIENTS:
            raise HTTPException(404, "没有这个智能体")
        try:
            await asyncio.to_thread(hub.agents.set, client_id, body.connected)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        clients = await asyncio.to_thread(hub.agents.clients)
        hub.meeting.emit("agents", clients=clients)
        return {"clients": clients}

    # ---------- 常用词 ----------
    @app.get("/api/glossary")
    async def get_glossary():
        """常用词表：`text` 是原文（一行一个词，后面可以跟「：说明」，# 开头是注释），`count` 是里面有几个词。"""
        return await asyncio.to_thread(h().glossary_state)

    @app.put("/api/glossary")
    async def put_glossary(body: GlossaryIn):
        """把整张词表换成 `text`。下一次整理纪要和整理稿就用新的。"""
        await asyncio.to_thread(glossary.save, body.text)
        return await h().glossary_changed()

    @app.post("/api/glossary/import")
    async def import_glossary(body: GlossaryImportIn):
        """把一份名单并进词表：只追加还没有的词，已有的行不动。`name` 是文件名，按后缀认格式
        （.csv / .tsv 表格、.yaml 公司本体，其余当一行一个）。`added` 是新加了几个。"""
        name = Path(body.name.replace("\\", "/")).name
        try:
            terms = await asyncio.to_thread(glossary.from_file, name, body.text)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        added = await asyncio.to_thread(glossary.add, terms, f"从 {name} 导入" if name else "导入")
        return {**await h().glossary_changed(), "added": len(added)}

    @app.post("/mcp", include_in_schema=False)
    async def mcp(request: Request):
        """HTTP 方式的 MCP：和 mcp_server.py 是同一套只读工具，给不方便拉起本地命令的智能体用。"""
        if from_another_site(request.headers.get("origin")):
            raise HTTPException(403, "会议记录只给这台电脑上的程序读")
        try:
            msg = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "不是合法的 JSON"}}, 400)
        hub = h()
        if hub.meeting.active:
            hub.meeting.save()      # 正在开的这场：先把还没落盘的写下去，读到的就是最新的
        batch = msg if isinstance(msg, list) else [msg]
        replies = [r for m in batch if (r := await asyncio.to_thread(mcp_server.respond, m, hub.archive)) is not None]
        if not replies:
            return Response(status_code=202)       # 只有通知，没有要回的
        return JSONResponse(replies if isinstance(msg, list) else replies[0])

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

    @app.post("/api/record")
    async def record(body: RecordIn):
        """「这段别记」/「可以记了」：on 为假时照样听、照样接话，但说的话不进会议记录、不录音。"""
        need_meeting()
        h().meeting.set_recording(body.on)
        return {"recording": h().meeting.recording}

    @app.patch("/api/speakers/{speaker_id}")
    async def rename_speaker(speaker_id: int, body: RenameIn):
        if speaker_id not in h().meeting.speakers.speakers:
            raise HTTPException(404, "没有这位说话人")
        speaker = h().meeting.rename_speaker(speaker_id, body.name)
        await h().remember_name(speaker["name"])
        return speaker

    @app.patch("/api/lines/{line_id}")
    async def reassign_line(line_id: int, body: LineSpeakerIn):
        try:
            return h().meeting.reassign_line(line_id, body.speaker_id).to_dict()
        except KeyError:
            raise HTTPException(404, "没有这句话或这位说话人") from None

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
        mic = MicWatch(hub, ws)
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    mic.heard()
                    if hub.meeting.active:
                        hub.audio.feed(msg["bytes"])
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
            mic.close()
            if hub.player.ws is ws:
                hub.player.ws = None
                hub.meeting.set_flag("stage_online", False)

    return app
