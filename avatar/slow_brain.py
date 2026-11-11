"""慢脑：按需启动一个本机 Claude Code 进程去干活（查资料 / 查代码 / 做设计）。

每个任务一个独立进程，只给它完成这类任务所需的最少工具。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Callable

from .config import DESIGN_DIR, WORKSPACE, Settings, child_env
from .floor import WorkResult
from .meeting import Task

log = logging.getLogger("avatar.slow")

_COMMON = "不要反问，不要寒暄，直接给结果。全程用中文。"

PROMPTS = {
    "查资料": (
        "你在给一场正在进行的会议做后台调研。会上需要这个信息：\n{brief}\n\n"
        "今天是 {today}。请联网搜索并核实，优先官方文档和一手来源。"
        "会议在等结果：最多搜索 4 次、打开 3 个网页；网页打不开就换一个来源，不要反复重试。\n"
        "输出 Markdown：第一段用两三句话直接给结论（会被口头转述给与会者）；然后是要点，最多 6 条；"
        "最后以「来源」为小标题列出链接。查不到或拿不准的地方直说。400 字以内。" + _COMMON
    ),
    "查代码": (
        "你在给一场正在进行的会议查这个项目的代码。问题：\n{brief}\n\n"
        "只读不改。输出 Markdown：第一段用两三句话直接回答（会被口头转述）；"
        "然后是关键位置，每处写「文件路径:行号」加一句说明，最多 8 处；有风险或拿不准的地方单列。400 字以内。" + _COMMON
    ),
    "做设计": (
        "你在给一场正在进行的会议出一份设计稿。需求：\n{brief}\n\n"
        "写一个可以直接用浏览器打开的网页，保存到这个绝对路径：{target}\n"
        "要求：单文件，样式和脚本内联，不引用外网资源，"
        "同时适配浅色、深色和手机宽度。内容用真实合理的中文示例数据，不要占位文字。"
        "界面上不要放解释设计思路的说明文字。\n"
        "写完后回复三四句话：做了什么、关键取舍、还有什么没定。" + _COMMON
    ),
}

# 每类任务最多做多久（秒）。会议在等，宁可说没查到也不要一直卡着
TIME_LIMITS = {"查资料": 240, "查代码": 240, "做设计": 600}

STALL = 60   # 这么久没有任何输出就当作卡死，重来一次

TOOLS = {
    "查资料": "WebSearch,WebFetch",
    "查代码": "Read,Grep,Glob",
    "做设计": "Read,Grep,Glob,Write,Edit",
}


def describe(tool: str, inp: dict) -> str:
    """把工具调用翻译成人话进度。"""
    name = Path(str(inp.get("file_path") or inp.get("path") or "")).name
    if tool == "WebSearch":
        return f"正在搜索：{str(inp.get('query', ''))[:40]}"
    if tool == "WebFetch":
        host = str(inp.get("url", "")).split("/")[2:3]
        return f"正在读网页 {host[0] if host else ''}".strip()
    if tool == "Read":
        return f"正在读 {name}" if name else "正在读文件"
    if tool == "Grep":
        return f"正在搜代码：{str(inp.get('pattern', ''))[:30]}"
    if tool == "Glob":
        return "正在找文件"
    if tool in ("Write", "Edit"):
        return f"正在写 {name}" if name else "正在写文件"
    return "正在处理"


class SlowBrain:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def run(self, task: Task, on_progress: Callable[[str], None]) -> WorkResult:
        s = self.settings
        project = Path(s.project_dir).expanduser() if s.project_dir else None
        artifact = ""
        extra: list[str] = []

        if task.kind == "查代码":
            if not project or not project.is_dir():
                return WorkResult(False, "还没有登记项目目录，没法查代码")
            cwd = project
        elif task.kind == "做设计":
            folder = f"task-{task.id}-{int(time.time())}"
            cwd = DESIGN_DIR / folder
            cwd.mkdir(parents=True, exist_ok=True)
            artifact = f"/designs/{folder}/index.html"
            extra += ["--permission-mode", "acceptEdits"]
            if project and project.is_dir():
                extra += ["--add-dir", str(project)]
        else:
            cwd = WORKSPACE / "research"
            cwd.mkdir(parents=True, exist_ok=True)

        prompt = PROMPTS[task.kind].format(brief=task.brief, today=dt.date.today().isoformat(),
                                           target=cwd / "index.html")
        if task.kind == "做设计" and s.use_my_skills:
            # 加载用户本机的技能和插件；声明自己是被派出的子任务，避免流程类技能停下来提问
            prompt = "你是被派出执行具体任务的子代理，没有人可以回答你的问题，直接完成任务。\n\n" + prompt
        else:
            extra += ["--setting-sources", "", "--disable-slash-commands", "--strict-mcp-config"]
        if s.slow_model:
            extra += ["--model", s.slow_model]

        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",     # 生成过程中持续有输出，才能分辨「在干活」和「卡死了」
               "--no-session-persistence", "--tools", TOOLS[task.kind],
               "--allowedTools", TOOLS[task.kind], *extra]
        on_progress("正在准备")
        result: WorkResult | None = None
        try:
            async with asyncio.timeout(TIME_LIMITS[task.kind]):
                for attempt in (1, 2):
                    result, stalled = await self._attempt(cmd, cwd, on_progress)
                    if result is not None or not stalled:
                        break
                    if attempt == 1:
                        on_progress("网络不稳，正在重试")
        except asyncio.TimeoutError:
            return WorkResult(False, "花的时间太长，没做完")
        if result is None:
            return WorkResult(False, "网络不稳，试了两次都没做成")
        if result.ok and artifact:
            if (cwd / "index.html").exists():
                result.artifact = artifact
            else:
                return WorkResult(False, "设计稿没有生成出来")
        return result

    async def _attempt(self, cmd: list[str], cwd: Path, on_progress: Callable[[str], None]) -> tuple[WorkResult | None, bool]:
        """跑一次。返回（结果，是否卡死）。超过 STALL 秒没有任何输出算卡死。"""
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, env=child_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=16 << 20,
        )
        result: WorkResult | None = None
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), STALL)
                except asyncio.TimeoutError:
                    return None, True
                if not raw:
                    break
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("type") == "assistant":
                    for block in ev.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            on_progress(describe(block.get("name", ""), block.get("input") or {}))
                elif ev.get("type") == "result":
                    text = (ev.get("result") or "").strip()
                    ok = not ev.get("is_error") and bool(text)
                    result = WorkResult(ok, text or "没有得到结果")
            return result, False
        finally:
            if proc.returncode is None:
                proc.kill()
