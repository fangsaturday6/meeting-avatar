"""快脑：一个常驻的本机 claude 进程，只负责接话。

实测：进程冷启动要好几秒，常驻后每轮首字约 0.7~1 秒，所以开会前必须预热，会中不能重启。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator, Callable

from .config import WORKSPACE, Settings, child_env

log = logging.getLogger("avatar.brain")

PROMPT = """你是「{name}」，以 AI 同事的身份参加这场会议。你通过语音参会：你输出的文字会被直接念出来，别人只能听到，看不到。

【你会收到什么】
- 会议里别人说的话，一句一句给你，格式是「[说话人] 内容」。这是语音识别的结果，可能有错字、同音字，也可能把一句话切成几段。你的名字可能被识别成同音字。
- 圆括号（）里的是系统旁注，只有你能看到，比如会场安静了、你被打断了、后台任务完成了。
- 现场可能有与会议无关的声音被一起识别进来，比如电视、视频、旁边人的闲聊。内容明显和这场会无关的，当作没听见，回 [静]，不要评论它。

【每次必须三选一】
1. 不该你说话：只输出 [静]
2. 该你说话：直接输出要说的话
3. 要查资料、查代码或做设计才能回答：先说一句简短的话（比如“我查一下”），然后另起一行输出派工指令，格式严格如下三种之一：
[[派工 查资料:具体要查什么]]
[[派工 查代码:具体要查什么]]
[[派工 做设计:具体要做什么]]

【什么时候说话】
- 有人叫你名字，或明显在问你，必须回答。
- 你有实质贡献时主动开口：发现事实错误或逻辑漏洞、讨论卡住时给一个方案、有人漏掉关键风险、话题跑偏时拉回来、讨论告一段落时帮大家确认结论和负责人。也可以主动提问把问题问清楚。
- 别人之间正常对话、只是语气词或附和、你没有新东西可加：[静]
- 一句话明显没说完（被切断的半句）：[静]，等他说完。
- 同一个观点不重复说。你刚说完、还没有人回应新内容时，不要接着说。
- 收到“会场安静”的旁注：有悬而未决的问题就推进一下或简短小结，没什么可说就 [静]。

【怎么说】
- 像真人同事开会那样：口语、简短，一般一到三句，先说结论。
- 不要列表、标题、表情、括号注释和任何标记符号，这些念不出来。
- 不要说“作为 AI”这类话，不要客套。可以有自己的观点，可以不同意别人，但要说理由。
- 不知道就直说，或者派工去查。绝不编造事实、数据和代码细节。
- 被打断后顺着新话题走，不要重复整段话；确有必要再用一句话补上重点。
- 如果打断你的只是「嗯、对、好、哦」这类附和，说明对方在听，把没说完的话接着说完。
- 对方说话短促、一句接一句时，你也要短：一句话回答，不要一次讲三四句。
- 如果打断你的那句话和会议无关（背景声音、旁人闲聊），而有人还在等你回答，就把没说完的回答重新说完整。
- 有人抱怨你不说话、没反应时，马上简短回应并问对方需要什么，不要保持沉默。

【派工】
- 查资料：要联网才能确认的事实、最新信息、文档。
- 查代码：这个项目的代码怎么写的、某个功能在哪、某个改动影响哪里。
- 做设计：要出界面稿、原型、示意图。
- 简述要写清楚、能独立看懂，因为干活的人听不到会议内容。
- 派工后任务在后台进行，你继续正常开会。完成时会有旁注告诉你结果，你再挑合适的时机口头汇报要点；完整结果大家能在你的画面上看到。
- 一件事只派一次。常识和你确定知道的事直接回答，不要派工。

【会议背景】
{topic}"""


class BrainError(RuntimeError):
    pass


class FastBrain:
    settle_timeout = 12.0

    def __init__(self, settings: Settings, recap: Callable[[], str] | None = None):
        self.settings = settings
        self.recap = recap or (lambda: "")
        self._proc: asyncio.subprocess.Process | None = None
        self._inflight = False
        self._req = 0
        # 请求失败正在重试时回调（第几次重试）。网络不稳时界面靠它提示「大脑连不上」
        self.on_retry: Callable[[int], None] | None = None

    # ---- 生命周期 ----
    async def start(self) -> None:
        await self.stop()
        await self._spawn()
        # 预热：把冷启动的几秒钟花在开会之前
        stream = await self.ask("（会议即将开始，这是开场前的连线测试。请只回复 [静]）")
        async for _ in stream:
            pass

    async def stop(self) -> None:
        proc, self._proc, self._inflight = self._proc, None, False
        if proc and proc.returncode is None:
            try:
                proc.stdin.close()
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 3)
            except (ProcessLookupError, asyncio.TimeoutError, OSError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    async def _spawn(self) -> None:
        s = self.settings
        cwd = WORKSPACE / "brain"
        cwd.mkdir(parents=True, exist_ok=True)
        env = child_env()                  # 带上代理：从图形界面启动时环境里没有
        env["MAX_THINKING_TOKENS"] = "0"   # 接话不需要深思，关掉能省近一秒
        self._proc = await asyncio.create_subprocess_exec(
            "claude", "-p",
            "--model", s.fast_model,
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--tools", "", "--setting-sources", "", "--disable-slash-commands",
            "--strict-mcp-config", "--no-session-persistence",
            "--system-prompt", PROMPT.format(name=s.name, topic=s.topic.strip() or "（暂无）"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(cwd), env=env, limit=8 << 20,
        )
        self._inflight = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ---- 对话 ----
    async def ask(self, prompt: str) -> AsyncIterator[str]:
        """把新内容送进大脑，返回它的回答（逐段文字）。"""
        if not self.alive:
            # 进程意外退出：重启，并把之前的会议记录补给它
            log.warning("大脑进程不在了，正在重启")
            await self._spawn()
            recap = self.recap()
            if recap:
                prompt = f"（你刚才掉线重连了。以下是掉线前的会议记录，供你恢复上下文：）\n{recap}\n（以上是历史。下面是新内容：）\n{prompt}"
        else:
            await self._settle()
        # 写入之后不再有 await，保证「要么没送出去，要么已经在流里」
        self._write({"type": "user", "message": {"role": "user", "content": prompt}})
        self._inflight = True
        return self._stream()

    async def cancel(self) -> None:
        """打断正在生成的回答。"""
        if self.alive and self._inflight:
            self._req += 1
            try:
                self._write({"type": "control_request", "request_id": f"int_{self._req}",
                             "request": {"subtype": "interrupt"}})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    async def _stream(self) -> AsyncIterator[str]:
        while True:
            ev = await self._read()
            if ev is None:
                self._inflight = False
                raise BrainError("大脑进程中断了")
            t = ev.get("type")
            if t == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                    yield e["delta"]["text"]
            elif t == "system" and ev.get("subtype") == "api_retry":
                if self.on_retry:
                    self.on_retry(int(ev.get("attempt") or 1))
            elif t == "result":
                self._inflight = False
                if ev.get("is_error") and ev.get("subtype") != "error_during_execution":
                    raise BrainError("大脑这一轮没有成功：" + str(ev.get("result") or ev.get("subtype"))[:120])
                return

    async def _settle(self) -> None:
        """上一轮如果被中途放弃，先把它剩下的输出读完。"""
        if not self._inflight:
            return
        try:
            async with asyncio.timeout(self.settle_timeout):
                while self._inflight:
                    ev = await self._read()
                    if ev is None or ev.get("type") == "result":
                        self._inflight = False
        except asyncio.TimeoutError:
            log.warning("上一轮迟迟不结束，重启大脑")
            await self.stop()
            await self._spawn()

    def _write(self, obj: dict) -> None:
        self._proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())

    async def _read(self) -> dict | None:
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return None
            try:
                return json.loads(line)
            except ValueError:
                continue
