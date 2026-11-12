"""把会议记录接给本机的 AI：uv run python -m avatar.connect

在 Claude Code 和 Codex 里各登记一个名叫 meetings 的只读工具（见 mcp_server.py）。
登记之后，在任何项目里对它们说「看下刚才的会」「上次会上分给我什么事」，它们都能自己去读。
加 --remove 取消登记。重复运行没有副作用。

设置窗口里「接给我的智能体」那一组走的也是这里（Connector）；别的智能体用 guide() 给出的那段话自己接。
"""
from __future__ import annotations

import argparse
import logging
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .mcp_server import NAME

log = logging.getLogger("avatar.connect")
SERVER = [sys.executable, str(Path(__file__).resolve().parent / "mcp_server.py")]

# 能一键登记的客户端。编号是接口和界面元素的名字（/api/agents/claude、#agent-claude），不要改
CLIENTS = {
    "claude": {
        "name": "Claude Code",
        "bin": "claude",
        "get": ["claude", "mcp", "get", NAME],
        "remove": ["claude", "mcp", "remove", "-s", "user", NAME],
        "add": ["claude", "mcp", "add", "-s", "user", NAME, "--", *SERVER],
    },
    "codex": {
        "name": "Codex",
        "bin": "codex",
        "get": ["codex", "mcp", "get", NAME],
        "remove": ["codex", "mcp", "remove", NAME],
        "add": ["codex", "mcp", "add", NAME, "--", *SERVER],
    },
}

# 一个客户端的状态：on 已接上 / off 没接 / missing 没装 / taken 同名的位置被别的服务占着 / unknown 没能确认
STATES = ("on", "off", "missing", "taken", "unknown")


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def status(client_id: str) -> str:
    """这个客户端现在接没接上。要起一个子进程去问（一两秒），别在事件循环里调。"""
    c = CLIENTS[client_id]
    if not shutil.which(c["bin"]):
        return "missing"
    exists, info = _run(c["get"])
    if not exists:
        # 超时或配置读不了时不当成「没接」：宁可不登记，也不冒险盖掉别的
        return "off" if "No MCP server named" in info else "unknown"
    ours = "avatar/mcp_server.py" in info.replace("\\", "/")     # 仓库搬过家也认得出是我们的
    return "on" if ours else "taken"


def problem(client_id: str, state: str) -> str:
    """这个状态下为什么动不了，说给人听的。能动就返回空。"""
    name = CLIENTS[client_id]["name"]
    return {
        "missing": f"这台电脑上没装 {name}",
        "taken": f"{name} 里已经有一个叫 {NAME} 的别的服务，没动它",
        "unknown": f"没能确认 {name} 现在的配置，没动它",
    }.get(state, "")


def set_connected(client_id: str, connected: bool, state: str = "") -> str:
    """接上或取消，返回之后的状态。动不了或没成时抛 ValueError，内容是说给人听的原因。

    state：刚查过现在的状态就传进来，省得再问一遍。
    """
    c = CLIENTS[client_id]
    state = state or status(client_id)
    if why := problem(client_id, state):
        raise ValueError(why)
    if state == "on":
        _run(c["remove"])        # 重新登记前先拿掉旧的：仓库搬过家、换过 Python 时路径会变
    if not connected:
        return "off"
    ok, msg = _run(c["add"])
    if not ok:
        log.warning("%s 没接上：%s", c["name"], msg[-300:])      # 原始报错进日志，不上界面
        raise ValueError(f"没接上 {c['name']}，过一会儿再试")
    return "on"


class Connector:
    """接口层用的那一面。测试和演示后台换成假的，不碰真实的 Claude / Codex 配置。"""

    def clients(self) -> list[dict]:
        with ThreadPoolExecutor(len(CLIENTS)) as pool:       # 一个一个问要三四秒，一起问
            states = list(pool.map(status, CLIENTS))
        return [{"id": cid, "name": c["name"], "state": st} for (cid, c), st in zip(CLIENTS.items(), states)]

    def set(self, client_id: str, connected: bool) -> None:
        set_connected(client_id, connected)


def guide(base_url: str) -> str:
    """写给智能体看的接入说明：用户复制下来，粘贴给自己的智能体，它照着把会议记录接成自己的工具。"""
    base = base_url.rstrip("/")
    return (
        "这台电脑上的「会议数字人」存着我开过的每一场会：纪要（结论、待办、没定的事）、整理稿和逐句对话。\n"
        "请把它接成你的工具。之后我提到「刚才的会」「上次会议」「会上分给我的事」，就去读，不要凭印象回答。\n"
        "\n"
        "接法任选一种：\n"
        f"1. MCP（HTTP）：服务名 {NAME}，地址 {base}/mcp 。数字人开着的时候可用。\n"
        f"2. MCP（本地命令，标准输入输出）：服务名 {NAME}，命令 {shlex.join(SERVER)} 。数字人没开也能用。\n"
        f"3. 不支持 MCP 就直接请求：GET {base}/api/meetings 是会议列表（JSON，新的在前）；"
        f"GET {base}/api/meetings/latest 是最近一场的全文（Markdown），把 latest 换成列表里的 id 读别的场次。\n"
        "\n"
        "MCP 的三个工具都是只读的：list_meetings 列出最近的会，read_meeting 读一场（可以只要纪要），"
        "search_meetings 按关键词搜。\n"
        "记录是语音识别的结果，有错字和同音字，搜索时多换几个说法。\n"
    )


def connect(remove: bool = False) -> dict[str, str]:
    """命令行用：每个客户端都接上（或都取消），返回各自的结果。"""
    out = {}
    for cid, c in CLIENTS.items():
        state = status(cid)
        if state == "missing":
            out[c["name"]] = "没装，跳过"
        elif remove and state == "off":
            out[c["name"]] = "本来就没登记"
        else:
            try:
                out[c["name"]] = "已接上" if set_connected(cid, not remove, state) == "on" else "已取消"
            except ValueError as e:
                out[c["name"]] = str(e)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="把会议记录接给本机的 Claude Code 和 Codex")
    ap.add_argument("--remove", action="store_true", help="取消登记")
    args = ap.parse_args()
    for label, result in connect(args.remove).items():
        print(f"  {label}：{result}")
    if not args.remove:
        print("\n  新开一个 Claude Code 或 Codex 会话，说「看下最近一场会议」试试。")


if __name__ == "__main__":
    main()
