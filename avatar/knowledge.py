"""它对你的了解：这台电脑上有哪些项目、各是干什么的，以及你的 Claude 已经记下的事。

项目：只看目录名和每个项目的说明文件开头（CLAUDE.md / AGENTS.md / README），不读代码。
快脑靠这份清单在会上听懂「供应商那个项目」指的是哪个；慢脑靠它知道该去哪个目录查。

记忆：你平时用的 Claude Code 在每个项目里攒的记忆（~/.claude/projects/<项目>/memory/）、你给它定的
全局规矩（~/.claude/CLAUDE.md），以及你最近和它的每次会话的标题（知道你最近在忙什么）。
数字人是你的分身，这些它也该知道。会话只读标题，不读对话内容。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

_MARKERS = (".git", "CLAUDE.md", "AGENTS.md", "README.md", "readme.md", "README", "package.json",
            "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml", "composer.json", "requirements.txt")
_DOCS = ("CLAUDE.md", "AGENTS.md", "README.md", "readme.md", "README")
_SKIP = {"node_modules", "__pycache__", "venv", "dist", "build", "target", "vendor"}
# 你的 Claude Code 的家目录。测试用 AVATAR_CLAUDE_HOME 指到临时目录，免得读到真实的记忆
CLAUDE_HOME = Path(os.environ.get("AVATAR_CLAUDE_HOME") or Path.home() / ".claude")
_TTL = 3600.0     # 扫一遍约 0.2 秒；会议中不要反复扫，新项目最多一小时后认出来（重启后台立刻认）
_cache: dict[str, tuple[float, list["Project"]]] = {}


@dataclass
class Project:
    name: str        # 相对总目录的路径，如 Wxauto/wx-auto-android
    path: str
    about: str       # 一句话说明，取自说明文件
    mtime: float
    memory: str = ""  # 你的 Claude 在这个项目里的记忆目录；没有就是空

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "about": self.about}


def _is_project(d: Path) -> bool:
    try:
        return any((d / m).exists() for m in _MARKERS)
    except OSError:
        return False      # 进不去的文件夹（没权限、受系统保护）：跳过它，别让整份清单都没了


_JUNK = re.compile(r"(?i)^(this file provides guidance|template$|严肃声明|@|\[.*\]\(.*\)$|pip install|npm |yarn |license|copyright)")
_FILE_TITLE = re.compile(r"(?i)^(claude|agents|readme)(\.md)?\b.*|^项目(说明|总述)$|^说明$|^template$")


def _about(d: Path) -> str:
    """说明文件里的标题加第一句正文。模板套话不要，换下一个说明文件再找。"""
    for name in _DOCS:
        f = d / name
        try:
            with f.open(encoding="utf-8", errors="ignore") as fh:
                head = fh.read(3000)
        except OSError:
            continue
        title, body = "", ""
        for raw in head.splitlines():
            line = raw.strip()
            if not line or line.startswith(("![", "<", "[!", "---", "```", "|", "=")):
                continue
            if line.startswith("#"):
                if not title:
                    title = line.lstrip("# ").strip()
                elif body:
                    break
                continue
            line = re.sub(r"[*_`>]+", "", re.sub(r"^[-•]\s+", "", line)).strip()
            if line and not _JUNK.match(line):
                body = line
                break
        # 标题是文件名、模板名，或者就是目录名：没有信息量
        if _FILE_TITLE.match(title) or _JUNK.match(title) or title.lower() == d.name.lower():
            title = ""
        text = "：".join(x for x in (title, body) if x)
        if text:
            return " ".join(text.split())[:90]
    return ""


def _stamp(d: Path) -> float:
    for probe in (d / ".git" / "index", d / ".git", d):
        try:
            return probe.stat().st_mtime
        except OSError:
            continue
    return 0.0


def _is_dir(d: Path) -> bool:
    try:
        return d.is_dir()
    except OSError:
        return False


def _children(d: Path) -> list[Path]:
    try:
        return sorted(c for c in d.iterdir()
                      if not c.name.startswith(".") and c.name not in _SKIP and _is_dir(c))
    except OSError:
        return []


def _project(name: str, d: Path) -> Project:
    mem = memory_dir(d)
    return Project(name, str(d), _about(d), _stamp(d), str(mem) if mem else "")


# ---------- 你的 Claude 记下的事 ----------
def memory_dir(project: Path | str) -> Path | None:
    """Claude Code 给这个项目存记忆的目录（它把项目路径里的斜杠、点等都换成横线当目录名）。"""
    d = CLAUDE_HOME / "projects" / _encode(Path(project).expanduser()) / "memory"
    return d if (d / "MEMORY.md").is_file() else None


def _read_head(path: Path, limit: int) -> str:
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            text = fh.read(limit + 200).strip()
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n……"


def memory_index(project: Path | str, limit: int = 1500) -> str:
    """这个项目的记忆目录里的索引（MEMORY.md，一条记忆一行）。"""
    d = memory_dir(project)
    return _read_head(d / "MEMORY.md", limit) if d else ""


def project_brief(project: Path | str, limit: int = 1200) -> str:
    """一个项目的简介：README（没有就用 CLAUDE.md / AGENTS.md）第一个小节之前的那段话。

    只给这场会的主项目用——清单里的一句话不够它听懂会上在聊什么。
    """
    d = Path(project).expanduser()
    for name in ("README.md", "readme.md", "README", "CLAUDE.md", "AGENTS.md"):
        text = _read_head(d / name, 4000)
        if not text:
            continue
        lead = re.split(r"(?m)^##\s", text, maxsplit=1)[0]
        lead = "\n".join(l for l in lead.splitlines() if not l.lstrip().startswith(("![", "<", "[!", "```")))
        lead = re.sub(r"\n{3,}", "\n\n", lead).strip()
        if len(lead) > 15:
            return lead if len(lead) <= limit else lead[:limit].rsplit("\n", 1)[0] + "\n……"
    return ""


def owner_rules(limit: int = 1500) -> str:
    """你给 Claude 定的全局规矩（~/.claude/CLAUDE.md）。"""
    return _read_head(CLAUDE_HOME / "CLAUDE.md", limit)


def _encode(path: Path | str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


@dataclass
class Session:
    t: float
    project: str     # 项目在清单里的名字
    title: str
    cwd: str         # 这次会话是在哪个目录里进行的


_CWD = re.compile(r'"cwd":\s*("(?:[^"\\]|\\.)*")')


def _session_info(f: Path) -> tuple[str, str]:
    """一次 Claude 会话的（标题，工作目录）。两样在文件里都反复出现，读末尾一段就能找到；没起过标题的返回空标题。"""
    try:
        with f.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 300_000))
            tail = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return "", ""
    title = ""
    for line in reversed(tail.splitlines()):
        if '"custom-title"' in line:
            try:
                title = " ".join(str(json.loads(line).get("customTitle") or "").split())[:40]
                break
            except ValueError:
                continue
    cwd = ""
    if title and (m := _CWD.search(tail)):
        try:
            cwd = json.loads(m.group(1))
        except ValueError:
            pass
    return title, cwd


def _short(path: Path, root: Path | None = None) -> str:
    """路径写成短名字：总目录下的相对总目录（shop），家目录下的相对家目录（code/Achat），别处的用最后一段。"""
    for base in (root, Path.home()):
        if base is not None:
            try:
                rel = path.resolve().relative_to(base.resolve())
                if rel.parts:
                    return str(rel)
            except (OSError, ValueError):
                continue
    return path.name


def _raw_sessions(days: float = 30) -> list[tuple[float, str, str]]:
    """最近每次会话的（时间，标题，所在目录），新的在前。扫一遍要一两秒，结果留一小时。"""
    key = f"sessions:{CLAUDE_HOME}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    since = time.time() - days * 86400
    out: list[tuple[float, str, str]] = []
    try:
        folders = [d for d in (CLAUDE_HOME / "projects").iterdir() if d.is_dir()]
    except OSError:
        folders = []
    for d in folders:
        try:
            files = [(f.stat().st_mtime, f) for f in d.glob("*.jsonl")]
        except OSError:
            continue
        for mtime, f in files:
            if mtime < since:
                continue
            title, cwd = _session_info(f)
            if title and cwd:
                out.append((mtime, title, cwd.split("/.claude/worktrees/")[0]))   # 工作树算到原项目头上
    out.sort(reverse=True)
    _cache[key] = (time.time(), out)
    return out


def _also_active(root: Path | None, projects: list["Project"]) -> list["Project"]:
    """清单里没有、但你最近在里面用 Claude 干过活的目录（没有说明文件的项目、总目录之外的项目）。"""
    have = {str(Path(p.path).resolve()) for p in projects}
    home = Path.home().resolve()
    cands: set[Path] = set()
    for _t, _title, cwd in _raw_sessions():
        d = Path(cwd)
        try:
            real = d.resolve()
            rel = real.relative_to(home)
        except (OSError, ValueError):
            continue      # 家目录之外的（临时目录等）不算
        if rel.parts and _is_dir(d) and not any(x.startswith(".") for x in rel.parts) \
                and (root is None or real != root.resolve()):
            cands.add(real)
    found: list[Project] = []
    for real in sorted(cands, key=lambda x: len(x.parts)):       # 先上层后下层：项目里的子目录不单列
        if str(real) in have or any(str(par) in have for par in real.parents):
            continue
        have.add(str(real))
        found.append(_project(_short(real, root), real))
    return found


def everything(s: Settings) -> list["Project"]:
    """它认识的全部项目：项目总目录下的，加上你最近用 Claude 干过活的其他目录。"""
    root = root_of(s)
    projects = scan(root)
    projects = projects + _also_active(root, projects)
    # 最近用 Claude 干过活的排前面：会上提到的多半是手头正在做的
    last: dict[str, float] = {}
    for t, _title, cwd in _raw_sessions():
        for p in projects:
            if cwd == p.path or cwd.startswith(p.path + "/"):
                last[p.path] = max(last.get(p.path, 0.0), t)
    return sorted(projects, key=lambda p: -max(p.mtime, last.get(p.path, 0.0)))


def recent_sessions(projects: list["Project"], limit: int = 30) -> list[Session]:
    """你最近和 Claude 的会话，新的在前，标上是哪个项目。只有标题，没有对话内容。"""
    paths = sorted(((str(Path(p.path).resolve()), p.name) for p in projects), key=lambda x: -len(x[0]))
    seen, out = set(), []
    for t, title, cwd in _raw_sessions():
        try:
            real = str(Path(cwd).resolve())
        except OSError:
            real = cwd
        name = next((n for path, n in paths if real == path or real.startswith(path + "/")), "") or _short(Path(cwd))
        if (name, title) not in seen:
            seen.add((name, title))
            out.append(Session(t, name, title, cwd))
            if len(out) >= limit:
                break
    return out


def sessions_text(sessions: list[Session]) -> str:
    return "\n".join(f"- {time.strftime('%m月%d日', time.localtime(x.t))} {x.project}：{x.title}" for x in sessions)


def scan(root: Path | None, limit: int = 150) -> list[Project]:
    """总目录下的项目，最近动过的在前。不是项目的文件夹再往下看一层（如「公司/某项目」）。"""
    if root is None or not _is_dir(root):
        return []
    key = str(root.resolve())
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1][:limit]
    found: list[Project] = []
    for child in _children(root):
        if _is_project(child):
            found.append(_project(child.name, child))
            continue
        for sub in _children(child)[:40]:
            if _is_project(sub):
                found.append(_project(f"{child.name}/{sub.name}", sub))
    found.sort(key=lambda p: -p.mtime)
    _cache[key] = (time.time(), found)
    return found[:limit]


def forget() -> None:
    _cache.clear()


def root_of(s: Settings) -> Path | None:
    """你所有项目所在的目录。没单独设置时，取「项目目录」的上一级（项目通常并排放）。"""
    if s.projects_root:
        return Path(s.projects_root).expanduser()
    if s.project_dir:
        return Path(s.project_dir).expanduser().parent
    return None


def main_project(s: Settings, projects: list[Project]) -> str:
    """这场会主要讨论的项目，在清单里叫什么。"""
    if not s.project_dir:
        return ""
    want = Path(s.project_dir).expanduser()
    return next((p.name for p in projects if Path(p.path) == want), want.name)


def for_brain(projects: list[Project], limit: int = 45) -> str:
    """给快脑：只要名字和一句话，不要路径（路径念不出来）。

    一个文件夹下面有一大堆子项目时并成一行，免得一个大仓库把清单占满。
    """
    groups: dict[str, list[Project]] = {}
    for p in projects:
        groups.setdefault(p.name.split("/")[0] if "/" in p.name else "", []).append(p)
    lines, seen = [], set()
    for p in projects:
        top = p.name.split("/")[0] if "/" in p.name else ""
        subs = groups[top] if top else []
        if len(subs) >= 5:
            if top not in seen:
                seen.add(top)
                names = "、".join(x.name.split("/", 1)[1] for x in subs[:4])
                lines.append(f"- {top}：下面有 {len(subs)} 个子项目，如 {names}")
        else:
            lines.append(f"- {p.name}" + (f"：{p.about[:50]}" if p.about else ""))
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def for_worker(projects: list[Project]) -> str:
    """给慢脑：带路径，它要进目录去查。"""
    return "\n".join(f"- {p.name}（{p.path}）" + (f"：{p.about}" if p.about else "")
                     + (f"［记忆在 {p.memory}］" if p.memory else "") for p in projects)
