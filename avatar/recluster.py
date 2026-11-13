"""会后重聚类：散会后拿整场的声纹重新分一次是谁说的。

会上只能逐句、按先来后到归属（speakers.SpeakerBook），一个人常被拆成几位，几个人也会被并成一位。
散会后整场的声纹都在手里，拿够长的句子做一次整体聚类，再把短句按最像的那位归进去，比会上准得多。
（FluidAudio 在英文 AMI 上测得在线聚类说话人错误率 38–56%，会后整体聚类 10.6%；中文会议没有公开数字。）

规则：
- 只用够长（≥ min_seconds）的句子定有几个人、谁是谁；短句只跟着走，拿不准就不动。
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


def sherpa_cluster(x: np.ndarray, distance: float) -> list[int]:
    """sherpa-onnx 自带的层次聚类：余弦距离小于 distance 的并成一类（实测不是单连接，不会串成一长串）。"""
    import sherpa_onnx as so
    c = so.FastClustering(so.FastClusteringConfig(num_clusters=-1, threshold=distance))
    return list(c(np.ascontiguousarray(x, dtype=np.float32)))


def recluster(items: list[Item], cluster: Callable[[np.ndarray, float], list[int]] = sherpa_cluster,
              min_seconds: float = 2.0, distance: float = 0.45, short_min: float = 0.3,
              short_margin: float = 0.05) -> dict[int, int]:
    """返回要改的句子：{line_id: 新的说话人编号}。负数编号表示要新建一位说话人（同一个负数是同一位）。

    distance 0.45 即相似度 0.55，和会上归属用的阈值一致（同一个人长句 0.82~0.95，不同人一般低于 0.42）。
    """
    voiced = [it for it in items if it.emb is not None and len(it.emb)]
    long_ = [it for it in voiced if it.seconds >= min_seconds]
    if len(long_) < 3:
        return {}
    x = _unit(np.stack([np.asarray(it.emb, dtype=np.float32).ravel() for it in long_]))
    labels = cluster(x, distance)
    label_of = {it.line_id: lab for it, lab in zip(long_, labels)}
    centroids = {lab: _unit(x[[i for i, l in enumerate(labels) if l == lab]].mean(axis=0)) for lab in set(labels)}

    # 短句：跟最像的那一簇走；最像的不够像、或者和第二像的差不多，就不动
    if len(centroids) > 0:
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

    # 新簇对上会上的编号：重合的句子越多越优先，一个编号只给一簇
    overlap = Counter((label_of[it.line_id], it.speaker_id) for it in voiced
                      if it.line_id in label_of and it.speaker_id is not None)
    mapping: dict[int, int] = {}
    taken: set[int] = set()
    for (lab, sid), _n in sorted(overlap.items(), key=lambda kv: -kv[1]):
        if lab not in mapping and sid not in taken:
            mapping[lab] = sid
            taken.add(sid)
    fresh = -1
    for lab in sorted(centroids):
        if lab not in mapping:
            mapping[lab] = fresh
            fresh -= 1

    changes = {}
    for it in voiced:
        if it.fixed or it.line_id not in label_of:
            continue
        new = mapping[label_of[it.line_id]]
        if new != it.speaker_id:
            changes[it.line_id] = new
    return changes
