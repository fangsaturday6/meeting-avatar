"""把会议记录接给本机的 AI：uv run python -m avatar.connect

在 Claude Code 和 Codex 里各登记一个名叫 meetings 的只读工具（见 mcp_server.py）。
登记之后，在任何项目里对它们说「看下刚才的会」「上次会上分给我什么事」，它们都能自己去读。
加 --remove 取消登记。重复运行没有副作用。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .mcp_server import NAME

SERVER = [sys.executable, str(Path(__file__).resolve().parent / "mcp_server.py")]

SCRIPT = SERVER[1]

CLIENTS = {
    "Claude Code": {
        "bin": "claude",
        "get": ["claude", "mcp", "get", NAME],
        "remove": ["claude", "mcp", "remove", "-s", "user", NAME],
        "add": ["claude", "mcp", "add", "-s", "user", NAME, "--", *SERVER],
    },
    "Codex": {
        "bin": "codex",
        "get": ["codex", "mcp", "get", NAME],
        "remove": ["codex", "mcp", "remove", NAME],
        "add": ["codex", "mcp", "add", NAME, "--", *SERVER],
    },
}


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def connect(remove: bool = False) -> dict[str, str]:
    """返回每个客户端的结果：已接上 / 已取消 / 没装 / 失败原因。"""
    out = {}
    for label, c in CLIENTS.items():
        if not shutil.which(c["bin"]):
            out[label] = "没装，跳过"
            continue
        exists, info = _run(c["get"])
        if not exists and "No MCP server named" not in info:
            out[label] = "没能确认现有的配置，没动它"       # 超时或配置读不了：宁可不登记，也不冒险盖掉别的
            continue
        ours = exists and "avatar/mcp_server.py" in info.replace("\\", "/")    # 仓库搬过家也认得出是我们的
        if exists and not ours:
            out[label] = f"已经有一个叫 {NAME} 的别的服务，没动它"      # 不是我们登记的，不碰
            continue
        if ours:
            _run(c["remove"])
        if remove:
            out[label] = "已取消" if ours else "本来就没登记"
            continue
        ok, msg = _run(c["add"])
        out[label] = "已接上" if ok else f"没接上：{msg.splitlines()[-1] if msg else '原因不明'}"
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
