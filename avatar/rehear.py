"""散会后重听一遍：同一个人接连说的几句碎话拼起来（最长约 15 秒），用同一个识别模型再认一次。

会上断句一停顿就切，一句话常被切成几截：句首句尾的字被削掉，「回价」认成「回家」，「询价」认成「请家」。
拼长了模型能看到前后文，字准得多。实测（2026-11-14，两场真实会议）：拼起来的 261 段里，
清楚的段落重认后明显更好，会上漏掉的字也找回来了；听不清的段落两版都是乱码，有时重认反而丢字，
所以重认的字数比会上少了两成以上就不用，留着会上的。

这是重新识别，不是改字：改字只在整理稿里做（slow_brain.polish）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

GAP = 1.5          # 两句之间停顿不超过这么多秒，才算接着说
LIMIT = 15.0       # 拼起来最长这么多秒（SenseVoice 一次认 15 秒左右最稳，断句本身最长切 20 秒）
KEEP = 0.8         # 重认的字数不到会上的这个比例，就不用


@dataclass
class Piece:
    line_ids: list[int]
    channel: str
    start: float
    end: float
    live: str                       # 会上认出来的，几句接在一起
    texts: list[str] = field(default_factory=list)


def pieces(lines, channel_of: dict[int, str]) -> list[Piece]:
    """会议记录里能拼的段落：同一路声音、同一位说话人、紧挨着的几句（中间没有数字人说话、没有打字的）。
    只有一句的不用重认（声音和会上一样）。lines 是 Meeting.lines。"""
    out: list[Piece] = []
    cur: Piece | None = None
    last_speaker = None
    for line in lines:
        ch = channel_of.get(line.id)
        heard = line.who == "human" and line.at is not None and not line.off and ch is not None
        if not heard:
            cur = None
            continue
        end = line.at + (line.seconds or 0.0)
        if (cur is not None and ch == cur.channel and line.speaker_id == last_speaker
                and line.at - cur.end <= GAP and end - cur.start <= LIMIT):
            cur.line_ids.append(line.id)
            cur.texts.append(line.text)
            cur.end = max(cur.end, end)
        else:
            cur = Piece([line.id], ch, line.at, end, "", [line.text])
            out.append(cur)
        last_speaker = line.speaker_id
    for p in out:
        p.live = "".join(p.texts)
    return [p for p in out if len(p.line_ids) > 1]


def _chars(text: str) -> int:
    return len(re.sub(r"[\s，。？！、；：,.?!;:]", "", text))


def better(live: str, again: str) -> bool:
    """重认的能不能用：不能是空的，字数不能比会上少太多（听不清的段落重认时容易丢字）。"""
    return bool(again) and _chars(again) >= KEEP * _chars(live)
