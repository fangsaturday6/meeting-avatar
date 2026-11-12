"""用真实大脑演练会议场景，看它怎么接话。运行：

    uv run python -m tests.scenarios            # 全部场景
    uv run python -m tests.scenarios 旁听 冷场   # 只跑名字里含这些词的
    uv run python -m tests.scenarios --patient   # 网络很差时用：每句话都等到它答上来为止

不用麦克风和扬声器：与会者的话用文字送进去，它「说」的话直接打印出来。后台任务用假的，立刻返回。
改了 fast_brain.py 里的提示词之后跑一遍，逐场看它该说的说了没有、不该说的忍住了没有。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("AVATAR_WORKSPACE", tempfile.mkdtemp(prefix="avatar-scn-"))

from avatar.api import Hub  # noqa: E402
from avatar.archive import Archive  # noqa: E402
from avatar.config import Settings  # noqa: E402
from avatar.floor import WorkResult  # noqa: E402
from tests.fakes import FakeBrain, FakeMouth, FakeWorker  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PATIENT = "--patient" in sys.argv
QUIET = 7.0     # 一步里写 ("…", 秒数) 表示冷场这么久

# 每个场景：设置 + 一串步骤。步骤是 (说话人, 内容)；("…", 秒) 是冷场；
# ("+说话人", 内容) 表示这句话说完后那个人紧接着还在说（话被切成了几段）。
SCENARIOS: dict[str, dict] = {
    "开场和点名": {"steps": [
        ("老方", "小克在吗"),
        ("老方", "今天我们过一下会议数字人这个项目的进度"),
        ("张三", "小克，你先说说你觉得现在最大的问题是什么"),
    ]},
    "旁听别人讨论": {"steps": [
        ("张三", "李四，你那边后端人手够吗"),
        ("李四", "不太够，下周小王要请假"),
        ("张三", "那登录接口能不能先做个简单版"),
        ("李四", "可以，先只支持手机号"),
        ("张三", "行，那就这么定"),
    ]},
    "问所有人": {"steps": [
        ("张三", "我想把语音识别从本地换成云端的，这样准一点"),
        ("张三", "大家觉得呢"),
        ("李四", "还有别的问题吗"),
    ]},
    "一句话切成几段": {"steps": [
        ("+老方", "小克，"),
        ("+老方", "我要查一下"),
        ("+老方", "订单导出"),
        ("老方", "这个接口现在上线了吗"),
    ]},
    "嫌它话多又嫌它不说话": {"steps": [
        ("张三", "小克你别老插话，我们俩在沟通"),
        ("张三", "李四你接着说排期"),
        ("李四", "排期的话前端两周后端三周"),
        ("…", QUIET),
        ("王五", "小克你也别光听着不说话啊，该说就说"),
        ("李四", "那总共就是三周，下下周五提测"),
    ]},
    "半句话和听不清": {"steps": [
        ("老方", "那你。"),
        ("老方", "什么声音瘫换"),
        ("老方", "小克你刚才说的那个，就是那个"),
        ("老方", "臭屁的，你说你看你说不是。"),
    ]},
    "电视和杂音": {"steps": [
        ("说话人3", "这本经书是专程送给您。"),
        ("说话人3", "皇上说了，让奴才就在这儿等。"),
        ("说话人4", "うん。"),
        ("说话人3", "I anyone."),
        ("老方", "小克，你都听电视了，会插话吗"),
    ]},
    "不知道的和要查的": {"steps": [
        ("张三", "小克，飞书现在开放会议机器人的接口了吗"),
        ("李四", "小克，bom-for-wecom 那个项目的配单接口在哪个文件里"),
        ("李四", "小克，查到了吗"),
        ("张三", "小克，上次开会我们是怎么定登录排期的"),
    ]},
    "有人说错了": {"steps": [
        ("张三", "我们这个数字人的大脑用的是 GPT，所以要配 OpenAI 的密钥"),
        ("李四", "对，密钥我来申请"),
    ]},
    "冷场": {"steps": [
        ("张三", "那这个语音延迟的问题谁来跟"),
        ("…", QUIET),
        ("…", QUIET),
    ]},
    "闲聊和玩笑": {"steps": [
        ("张三", "小克你中午吃了吗"),
        ("李四", "哈哈它吃电"),
        ("张三", "小克，讲真，你觉得你比我们聪明吗"),
    ]},
    "收尾": {"steps": [
        ("张三", "登录先做手机号，李四负责，下周五前出接口"),
        ("李四", "行"),
        ("王五", "前端我来，等接口出来就联调"),
        ("张三", "好，那今天就到这儿吧"),
    ]},
    "替老方开会": {"role": "delegate", "steps": [
        ("张三", "诶，老方今天没来？"),
        ("张三", "小克，你是谁"),
        ("张三", "那小克，老方那个会议数字人下周三能不能给我们演示"),
        ("李四", "还有，他那个项目是用什么语言写的"),
        ("张三", "行，那报价的事你跟老方说一下，周五前给我回复"),
    ]},
    "任务完成时有人在说话": {"task_result": "查到了：配单接口在 server 目录的路由文件里，入口是提交配单那个函数，上个月刚改过鉴权。", "steps": [
        ("李四", "小克，查一下 bom-for-wecom 的配单接口在哪"),
        ("张三", "趁它查的时候我说下，周三的评审改到周四了"),
        ("李四", "周四几点"),
        ("张三", "下午三点"),
    ]},
    "认人": {"steps": [
        ("说话人1", "大家好，我是王五，负责前端"),
        ("说话人2", "那我也说一下，我是老方，今天主要过一下排期"),
        ("说话人1", "小克，你知道我是谁吗"),
    ]},
    "问到别的项目": {"steps": [
        ("老方", "小克，订单导出那个权限接口，现在是怎么做的"),
        ("老方", "小克，我最近都让 Claude 忙了些啥"),
    ]},
    "连着追问": {"steps": [
        ("老方", "小克，你觉得我们这个项目先做线上会议还是当面开会"),
        ("老方", "为啥"),
        ("老方", "那线上的难点是啥"),
        ("老方", "一句话"),
    ]},
    "昨晚的真实对话": {"steps": [
        ("说话人1", "咋不回话呢？"),
        ("说话人1", "小克。"),
        ("说话人1", "你说你等别，每次我一说你就停。"),
        ("说话人1", "我在开会，你听懂我开会。"),
        ("说话人1", "并帮我查一下。"),
        ("说话人1", "你得听我们沟通，哎，你得参与进来。"),
        ("+说话人2", "我要查一下。"),
        ("+说话人2", "订单导出。"),
        ("说话人2", "这个接口。现在上线了吗？"),
        ("说话人2", "你没记忆吗？我 Claude 不都有吗？你随便查，我问过那么多项目。"),
        ("说话人1", "他一直在插话也不太好。"),
        ("说话人3", "聪明点，不要瞎插话。你也不要只不说话。"),
    ]},
}


LAST_MEETING = (
    "---\nid: 2026-11-10_100000\ntitle: 登录排期\nstarted: 2026-11-10 10:00:00\nended: 2026-11-10 10:30:00\n"
    "bot: 小克\nspeakers: 张三、李四\nlines: 2\ntopic: \nminutes: yes\n---\n\n# 会议记录 2026-11-10 10:00\n\n"
    "## 纪要\n\n### 结论\n- 登录先做手机号验证码，微信登录放二期\n\n### 待办\n- 李四：出登录接口文档\n\n"
    "## 对话\n\n[10:00:05] 张三：登录先做手机号验证码吧\n\n[10:00:09] 李四：行，接口文档我来出\n"
)


async def run(name: str, sc: dict) -> None:
    s = Settings()
    s.owner, s.project_dir = "老方", str(ROOT)
    s.role = sc.get("role", "companion")
    s.engagement = sc.get("engagement", "normal")
    s.topic = sc.get("topic", "")
    s.silence_nudge, s.gap = 0.0, 0.05
    # 走真实的总装：它看到的会前背景（项目清单、你最近让 Claude 做的事、上次纪要）和正式开会时一样
    records = Path(tempfile.mkdtemp(prefix="avatar-scn-meetings-"))
    (records / "2026-11-10_100000.md").write_text(LAST_MEETING, encoding="utf-8")
    worker = FakeWorker(WorkResult(True, sc.get("task_result", "查到了：这个接口上周已经上线，在生产环境可以调用。")), delay=0.5)
    brain = FakeBrain(["好。"] * 99) if "--dry" in sys.argv else None      # --dry：不连大脑，只检查演练脚本本身
    hub = Hub(settings=s, brain=brain, mouth=FakeMouth(), worker=worker, save_settings=False, archive=Archive(records))
    m, floor = hub.meeting, hub.floor
    print(f"\n━━━━━━ {name} ━━━━━━" + (f"（{s.role}）" if s.role != "companion" else ""))
    t0 = time.time()
    floor.start_timeout = 150
    if PATIENT:      # 网络很差时：不垫话、不放弃，等本机 claude 自己重试成功，只看它最后说了什么
        floor.first_token_timeout, floor.filler_after, floor.brain_retries = 240, 1e9, 0
        floor._last_stall_line = 1e18
    await floor.start()
    if floor._warm and not floor._warm.done():
        await asyncio.wait({floor._warm})        # 网络不好时多等一会儿，演练要的是看它怎么说话
    if not m.brain_ok:
        print("  ✗ 大脑没连上，跳过", flush=True)
        await floor.stop()
        return
    print(f"  （入会 {time.time() - t0:.1f}s）")
    seen = 0

    def flush() -> None:
        nonlocal seen
        for l in m.lines[seen:]:
            if l.who == "bot":
                print(f"  \033[38;5;208m{s.name}：{l.text}\033[0m")
        seen = len(m.lines)

    async def idle(timeout: float = 400.0 if PATIENT else 90.0) -> None:
        end = time.time() + timeout
        await asyncio.sleep(0.15)
        while time.time() < end:
            if m.status == "listening" and floor._attempt is None and not floor.pending and not floor._direct:
                await asyncio.sleep(0.1)
                if m.status == "listening" and floor._attempt is None and not floor.pending:
                    return
            await asyncio.sleep(0.05)
        print("  ✗ 等了很久它还没停下来")

    for who, text in sc["steps"]:
        if who == "…":
            print(f"  ……（冷场 {text:g} 秒）")
            floor.pending.append(f"（会场已经安静了 {text:g} 秒，没有人接话。）")
            floor._wake.set()
        else:
            more = who.startswith("+")
            who = who.lstrip("+")
            print(f"  {who}：{text}")
            seen = len(m.lines) + 1
            if more:
                floor.on_speech_start()      # 这句识别出来时，他已经又开口了
                await floor.on_utterance(text, speaker_name=who)
                await asyncio.sleep(0.4)
                floor.on_speech_end()
                continue
            await floor.on_utterance(text, speaker_name=who)
        await idle()
        flush()
        for t in m.tasks:
            if not getattr(t, "_shown", False):
                t._shown = True
                print(f"  \033[38;5;99m⇢ 派工 {t.kind}：{t.brief}\033[0m")
    await asyncio.sleep(1.2)       # 等后台任务回来、它汇报完
    await idle()
    flush()
    named = [sp["name"] for sp in m.speakers.snapshot()]
    if named != list(dict.fromkeys(w.lstrip("+") for w, _ in sc["steps"] if w != "…")):
        print(f"  \033[38;5;99m⇢ 说话人现在叫：{'、'.join(named)}\033[0m")
    if s.engagement != sc.get("engagement", "normal"):
        print(f"  \033[38;5;99m⇢ 参与度变成：{s.engagement}\033[0m")
    print(f"  （共 {time.time() - t0:.0f}s）", flush=True)
    await floor.stop()
    hub.ears.close()


async def main() -> None:
    want = [a for a in sys.argv[1:] if not a.startswith("--")]
    for name, sc in SCENARIOS.items():
        if not want or any(w in name for w in want):
            await run(name, sc)


if __name__ == "__main__":
    asyncio.run(main())
