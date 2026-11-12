"""发言决策：什么时候想、什么时候开口、什么时候让话。

Floor 只认四个可替换的部件（大脑、嘴、播放通道、干活的），自己不碰音频、模型和网络，
所以可以用假部件完整测试。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Protocol

from .meeting import Line, Meeting, Task
from .textutil import (TASK_KINDS, ReplyStream, SentenceSplitter, echo_score, fix_name,
                       is_backchannel, similar)

log = logging.getLogger("avatar.floor")


@dataclass
class WorkResult:
    ok: bool
    text: str
    artifact: str = ""


class Brain(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def ask(self, prompt: str) -> AsyncIterator[str]: ...
    async def cancel(self) -> None: ...


class Mouth(Protocol):
    async def synth(self, text: str) -> tuple[bytes, str]: ...


class Player(Protocol):
    async def play(self, clip_id: int, audio: bytes, mime: str, text: str) -> None: ...
    async def stop(self) -> None: ...


class Worker(Protocol):
    async def run(self, task: Task, on_progress: Callable[[str], None]) -> WorkResult: ...


class _GiveUp(Exception):
    """一直有人在说话，等不到开口的空档。"""


@dataclass
class _Attempt:
    """数字人的一次发言尝试：从开始想，到说完或被放弃。"""
    prompt: str = ""
    sent: bool = False                 # 提示是否已经送进大脑
    phase: str = "thinking"            # thinking | waiting | speaking
    planned: list[str] = field(default_factory=list)   # 大脑想说的句子
    clips: dict[int, str] = field(default_factory=dict)  # 已送去播放的片段
    order: list[int] = field(default_factory=list)
    started: set[int] = field(default_factory=set)
    ended: set[int] = field(default_factory=set)
    synths: list[asyncio.Task] = field(default_factory=list)
    line: Line | None = None
    all_sent: bool = False
    aborted: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    queue: asyncio.Queue | None = None  # 这次发言的待播队列
    trigger_speaker: int | None = None  # 是谁的话引出了这次发言；None = 没人问，是它自己想说
    push: bool = False                  # 顶着说：周围一直有声音，不再因为有人声就停，只认「停一下」和按钮
    speak_started: float = 0.0
    fixed: bool = False                 # 不经大脑、照原文说的话（主持人让说的、续说的、「网络断了」）
    taken: int = 0                      # planned 里已经交给播放（或合成失败跳过）的句数
    closed: bool = False                # 大脑这一轮已经收尾，不会再有新句子
    echo: str | bool | None = None      # 学舌检查：None=还没看第一句，False=不是学舌，字符串=那句话还没抄完的部分

    def spoken(self) -> list[str]:
        return [self.clips[c] for c in self.order if c in self.started]

    @property
    def playing(self) -> bool:
        return any(c not in self.ended for c in self.started)


# 顶着说的时候，听到这些词（或自己的名字）才停
_STOP_WORDS = re.compile(r"停一下|停一停|停下|等一下|等等|等会|别说了|先别说|不要说了|闭嘴|安静|打住")


class Floor:
    stall_line = "不好意思，我这边网络有点卡，稍等一下。"   # 大脑连不上时，用本地语音先说这句
    stall_line_every = 45.0  # 这句话最多多久说一次
    offline_line = "我这边网络断了，暂时回答不了，恢复了你再叫我。"   # 试了几次都连不上时说这句
    brain_retries = 1       # 有人在等回答而大脑没答上来时，自动再试几次
    give_up_after = 12.0    # 举手最多等多久；等不到就把话咽回去
    early_cut = 1.5         # 开口后这么短时间内就被打断，算「刚开口就被打断」
    asr_grace = 0.35        # 别人刚说完时，等这么久让那句话的文字先到，免得抢答上一句
    first_token_timeout = 20.0
    listen_cap = 2.5        # 说话的人还没停时，最多再听这么久才开始想（一句话常被切成几段）
    listen_cap_named = 1.0  # 被叫了名字时少等一会儿
    filler_after = 2.5      # 被点名后这么久还没想出第一句，先垫一句，免得像没听见
    fillers = ("嗯，我想一下。", "稍等，我想想。", "嗯，让我想想。")
    resume_window = 6.0     # 被打断后这么久之内对方只是附和了一声，就接着把话说完
    max_restarts = 6        # 还没开口就被新的话打回去重想，连续最多这么多次，之后先把想好的说出来
    start_wait = 12.0       # 入会最多让人等这么久；大脑还没连上就先开始会议，它在后台接着连
    start_timeout = 60.0    # 大脑预热最多多久，超过就算这次没连上（之后有人说话时再试）
    task_timeout = 600.0    # 后台任务最长多久
    echo_threshold = 0.6

    def __init__(self, meeting: Meeting, brain: Brain, mouth: Mouth, player: Player,
                 worker: Worker | None = None, clock: Callable[[], float] = time.monotonic):
        self.m = meeting
        self.brain, self.mouth, self.player, self.worker = brain, mouth, player, worker
        self.clock = clock
        self.pending: list[str] = []   # 会触发大脑的新内容
        self.carry: list[str] = []     # 下次顺带告诉大脑的旁注
        self._direct: list[tuple[str, str]] = []   # 不经大脑、直接要说的话：（内容，说完后告诉大脑的旁注）
        self._cut: tuple[float, list[str], str] | None = None   # 被打断的时刻、没说完的句子、当时记的旁注
        self._filler_seq = 0
        self._wake = asyncio.Event()
        self._speech_changed = asyncio.Event()
        self._attempt: _Attempt | None = None
        self._loop_task: asyncio.Task | None = None
        self._barge_task: asyncio.Task | None = None
        self._nudge_task: asyncio.Task | None = None
        self._warm: asyncio.Task | None = None     # 入会时的大脑预热
        self._bg: set[asyncio.Task] = set()
        self._nudge_ok = False
        self._last_speech_end = -1e9
        self._speech_end_at = -1e9     # 收到「人声结束」的时刻（此时那句话还在识别中）
        self._last_barge = -1e9
        self._overlap = False          # 当前这段人声开始时，数字人是否正在出声
        self._recent_bot: deque[tuple[float, str]] = deque(maxlen=12)
        self._clip_seq = 0
        self._pending_sid: int | None = None   # pending 里最近一句话是谁说的
        self._restarts = 0             # 连续几次「刚想好又被新的话打回去重想」
        self._early_cuts = 0           # 连续几次「刚开口就被打断」
        # 可选：问「现在正在说话的是谁」，返回声纹向量或 None。由耳朵提供
        self.voice_probe: Callable[[], Awaitable[object]] | None = None
        self._last_stall_line = -1e9
        self._brain_fails = 0          # 连续几轮大脑没答上来
        if hasattr(brain, "on_retry"):
            brain.on_retry = self._brain_retrying

    # ================= 生命周期 =================
    async def start(self) -> None:
        if self.m.active:
            return
        self.m.reset()
        self.pending.clear(); self.carry.clear(); self._direct.clear()
        self._nudge_ok, self._cut = False, None
        self._pending_sid, self._restarts, self._early_cuts = None, 0, 0
        self.m.set_status("starting")      # 大脑预热要几秒
        warm = self._warm = asyncio.create_task(self._warm_up())
        done, _ = await asyncio.wait({warm}, timeout=self.start_wait)
        if self._warm is not warm or self.m.status != "starting":
            return       # 等的时候会议已经被结束了（或者又重新开了一场）：这次入会作废，别把它复活
        if not done:
            # 网络不好时别让人干等：会议先开始，字幕照常，界面提示大脑还没连上；它在后台接着连
            log.warning("大脑 %.0f 秒还没连上，先开始会议", self.start_wait)
            self.m.set_flag("brain_ok", False)
        self.m.set_status("listening")
        self._loop_task = asyncio.create_task(self._loop())

    async def _warm_up(self) -> None:
        try:
            await asyncio.wait_for(self.brain.start(), self.start_timeout)
            self.m.set_flag("brain_ok", True)
        except asyncio.TimeoutError:
            log.warning("大脑 %.0f 秒没连上，之后有人说话时再试", self.start_timeout)
            self.m.set_flag("brain_ok", False)
        except Exception:  # noqa: BLE001
            log.exception("大脑启动失败")
            self.m.set_flag("brain_ok", False)

    async def stop(self) -> None:
        if not self.m.active:
            return
        for t in (self._loop_task, self._barge_task, self._nudge_task, self._warm, *self._bg):
            if t:
                t.cancel()
        if self._attempt and self._attempt.task:
            self._attempt.task.cancel()
        self._attempt = None
        await self._safely(self.player.stop())
        await self._safely(self.brain.stop())
        for t in self.m.tasks:
            if t.status == "running":
                self.m.update_task(t, status="failed", result="会议结束，任务中止", progress="")
        self.m.set_flag("humans_speaking", False)
        self.m.set_status("off")

    # ================= 耳朵送来的事件 =================
    def on_speech_start(self) -> None:
        if not self.m.active:
            return
        att = self._attempt
        self._overlap = bool(att and att.playing)
        self.m.set_flag("humans_speaking", True)
        self._speech_changed.set()
        self._cancel(self._nudge_task)
        if att and att.phase == "speaking" and not att.push:
            self._cancel(self._barge_task)
            self._barge_task = asyncio.create_task(self._barge_later())

    def on_speech_end(self, ago: float = 0.0) -> None:
        """有人停下了。ago：实际是多少秒之前停的（断句本身要等一段静音才能确认）。"""
        if not self.m.active:
            return
        self._last_speech_end = self.clock() - ago
        self._speech_end_at = self.clock()
        self.m.set_flag("humans_speaking", False)
        self._speech_changed.set()
        self._cancel(self._barge_task)
        self._arm_nudge()

    async def on_utterance(self, text: str, embedding=None, speaker_name: str | None = None,
                           seconds: float = 10.0) -> Line | None:
        """有人说完了一句话。返回字幕行；被判为回声则返回 None。"""
        text = fix_name(text.strip(), self.m.settings.name)
        if not self.m.active or not text:
            return None
        overlapped, self._overlap = self._overlap, False
        if speaker_name is None and overlapped and self._is_echo(text):
            if self.clock() - self._last_barge < 4:
                # 自己的回声把自己打断了：把没说完的话接着说完
                self._resume("（刚才是音箱回声把你打断了，并没有人插话，你已经把话接着说完：「{said}」）")
            return None

        sid = self.m.speakers.by_name(speaker_name) if speaker_name else self.m.speakers.assign(embedding, seconds)
        self.m.emit("speaker", speaker=self.m.speakers.speakers[sid].to_dict())
        line = self.m.add_line("human", text, sid, seconds if speaker_name is None else 0.0)
        self._nudge_ok = True
        if is_backchannel(text) and self.m.settings.name not in text and self._resume(
                f"（你说到一半时 [{line.speaker}] 附和了一声「{text}」，他在听，你已经把话接着说完：「" + "{said}」）"):
            return line
        self._cut = None
        self.pending.append(f"[{line.speaker}] {text}")
        self._pending_sid = sid

        att = self._attempt
        if att and not att.aborted:
            if att.phase == "speaking":
                # 顶着说的时候不看人声，只认明确的「停一下」或叫它名字
                if att.push and self._wants_stop(text):
                    self._last_barge = self.clock()
                    await self._abort("barge")      # 明确叫停：不记「没说完的话」，之后一声「嗯」不该把它续上
            elif att.fixed:
                pass     # 照原文要说的话（主持人让说的、续说的）：不因为有人说话就作废，等到空档照说
            else:
                # 还没开口，又有人说话了：要不要带上新内容重想？
                # 等它回答的那个人接着说（或者有人点了它的名）：重想，他的话还没说完。
                # 别人说了一句正经话（可能已经替它把问题答了）：也重想，但最多两次——
                # 否则旁边一直有人说话它就永远开不了口。「嗯」「对」这种不算。
                asker = (att.trigger_speaker is None or sid == att.trigger_speaker
                         or self.m.settings.name in text)
                real = len(re.sub(r"[^\w一-鿿]", "", text)) >= 4 and not is_backchannel(text)
                if asker and self._restarts < self.max_restarts:
                    await self._abort("new_speech")
                elif not asker and self._restarts < 2 and (not self._real(att) or real):
                    self._pending_sid = att.trigger_speaker    # 等回答的还是原来那个人，不是插话的这位
                    await self._abort("new_speech")
        self._wake.set()
        if not self.m.humans_speaking:
            self._arm_nudge()
        return line

    # ================= 形象页送来的播放回报 =================
    def on_clip_started(self, clip_id: int) -> None:
        att = self._attempt
        if not att or clip_id not in att.clips or clip_id in att.started:
            return
        att.started.add(clip_id)
        self._recent_bot.append((self.clock(), att.clips[clip_id]))
        text = "".join(att.spoken())
        if att.line is None:
            att.line = self.m.add_line("bot", text)
        else:
            self.m.update_line(att.line, text=text)

    def on_clip_ended(self, clip_id: int) -> None:
        att = self._attempt
        if not att or clip_id not in att.clips:
            return
        att.ended.add(clip_id)
        if (att.phase == "speaking" and not att.all_sent and not att.playing and not att.aborted
                and not self._real(att)):
            # 只垫了一句「我想一下」，正经的回答还没想出来：算回「在想」。
            # 这样回答出来时会重新等空档，这期间的杂音也不会被当成「打断」把问题作废
            att.phase = "thinking"
            self.m.set_status("thinking")
        self._check_done(att)

    # ================= 人工控制 =================
    async def hush(self) -> None:
        """让数字人立刻停下。"""
        self._direct.clear()
        self._cut = None
        await self._abort("hush")

    async def set_muted(self, muted: bool) -> None:
        self.m.set_flag("muted", muted)
        if muted:
            self._direct.clear()
            self._cut = None
            await self._abort("mute")
        elif self.pending:
            self._wake.set()

    def say(self, text: str) -> None:
        """不经大脑，直接让数字人说一句话。"""
        if text.strip():
            self._direct.append((text.strip(), "（主持人让你说了这句话：「{said}」）"))
            self._wake.set()

    def _resume(self, note: str) -> bool:
        """把刚才被打断、没说完的话接着说完（不经大脑，一字不改）。没有可续的话就返回 False。"""
        cut, self._cut = self._cut, None
        if not cut or self._attempt is not None or self.clock() - cut[0] > self.resume_window or not cut[1]:
            return False     # 已经在想别的了（那条「没说出口」的旁注也送出去了），再续就会说两遍
        if cut[2] in self.carry:
            self.carry.remove(cut[2])      # 「后面的没说出口」不成立了：马上就接着说
        self._direct.insert(0, ("".join(cut[1]), note))
        self._wake.set()
        return True

    def note_engagement(self) -> None:
        """参与度被人调了：下次说话前告诉大脑。"""
        from .fast_brain import ENGAGEMENT_NOTE
        self.carry.append(f"（主持人把你的参与度调成了：{ENGAGEMENT_NOTE[self.m.settings.engagement]}。）")

    def dispatch(self, kind: str, brief: str) -> Task:
        if kind not in TASK_KINDS:
            raise ValueError("没有这种任务")
        task = self.m.add_task(kind, brief)
        self.m.show_on_board(task.id)
        t = asyncio.create_task(self._run_task(task))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return task

    # ================= 主循环 =================
    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.m.active and not self.m.muted and (self._direct or self.pending):
                if not self._direct:
                    await self._hear_them_out()      # 等的时候情况可能变了（被静音、有了直接要说的话），重新判断
                    if not self._direct and (self.m.muted or not self.pending):
                        continue
                att = _Attempt()
                if self._early_cuts >= 2:
                    # 连着两次刚开口就被打断：多半是环境声或回声，这次顶着说完
                    att.push, self._early_cuts = True, 0
                    log.info("连续两次刚开口就被打断，这次顶着说")
                if self._direct:
                    att.fixed = True
                    coro = self._speak_fixed(att, *self._direct.pop(0))
                else:
                    self._cut = None       # 开始想新的了：更早被打断的那段不再续
                    att.prompt = "\n".join(self.carry + self.pending)
                    att.trigger_speaker, self._pending_sid = self._pending_sid, None
                    self.carry.clear(); self.pending.clear()
                    coro = self._think_and_speak(att)
                self._attempt = att
                att.task = asyncio.create_task(coro)
                await asyncio.wait({att.task})
                if self._attempt is att:
                    self._attempt = None
                if not att.aborted:
                    self._restarts = 0
                    if att.order:
                        self._early_cuts = 0
                if not att.task.cancelled() and att.task.exception():
                    log.error("发言出错", exc_info=att.task.exception())
            if self.m.active:
                self.m.set_status("listening")
                self._arm_nudge()

    async def _hear_them_out(self) -> None:
        """说话的人还没停：先听完再想。

        一句话常被停顿切成几段送来（「我要查一下」「订单导出」「这个接口」），每来一段就想一次，
        想的全是半句话，还一次次作废。真人是等对方说完再接。但不能无限等：周围一直有声音时到点就开始想。
        """
        name = self.m.settings.name
        named = any(p.startswith("[") and name in p for p in self.pending)
        start, heard = self.clock(), False
        cap = self.listen_cap_named if named else self.listen_cap
        while self.m.active and not self._direct:
            self._speech_changed.clear()
            if self.m.humans_speaking:
                heard = True
                wait = start + cap - self.clock()
            elif heard:
                # 刚停下：那句话的文字还在识别，等它到了一起想
                wait = self.asr_grace - (self.clock() - self._speech_end_at)
            else:
                return
            if wait <= 0:
                return
            try:
                await asyncio.wait_for(self._speech_changed.wait(), wait)
            except asyncio.TimeoutError:
                pass

    def _real(self, att: _Attempt) -> list[str]:
        """这次发言里大脑真正想说的句子（不算「我想一下」「网络卡了」这种垫话）。"""
        return [x for x in att.planned if x != self.stall_line and x not in self.fillers]

    def _parroting(self, att: _Attempt, sentence: str, prefixed: bool = False) -> bool:
        """大脑有时会照着字幕的样子，先把别人刚说的话原样抄一遍再回答。抄的那几句不念。

        只看回答开头：第一句是某句人话的开头（至少 6 个字，或者就是整句；带着「[某人]」前缀时两个字就算），
        之后接着往下抄的也算；
        一旦出现不是抄的句子就不再检查。「先做登录」这种只是用了问题里的词的正常回答不受影响。
        """
        if att.echo is False:
            return False
        n = re.sub(r"[^\w一-鿿]", "", sentence).lower()
        if not n:
            return False
        if att.echo is None:
            for line in att.prompt.splitlines():
                if not line.startswith("["):
                    continue
                heard = re.sub(r"[^\w一-鿿]", "", line.partition("]")[2]).lower()
                if heard.startswith(n) and (len(n) >= (2 if prefixed else 6) or n == heard):
                    att.echo = heard[len(n):]
                    log.info("大脑在学舌，这句不念：%s", sentence)
                    return True
            att.echo = False
            return False
        if att.echo.startswith(n):
            att.echo = att.echo[len(n):]
            return True
        att.echo = False
        return False

    def _addressed(self, prompt: str) -> bool:
        name = self.m.settings.name
        return any(l.startswith("[") and name in l for l in prompt.splitlines())

    async def _fill_silence(self, att: _Attempt) -> None:
        """被点名了却迟迟想不出第一句：先垫一句，让对方知道听到了。"""
        await asyncio.sleep(self.filler_after)
        if att.planned or att.aborted or att.closed:
            return
        if not self.m.brain_ok:
            # 不是在想，是连不上：照实说，别让人以为马上有回答
            if self.clock() - self._last_stall_line > self.stall_line_every:
                self._last_stall_line = self.clock()
                self._plan(att, att.queue, self.stall_line)
            return
        self._filler_seq += 1
        self._plan(att, att.queue, self.fillers[self._filler_seq % len(self.fillers)])

    async def _think_and_speak(self, att: _Attempt) -> None:
        att.queue = asyncio.Queue()
        filler = asyncio.create_task(self._fill_silence(att)) if self._addressed(att.prompt) else None
        try:
            await self._think(att)
        finally:
            self._cancel(filler)

    async def _think(self, att: _Attempt) -> None:
        self.m.set_status("thinking")
        rs = ReplyStream()
        queue: asyncio.Queue = att.queue
        speaker = asyncio.create_task(self._speak(att, queue))
        reply = None
        t0, first = self.clock(), None
        try:
            if self._warm and not self._warm.done():
                await asyncio.wait({self._warm})     # 入会预热还没完：等它，不然两路回答会串在一起
            stream = await self.brain.ask(att.prompt)
            att.sent = True
            it = stream.__aiter__()
            while True:
                try:
                    # 只给第一个字设时限：网络卡住时别让它一直「在想」
                    delta = await (asyncio.wait_for(it.__anext__(), self.first_token_timeout)
                                   if first is None else it.__anext__())
                except StopAsyncIteration:
                    break
                first = first if first is not None else self.clock() - t0
                for s in rs.feed(delta):
                    if not self._parroting(att, s, rs.prefixed):
                        self._plan(att, queue, s)
            rest, reply = rs.finish()
            for s in rest:
                if not self._parroting(att, s, rs.prefixed):
                    self._plan(att, queue, s)
            if reply.dispatch and not self._real(att):
                self._plan(att, queue, "我查一下。")      # 光派了工没出声：别人不知道它听到了
            self.m.set_flag("brain_ok", True)
            self._brain_fails = 0
            log.info("大脑 首字%.1fs 共%.1fs｜听到：%s｜回答：%s", first or 0, self.clock() - t0,
                     att.prompt[-70:].replace("\n", " / "), rs.raw.strip()[:70].replace("\n", " ") or "（空）")
        except asyncio.CancelledError:
            log.info("大脑 被叫停（%s）｜听到：%s｜已想到：%s", att.aborted or "会议结束",
                     att.prompt[-70:].replace("\n", " / "), rs.raw.strip()[:50].replace("\n", " ") or "（还没想出来）")
            speaker.cancel()
            self._cancel(*att.synths)
            raise
        except asyncio.TimeoutError:
            log.warning("大脑 %.0f 秒没有回应，放弃这一轮", self.first_token_timeout)
            self.m.set_flag("brain_ok", False)
            await self._safely(self.brain.cancel())
            self._brain_failed(att)
        except Exception:  # noqa: BLE001
            log.exception("大脑出错")
            self.m.set_flag("brain_ok", False)
            self._brain_failed(att)
        att.closed = True
        queue.put_nowait(None)
        if reply and reply.dispatch:
            self._dispatch_from_brain(*reply.dispatch)   # 不等话说完就开工
        for old, new in (reply.names if reply else ()):
            self._name_speaker(old, new)
        if reply and reply.engagement:
            self.m.change_settings({"engagement": reply.engagement}, source="brain")   # 它听到「少说点 / 多说点」后自己调
        try:
            await speaker
        except _GiveUp:
            self._cancel(*att.synths)
            if self._real(att):
                self.carry.append("（一直有人在说话，你想说的那句没找到机会说出口。）")
        except asyncio.CancelledError:
            speaker.cancel()
            self._cancel(*att.synths)
            raise

    def _brain_failed(self, att: _Attempt) -> None:
        """这一轮大脑没答上来。有人在等回答：先自动再试，还不行就照实说，别让人干等；没人等就算了。"""
        said_something = any(x != self.stall_line and x not in self.fillers for x in att.planned)
        if said_something or not self._addressed(att.prompt):
            return
        self._brain_fails += 1
        if self._brain_fails <= self.brain_retries:
            self.pending.insert(0, att.prompt)
            if self._pending_sid is None:
                self._pending_sid = att.trigger_speaker
            self.carry.append("（刚才网络断了一下，下面的话你还没回应。）")
        else:
            self._brain_fails = 0
            self._direct.append((self.offline_line, ""))

    def _brain_retrying(self, attempt: int) -> None:
        """大脑的请求失败、正在重试（多半是网络不稳）。让界面看得见；有人在等回答时先用本地语音打个招呼。"""
        log.warning("大脑连不上，第 %d 次重试", attempt)
        self.m.set_flag("brain_ok", False)
        att = self._attempt
        now = self.clock()
        # 只在确实有人在等它时才出声：被叫了名字，或者半分钟内它刚说过话（正在对话中）。
        # 否则背景里随便一句话都会引出一句「我网络卡了」
        in_talk = any(now - ts < 30 for ts, _ in self._recent_bot)
        wanted = att is not None and (self.m.settings.name in att.prompt or in_talk)
        if (att and wanted and att.queue is not None and att.trigger_speaker is not None and not att.aborted
                and not att.planned and now - self._last_stall_line > self.stall_line_every):
            self._last_stall_line = now
            self._plan(att, att.queue, self.stall_line)

    def _name_speaker(self, old: str, new: str) -> None:
        """大脑从对话里认出了某位说话人（「我是张三」）。只给还没起名的人改，不覆盖主持人手工改的名字。"""
        new = re.sub(r"[：:、\s]+", " ", new).strip()[:12]      # 名字会原样写进会议记录：不留换行和分隔符
        if not re.fullmatch(r"说话人\d+", old) or not new or re.fullmatch(r"说话人\d+", new) or new == self.m.settings.name:
            return
        book = self.m.speakers
        if any(sp.name == new for sp in book.speakers.values()):
            return                                   # 已经有人叫这个名字了，多半是认错
        for sp in book.speakers.values():
            if sp.name == old:
                self.m.rename_speaker(sp.id, new)
                log.info("认出 %s 是 %s", old, new)
                return

    def _dispatch_from_brain(self, kind: str, brief: str) -> None:
        # 大脑重新组织发言时可能重复派同一件事，正在做的就不再派
        for t in self.m.tasks:
            if t.status == "running" and t.kind == kind and similar(t.brief, brief) > 0.5:
                return
        self.dispatch(kind, brief)

    async def _speak_fixed(self, att: _Attempt, text: str, note: str = "") -> None:
        sp = SentenceSplitter()
        queue: asyncio.Queue = asyncio.Queue()
        for s in sp.feed(text) + sp.flush():
            self._plan(att, queue, s)
        queue.put_nowait(None)
        try:
            await self._speak(att, queue)
        except _GiveUp:
            self.carry.append(f"（一直有人在说话，这句话你没找到机会说出口：「{text[:80]}」）")
            return
        except asyncio.CancelledError:
            self._cancel(*att.synths)
            raise
        said = "".join(att.spoken())
        if said and note:
            self.carry.append(note.replace("{said}", said))

    def _plan(self, att: _Attempt, queue: asyncio.Queue, sentence: str) -> None:
        att.planned.append(sentence)
        synth = asyncio.create_task(self.mouth.synth(sentence))
        att.synths.append(synth)
        queue.put_nowait((sentence, synth))

    async def _speak(self, att: _Attempt, queue: asyncio.Queue) -> None:
        """按顺序把合成好的句子送去播放；第一句之前先确认没人在说话。"""
        while (item := await queue.get()) is not None:
            sentence, synth = item
            try:
                audio, mime = await synth
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("这句话没合成出来：%s", sentence)
                att.taken += 1
                continue
            if att.phase != "speaking":
                await self._wait_for_floor(att)
                att.phase = "speaking"
                att.speak_started = self.clock()
            self.m.set_status("speaking")
            self._clip_seq += 1
            cid = self._clip_seq
            att.clips[cid] = sentence
            att.order.append(cid)
            await self.player.play(cid, audio, mime, sentence)
            att.taken += 1
        att.all_sent = True
        if not att.order:
            return
        self._check_done(att)
        budget = 5 + 0.4 * sum(len(s) for s in att.clips.values())
        try:
            await asyncio.wait_for(att.done.wait(), budget)
        except asyncio.TimeoutError:
            # 形象页没回报播放结束（比如页面被关了）：当作说完
            for cid in att.order:
                self.on_clip_started(cid)

    async def _wait_for_floor(self, att: _Attempt) -> None:
        """等一个开口的空档。

        没人问、是它自己想说的话：一直礼貌地等，等不到就算了。
        有人问它的话：等 hold 秒；到时候如果是问它的人自己还在说，就继续等；
        如果是别的声音（旁人闲聊、电视）一直不停，就顶着说——否则环境一吵它就永远答不了话。
        """
        polite = att.trigger_speaker is None
        start = self.clock()
        limit = self.give_up_after if polite else min(self.m.settings.hold, self.give_up_after)
        while True:
            self._speech_changed.clear()
            if not self.m.humans_speaking:
                remain = self.m.settings.gap - (self.clock() - self._last_speech_end)
                if att.phase == "waiting":
                    # 等的过程中有人说过话：那句话的文字还在识别，等它到了再决定（可能要重想）
                    remain = max(remain, self.asr_grace - (self.clock() - self._speech_end_at))
                if remain <= 0:
                    return
                wait = remain
            else:
                att.phase = "waiting"
                self.m.set_status("waiting")
                wait = start + limit - self.clock()
                if wait <= 0:
                    if polite or limit >= self.give_up_after:
                        log.info("等了 %.0f 秒都没有空档，这句话不说了", limit)
                        raise _GiveUp
                    if await self._asker_still_talking(att):
                        limit = self.give_up_after
                        continue
                    att.push = True
                    log.info("等了 %.1f 秒旁边的声音还没停，顶着说", limit)
                    return
            try:
                await asyncio.wait_for(self._speech_changed.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def _asker_still_talking(self, att: _Attempt) -> bool:
        """现在正在说话的，是不是刚才问它的那个人。分辨不出来时当作不是。"""
        if self.voice_probe is None or att.trigger_speaker is None:
            return False
        try:
            emb = await self.voice_probe()
        except Exception:  # noqa: BLE001
            log.exception("分辨说话人失败")
            return False
        if emb is None:
            return False
        return self.m.speakers.similarity(att.trigger_speaker, emb) >= self.m.speakers.threshold

    def _check_done(self, att: _Attempt) -> None:
        if att.all_sent and all(c in att.ended for c in att.order):
            att.done.set()

    # ================= 放弃与打断 =================
    async def _abort(self, reason: str, resumable: bool = False) -> None:
        """放弃当前这次发言。resumable：是被人声盖住的（不是明确叫停），对方若只是附和可以续说。"""
        att = self._attempt
        if not att or att.aborted or not att.task:
            return
        att.aborted = reason
        phase, spoken = att.phase, att.spoken()
        att.task.cancel()
        if reason == "new_speech":
            self._restarts += 1
        elif reason == "barge":
            early = self.clock() - att.speak_started < self.early_cut
            self._early_cuts = self._early_cuts + 1 if early else 0
        if not att.sent and att.prompt:
            self.pending.insert(0, att.prompt)   # 还没送进大脑，放回去下次一起送
            if self._pending_sid is None:
                self._pending_sid = att.trigger_speaker
        else:
            await self._safely(self.brain.cancel())
        if att.clips:
            await self._safely(self.player.stop())

        if phase == "speaking" and spoken:
            if att.line:
                self.m.update_line(att.line, cut=True)
            tail = spoken[-1][-16:]
            said_real = [x for x in spoken if x != self.stall_line and x not in self.fillers]
            if reason == "barge" and not said_real and not att.fixed:
                # 只应了一声「我想一下」就被声音盖住了，回答一个字还没出口：问题不能就这么丢了
                self.pending.insert(0, "（你刚应了一声，还没来得及回答就有声音盖了过来。"
                                       "结合后面的话判断：对方还在等的话，现在把刚才的问题答了。）")
                if self._pending_sid is None:
                    self._pending_sid = att.trigger_speaker
                self._wake.set()
            elif reason == "barge":
                note = f"（你说到「{tail}」时被人打断了，后面的没说出口。）"
                if resumable:
                    # 记下没说完的（从被打断的那一句起）：如果对方只是附和了一声，就接着说完
                    unfinished = [att.clips[c] for c in att.order if c not in att.ended]
                    rest = [x for x in unfinished + att.planned[att.taken:]
                            if x != self.stall_line and x not in self.fillers]
                    self._cut = (self.clock(), rest, note)
                self.carry.append(note)
            elif reason in ("hush", "mute"):
                self.carry.append(f"（你说到「{tail}」时主持人让你先停下。除非被点名，先别再提这件事。）")
        elif self._real(att) and reason == "new_speech":
            self.carry.append("（你刚才想说的话还没出口，又有人说话了。请结合下面的新内容重新判断该不该说、说什么："
                              "别人已经把问题答了就不用重复；只是无关的声音，就把原来要说的照常说出来。）")

    async def _barge_later(self) -> None:
        st = self.m.settings
        att = self._attempt
        # 刚开口的头一小段不算打断：回声消除需要一点时间才稳定，这时最容易被自己的声音误触发
        grace = max(0.0, st.barge_grace - (self.clock() - att.speak_started)) if att else 0.0
        await asyncio.sleep(st.barge_delay + grace)
        att = self._attempt
        if self.m.humans_speaking and att and att.phase == "speaking" and not att.push:
            self._last_barge = self.clock()
            log.info("被打断：说到「%s」", "".join(att.spoken())[-20:])
            await self._abort("barge", resumable=True)

    def _wants_stop(self, text: str) -> bool:
        return self.m.settings.name in text or bool(_STOP_WORDS.search(text))

    def _is_echo(self, text: str) -> bool:
        now = self.clock()
        recent = "".join(t for ts, t in self._recent_bot if now - ts < 15)
        return bool(recent) and echo_score(text, recent) >= self.echo_threshold

    # ================= 冷场提示 =================
    def _arm_nudge(self) -> None:
        self._cancel(self._nudge_task)
        # 让它少说的时候，冷场也不用它来救
        if (self.m.active and self._nudge_ok and self.m.settings.silence_nudge > 0
                and self.m.settings.engagement != "quiet"):
            self._nudge_task = asyncio.create_task(self._nudge_later())

    async def _nudge_later(self) -> None:
        secs = self.m.settings.silence_nudge
        await asyncio.sleep(secs)
        if (self.m.active and not self.m.muted and not self.m.humans_speaking
                and self._attempt is None and self._nudge_ok and not self.pending):
            self._nudge_ok = False   # 每次冷场只提示一次，直到又有人说话
            self.pending.append(f"（会场已经安静了 {secs:g} 秒，没有人接话。）")
            self._wake.set()

    # ================= 后台任务 =================
    async def _run_task(self, task: Task) -> None:
        if self.worker is None:
            res = WorkResult(False, "这台机器上没有配置干活的能力")
        else:
            try:
                res = await asyncio.wait_for(
                    self.worker.run(task, lambda p: self.m.update_task(task, progress=p)),
                    self.task_timeout,
                )
            except asyncio.TimeoutError:
                res = WorkResult(False, "花的时间太长，没做完")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("后台任务出错")
                res = WorkResult(False, "中途出错了")
        self.m.update_task(task, status="done" if res.ok else "failed",
                           result=res.text, artifact=res.artifact, progress="")
        self.m.show_on_board(task.id)
        if res.ok:
            self.pending.append(
                f"（后台任务完成｜{task.kind}：{task.brief}）\n{res.text[:1800]}\n"
                "（请挑个空档用口语向大家汇报，先说结论，两三句话；完整内容已经显示在你的屏幕上。和现在聊的无关了就一句话带过。）"
            )
        else:
            self.pending.append(
                f"（后台任务没做成｜{task.kind}：{task.brief}｜原因：{res.text[:200]}。请简短告诉大家。）"
            )
        self._wake.set()

    # ================= 小工具 =================
    @staticmethod
    def _cancel(*tasks: asyncio.Task | None) -> None:
        for t in tasks:
            if t and not t.done():
                t.cancel()

    @staticmethod
    async def _safely(aw) -> None:
        try:
            await aw
        except Exception:  # noqa: BLE001
            log.exception("部件调用失败")
