"""MCP 服务：本机的 Claude Code / Codex 通过它读会议记录。"""
import json
import subprocess
import sys
from pathlib import Path

from avatar.archive import Archive
from avatar.mcp_server import handle

ROOT = Path(__file__).resolve().parent.parent


def seed(tmp_path) -> Archive:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "2026-11-10_100000.md").write_text(
        "---\nid: 2026-11-10_100000\ntitle: 登录排期\nstarted: 2026-11-10 10:00:00\nended: 2026-11-10 10:30:00\n"
        "bot: 小克\nspeakers: 张三、李四\nlines: 3\ntopic: \nminutes: yes\n---\n\n# 会议记录 2026-11-10 10:00\n\n"
        "## 纪要\n\n### 结论\n- 先做登录\n\n## 对话\n\n[10:00:05] 张三：我们先做登录还是支付\n\n"
        "[10:00:09] 小克：先做登录，支付依赖它。\n\n[10:00:20] 李四：供应商接口下周上线\n", encoding="utf-8")
    (tmp_path / "2026-11-12_000458.md").write_text(
        "---\nid: 2026-11-12_000458\nstarted: 2026-11-12 00:04:58\nended: 2026-11-12 00:10:01\n"
        "bot: 小克\nspeakers: 张三\nlines: 1\ntopic: \n---\n\n# 会议记录 2026-11-12 00:04\n\n"
        "## 对话\n\n[00:07:10] 张三：供应商那个接口上线了吗\n", encoding="utf-8")
    return Archive(tmp_path)


def call(archive, name, **args):
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}, archive)
    return r["result"]["content"][0]["text"], r["result"]["isError"]


def test_handshake_and_tool_list(tmp_path):
    a = seed(tmp_path)
    init = handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, a)
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["serverInfo"]["name"] == "meetings"
    assert "会议" in init["result"]["instructions"]
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, a) is None
    tools = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, a)["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_meetings", "read_meeting", "search_meetings"]
    assert handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, a)["error"]["code"] == -32601


def test_list_read_and_search(tmp_path):
    a = seed(tmp_path)
    text, bad = call(a, "list_meetings")
    lines = text.splitlines()
    assert not bad and lines[0].startswith("- 2026-11-12_000458｜2026-11-12 00:04｜5 分钟｜（无标题）｜张三｜1 句｜还没整理纪要｜")
    assert "登录排期" in lines[1] and "有纪要" in lines[1] and lines[1].endswith("2026-11-10_100000.md")
    assert call(a, "list_meetings", limit=1)[0].count("\n") == 0

    assert "供应商那个接口上线了吗" in call(a, "read_meeting")[0]                    # 默认最近一场
    assert call(a, "read_meeting", id="2026-11-10_100000", part="minutes")[0] == "### 结论\n- 先做登录"
    dialogue = call(a, "read_meeting", id="2026-11-10_100000", part="dialogue")[0]
    assert dialogue.startswith("[10:00:05] 张三") and "纪要" not in dialogue
    assert "还没有纪要" in call(a, "read_meeting", part="minutes")[0]
    # 整理稿：有就给整理稿，还没有就给逐句对话
    assert call(a, "read_meeting", id="2026-11-10_100000", part="polished")[0] == dialogue
    a.set_minutes("2026-11-10_100000", "登录排期", "### 结论\n- 先做登录", "[10:00:05] 张三：先做登录。")
    assert call(a, "read_meeting", id="2026-11-10_100000", part="polished")[0] == "[10:00:05] 张三：先做登录。"
    assert call(a, "read_meeting", id="2026-11-10_100000", part="minutes")[0] == "### 结论\n- 先做登录"
    assert call(a, "read_meeting", id="2026-11-10_100000", part="dialogue")[0] == dialogue
    assert call(a, "read_meeting", id="../../etc/passwd") == ("没有这场会议的记录。先用 list_meetings 看有哪些。", True)

    text, bad = call(a, "search_meetings", query="供应商 接口")
    assert not bad and text.count("## ") == 2 and "李四：供应商接口下周上线" in text and "张三：供应商那个接口上线了吗" in text
    assert "先做登录还是支付" not in text
    assert call(a, "search_meetings", query="火星")[0].startswith("没有搜到")
    assert call(a, "search_meetings", query="  ")[1] is True
    unknown = handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "sing"}}, a)
    assert unknown["error"]["code"] == -32602


def test_runs_as_a_plain_script_over_stdio(tmp_path):
    """客户端是直接拿 python 跑这个文件的：不在项目目录里、没有包环境也要能起来。"""
    seed(tmp_path / "meetings")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_meeting", "arguments": {"part": "dialogue"}}}]
    r = subprocess.run([sys.executable, str(ROOT / "avatar" / "mcp_server.py")], cwd=str(tmp_path),
                       input="\n".join(json.dumps(m) for m in msgs) + "\nnot json\n", capture_output=True, text=True,
                       timeout=20, env={"AVATAR_WORKSPACE": str(tmp_path), "PATH": "/usr/bin:/bin"})
    out = [json.loads(l) for l in r.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, 2], r.stderr
    assert out[1]["result"]["content"][0]["text"] == "[00:07:10] 张三：供应商那个接口上线了吗"


def test_bad_requests_get_an_error_instead_of_killing_the_server(tmp_path):
    seed(tmp_path / "meetings")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_meeting", "arguments": {"id": 123}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_meetings", "arguments": {"query": 7}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": "不是对象"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_meeting", "arguments": {"nope": 1}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "list_meetings", "arguments": {"limit": "很多"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "list_meetings"}}]
    r = subprocess.run([sys.executable, str(ROOT / "avatar" / "mcp_server.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, timeout=20, env={"AVATAR_WORKSPACE": str(tmp_path), "PATH": "/usr/bin:/bin"})
    out = {o["id"]: o for o in map(json.loads, r.stdout.splitlines())}
    assert sorted(out) == [1, 2, 3, 4, 5, 6], r.stderr           # 每条都有回音，进程一直活着
    assert out[1]["result"]["isError"] and out[4]["result"]["isError"] and out[5]["result"]["isError"]
    assert not out[2]["result"]["isError"]                       # 数字当关键词也能搜，不会崩
    assert "error" in out[3] and "2026-11-12_000458" in out[6]["result"]["content"][0]["text"]


def test_long_meetings_are_read_in_pages(tmp_path):
    from avatar import mcp_server
    a = seed(tmp_path)
    body = "".join(f"[10:{i // 60:02d}:{i % 60:02d}] 张三：第{i}句话，讨论登录接口的排期和人手安排。\n\n" for i in range(900))
    (tmp_path / "2026-11-13_090000.md").write_text(
        "---\nid: 2026-11-13_090000\nstarted: 2026-11-13 09:00:00\nended: 2026-11-13 10:00:00\nlines: 900\n---\n\n"
        "# 会议记录\n\n## 对话\n\n" + body, encoding="utf-8")
    first, bad = call(a, "read_meeting", part="dialogue")
    assert not bad and len(first) < mcp_server.PAGE + 200 and "第0句话" in first
    nxt = int(first.rsplit("offset=", 1)[1].split("；")[0])
    assert nxt == mcp_server.PAGE
    pages, offset = [first], nxt
    while "offset=" in pages[-1]:
        pages.append(call(a, "read_meeting", part="dialogue", offset=offset)[0])
        offset += mcp_server.PAGE
    assert "第899句话" in pages[-1] and "后面还有" not in pages[-1] and len(pages) >= 3
