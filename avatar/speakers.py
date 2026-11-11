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
    def __init__(self, threshold: float = 0.45):
        self.threshold = threshold
        self.speakers: dict[int, Speaker] = {}
        self.last_id: int | None = None
        self._next = 1

    def _new(self, name: str | None = None) -> Speaker:
        sp = Speaker(id=self._next, name=name or f"说话人{self._next}")
        self.speakers[sp.id] = sp
        self._next += 1
        return sp

    def assign(self, embedding: np.ndarray | None) -> int:
        """给一段语音找说话人。没有声纹（片段太短）时沿用上一位。"""
        if embedding is None or len(embedding) == 0:
            if self.last_id is None:
                self.last_id = self._new().id
            self.speakers[self.last_id].count += 1
            return self.last_id

        e = _unit(embedding)
        best, best_sim = None, -1.0
        for sp in self.speakers.values():
            if sp.centroid is None:
                continue
            sim = float(np.dot(sp.centroid, e))
            if sim > best_sim:
                best, best_sim = sp, sim
        if best is None or best_sim < self.threshold:
            best = self._new()
            best.centroid = e
        else:
            # 质心滑动更新：样本越多越稳，但保留适应性
            w = min(best.count, 20)
            best.centroid = _unit(best.centroid * w + e)
        best.count += 1
        self.last_id = best.id
        return best.id

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
        sp.name = name.strip() or sp.name
        return sp

    def name_of(self, speaker_id: int | None) -> str:
        sp = self.speakers.get(speaker_id) if speaker_id is not None else None
        return sp.name if sp else "有人"

    def snapshot(self) -> list[dict]:
        return [sp.to_dict() for sp in self.speakers.values()]
