"""测试用的假部件：不需要模型、网络和 Claude。"""
import asyncio

from avatar.floor import WorkResult


class FakeBrain:
    def __init__(self, replies=(), delay=0.0):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.cancels = 0
        self.delay = delay
        self.started = False
        self.on_retry = None          # Floor 会接上：大脑请求重试时回调
        self.retries_before_reply = 0  # 回答前先模拟几次网络重试

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def ask(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else "[静]"

        async def gen():
            for n in range(self.retries_before_reply):
                await asyncio.sleep(self.delay)
                if self.on_retry:
                    self.on_retry(n + 1)
            for i in range(0, len(reply), 3):
                await asyncio.sleep(self.delay)
                yield reply[i:i + 3]
        return gen()

    async def cancel(self):
        self.cancels += 1


class FakeMouth:
    def __init__(self):
        self.said: list[str] = []

    async def synth(self, text):
        self.said.append(text)
        return text.encode(), "audio/fake"


class FakePlayer:
    """auto=True：收到片段立刻回报开始和结束。auto=False：只回报开始，结束由测试手动触发。"""

    def __init__(self, auto=True):
        self.floor = None
        self.auto = auto
        self.played: list[tuple[int, str]] = []
        self.stops = 0

    async def play(self, clip_id, audio, mime, text):
        self.played.append((clip_id, text))
        self.floor.on_clip_started(clip_id)
        if self.auto:
            self.floor.on_clip_ended(clip_id)

    async def stop(self):
        self.stops += 1

    def finish_all(self):
        for cid, _ in self.played:
            self.floor.on_clip_ended(cid)

    @property
    def texts(self):
        return [t for _, t in self.played]


class FakeWorker:
    def __init__(self, result=None, delay=0.0):
        self.result = result or WorkResult(True, "查到了：接口文档在开放平台第三章。")
        self.delay = delay
        self.ran: list[tuple[str, str]] = []

    async def run(self, task, on_progress):
        self.ran.append((task.kind, task.brief))
        on_progress("正在搜索")
        await asyncio.sleep(self.delay)
        return self.result
