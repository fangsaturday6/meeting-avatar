"""会议存档：每场会一个 Markdown 文件，放在 workspace/meetings/，人和 Claude 都能直接读。

文件随会议进行整份重写（先写临时文件再替换），所以任何时刻打开都是完整的。
开头的元数据块一行一项，列表接口只读这一块。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import DESIGN_DIR, WORKSPACE
from .textutil import tidy

if TYPE_CHECKING:
    from .meeting import Meeting

MEETINGS_DIR = WORKSPACE / "meetings"

_STAMP = "%Y-%m-%d %H:%M:%S"
_ID = re.compile(r"[\w-]+")     # 文件名里不允许出现点和斜杠，接口传来的编号走不出这个目录
_TASK_STATE = {"running": "进行中", "done": "完成", "failed": "失败"}


def _stamp(t: float) -> str:
    return time.strftime(_STAMP, time.localtime(t))


def _clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def _epoch(stamp: str | None) -> float:
    try:
        return time.mktime(time.strptime(stamp or "", _STAMP))
    except ValueError:
        return 0.0


def _one_line(s: str) -> str:
    return " ".join(s.split())


def _task_block(t) -> list[str]:
    span = f"{_clock(t.started)} – {_clock(t.ended)}" if t.ended else f"{_clock(t.started)} 起"
    out = [f"### {t.kind}：{_one_line(t.brief)}", "", f"{span} · {_TASK_STATE.get(t.status, t.status)}", ""]
    if t.result.strip():
        out += [t.result.strip(), ""]
    if t.artifact:
        out += [f"设计稿：{DESIGN_DIR / t.artifact.removeprefix('/designs/')}", ""]
    return out


NO_CONTENT = "无实质内容"

SAME_PARAGRAPH = 2.0    # 同一个人停顿不超过这么久又接着说：记成一段
SAME_SENTENCE = 1.0     # 停顿比这还短：是断句把一句话切开了，中间的句号换成逗号
PARAGRAPH = 160         # 一段攒到这么多字就另起一段


def _join(a: str, b: str, pause: float) -> str:
    if a.endswith("。") and pause < SAME_SENTENCE:
        a = a[:-1] + "，"
    elif a[-1:].isalnum():      # 前一句没带标点（打字的、被截断的）
        a += " " if a[-1:].isascii() and b[:1].isascii() else "，"
    return a + b


def paragraphs(lines) -> list[tuple]:
    """逐句的识别结果 → 能读的记录，每项是（这一段的第一句，整理后的文字）。

    人说的话去掉语气词和口吃（只剩语气词的整句不要），同一个人连着说的并成一段。
    数字人说的话和旁注原样保留。内存里的逐句原话不动，接话逻辑和界面上的实时字幕用的还是原话。
    """
    out: list[list] = []     # [第一句, 文字, 最后一句]
    for l in lines:
        text = _one_line(l.text)
        if l.who != "human":
            out.append([l, text, l])
            continue
        if not tidy(text):
            continue
        prev = out[-1] if out else None
        if (prev and prev[2].who == "human" and prev[2].speaker_id == l.speaker_id and len(prev[1]) < PARAGRAPH
                and (pause := l.t - l.seconds - prev[2].t) <= SAME_PARAGRAPH):
            prev[1], prev[2] = _join(prev[1], text, pause), l
        else:
            out.append([l, text, l])
    return [(first, tidy(text) if first.who == "human" else text) for first, text, _ in out]


def split_minutes(raw: str) -> tuple[str, str]:
    """把整理纪要的人交回来的文字拆成（标题，正文）。第一行是标题。"""
    lines = [l.rstrip() for l in raw.strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return "", ""
    if re.match(r"\s*(#{2,}|[-*•]\s)", lines[0]):
        # 没写标题，上来就是「### 待办」或一条要点：整段都是正文，别把小节名当成会议标题
        return "", "\n".join(lines).strip()
    title = re.sub(r"^(会议)?标题\s*[：:]\s*", "", _one_line(lines[0]).strip("#*「」《》\"' ")).strip("「」《》\"' ")
    body = "\n".join(lines[1:]).strip()
    if title.startswith(NO_CONTENT) or not body:
        return NO_CONTENT, ""       # 只有一行、没有正文：不管它怎么措辞，都是「没有可整理的内容」
    return title[:30], body


def with_minutes(text: str, title: str, summary: str, polished: str = "") -> str:
    """在一份会议记录里写入（或替换）标题、纪要和整理稿。它们放在逐句对话前面，打开文件先看到的就是它们。

    整理稿是会后把对话书面化的版本（改错字、去口头禅），格式和「对话」一样；没有就不写这一节。
    """
    head, sep, rest = text.partition("\n---\n")
    if not sep or not head.startswith("---"):
        return text
    keep = [l for l in head.splitlines()[1:] if l.partition(":")[0].strip() not in ("title", "minutes")]
    keep.insert(1 if keep else 0, f"title: {_one_line(title)}")
    keep.append(f"minutes: {'yes' if summary.strip() else 'none'}")
    before, mark, after = rest.partition("\n## 对话\n")
    if not mark:
        return text
    before = before.split("\n## 纪要\n")[0].split("\n## 整理稿\n")[0].rstrip("\n")
    # 纪要里的大标题一律降成三级：文件靠「## 对话」这样的二级标题分节，纪要里不能出现
    summary = re.sub(r"(?m)^#{1,2}\s+", "### ", summary.strip())
    block = f"\n\n## 纪要\n\n{summary}\n" if summary else "\n"
    if polished.strip():
        block = block.rstrip("\n") + f"\n\n## 整理稿\n\n{polished.strip()}\n"
    return "---\n" + "\n".join(keep) + sep + before + block + mark + after


def minutes_of(text: str) -> str:
    """一份会议记录里的纪要正文；没有就是空。只认「对话」之前的那一节（后面任务结果里引用的不算）。"""
    return text.partition("\n## 对话\n")[0].partition("\n## 纪要\n")[2].partition("\n## 整理稿\n")[0].strip()


def polished_of(text: str) -> str:
    """一份会议记录里的整理稿（书面化之后的对话）；没有就是空。"""
    return text.partition("\n## 对话\n")[0].partition("\n## 整理稿\n")[2].strip()


def dialogue_of(text: str) -> str:
    """一份会议记录里的对话和后台任务部分（不含元数据和已有的纪要），给整理纪要用。"""
    return text.partition("\n## 对话\n")[2].strip()


def render(m: "Meeting") -> str:
    """一场会的全部内容：元数据、纪要、逐句对话（带时刻和说话人）、后台任务及结果。"""
    text = _render(m)
    return with_minutes(text, m.title, m.summary, m.polished) if m.title or m.summary else text


def _render(m: "Meeting") -> str:
    end = m.ended_at or (m.lines[-1].t if m.lines else m.started_at)   # 没正常结束的会，以最后一句为准
    head = {
        "id": m.record_id,
        "started": _stamp(m.started_at),
        "ended": _stamp(end),
        "bot": m.settings.name,
        "owner": _one_line(m.settings.owner),
        "speakers": "、".join(s["name"] for s in m.speakers.snapshot()),
        "lines": sum(l.who != "note" and (l.who != "human" or bool(tidy(l.text))) for l in m.lines),
        "topic": _one_line(m.settings.topic),
    }
    out = ["---", *(f"{k}: {v}" for k, v in head.items()), "---", "",
           f"# 会议记录 {head['started'][:16]}", "", "## 对话", ""]
    for l, text in paragraphs(m.lines):
        out += [f"[{_clock(l.t)}] {l.speaker or '旁注'}：{text}{'（被打断）' if l.cut else ''}", ""]
    if m.tasks:
        out += ["## 后台任务", ""]
        for t in m.tasks:
            out += _task_block(t)
    return "\n".join(out)


def _meta(path: Path) -> dict:
    head: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as f:
            if f.readline().strip() == "---":
                for line in f:
                    if line.strip() == "---":
                        break
                    key, _, value = line.partition(":")
                    head[key.strip()] = value.strip()
    except (OSError, ValueError):
        pass    # 读不了或被手工改坏的文件：照样列出来，只是没有摘要
    return {
        "id": path.stem,
        "started_at": _epoch(head.get("started")),
        "ended_at": _epoch(head.get("ended")),
        "lines": int(head["lines"]) if head.get("lines", "").isdigit() else 0,
        "speakers": [s for s in head.get("speakers", "").split("、") if s],
        "bot": head.get("bot", ""),
        "topic": head.get("topic", ""),
        "title": head.get("title", ""),
        "minutes": head.get("minutes", ""),      # yes = 有纪要；none = 整理过但没有实质内容；空 = 还没整理
        "file": str(path.resolve()),
    }


class Archive:
    def __init__(self, root: Path = MEETINGS_DIR):
        self.root = Path(root)

    def _path(self, meeting_id: str) -> Path:
        return self.root / f"{meeting_id}.md"

    def new_id(self, started_at: float) -> str:
        """文件名就是开始时间，按名字排序即按时间排序。"""
        base = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(started_at))
        meeting_id, n = base, 1
        while self._path(meeting_id).exists():
            n += 1
            meeting_id = f"{base}_{n}"      # 下划线排在点号后面，同一秒开的第二场仍排在后面
        return meeting_id

    def save(self, m: "Meeting") -> bool:
        """把这场会整份写盘。还没有任何内容时不建文件。返回这次是不是新建了文件。"""
        if not m.record_id or not (m.lines or m.tasks):
            return False
        path = self._path(m.record_id)
        created = not path.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(render(m), encoding="utf-8")
        os.replace(tmp, path)
        return created

    def recent(self, limit: int = 50) -> list[dict]:
        """最近的会议，新的在前。"""
        paths = sorted(self.root.glob("*.md"), key=lambda p: p.stem, reverse=True)
        return [_meta(p) for p in paths[:limit]]

    def set_minutes(self, meeting_id: str, title: str, summary: str, polished: str = "") -> bool:
        """把纪要（和整理稿）写进已经存盘的会议记录。"""
        text = self.read(meeting_id)
        if text is None:
            return False
        path = self._path(meeting_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(with_minutes(text, title, summary, polished), encoding="utf-8")
        os.replace(tmp, path)
        return True

    def last_minutes(self, skip: str = "") -> str:
        """最近一场有纪要的会：标题加纪要正文。开下一场会之前给数字人看，它才接得上「上次说的那个」。"""
        for row in self.recent(8):
            if row["id"] == skip or row["minutes"] != "yes":
                continue
            body = minutes_of(self.read(row["id"]) or "")
            if body:
                day = time.strftime("%m月%d日", time.localtime(row["started_at"])) if row["started_at"] else ""
                return f"{day}「{row['title']}」\n{body[:1200]}"
        return ""

    def read(self, meeting_id: str) -> str | None:
        if not _ID.fullmatch(meeting_id):
            return None
        try:
            return self._path(meeting_id).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
