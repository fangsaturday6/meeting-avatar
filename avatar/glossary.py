"""常用词表：人名、公司名、行话。识别模型不认识这些词，会写成读音相同的平常字（回价 → 回家）。

词表是 workspace/glossary.txt，一行一个词，后面可以跟一句说明（「回价：供应商回复的报价」），# 开头是注释。
这里不改任何字：只按读音找出一段记录里可能被认错的地方，写成一段提示，交给会后的整理稿和纪要结合上下文判断。
照着读音直接换不行——「以及」和「一级」、「纪总」和「机种」读音也一样（实测盲换八成是误伤）。
只带和这段记录读音对得上的词，整张词表不出这台电脑。

词表怎么来：设置窗口里直接改（save）、导入一份名单（from_file + add）、会上给说话人改的名字（add）。
"""
from __future__ import annotations

import csv
import difflib
import os
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

from .config import WORKSPACE

GLOSSARY_FILE = WORKSPACE / "glossary.txt"
HINT_LIMIT = 40          # 一次最多提这么多个词，再多提示就成了噪音
TEXT_LIMIT = 400_000     # 词表最多这么多字
IMPORT_LIMIT = 2_000_000 # 一次导入的文件最多这么多字
_ZH = re.compile(r"[一-鿿]+")
_EN = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_FUZZY_MIN = 3           # 三个字以上的词才放宽到近音；两个字的必须读音完全相同
_SPELLING = 0.6          # 英文词拼写有这么像才算
_SPELLING_MIN = 4
# 表格第一行是这些就当表头：这一列是词
_TERM_HEADS = {"姓名", "名字", "名称", "词", "词语", "术语", "简称", "客户", "客户名称", "公司", "公司名称", "name", "term"}
_NOTE_HEADS = {"说明", "备注", "描述", "解释", "note", "description"}


# ---------- 词表文件 ----------
def read(path: Path | None = None) -> str:
    """词表原文。没有文件就是空的。"""
    try:
        return (path or GLOSSARY_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""


def _usable(term: str) -> bool:
    """能当词用：两到二十个字，里面得有字（纯数字、纯符号的不要）。"""
    return (2 <= len(term) <= 20 and bool(re.search(r"[A-Za-z一-鿿]", term))
            and not term.startswith("#") and not re.search(r"[：:\n\r]", term))


def parse(text: str) -> dict[str, str]:
    """词表原文 → 词 → 说明。一行一个，# 开头是注释。"""
    terms: dict[str, str] = {}
    for row in text.splitlines():
        row = row.strip()
        if not row or row.startswith("#"):
            continue
        term, note = (re.split(r"\s*[：:]\s*", row, maxsplit=1) + [""])[:2]
        if _usable(term):
            terms.setdefault(term, note[:60])
    return terms


def load(path: Path | None = None) -> dict[str, str]:
    """词 → 说明。没有文件就是空表。"""
    return parse(read(path))


def _write(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")       # 先写到旁边再换过去：写到一半断电不会留下半张词表
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save(text: str, path: Path | None = None) -> str:
    """把整张词表换成这段文字（设置窗口里改完就是走这里）。注释和空行照留，返回存下的原文。"""
    rows = [row.rstrip() for row in text.replace("\x00", "").splitlines()]
    text = "\n".join(rows).strip("\n")
    text = text + "\n" if text else ""
    _write(text, path or GLOSSARY_FILE)
    return text


def add(terms: dict[str, str], heading: str = "", path: Path | None = None) -> list[str]:
    """把还没有的词追加到词表末尾，已有的行（包括手写的）不动。heading 是这一批的来历，写成一行注释。返回新加的词。"""
    path = path or GLOSSARY_FILE
    old = read(path)
    have = parse(old)
    new = {t.strip(): " ".join(str(note or "").split())[:60] for t, note in terms.items()}
    new = {t: note for t, note in new.items() if _usable(t) and t not in have}
    if not new:
        return []
    heading = " ".join(heading.split())
    last = next((r.strip() for r in reversed(old.splitlines()) if r.startswith("#")), "")
    rows = [f"{t}：{note}" if note else t for t, note in new.items()]
    if heading and last != f"# {heading}":            # 同一个来历接着加，就不再写一遍标题
        rows = ([""] if old.strip() else []) + [f"# {heading}"] + rows
    _write(old.rstrip("\n") + ("\n" if old.strip() else "") + "\n".join(rows) + "\n", path)
    return list(new)


# ---------- 导入：别处的名单 → 词 ----------
def from_file(name: str, text: str) -> dict[str, str]:
    """一份名单 → 词 → 说明。按文件名认格式：.csv / .tsv 是表格，.yaml / .yml 是公司本体，其余当一行一个。读不懂抛 ValueError。"""
    text = text.lstrip("﻿")
    kind = Path(name).suffix.lower()
    if kind in (".csv", ".tsv"):
        return _from_table(text, "\t" if kind == ".tsv" else ",")
    if kind in (".yaml", ".yml"):
        return _from_ontology(Path(name).name, text)
    return parse(text)


def _from_table(text: str, sep: str) -> dict[str, str]:
    """表格：默认第一列是词、第二列是说明。有表头就按表头找（通讯录常常第一列是工号，「姓名」在后面）。"""
    try:
        rows = [[c.strip() for c in row] for row in csv.reader(text.splitlines(), delimiter=sep) if any(c.strip() for c in row)]
    except csv.Error:
        raise ValueError("这个表格读不懂，另存成 CSV 再试") from None
    if not rows:
        return {}
    head = [c.lower() for c in rows[0]]
    term_at, note_at = 0, 1
    named = next((i for i, c in enumerate(head) if c in _TERM_HEADS), None)
    if named is not None:
        rows, term_at = rows[1:], named
        note_at = next((i for i, c in enumerate(head) if c in _NOTE_HEADS), -1)
    terms: dict[str, str] = {}
    for row in rows:
        term, note = (row[i] if 0 <= i < len(row) else "" for i in (term_at, note_at))
        if _usable(term):
            terms.setdefault(term, " ".join(note.split())[:60])
    return terms


def _first_sentence(text) -> str:
    return re.split(r"[。；\n]", " ".join(str(text or "").split()), maxsplit=1)[0][:40]


def _from_ontology(name: str, text: str) -> dict[str, str]:
    """公司本体（ontology-kit 的格式）里有名字的东西：公司名、对象、属性、枚举值、规则名、动作名、参数和别名、容易混的词。
    规则和动作的正文不取——它们帮不上识别。"""
    try:
        import yaml
        data = yaml.safe_load(text)
    except ImportError:
        raise ValueError("这台电脑上读不了 YAML 文件") from None
    except yaml.YAMLError:
        raise ValueError("这个文件读不懂") from None
    found: dict[str, str] = {}
    zh, en = re.compile(r"[一-鿿]{2,8}"), re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")

    def put(name_, note: str = "") -> None:
        name_ = str(name_ or "").strip()
        whole = zh.fullmatch(name_) or en.fullmatch(name_)
        # 「询价任务ID」「SKU_ID」这种拆开；拆出来的碎片不带说明
        for word in [name_] if whole else zh.findall(name_) + en.findall(name_):
            if not found.get(word):
                found[word] = note if whole else found.get(word, "")

    def each(value) -> list:
        return value if isinstance(value, list) else []

    def table(value) -> dict:
        return value if isinstance(value, dict) else {}

    def names(value) -> list:          # 参数表有的写成一串名字，有的写成「名字: 类型」
        return list(value) if isinstance(value, (list, dict)) else []

    if name == "ontology.yaml":        # 仓库的总说明：只有公司名有用
        put(table(data).get("name"), "公司名")
        data = []
    for doc in data if isinstance(data, list) else [data]:
        if not isinstance(doc, dict):
            continue
        put(doc.get("object"), _first_sentence(doc.get("description")))
        for prop, spec in table(doc.get("properties")).items():
            put(prop)
            for value in each(table(spec).get("values")):
                put(value)
        for rule in each(doc.get("logic")):
            put(table(rule).get("name"))
        for action in each(doc.get("actions")):
            put(table(action).get("name"))
            for field in names(table(action).get("input")) + names(table(action).get("returns")):
                put(field)
        for word, spec in table(doc.get("vocabulary")).items():
            put(word)
            for alias in each(table(spec).get("别名")):
                put(alias, f"就是{word}")
        for item in each(doc.get("disambiguation")):
            put(table(item).get("词"), _first_sentence(table(item).get("含义")))
        for param in each(doc.get("param_order")):
            put(param)
    return {t: note for t, note in found.items() if _usable(t)}


def _fuzzy(syllable: str) -> str:
    """近音归一：平翘舌、前后鼻音、n 和 l。"""
    syllable = re.sub(r"^([zcs])h", r"\1", syllable)
    syllable = re.sub(r"^l", "n", syllable)
    return re.sub(r"([aei])ng$", r"\1n", syllable)


def suspects(text: str, terms: Iterable[str]) -> dict[str, dict[str, int]]:
    """词 → {记录里读音相近的写法: 出现几次}。写对了的、本身就是词表里另一个词的，不算。"""
    terms = list(terms)
    zh = [t for t in terms if _ZH.fullmatch(t)]
    # 三个字母以内的缩写不比拼写：MCP 和 MPN、SKU 和 SPU 只差一个字母，却是两个词
    en = {t.lower(): t for t in terms if _EN.fullmatch(t) and len(t) >= _SPELLING_MIN}
    found: dict[str, Counter] = {}
    if zh and _ZH.search(text):
        try:
            from pypinyin import lazy_pinyin
        except ImportError:      # 没装拼音库就不提示，不影响整理
            zh = []
        known, exact, near = set(zh), {}, {}
        for t in zh:
            py = lazy_pinyin(t)
            exact.setdefault((len(t), " ".join(py)), []).append(t)
            if len(t) >= _FUZZY_MIN:
                near.setdefault((len(t), " ".join(map(_fuzzy, py))), []).append(t)
        sizes = sorted({len(t) for t in zh})
        for run in _ZH.findall(text) if zh else ():       # 只在连着的汉字里找，不跨标点
            py = lazy_pinyin(run)
            for n in sizes:
                for i in range(len(run) - n + 1):
                    heard = run[i:i + n]
                    if heard in known:
                        continue
                    sound = py[i:i + n]
                    for t in exact.get((n, " ".join(sound))) or near.get((n, " ".join(map(_fuzzy, sound)))) or ():
                        found.setdefault(t, Counter())[heard] += 1
    for heard in _EN.findall(text) if en else ():
        low = heard.lower()
        if low in en or len(low) < _SPELLING_MIN:
            continue
        for key, t in en.items():
            if difflib.SequenceMatcher(None, low, key).ratio() >= _SPELLING:
                found.setdefault(t, Counter())[heard] += 1
    return {t: dict(c) for t, c in found.items()}


def hint(text: str, path: Path | None = None, limit: int = HINT_LIMIT) -> str:
    """给整理稿和纪要的提示词用的一段：这段记录里哪些字可能是词表里的词被认错了。没有就是空。要读盘、算拼音，在线程里调。"""
    terms = load(path)
    found = suspects(text, terms) if terms else {}
    if not found:
        return ""
    # 字多的对上了更可信，出现次数多的更要紧
    order = sorted(found, key=lambda t: (-len(t), -sum(found[t].values()), t))[:limit]
    rows = [f"- {t}（记录里写成：{'、'.join(found[t])}）" + (f"：{terms[t]}" if terms[t] else "") for t in order]
    return ("主人登记的常用词（人名、公司名、行话）里，下面这些和记录里的某些字读音或拼写相近，可能是被认错了：\n"
            + "\n".join(rows)
            + "\n只有上下文确实在说这个词时才改成它；记录里原来的字本身讲得通（「以及」「一句话」这种平常话），就不动。")
