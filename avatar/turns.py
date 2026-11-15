"""散会后找换人的地方：会上断成一句的话里要是有几个人，按声纹切开，再分一次是谁说的。

会上断句只看有没有声音，几个人抢着说、一个接一个不停顿的时候，断出来的一句里有好几个人；
整句算一个声纹是几个人的平均，谁也不像，认人就乱了。公开会议录音实测（2026-11-15，AliMeeting 8 场，
见录音路线图结论区）：同一个人的整句声纹彼此相似度 0.57~0.80、不同人只有 0.05~0.27，声纹本身分得开，
认错主要是这种混着几个人的句子造成的；抢话最凶的一场，一个人单独说满 2 秒的句子全场只有 2 句。

做法和常见的说话人日志一样：每句按 1.5 秒的小窗、每 0.75 秒取一个算声纹（1.5 秒的小窗同一人相似度中位 0.5、
不同人 0.18，够分）；全场的小窗一起做平均连接聚类（相似度 ≥ 0.3，取法见 MIN_SIM），太小的簇不算一个人；
每句里看小窗归哪位，短于 1 秒的一截并给旁边，剩下两位以上就在两段之间切开。
字不重认（这里只管谁说的）：同一段声音再认一遍拿每个字的时间，按切点把字分给前后几段（cut_text）。

规则和 recluster.py 一样：用户定过的句子不动（它的声音照样拿来参考）；不到 1.5 秒、切不出小窗的短句
按整句声纹跟最像的那位走，拿不准就不动（原来那位分完已经没有了的除外）；新分出来的人按重合最多对上会上的编号，对不上的用负数。
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .recluster import _unit, average_linkage, match_ids
from .textutil import strip_noise

WIDTH = 1.5        # 小窗多长（秒）
HOP = 0.75         # 每隔多久取一个（0.25 秒和 0.75 秒认对的一样多，后者省三分之二的算力）
# 小窗聚类：两簇平均相似度到这么多就并。真实会议室里的声纹比公开录音难分（不同人的簇心也有 0.45~0.7）：
# 0.25 会把公开录音里的四个人并成两个，0.28 也会在一场四人会里并掉一位；0.3 公开录音最好，
# 两场真实会议（2 人、4 人）各多拆出一位小的（占 7%，集中在其中几分钟）。宁可多拆：多出来的一位改个名就并回去了，
# 并错了的要一句句改。
MIN_SIM = 0.3
MIN_SHARE = 0.05   # 一簇的小窗不到全场的这个比例，就不算一个人
MIN_RUN = 1.0     # 一句里归同一位的一截短于这么多秒，并给旁边（一两个小窗认错很常见）
MOST = 1500        # 参加聚类的小窗最多这么多个（多了均匀抽；聚类是平方级的，三小时的会也几秒算完）


@dataclass
class Turn:
    line_id: int
    seconds: float
    windows: np.ndarray             # 每个小窗的声纹，一行一个，起点依次是 offsets(seconds)
    emb: np.ndarray | None          # 整句的声纹（切不出小窗的短句靠它）
    speaker_id: int | None
    fixed: bool = False             # 用户定过的：不改不切，但声音照样拿来参考


def offsets(seconds: float, width: float = WIDTH, hop: float = HOP) -> list[float]:
    """一句话里每个小窗从第几秒开始。不够一个小窗长就是空的。"""
    n = int((seconds - width) / hop + 1e-6) + 1 if seconds >= width - 1e-6 else 0
    return [round(k * hop, 3) for k in range(n)]


def runs(labels, min_n: int) -> list[tuple[int, int]]:
    """一串小窗的归属 → [(归谁, 连着几个)]。最短的一截不到 min_n 个就并给旁边长的那截，直到都够长。"""
    out: list[list[int]] = []
    for lab in labels:
        if out and out[-1][0] == lab:
            out[-1][1] += 1
        else:
            out.append([int(lab), 1])
    while len(out) > 1:
        j = min(range(len(out)), key=lambda q: out[q][1])
        if out[j][1] >= min_n:
            break
        nb = j - 1 if j > 0 and (j == len(out) - 1 or out[j - 1][1] >= out[j + 1][1]) else j + 1
        out[nb][1] += out[j][1]
        del out[j]
        k = 0
        while k < len(out) - 1:
            if out[k][0] == out[k + 1][0]:
                out[k][1] += out[k + 1][1]
                del out[k + 1]
            else:
                k += 1
    return [(a, b) for a, b in out]


def plan(turns: list[Turn], cluster: Callable[[np.ndarray, float], list[int]] = average_linkage,
         min_sim: float = MIN_SIM, min_share: float = MIN_SHARE, min_run: float = MIN_RUN,
         width: float = WIDTH, hop: float = HOP, most: int = MOST,
         short_min: float = 0.3, short_margin: float = 0.05,
         ) -> tuple[dict[int, int], dict[int, list[tuple[float, float, int]]]]:
    """返回 (整句改判 {行号: 说话人编号}, 要切开的 {行号: [(从第几秒, 到第几秒, 说话人编号), …]})。
    秒数从这句话的起点算；负数编号表示要新建一位（同一个负数是同一位）。"""
    voiced = [t for t in turns if len(t.windows)]
    if sum(len(t.windows) for t in voiced) < 3:
        return {}, {}
    x = _unit(np.concatenate([np.asarray(t.windows, dtype=np.float32).reshape(len(t.windows), -1) for t in voiced]))
    sub = x[::max(1, math.ceil(len(x) / most))]
    labels = np.asarray(cluster(sub, min_sim))
    counts = np.bincount(labels)
    major = [k for k in range(len(counts)) if counts[k] >= min_share * len(labels)]
    if not major:
        return {}, {}
    centroids = _unit(np.stack([sub[labels == k].mean(axis=0) for k in major]))
    owner = np.argmax(x @ centroids.T, axis=1)          # 每个小窗归哪一簇（major 里的第几个）

    per: dict[int, np.ndarray] = {}
    pos = 0
    for t in voiced:
        per[t.line_id] = owner[pos:pos + len(t.windows)]
        pos += len(t.windows)
    overlap = Counter()
    for t in voiced:
        if t.speaker_id is not None:
            for lab in per[t.line_id]:
                overlap[(int(lab), t.speaker_id)] += 1
    mapping = match_ids(overlap, range(len(major)))
    alive = set(mapping.values())

    changes: dict[int, int] = {}
    splits: dict[int, list[tuple[float, float, int]]] = {}
    min_n = max(1, math.ceil(min_run / hop - 1e-6))
    for t in turns:
        if t.fixed:
            continue
        if t.line_id not in per:                        # 短句：整句声纹跟最像的那位走
            if t.emb is None or not len(t.emb):
                continue
            sims = centroids @ _unit(np.asarray(t.emb, dtype=np.float32).ravel())
            order = np.argsort(-sims)
            second = sims[order[1]] if len(order) > 1 else -1.0
            sure = sims[order[0]] >= short_min and sims[order[0]] - second >= short_margin
            if not sure and t.speaker_id in alive:      # 拿不准就不动；原来那位已经没有了，就跟最像的走
                continue
            sid = mapping[int(order[0])]
            if sid != t.speaker_id:
                changes[t.line_id] = sid
            continue
        rs = runs(per[t.line_id], min_n)
        if len(rs) == 1:
            sid = mapping[rs[0][0]]
            if sid != t.speaker_id:
                changes[t.line_id] = sid
            continue
        parts, start, n = [], 0.0, 0
        for q, (lab, k) in enumerate(rs):
            n += k
            end = t.seconds if q == len(rs) - 1 else round((n - 0.5) * hop + width / 2, 3)   # 两段小窗中心的正中
            parts.append((start, end, mapping[lab]))
            start = end
        splits[t.line_id] = parts
    return changes, splits


_PUNCT = set("，。？！、；：,.?!;:…")


def _join(tokens: list[str]) -> str:
    return "".join(tokens).replace("▁", " ").strip()


def cut_text(tokens: list[str], times: list[float], parts: list[tuple[float, float, int]],
             ) -> list[tuple[float, float, int, str]]:
    """按每个字的时间（秒，从这句话的起点算）把字分给切开的几段 → [(起, 止, 说话人编号, 文字)]。
    分到的全是标点或杂音的一段不单独成句，并给前一段（第一段并给后一段）；并完挨着的同一位再合成一段。"""
    bins: list[list[str]] = [[] for _ in parts]
    for tok, at in zip(tokens, times):
        k = next((i for i, (_, end, _) in enumerate(parts) if at < end), len(parts) - 1)
        if tok in _PUNCT and k > 0 and not bins[k]:
            k -= 1                                      # 切口后头的标点算前一段的句尾
        bins[k].append(tok)
    out = [[s, e, sid, toks] for (s, e, sid), toks in zip(parts, bins)]
    k = 0
    while len(out) > 1 and k < len(out):
        if strip_noise(_join(out[k][3])):
            k += 1
            continue
        nb = k - 1 if k > 0 else k + 1
        lo, hi = sorted((k, nb))
        keep = out[nb]
        merged = [out[lo][0], out[hi][1], keep[2], out[lo][3] + out[hi][3]]
        out[lo:hi + 1] = [merged]
        k = 0
    k = 0
    while k < len(out) - 1:
        if out[k][2] == out[k + 1][2]:
            out[k] = [out[k][0], out[k + 1][1], out[k][2], out[k][3] + out[k + 1][3]]
            del out[k + 1]
        else:
            k += 1
    return [(s, e, sid, _join(toks)) for s, e, sid, toks in out]
