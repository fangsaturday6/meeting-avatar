"""会后重聚类：散会后拿整场的声纹重新分一次是谁说的。

会上只能逐句、按先来后到归属（speakers.SpeakerBook），一个人常被拆成几位，几个人也会被并成一位。
散会后整场的声纹都在手里，拿够长的句子做一次整体聚类，再把短句按最像的那位归进去。

真实会议实测（2026-11-14，三场会、CAM++ 声纹，见录音路线图结论区）：屋里收音的声纹比安静时散得多，
同一个人的长句之间相似度常常只有 0.5~0.7。sherpa-onnx 自带的聚类在这种声纹上切得很碎（39 分钟两个人
分出 26 簇），改用平均连接、相似度 ≥ 0.5，再把占说话时长不到 5% 的小簇当成杂音而不是新的人，
两场已知人数的会（2 人、4 人）都估对了。

规则：
- 只用够长（≥ min_seconds）的句子定有几个人、谁是谁；短句和小簇里的句子只跟着走，拿不准就不动。
- 没有声纹的句子不动；用户亲手定过的（改过名的说话人、单独改判过的句子）不动。
- 新分出来的簇按「和会上哪位说话人重合的句子最多」对上原编号；对不上的用负数，表示要新建一位。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass
class Item:
    line_id: int
    seconds: float
    emb: np.ndarray | None
    speaker_id: int | None
    fixed: bool = False         # 用户定过的：不改它，但它的声音照样拿来参考


def _unit(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.where(n > 0, n, 1)


def average_linkage(x: np.ndarray, min_sim: float) -> list[int]:
    """平均连接的层次聚类：两簇之间所有句对的平均相似度 ≥ min_sim 就并。x 是单位向量，一行一句。
    几百句只要零点几秒；一场三小时的会长句也就上千句。"""
    n = len(x)
    if n == 0:
        return []
    s = (x @ x.T).astype(np.float64)
    np.fill_diagonal(s, -np.inf)
    size = np.ones(n)
    label = np.arange(n)
    while True:
        i, j = np.unravel_index(int(np.argmax(s)), s.shape)
        if s[i, j] < min_sim:
            break
        row = (s[i] * size[i] + s[j] * size[j]) / (size[i] + size[j])
        s[i, :] = row
        s[:, i] = row
        s[i, i] = -np.inf
        s[j, :] = -np.inf
        s[:, j] = -np.inf
        size[i] += size[j]
        label[label == j] = i
    _, dense = np.unique(label, return_inverse=True)
    return [int(v) for v in dense]


def match_ids(overlap: Counter, labels) -> dict[int, int]:
    """新簇对上会上的说话人编号：重合得越多越优先，一个编号只给一簇；对不上的给负数（要新建一位）。
    overlap：{(簇, 会上的编号): 重合多少}。"""
    mapping: dict[int, int] = {}
    taken: set[int] = set()
    for (lab, sid), _n in sorted(overlap.items(), key=lambda kv: -kv[1]):
        if lab not in mapping and sid not in taken:
            mapping[lab] = sid
            taken.add(sid)
    fresh = -1
    for lab in sorted(labels):
        if lab not in mapping:
            mapping[lab] = fresh
            fresh -= 1
    return mapping


def recluster(items: list[Item], cluster: Callable[[np.ndarray, float], list[int]] = average_linkage,
              min_seconds: float = 2.0, min_sim: float = 0.5, min_share: float = 0.05,
              short_min: float = 0.3, short_margin: float = 0.05) -> dict[int, int]:
    """返回要改的句子：{line_id: 新的说话人编号}。负数编号表示要新建一位说话人（同一个负数是同一位）。

    min_share：一簇的长句合计不到全场长句时长的这个比例，就不算一个人，里面的句子按短句处理。
    """
    voiced = [it for it in items if it.emb is not None and len(it.emb)]
    long_ = [it for it in voiced if it.seconds >= min_seconds]
    if len(long_) < 3:
        return {}
    x = _unit(np.stack([np.asarray(it.emb, dtype=np.float32).ravel() for it in long_]))
    labels = cluster(x, min_sim)
    secs = Counter()
    for it, lab in zip(long_, labels):
        secs[lab] += it.seconds
    total = sum(secs.values())
    major = {lab for lab, s in secs.items() if s >= min_share * total}
    label_of = {it.line_id: lab for it, lab in zip(long_, labels) if lab in major}
    centroids = {lab: _unit(x[[i for i, l in enumerate(labels) if l == lab]].mean(axis=0)) for lab in major}

    # 短句、小簇里的句子：跟最像的那一簇走；最像的不够像、或者和第二像的差不多，就不动
    keys = list(centroids)
    cmat = np.stack([centroids[k] for k in keys])
    for it in voiced:
        if it.line_id in label_of:
            continue
        sims = cmat @ _unit(np.asarray(it.emb, dtype=np.float32).ravel())
        order = np.argsort(-sims)
        best = sims[order[0]]
        second = sims[order[1]] if len(order) > 1 else -1.0
        if best >= short_min and best - second >= short_margin:
            label_of[it.line_id] = keys[order[0]]

    overlap = Counter((label_of[it.line_id], it.speaker_id) for it in voiced
                      if it.line_id in label_of and it.speaker_id is not None)
    mapping = match_ids(overlap, centroids)

    changes = {}
    for it in voiced:
        if it.fixed or it.line_id not in label_of:
            continue
        new = mapping[label_of[it.line_id]]
        if new != it.speaker_id:
            changes[it.line_id] = new
    return changes
