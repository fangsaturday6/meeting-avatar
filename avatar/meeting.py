"""会议状态：字幕、说话人、后台任务、数字人状态。所有变化都通过事件广播给界面，并随时存档。"""
from __future__ import annotations

import asyncio
import logging
import re
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
    seconds: float = 0.0     # 这句话说了多久（听来的才有；打字的、数字人说的是 0）
    pinned: bool = False     # 用户亲手把这句判给了某人：会后重新核对说话人时不动
    off: bool = False        # 有人说了「这段别记」之后说的：照样听、照样接话，但不进会议记录、不录音
    at: float | None = None  # 这句话从录音的第几秒开始（听来的才有），对得回 workspace/audio/<会议编号>/ 里的声音

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
        self.polished = ""                      # 会后书面化的对话（整理稿），格式和存档里的「对话」一样
        self.on_settings: Callable[[list[str], str], None] | None = None   # 设置变了（谁改的都算）之后回调，用来存盘
        self._save_timer: asyncio.TimerHandle | None = None
        self.speakers = SpeakerBook()
        self.lines: list[Line] = []
        self.tasks: list[Task] = []
        self.status = "off"
        self.muted = False
        self.brain_ok = True
        self.stage_online = False               # 有窗口接通了声音（负责收音和播放）
        self.mic_online = False                 # 那个窗口的麦克风声音确实在送过来（耳机断开时会停）
        self.mic_health = "ok"                  # ok | silent（送来的一直是数字静音：麦克风被关了、没权限）
        self.system_health = "ok"               # 「我在开线上会」时这台电脑放出的声音：ok | silent（一分钟都是零，多半没给系统录音权限）
        self.humans_speaking = False
        self.private = False                    # 这场会不给智能体看（MCP、下一场会的背景、查会议都读不到）
        self.recording = True                   # 有人说「这段别记」就是假，说「可以记了」再恢复
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
        self.title = self.summary = self.polished = ""
        self.private = False
        self.recording = True
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

    def set_minutes(self, title: str, summary: str, polished: str = "") -> None:
        self.title, self.summary, self.polished = title, summary, polished
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

    def set_recording(self, on: bool) -> None:
        self.set_flag("recording", on)
        self._save_soon()

    def set_private(self, private: bool) -> None:
        self.set_flag("private", private)
        self.save()

    def set_flag(self, name: str, value: bool) -> None:
        if getattr(self, name) != value:
            setattr(self, name, value)
            self.emit("flag", name=name, value=value)

    # ---- 字幕 ----
    def add_line(self, who: str, text: str, speaker_id: int | None = None, seconds: float = 0.0,
                 at: float | None = None) -> Line:
        self._line_id += 1
        name = self.settings.name if who == "bot" else (self.speakers.name_of(speaker_id) if who == "human" else "")
        line = Line(id=self._line_id, t=time.time(), who=who, text=text, speaker_id=speaker_id, speaker=name,
                    seconds=seconds, at=at, off=not self.recording)
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
        """改名。起的名字已经是另一位说话人的，说明两位是同一个人：并到那一位。"""
        wanted = " ".join(name.split())
        same = next((sp for sid, sp in self.speakers.speakers.items() if sid != speaker_id and sp.name == wanted), None)
        if same is not None:
            return self.merge_speakers(speaker_id, same.id)
        old = self.speakers.name_of(speaker_id)
        sp = self.speakers.rename(speaker_id, name)
        if self.polished and old != sp.name:    # 整理稿是成文的，行首的名字跟着改
            self.polished = re.sub(rf"(?m)^(\[[\d:]+\] ){re.escape(old)}：", lambda m: f"{m.group(1)}{sp.name}：",
                                   self.polished)
        for line in self.lines:
            if line.who == "human" and line.speaker_id == speaker_id:
                line.speaker = sp.name
        self.emit("speaker", speaker=sp.to_dict())
        self._save_soon()
        return sp.to_dict()

    def merge_speakers(self, src: int, dst: int) -> dict:
        """src 其实就是 dst：句子全归 dst，src 不再出现。"""
        sp = self.speakers.merge(src, dst)
        sp.locked = True
        for line in self.lines:
            if line.who == "human" and line.speaker_id == src:
                line.speaker_id, line.speaker = dst, sp.name
        self.emit("snapshot", state=self.snapshot())
        self._save_soon()
        return sp.to_dict()

    def reassign_line(self, line_id: int, speaker_id: int) -> Line:
        """这一句其实是另一个人说的。之后会后重新核对也不会再改它。没有这句或这位时抛 KeyError。"""
        line = next((l for l in self.lines if l.id == line_id and l.who == "human"), None)
        if line is None or speaker_id not in self.speakers.speakers:
            raise KeyError(line_id if line is None else speaker_id)
        self.apply_speakers({line_id: speaker_id})
        line.pinned = True
        return line

    def apply_speakers(self, changes: dict[int, int],
                       splits: dict[int, list[tuple[float, float, int, str]]] | None = None) -> int:
        """把这些句子改判给别的说话人（{行号: 说话人编号}，负数表示新建一位）。返回动了几句。

        splits：散会后按声纹切开的句子（turns.py），{行号: [(从这句第几秒, 到第几秒, 说话人编号, 文字), …]}，
        第一段沿用原来的行号，后面几段是新的行号，排在原来那句的位置上。
        改完重数每位说了几句；一句都不剩、又没被起过名字的说话人去掉。
        """
        created: dict[int, int] = {}

        def resolve(sid: int, fallback: int | None) -> int | None:
            if sid < 0:
                sid = created.setdefault(sid, self.speakers._new().id)
            return sid if sid in self.speakers.speakers else fallback

        n = 0
        out: list[Line] = []
        for line in self.lines:
            parts = (splits or {}).get(line.id) if line.who == "human" else None
            if parts and len(parts) > 1:
                for k, (a, b, sid, text) in enumerate(parts):
                    if k == 0:
                        piece = line
                    else:
                        self._line_id += 1
                        piece = Line(id=self._line_id, t=line.t + a, who="human", text="", off=line.off)
                    piece.speaker_id = resolve(sid, line.speaker_id)
                    piece.speaker = self.speakers.name_of(piece.speaker_id)
                    piece.text, piece.seconds = text, round(b - a, 3)
                    piece.at = None if line.at is None else round(line.at + a, 3)
                    out.append(piece)
                n += 1
                continue
            out.append(line)
            if line.who != "human" or line.id not in changes:
                continue
            sid = resolve(changes[line.id], None)
            if sid is None or sid == line.speaker_id:
                continue
            line.speaker_id, line.speaker = sid, self.speakers.name_of(sid)
            n += 1
        self.lines = out
        if n:
            counts = {sid: 0 for sid in self.speakers.speakers}
            for line in self.lines:
                if line.who == "human" and line.speaker_id in counts:
                    counts[line.speaker_id] += 1
            for sid, c in counts.items():
                if c == 0 and not self.speakers.speakers[sid].locked:
                    self.speakers.forget(sid)
                elif sid in self.speakers.speakers:
                    self.speakers.speakers[sid].count = c
            self.emit("snapshot", state=self.snapshot())
            self._save_soon()
        return n

    def rejoin_lines(self, merged: list[tuple[list[int], str, float]]) -> int:
        """散会后重听（rehear.py）：几句碎话换成重认出的一整句。[(行号们, 新的文字, 合起来多长), …]。
        留第一句的行号和起点，其余几句去掉。返回换了几段。"""
        drop: set[int] = set()
        n = 0
        for ids, text, seconds in merged:
            keep = next((l for l in self.lines if l.id == ids[0] and l.who == "human"), None)
            if keep is None or not text:
                continue
            keep.text, keep.seconds = text, round(seconds, 3)
            drop.update(ids[1:])
            n += 1
        if drop:
            self.lines = [l for l in self.lines if l.id not in drop]
            for sp in self.speakers.speakers.values():
                sp.count = sum(1 for l in self.lines if l.who == "human" and l.speaker_id == sp.id)
            self.emit("snapshot", state=self.snapshot())
        return n

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
            "mic_online": self.mic_online,
            "mic_health": self.mic_health,
            "system_health": self.system_health,
            "humans_speaking": self.humans_speaking,
            "private": self.private,
            "recording": self.recording,
            "started_at": self.started_at,
            "record_id": self.record_id,
            "settings": self.settings.to_dict(),
            "speakers": self.speakers.snapshot(),
            "lines": [l.to_dict() for l in self.lines[-300:]],
            "tasks": [t.to_dict() for t in self.tasks],
            "board_task_id": self.board_task_id,
        }
