"""文字工具：流式分句、快脑输出解析、回声判重。纯函数，无外部依赖。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

SILENT = "[静]"
TASK_KINDS = ("查资料", "查代码", "查会议", "做设计")
_DISPATCH_RE = re.compile(r"\[\[\s*派工\s*(查资料|查代码|查会议|做设计)\s*[:：]\s*(.+?)\s*\]\]", re.S)
# 大脑听到「少说点 / 多说点」时用这个标记调整自己的参与度
ENGAGE_WORDS = {"少说": "quiet", "适中": "normal", "多说": "active"}
_ENGAGE_RE = re.compile(r"\[\[\s*参与\s*[:：]?\s*(少说|适中|多说)\s*\]\]")
# 大脑认出某位说话人是谁时用这个标记给他改名：[[称呼 说话人2=张三]]
_NAME_RE = re.compile(r"\[\[\s*称呼\s*[:：]?\s*([^=＝\]]{1,30}?)\s*[=＝]\s*([^=＝\]]{1,30}?)\s*\]\]")

_HARD = "。！？!?；;\n"
_SOFT = "，,、：:"
_CLOSERS = "”’\"'）)】」』"


def clean_for_speech(text: str) -> str:
    """去掉念不出来的东西：标记符号、表情、括号里的舞台提示。"""
    text = text.replace(SILENT, "")
    text = re.sub(r"^\s*\[[^\[\]\n]{1,16}\]\s*", "", text)       # 学着字幕格式带出来的「[说话人1] 」前缀
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


# ---- 自言自语 ----
# 快脑（关了思考的小模型）偶尔把推敲过程用英文写进正文："Wait — let me reconsider this. …"，写完才是真正要说的那句。
# 它该说的是中文口语，英文只会是夹在话里的个别词和名字（TV、GitHub Actions、Sign in with Apple）。
# 连着四个以上英文词、里面又有「我、应该、不、等等」这类词，就不是在说话，是在跟自己嘀咕。
_EN_WORD = r"[A-Za-z][A-Za-z0-9_'’./-]*"
_EN_GAP = r"[ \t,;:!?\"“”‘’()—–…'.-]+"
_SELFTALK = re.compile(rf"{_EN_WORD}(?:{_EN_GAP}{_EN_WORD}){{3,}}")
_SELFTALK_NEAR = re.compile(rf"{_EN_WORD}(?:{_EN_GAP}{_EN_WORD}){{2,}}")   # 已经在自言自语之后：三个词就算
_EN_TAIL = re.compile(rf"{_EN_WORD}(?:{_EN_GAP}{_EN_WORD})*(?:{_EN_GAP})?$")
_EN_LABEL = re.compile(rf"^\s*{_EN_WORD}(?:{_EN_GAP}{_EN_WORD})*\s*[:：]\s*")   # 行首的 "Output:"、"So final:"
_CJK = re.compile(r"[一-鿿]")
_EN_MUTTER = frozenset(   # 人称、情态、否定、系词、口头禅：产品名和命令里一般没有
    "i i'm i'll i'd i've me my we you it it's this that is are was am be do does don't doesn't did not no "
    "so but if should shouldn't must can can't cannot could will won't would need needs let let's wait hmm "
    "also just actually here there what which why because say says output answer reply respond user rules".split())


def _find_selftalk(line: str) -> re.Match | None:
    for m in _SELFTALK.finditer(line):
        words = (w.lower().replace("’", "'").strip(".-/") for w in re.findall(_EN_WORD, m.group()))
        if any(w in _EN_MUTTER for w in words):
            return m
    return None


def _last_words(lines: list[str]) -> list[str]:
    """自言自语之后，它最后想好要说的那一段：最后一段带中文的话里，排在英文嘀咕后面的那几行。"""
    para: list[str] = []
    for line in reversed(lines):
        if not _CJK.search(line):          # 空行、"Output:"、"<br>"
            if para:
                break
            continue
        if _SELFTALK_NEAR.search(line):    # 还在嘀咕（哪怕夹着中文）：要说的话在它后面
            break
        para.insert(0, line)
    # 行首的 "Output:"、"So final:" 和它给这句话加的引号不念
    return [_EN_LABEL.sub("", x).strip(" \t\"“”") if _EN_LABEL.match(x) else x for x in para]


def without_selftalk(text: str, final: bool = True) -> tuple[str, bool]:
    """把大脑输出里的自言自语去掉。返回（可以念的部分，有没有自言自语）。text 是已经剔掉 [[标记]] 的输出。

    - 一行里出现成段的英文嘀咕：从那里起整行不念（后面夹着的中文是它在引用规矩，不是要说的话）。
    - 这之后的内容先都不念，等它写完（final）只留最后一段干净的中文——那才是它想好要说的；
      中间的推敲和草稿、单独一行的 "Output:"、行首的 "So final:" 都不要。
    - 流式调用时（final=False），一行结尾还没写完的英文词先压着，看清是个别词还是一段话再放行，
      保证已经交出去的部分之后不会变。
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _find_selftalk(line)
        if m:
            head = "\n".join(lines[:i] + [line[:m.start()]])
            if not final:
                return head, True
            return "\n".join([head] + _last_words(lines[i + 1:])), True
    if not final and (m := _EN_TAIL.search(lines[-1])):
        return text[:len(text) - len(lines[-1]) + m.start()], False
    return text, False


@dataclass
class Reply:
    silent: bool = False
    speech: str = ""
    dispatch: tuple[str, str] | None = None  # (类型, 简述)
    engagement: str | None = None            # quiet | normal | active：它要调整自己的参与度
    names: tuple[tuple[str, str], ...] = ()  # (原来的叫法, 真名)：它认出了某位说话人是谁
    selftalk: bool = False                   # 输出里夹了自言自语（见 without_selftalk）：这一轮跑偏了


def parse_reply(raw: str) -> Reply:
    s = raw.strip()
    m = _DISPATCH_RE.search(s)
    dispatch = (m.group(1), m.group(2).strip()) if m else None
    e = _ENGAGE_RE.search(s)
    engagement = ENGAGE_WORDS[e.group(1)] if e else None
    names = tuple((a.strip(), b.strip()) for a, b in _NAME_RE.findall(s))
    said, selftalk = without_selftalk(re.sub(r"\[\[.*?\]\]", "", s, flags=re.S))
    speech = clean_for_speech(said)
    silent = s.startswith("[静") or not speech
    return Reply(silent=silent, speech="" if silent else speech, dispatch=dispatch,
                 engagement=engagement, names=names, selftalk=selftalk)


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

    @property
    def prefixed(self) -> bool:
        """回答是不是以「[某人] 」开头：这是它在照抄字幕格式的明显迹象。"""
        s = self.raw.lstrip()
        return not s.startswith("[静") and bool(re.match(r"\[[^\[\]\n]{1,16}\]", s))

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
        """到目前为止可以念的部分：完整的 [[标记]] 剔掉，还没写完的标记先压住不念。

        标记可能出现在话的前面、中间或后面，剔掉之后前后的话都要照常念出来。
        """
        text = re.sub(r"\[\[.*?\]\]", "", self.raw, flags=re.S)
        k = text.find("[[")
        if k >= 0:
            text = text[:k]            # 标记还没写完
        elif not final and text.endswith("["):
            text = text[:-1]           # 可能是标记的开头，等下一个字再说
        return without_selftalk(text, final)[0]      # 自言自语不念

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


def mentions(said: str, source: str) -> bool:
    """said 这段话像不像在讲 source 里的内容：said 里相邻的两个字，有两成以上（且不少于 8 处）在 source 里出现过。

    只用来判断「它答话时是不是顺带把查到的结果讲了」。口头转述会换说法，所以门槛低；
    「在，你说」「还在查，快了」这种和结果没有重合的话过不了。
    """
    s, src = _norm(said), _norm(source)
    pairs = [s[i:i + 2] for i in range(len(s) - 1)]
    have = {src[i:i + 2] for i in range(len(src) - 1)}
    hits = sum(p in have for p in pairs)
    return hits >= 8 and hits >= 0.2 * len(pairs)


_BACKCHANNEL = re.compile(
    r"(嗯|恩|唔|啊|哦|噢|喔|诶|欸|对|好|是|行|的|呀|吧|嘛|ok|okay|yes|yeah|明白|知道了?|懂了?|可以|没错|没问题|你?继续|你?接着说|你说|然后呢)+", re.I)


def is_backchannel(text: str) -> bool:
    """这句话是不是只是附和（嗯、对、好的、你继续）：说明对方在听，不是要抢话。"""
    n = _norm(text)
    return 0 < len(n) <= 6 and bool(_BACKCHANNEL.fullmatch(n))


def fix_name(text: str, name: str) -> str:
    """把语音识别写错的名字改回来：读音相同的字（小课、小柯、小可……）都当作在叫「小克」。"""
    n = len(name)
    if n < 2 or len(text) < n:
        return text
    try:
        from pypinyin import lazy_pinyin
    except ImportError:      # 没装拼音库就不纠正，不影响开会
        return text
    target = lazy_pinyin(name)
    out, i = [], 0
    while i < len(text):
        win = text[i:i + n]
        if len(win) == n and win != name and all("\u4e00" <= c <= "\u9fff" for c in win) and lazy_pinyin(win) == target:
            out.append(name)
            i += n
        else:
            out.append(text[i])
            i += 1
    return "".join(out)



# ================= 听写清洗 =================
# 两层：strip_noise 在识别完马上用，只挡「根本不是人话」的；tidy 在写会议记录时用，去语气词和口吃。
# 接话逻辑拿到的是 strip_noise 之后的原话（它要靠「嗯」「对」判断对方在听），记录里才是 tidy 之后的。

_P = "，。！？、；：,.!?;:"
_FOREIGN = re.compile(r"[぀-ヿᄀ-ᇿ가-힯]+")   # 假名、韩文：识别锁了中文，出现就是杂音被认错
_FILLER = re.compile(r"[嗯呃]+|(?<![A-Za-z])(?:u+h*m+|u+h+|e+m{2,}|h+m{2,})(?![A-Za-z])", re.I)
_INTERJ = re.compile(rf"(?<![^{_P}\s])[啊哦噢喔诶欸哎唉]+(?=[{_P}\s]|$)")   # 单独成一小句的才算拖音，「对啊」的啊不算
_RUN = re.compile(r"([一-鿿])\1{2,}")                       # 同一个字连说三次以上
_STUTTER = set("我你他她它这那就是不对好有没在了也都还把给和跟让被要会能可行")
_PAIR = re.compile(r"(?<!其)([我你他她它这那])\1")           # 这几个字连着两个一定是口吃（「其他他们」除外）
_UNIT = re.compile(r"([一-鿿]{2,4}?)\1+")                   # 「就是就是」「他就他就」
_COUNTING = set("一二两三四五六七八九十几每半")               # 「一个一个」「两个两个」是在数数，不是口吃
_CLAUSE = re.compile(                                       # 「他就，他就回了」「对，对，对。」「我，我觉得」
    rf"(?<![^{_P}\s])(?:([一-鿿]{{2,4}})[，、,]\s*(?=\1)"
    rf"|([一-鿿])[，、,]\s*(?=\2(?:[{_P}\s]|$))"
    rf"|([我你他她它这那])[，、,]\s*(?=\3))")


def strip_noise(text: str, event: str = "") -> str:
    """识别结果里不是人话的部分去掉；整段都不是就返回空。event 是识别模型判断的声音类型（人声、喘气、音乐……）。"""
    text = _FOREIGN.sub("", text).strip()
    core = _norm(text)
    if not core:
        return ""
    if event and "Speech" not in event and len(core) <= 4:     # 模型自己都说不是人声，字又没几个
        return ""
    return text


def _fix_punct(text: str) -> str:
    text = re.sub(rf"[，、,；;：:]\s*(?=[{_P}])", "", text)       # 删字之后挨在一起的标点只留后一个
    text = re.sub(r"(?<=[。！？!?])\s*[，、,。]+", "", text)
    return re.sub(rf"^[\s{_P}]+", "", text).strip()


def _fold_unit(m: re.Match) -> str:
    unit = m.group(1)
    return m.group(0) if unit[0] in _COUNTING or len(set(unit)) == 1 else unit


def tidy(text: str) -> str:
    """把一句口语整理成能读的记录：去掉语气词、拖音和口吃式的重复，别的一个字不动。只剩语气词的句子返回空。"""
    text = _fix_punct(_INTERJ.sub("", _FILLER.sub("", text)))
    text = _RUN.sub(lambda m: m.group(1) * (1 if m.group(1) in _STUTTER else 2), text)
    text = _UNIT.sub(_fold_unit, _PAIR.sub(r"\1", text))
    while (folded := _CLAUSE.sub("", text)) != text:
        text = folded
    text = _fix_punct(text)
    return text if _has_words(text) else ""
