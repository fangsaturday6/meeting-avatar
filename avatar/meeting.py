"""会议状态：字幕、说话人、后台任务、数字人状态。所有变化都通过事件广播给界面，并随时存档。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Callable

from .config import Settings
from .speakers import SpeakerBook

if TYPE_CHECKING:
    from .archive import Archive

log = logging.getLogger("avatar.meeting")

# 数字人状态。off=未开会 starting=正在入会 listening=在听 thinking=在想 waiting=举手等空档 speaking=在说
STATUSES = ("off", "starting", "listening", "thinking", "waiting", "speaking")


@dataclass
class Line:
    id: int
    t: float
    who: str                 # human | bot | note
    text: str
    speaker_id: int | None = None
    speaker: str = ""
    cut: bool = False        # 数字人这句话被打断了

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    id: int
    kind: str                # 查资料 | 查代码 | 做设计
    brief: str
    status: str = "running"  # running | done | failed
    progress: str = ""
    result: str = ""
    artifact: str = ""       # 设计稿的访问地址
    started: float = field(default_factory=time.time)
    ended: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class Meeting:
    save_delay = 1.0     # 内容变化后最多攒多久写一次盘

    def __init__(self, settings: Settings | None = None, archive: "Archive | None" = None):
        self.settings = settings or Settings()
        self.archive = archive                  # 不给就不存档（单元测试用）
        self.record_id = ""                     # 这场会的存档编号，也是文件名
        self.ended_at = 0.0
        self.title = ""                         # 会后纪要的标题和正文（整理出来之前是空的）
        self.summary = ""
        self.on_settings: Callable[[list[str], str], None] | None = None   # 设置变了（谁改的都算）之后回调，用来存盘
        self._save_timer: asyncio.TimerHandle | None = None
        self.speakers = SpeakerBook()
        self.lines: list[Line] = []
        self.tasks: list[Task] = []
        self.status = "off"
        self.muted = False
        self.brain_ok = True
        self.stage_online = False
        self.humans_speaking = False
        self.started_at = 0.0
        self.board_task_id: int | None = None   # 资料板当前展示哪个任务
        self._subs: set[asyncio.Queue] = set()
        self._line_id = 0
        self._task_id = 0

    # ---- 事件 ----
    def subscribe(self, maxsize: int = 500) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def emit(self, type_: str, **data) -> None:
        ev = {"type": type_, **data}
        for q in self._subs:
            if q.full():  # 界面太慢就丢最旧的，绝不阻塞会议
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(ev)

    # ---- 生命周期 ----
    @property
    def active(self) -> bool:
        return self.status != "off"

    def reset(self) -> None:
        self.save()      # 上一场还没落盘的改动先写完，再清空
        self.speakers = SpeakerBook()
        self.lines.clear()
        self.tasks.clear()
        self.board_task_id = None
        self.started_at = time.time()
        self.ended_at = 0.0
        self.title = self.summary = ""
        self.record_id = self.archive.new_id(self.started_at) if self.archive else ""
        self.emit("snapshot", state=self.snapshot())

    def set_status(self, status: str) -> None:
        assert status in STATUSES, status
        if status != self.status:
            self.status = status
            if status == "off":
                self.ended_at = time.time()
            self.emit("status", status=status)
            if status == "off":
                self.save()

    # ---- 存档 ----
    def _save_soon(self) -> None:
        if self.archive is None or self._save_timer is not None:
            return
        try:
            self._save_timer = asyncio.get_running_loop().call_later(self.save_delay, self.save)
        except RuntimeError:     # 不在事件循环里：直接写
            self.save()

    def save(self) -> None:
        """把这场会现在的全部内容写盘。存不上只记日志，绝不影响会议。"""
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        if self.archive is None:
            return
        try:
            created = self.archive.save(self)
            if created or (not self.active and self.record_id):
                self.emit("meetings", meetings=self.archive.recent())
        except OSError:
            log.exception("会议记录没存上")

    def set_minutes(self, title: str, summary: str) -> None:
        self.title, self.summary = title, summary
        self.save()

    # ---- 设置 ----
    def change_settings(self, patch: dict, source: str = "user") -> list[str]:
        """改设置的唯一入口：界面、接口、数字人自己（听到「少说点」）都走这里。source 是谁改的：user | brain。"""
        changed = self.settings.update(patch)
        if changed:
            if self.on_settings:
                self.on_settings(changed, source)
            self.emit("settings", settings=self.settings.to_dict(), changed=changed)
        return changed

    def set_flag(self, name: str, value: bool) -> None:
        if getattr(self, name) != value:
            setattr(self, name, value)
            self.emit("flag", name=name, value=value)

    # ---- 字幕 ----
    def add_line(self, who: str, text: str, speaker_id: int | None = None) -> Line:
        self._line_id += 1
        name = self.settings.name if who == "bot" else (self.speakers.name_of(speaker_id) if who == "human" else "")
        line = Line(id=self._line_id, t=time.time(), who=who, text=text, speaker_id=speaker_id, speaker=name)
        self.lines.append(line)
        self.emit("line", line=line.to_dict())
        self._save_soon()
        return line

    def update_line(self, line: Line, text: str | None = None, cut: bool | None = None) -> None:
        if text is not None:
            line.text = text
        if cut is not None:
            line.cut = cut
        self.emit("line", line=line.to_dict())
        self._save_soon()

    def rename_speaker(self, speaker_id: int, name: str) -> dict:
        sp = self.speakers.rename(speaker_id, name)
        for line in self.lines:
            if line.who == "human" and line.speaker_id == speaker_id:
                line.speaker = sp.name
        self.emit("speaker", speaker=sp.to_dict())
        self._save_soon()
        return sp.to_dict()

    # ---- 任务 ----
    def add_task(self, kind: str, brief: str) -> Task:
        self._task_id += 1
        task = Task(id=self._task_id, kind=kind, brief=brief)
        self.tasks.append(task)
        self.emit("task", task=task.to_dict())
        self._save_soon()
        return task

    def update_task(self, task: Task, **changes) -> None:
        for k, v in changes.items():
            setattr(task, k, v)
        if task.status != "running" and not task.ended:
            task.ended = time.time()
        self.emit("task", task=task.to_dict())
        self._save_soon()

    def get_task(self, task_id: int) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def show_on_board(self, task_id: int | None) -> None:
        self.board_task_id = task_id
        self.emit("board", task_id=task_id)

    @property
    def working(self) -> bool:
        return any(t.status == "running" for t in self.tasks)

    # ---- 全量 ----
    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "muted": self.muted,
            "brain_ok": self.brain_ok,
            "stage_online": self.stage_online,
            "humans_speaking": self.humans_speaking,
            "started_at": self.started_at,
            "record_id": self.record_id,
            "settings": self.settings.to_dict(),
            "speakers": self.speakers.snapshot(),
            "lines": [l.to_dict() for l in self.lines[-300:]],
            "tasks": [t.to_dict() for t in self.tasks],
            "board_task_id": self.board_task_id,
        }
