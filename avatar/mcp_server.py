"""把会议记录接给本机的 Claude Code、Codex 等 AI：一个只读的 MCP 服务（标准输入输出）。

它直接读 workspace/meetings/ 里的文件，不依赖后台是否在运行，正在开的会也读得到（最多落后一秒）。
只用标准库，启动快；由 AI 客户端按需拉起，注册方法见 `python -m avatar.connect`。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):          # 被客户端当脚本直接运行时，把项目根目录加进来
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from avatar.archive import Archive, dialogue_of, minutes_of  # noqa: E402

NAME = "meetings"
INSTRUCTIONS = (
    "这台电脑上的「会议数字人」参加过的会议：每场会的逐句对话、后台任务结果和会后纪要（结论、待办、没定的事）。"
    "用户提到「刚才的会」「上次会议」「会上说的」「会议纪要」「会上分给我的事」时，用这里的工具去读，不要凭印象回答。"
    "记录是语音识别的结果，有错字和同音字，搜索时多换几个说法。"
)

TOOLS = [
    {
        "name": "list_meetings",
        "description": "列出最近的会议，新的在前：编号、开始时间、时长、标题、说话人、句数、有没有纪要、文件路径。",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "最多列几场，默认 10", "minimum": 1, "maximum": 100}}},
    },
    {
        "name": "read_meeting",
        "description": "读一场会议。默认读最近一场的全部内容（纪要 + 逐句对话 + 后台任务结果）。正在开的会也能读。",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string", "description": "会议编号（list_meetings 给出的 id）；不填或填 latest 是最近一场"},
            "part": {"type": "string", "enum": ["all", "minutes", "dialogue"],
                     "description": "all = 全部（默认）；minutes = 只要纪要；dialogue = 只要逐句对话"},
            "offset": {"type": "integer", "minimum": 0,
                       "description": "从第几个字开始读，默认 0。长会一次读不完时，结尾会告诉你下一次的 offset"}}},
    },
    {
        "name": "search_meetings",
        "description": "在所有会议记录里搜关键词，返回命中的句子和它所在的会议。多个词用空格隔开，要求同一句里都出现。",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "关键词，空格分隔"},
            "limit": {"type": "integer", "description": "最多返回几条，默认 30", "minimum": 1, "maximum": 200}}},
    },
]


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "时间不详"


def list_meetings(archive: Archive, limit: int = 10) -> str:
    rows = archive.recent(max(1, min(int(limit or 10), 100)))
    if not rows:
        return "还没有任何会议记录。"
    out = []
    for r in rows:
        minutes = max(1, round((r["ended_at"] - r["started_at"]) / 60)) if r["started_at"] and r["ended_at"] else 0
        state = {"yes": "有纪要", "none": "无实质内容"}.get(r["minutes"], "还没整理纪要")
        out.append(
            f"- {r['id']}｜{_when(r['started_at'])}｜{minutes} 分钟｜{r['title'] or '（无标题）'}｜"
            f"{'、'.join(r['speakers']) or '无人发言'}｜{r['lines']} 句｜{state}｜{r['file']}")
    return "\n".join(out)


PAGE = 12000      # 一次最多返回这么多字：AI 客户端对工具输出有长度上限，一小时的会有三四万字


def _page(text: str, offset: int) -> str:
    offset = max(0, int(offset or 0))
    chunk = text[offset:offset + PAGE]
    left = len(text) - offset - len(chunk)
    if left > 0:
        chunk += f"\n\n（后面还有 {left} 字。接着读：offset={offset + len(chunk)}；只找某件事用 search_meetings 更快。）"
    return chunk


def read_meeting(archive: Archive, id: str = "latest", part: str = "all", offset: int = 0) -> str:  # noqa: A002
    meeting_id = str(id or "latest").strip()
    if meeting_id == "latest":
        meeting_id = next((r["id"] for r in archive.recent(1)), "")
    text = archive.read(meeting_id) if meeting_id else None
    if text is None:
        raise ValueError("没有这场会议的记录。先用 list_meetings 看有哪些。")
    if part == "minutes":
        return minutes_of(text) or "这场会还没有纪要。可以读 dialogue 自己归纳。"
    if part == "dialogue":
        return _page(dialogue_of(text), offset) or "这场会没有对话内容。"
    return _page(text, offset)


def search_meetings(archive: Archive, query: str, limit: int = 30) -> str:
    terms = [t.lower() for t in str(query or "").split() if t]
    if not terms:
        raise ValueError("要给出关键词。")
    limit = max(1, min(int(limit or 30), 200))
    hits: list[str] = []
    for r in archive.recent(500):
        text = archive.read(r["id"]) or ""
        body = text.partition("\n---\n")[2]
        found = [l.strip() for l in body.splitlines() if l.strip() and all(t in l.lower() for t in terms)]
        if found:
            hits.append(f"## {r['id']}｜{_when(r['started_at'])}｜{r['title'] or '（无标题）'}")
            for l in found:
                hits.append(l[:300])
                if sum(not h.startswith("## ") for h in hits) >= limit:
                    return "\n".join(hits) + "\n（命中太多，只列了前面的；用 read_meeting 读整场）"
    return "\n".join(hits) if hits else "没有搜到。语音识别有错字，换个说法或同音字再试。"


HANDLERS = {"list_meetings": list_meetings, "read_meeting": read_meeting, "search_meetings": search_meetings}


def handle(msg: dict, archive: Archive) -> dict | None:
    """处理一条 JSON-RPC 消息。通知（没有 id）不回。"""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": "1.0.0"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        fn = HANDLERS.get(params.get("name"))
        if fn is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "没有这个工具"}}
        try:
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            text, bad = fn(archive, **args), False
        except ValueError as e:
            text, bad = str(e), True
        except TypeError:
            text, bad = "参数不对。看一下这个工具的参数说明再试。", True
        return ok({"content": [{"type": "text", "text": text}], "isError": bad})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持 {method}"}}


def main() -> None:
    archive = Archive()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        try:
            reply = handle(msg, archive) if isinstance(msg, dict) else None
        except Exception as e:  # noqa: BLE001  一条坏请求不能让整个服务退出
            mid = msg.get("id") if isinstance(msg, dict) else None
            reply = None if mid is None else {
                "jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"处理这条请求时出错：{e}"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
