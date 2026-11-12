"""说话人区分：声纹向量 → 说话人。只做归属和改名，不碰音频。"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Speaker:
    id: int
    name: str
    count: int = 0
    centroid: np.ndarray | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "count": self.count}


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class SpeakerBook:
    """实测（CAM++ 声纹）：同一个人的长句相似度 0.82~0.95，不同的人一般低于 0.42；
    一两秒的短句不稳定，和本人长句的相似度可能只有 0.46。所以：
    阈值取 0.55；短句只用来归属，不用来新建说话人，也不更新质心。"""

    reliable_seconds = 2.0

    def __init__(self, threshold: float = 0.55):
        self.threshold = threshold
        self.speakers: dict[int, Speaker] = {}
        self.last_id: int | None = None
        self.last_scores: dict[str, float] = {}   # 最近一段声音和每位已知说话人有多像（查「为什么认成了他」用）；没有声纹时是空的
        self._next = 1

    def _new(self, name: str | None = None) -> Speaker:
        sp = Speaker(id=self._next, name=name or f"说话人{self._next}")
        self.speakers[sp.id] = sp
        self._next += 1
        return sp

    def _reuse_last(self) -> int:
        if self.last_id is None:
            self.last_id = self._new().id
        self.speakers[self.last_id].count += 1
        return self.last_id

    def assign(self, embedding: np.ndarray | None, seconds: float = 10.0) -> int:
        """给一段语音找说话人。seconds 是这段语音的长度。没有声纹时沿用上一位。"""
        self.last_scores = {}
        if embedding is None or len(embedding) == 0:
            return self._reuse_last()

        e = _unit(embedding)
        reliable = seconds >= self.reliable_seconds
        best, best_sim = None, -1.0
        for sp in self.speakers.values():
            if sp.centroid is None:
                continue
            sim = float(np.dot(sp.centroid, e))
            self.last_scores[sp.name] = sim
            if sim > best_sim:
                best, best_sim = sp, sim
        if best is None or best_sim < self.threshold:
            if not reliable and self.last_id is not None:
                return self._reuse_last()      # 短句对不上谁：多半还是刚才那位
            best = self._new()
            best.centroid = e
        elif reliable:
            # 质心滑动更新：样本越多越稳，但保留适应性
            w = min(best.count, 20)
            best.centroid = _unit(best.centroid * w + e)
        best.count += 1
        self.last_id = best.id
        return best.id

    def similarity(self, speaker_id: int, embedding: np.ndarray) -> float:
        """一段声音和某位说话人有多像（不改动任何记录）。没有声纹可比时返回 -1。"""
        sp = self.speakers.get(speaker_id)
        if sp is None or sp.centroid is None or embedding is None or len(embedding) == 0:
            return -1.0
        return float(np.dot(sp.centroid, _unit(embedding)))

    def by_name(self, name: str) -> int:
        """按名字取说话人（文字注入用），没有就新建。"""
        for sp in self.speakers.values():
            if sp.name == name:
                sp.count += 1
                self.last_id = sp.id
                return sp.id
        sp = self._new(name)
        sp.count = 1
        self.last_id = sp.id
        return sp.id

    def rename(self, speaker_id: int, name: str) -> Speaker:
        sp = self.speakers[speaker_id]
        sp.name = " ".join(name.split()) or sp.name     # 名字里不留换行：它会原样写进会议记录和给大脑的话里
        return sp

    def name_of(self, speaker_id: int | None) -> str:
        sp = self.speakers.get(speaker_id) if speaker_id is not None else None
        return sp.name if sp else "有人"

    def snapshot(self) -> list[dict]:
        return [sp.to_dict() for sp in self.speakers.values()]
