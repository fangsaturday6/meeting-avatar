"""出口过滤：登录账号的邮箱不出进程。

本机 claude 会把登录账号的邮箱自动放进每个 claude 进程的上下文，没有开关能关（见 FastBrain._spawn 的注释），
所以快脑和干活的进程都「知道」它。提示词里有「不说」的规矩，但那是软约束；这里是硬的一道：
它们的输出在进到嘴、派工、屏幕和会议记录之前先过一遍，邮箱换成一句占位的话。

挡得住的：整串邮箱、只有 @ 前面那截、夹着空格和标点一个字一个字拼出来的、数字换成中文数字念的。
挡不住的：它换一种说法描述（「第一位是五，然后两个杰」）。那种只能靠提示词。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("avatar.redact")

PLACEHOLDER = "那个邮箱"

# 拼着念时字和字之间会夹的东西：空格、标点、「点」。中文和字母数字不算，免得把正常的话吞进去
_SEP = r"(?:[^0-9a-z一-鿿]|点){0,3}"
_AT = r"(?:@|＠|at|艾特)?"
_DIGITS = dict(zip("0123456789", ("零〇", "一幺", "二两", "三", "四", "五", "六", "七", "八", "九")))


def _config_file() -> Path:
    # 和命令行取邮箱的地方是同一个文件。测试把 AVATAR_CLAUDE_HOME 指到临时目录，就不会读到真实账号
    home = os.environ.get("AVATAR_CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(home or Path.home()) / ".claude.json"


_seen: tuple[Path, float, str] | None = None


def login_email(path: Path | None = None) -> str:
    """本机 claude 登录账号的邮箱；没登录、读不到就是空。要读盘，只在线程里调。"""
    global _seen
    path = path or _config_file()
    try:
        stamp = path.stat().st_mtime
        if _seen and _seen[:2] == (path, stamp):
            return _seen[2]
        account = json.loads(path.read_text(encoding="utf-8")).get("oauthAccount") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    email = str(account.get("emailAddress") or "").strip()
    _seen = (path, stamp, email)
    return email


def _char(c: str) -> str:
    return f"[{c}{_DIGITS[c]}]" if c in _DIGITS else re.escape(c)


def _flat(x: str) -> list[str]:
    return [_char(c) for c in x.lower() if c.isascii() and c.isalnum()]


class Redactor:
    """把一段输出里的登录邮箱换成占位的话。

    整段的用 clean()。一点一点吐出来的用 feed() / flush()：结尾那几个字如果可能是邮箱的开头，
    先扣着，等后面的字来了再决定放还是挡，所以邮箱被切在两段中间也漏不出去。
    """

    def __init__(self, email: str = ""):
        local, _, domain = email.strip().partition("@")
        loc, dom = _flat(local), _flat(domain)
        self._full = self._head = None
        self._buf = ""
        self.hits = 0
        if not (loc and dom):
            return
        tail = [_SEP, _AT, _SEP, *self._spell(dom)]
        # @ 前面那截够长时，单独出现也挡；太短（bob@…）就只挡整串，免得误伤正常的词
        solo = len(loc) >= 4
        body = _SEP.join(loc)
        rest = "".join(tail)
        self._full = re.compile(f"{body}(?:{rest})?" if solo else body + rest, re.I)
        # 「到结尾为止，是不是邮箱的一个开头」：一层套一层的可选组，匹配任何一段前缀
        head = ""
        for tok in reversed([*self._spell(loc), *tail]):
            head = f"{tok}(?:{head})?" if head else tok
        self._head = re.compile(head + r"\Z", re.I)

    @staticmethod
    def _spell(chars: list[str]) -> list[str]:
        out: list[str] = []
        for c in chars:
            out += [_SEP, c] if out else [c]
        return out

    def clean(self, text: str) -> str:
        if not self._full or not text:
            return text
        out, n = self._full.subn(PLACEHOLDER, text)
        self._count(n)
        return out

    def reset(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> str:
        if not self._full:
            return text
        buf = self._buf + text
        out, pos, hold = [], 0, len(buf)
        for m in self._full.finditer(buf):
            if self._head.match(buf, m.start()):      # 后面还可能接着 @ 和域名：整个先扣着
                hold = m.start()
                break
            out += [buf[pos:m.start()], PLACEHOLDER]
            pos = m.end()
            self._count(1)
        else:
            m = self._head.search(buf, pos)
            if m:
                hold = m.start()
        out.append(buf[pos:hold])
        self._buf = buf[hold:]
        return "".join(out)

    def flush(self) -> str:
        buf, self._buf = self._buf, ""
        return self.clean(buf)

    def _count(self, n: int) -> None:
        if n:
            self.hits += n
            log.warning("输出里带着登录邮箱，已挡掉 %d 处", n)
