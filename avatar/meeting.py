"""会议状态：字幕、说话人、后台任务、数字人状态。所有变化都通过事件广播给界面。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field

from .config import Settings
from .speakers import SpeakerBook

# 数字人状态。off=未开会 listening=在听 thinking=在想 waiting=举手等空档 speaking=在说
STATUSES = ("off", "listening", "thinking", "waiting", "speaking")


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
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
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
        self.speakers = SpeakerBook()
        self.lines.clear()
        self.tasks.clear()
        self.board_task_id = None
        self.started_at = time.time()
        self.emit("snapshot", state=self.snapshot())

    def set_status(self, status: str) -> None:
        assert status in STATUSES, status
        if status != self.status:
            self.status = status
            self.emit("status", status=status)

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
        return line

    def update_line(self, line: Line, text: str | None = None, cut: bool | None = None) -> None:
        if text is not None:
            line.text = text
        if cut is not None:
            line.cut = cut
        self.emit("line", line=line.to_dict())

    def rename_speaker(self, speaker_id: int, name: str) -> dict:
        sp = self.speakers.rename(speaker_id, name)
        for line in self.lines:
            if line.who == "human" and line.speaker_id == speaker_id:
                line.speaker = sp.name
        self.emit("speaker", speaker=sp.to_dict())
        return sp.to_dict()

    # ---- 任务 ----
    def add_task(self, kind: str, brief: str) -> Task:
        self._task_id += 1
        task = Task(id=self._task_id, kind=kind, brief=brief)
        self.tasks.append(task)
        self.emit("task", task=task.to_dict())
        return task

    def update_task(self, task: Task, **changes) -> None:
        for k, v in changes.items():
            setattr(task, k, v)
        if task.status != "running" and not task.ended:
            task.ended = time.time()
        self.emit("task", task=task.to_dict())

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
            "settings": self.settings.to_dict(),
            "speakers": self.speakers.snapshot(),
            "lines": [l.to_dict() for l in self.lines[-300:]],
            "tasks": [t.to_dict() for t in self.tasks],
            "board_task_id": self.board_task_id,
        }
