"""文字工具：流式分句、快脑输出解析、回声判重。纯函数，无外部依赖。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

SILENT = "[静]"
TASK_KINDS = ("查资料", "查代码", "做设计")
_DISPATCH_RE = re.compile(r"\[\[\s*派工\s*(查资料|查代码|做设计)\s*[:：]\s*(.+?)\s*\]\]", re.S)

_HARD = "。！？!?；;\n"
_SOFT = "，,、：:"
_CLOSERS = "”’\"'）)】」』"


def clean_for_speech(text: str) -> str:
    """去掉念不出来的东西：标记符号、表情、括号里的舞台提示。"""
    text = text.replace(SILENT, "")
    text = re.sub(r"[（(][^（()）]{0,30}[)）]", "", text)       # （笑）(停顿)
    text = re.sub(r"[*#`_~>|]+", "", text)                     # markdown 符号
    text = re.sub(r"^\s*[-•·]\s+", "", text, flags=re.M)        # 列表符
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]", "", text)  # 表情
    return text.strip()


def _has_words(s: str) -> bool:
    return bool(re.search(r"[\w一-鿿]", s))


class SentenceSplitter:
    """把流式文字切成可以立刻送去合成的句子。

    规则：句号、问号、叹号、分号、换行处切；第一段为了抢时间，逗号处也切（至少 first_min 个字）；
    之后每段攒够 soft_max 个字就在逗号处切。段落小，合成才跟得上播放，句子之间不会卡顿
    （实测本地合成约为 4 倍速：12 个字的一段大约 0.5 秒合成完）。
    """

    def __init__(self, first_min: int = 4, soft_max: int = 12):
        self.buf = ""
        self.first = True
        self.first_min = first_min
        self.soft_max = soft_max

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while (cut := self._find_cut()) is not None:
            seg, self.buf = self.buf[:cut].strip(), self.buf[cut:]
            if seg and _has_words(seg):
                out.append(seg)
                self.first = False
        return out

    def flush(self) -> list[str]:
        seg, self.buf = self.buf.strip(), ""
        return [seg] if seg and _has_words(seg) else []

    def _find_cut(self) -> int | None:
        b = self.buf
        for i, ch in enumerate(b):
            if ch in _HARD:
                j = i + 1
                while j < len(b) and b[j] in _HARD + _CLOSERS:
                    j += 1
                return j
            if ch == ".":
                # 英文句号：后面是空白才算句末，"3.5" 不切；还没看到下一个字就先等
                if i + 1 < len(b) and b[i + 1] in " \n" and not b[i - 1 : i].isdigit():
                    return i + 1
                continue
            if ch in _SOFT:
                n = len(b[: i + 1].strip())
                if (self.first and n >= self.first_min) or n >= self.soft_max:
                    return i + 1
        return None


@dataclass
class Reply:
    silent: bool = False
    speech: str = ""
    dispatch: tuple[str, str] | None = None  # (类型, 简述)


def parse_reply(raw: str) -> Reply:
    s = raw.strip()
    m = _DISPATCH_RE.search(s)
    dispatch = (m.group(1), m.group(2).strip()) if m else None
    speech = clean_for_speech(re.sub(r"\[\[.*?\]\]", "", s, flags=re.S))
    if s.startswith("[静") or not speech:
        return Reply(silent=True, dispatch=dispatch)
    return Reply(silent=False, speech=speech, dispatch=dispatch)


class ReplyStream:
    """快脑输出的流式解析：边收字边吐出可以念的句子，过滤掉 [静] 和派工指令。"""

    def __init__(self) -> None:
        self.raw = ""
        self._silent: bool | None = None  # None = 还没看够，无法判断
        self._emitted = 0
        self._splitter = SentenceSplitter()

    @property
    def silent(self) -> bool:
        return bool(self._silent)

    def feed(self, delta: str) -> list[str]:
        self.raw += delta
        if self._silent is None:
            s = self.raw.lstrip()
            if not s or s == "[":
                return []
            self._silent = s.startswith("[静")
        if self._silent:
            return []
        return self._emit(self._speakable(final=False))

    def finish(self) -> tuple[list[str], Reply]:
        """收尾：返回剩余的句子和整段解析结果。"""
        rest: list[str] = []
        if self._silent is False:
            rest = self._emit(self._speakable(final=True)) + self._clean(self._splitter.flush())
        return rest, parse_reply(self.raw)

    def _speakable(self, final: bool) -> str:
        k = self.raw.find("[[")
        if k >= 0:
            return self.raw[:k]
        if not final and self.raw.endswith("["):
            return self.raw[:-1]
        return self.raw

    def _emit(self, speakable: str) -> list[str]:
        new, self._emitted = speakable[self._emitted :], max(self._emitted, len(speakable))
        return self._clean(self._splitter.feed(new))

    @staticmethod
    def _clean(sentences: list[str]) -> list[str]:
        return [c for s in sentences if (c := clean_for_speech(s)) and _has_words(c)]


def _norm(s: str) -> str:
    return re.sub(r"[^\w一-鿿]", "", s).lower()


def similar(a: str, b: str) -> float:
    """两句话的相似度，0~1，忽略标点。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def echo_score(heard: str, spoken: str) -> float:
    """听到的这句话有多大比例出现在数字人自己刚说的话里（用于判回声）。"""
    h, s = _norm(heard), _norm(spoken)
    if not h or not s:
        return 0.0
    blocks = SequenceMatcher(None, h, s).get_matching_blocks()
    return sum(b.size for b in blocks if b.size >= 2) / len(h)
