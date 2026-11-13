"""重放评测台：把一场会存下来的录音喂给真实的耳朵，看它现在会听出什么。

调断句、识别、认人的参数之前先用它比一比，别只看单元测试：

    uv run python -m avatar.replay latest                      # 最近一场，照现在的参数重听一遍
    uv run python -m avatar.replay latest --diff heard         # 和会上实际听到的逐句比
    uv run python -m avatar.replay 2026-11-13_100000 --set lead_in=0.3 --save lead03
    uv run python -m avatar.replay 2026-11-13_100000 --diff lead03   # 再和上面那次比

录音在 workspace/audio/<会议编号>/，见 recorder.py。识别要占满几个核，别在正开着真会的时候跑。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import difflib
import json
import sys
import time
from typing import Callable

import numpy as np

from .recorder import RATE, AudioStore
from .speakers import SpeakerBook

TUNABLE = ("lead_in", "min_silence", "min_embed_seconds", "vad_threshold", "min_speech", "max_speech", "threads")
FRAME = 640 * 2          # 和形象页一样，每次喂 40 毫秒


async def replay(pcm: bytes, ears) -> list[dict]:
    """把一段录音按帧喂给耳朵，返回它认出的每句话（at、seconds、text、emb）。"""
    rows: list[dict] = []

    async def heard(text, emb, seconds=0.0, at=None):
        rows.append({"at": at, "seconds": seconds, "text": text, "emb": emb})

    ears.on_utterance = heard
    for n, i in enumerate(range(0, len(pcm), FRAME)):
        ears.feed(pcm[i:i + FRAME])
        if n % 50 == 0:
            await asyncio.sleep(0)        # 让识别结果有机会回来，别把几千个片段全堆在队列里
    ears.flush()
    await ears.drain()
    return rows


def assign(rows: list[dict], threshold: float) -> None:
    """按会上同样的办法逐句认人，把 speaker 填进每一行。"""
    book = SpeakerBook(threshold)
    for r in rows:
        r["speaker"] = book.name_of(book.assign(r.get("emb"), r["seconds"]))


def _clock(at) -> str:
    if at is None:
        return "--:--.-"
    return f"{int(at // 60):02d}:{at % 60:04.1f}"


def _line(r: dict) -> str:
    who = r.get("speaker") or (f"说话人{r['speaker_id']}" if r.get("speaker_id") else "")
    return f"[{_clock(r.get('at'))}] {who} {r.get('seconds', 0):.1f}秒｜{r['text']}"


def _text_only(r: dict) -> str:
    return f"[{_clock(r.get('at'))}] {r['text']}"


def diff(a: list[dict], b: list[dict]) -> list[str]:
    """逐句比两次结果（只比位置和文字）。返回 - / + 开头的行；一样就是空列表。"""
    left, right = [_text_only(r) for r in a], [_text_only(r) for r in b]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=left, b=right, autojunk=False).get_opcodes():
        if tag != "equal":
            out += [f"- {x}" for x in left[i1:i2]] + [f"+ {x}" for x in right[j1:j2]]
    return out


def _latest(store: AudioStore) -> str | None:
    if not store.root.is_dir():
        return None
    ids = sorted(p.name for p in store.root.iterdir() if p.is_dir() and store.has_audio(p.name))
    return ids[-1] if ids else None


def _parse_sets(pairs: list[str]) -> dict:
    params = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        if key not in TUNABLE or not raw:
            raise ValueError(f"不能调的参数：{pair}（能调的有 {'、'.join(TUNABLE)}）")
        params[key] = int(raw) if raw.isdigit() else float(raw)
    return params


def _real_ears(**params):
    from .ears import Ears
    ears = Ears(lambda: None, lambda ago: None, None)
    for k, v in params.items():
        setattr(ears, k, v)          # 断句参数在 load 时生效，所以先设再加载
    return ears


def main(argv: list[str] | None = None, store: AudioStore | None = None,
         make_ears: Callable[..., object] = _real_ears) -> int:
    ap = argparse.ArgumentParser(prog="python -m avatar.replay", description="拿存下来的录音重听一遍")
    ap.add_argument("meeting", nargs="?", default="latest", help="会议编号，或 latest（默认）")
    ap.add_argument("--set", action="append", default=[], metavar="参数=值", help="改耳朵的参数：" + "、".join(TUNABLE))
    ap.add_argument("--threshold", type=float, default=SpeakerBook().threshold, help="认人的相似度阈值")
    ap.add_argument("--save", metavar="名字", help="把结果存成 replay-<名字>.jsonl，之后可以拿来比")
    ap.add_argument("--diff", metavar="heard|名字", help="和会上听到的（heard）或某次存下的结果逐句比")
    args = ap.parse_args(argv)
    store = store or AudioStore()

    mid = _latest(store) if args.meeting == "latest" else args.meeting
    pcm = store.pcm(mid) if mid else b""
    if not pcm:
        print(f"没有这场会的录音：{args.meeting}", file=sys.stderr)
        return 1
    try:
        params = _parse_sets(args.set)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    ears = make_ears(**params)
    ears.load()
    began = time.monotonic()
    rows = asyncio.run(replay(pcm, ears))
    spent = time.monotonic() - began
    ears.close()
    assign(rows, args.threshold)

    seconds = len(pcm) / 2 / RATE
    for r in rows:
        print(_line(r))
    people = len({r["speaker"] for r in rows})
    print(f"\n{mid}：{len(rows)} 句，{people} 位说话人；录音 {seconds / 60:.1f} 分钟，"
          f"识别用了 {spent:.1f} 秒（{spent / seconds if seconds else 0:.2f} 倍时长）")

    if args.save:
        folder = store.folder(mid)
        with (folder / f"replay-{args.save}.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                emb = r.get("emb")
                f.write(json.dumps({**r, "emb": None if emb is None else base64.b64encode(
                    np.asarray(emb, dtype=np.float32).tobytes()).decode()}, ensure_ascii=False) + "\n")
    if args.diff:
        other = store.rows(mid, "heard" if args.diff == "heard" else f"replay-{args.diff}")
        if not other:
            print(f"没有可比的结果：{args.diff}", file=sys.stderr)
            return 1
        changes = diff(other, rows)
        print(f"\n和 {args.diff} 一样" if not changes else f"\n和 {args.diff} 比（- 是 {args.diff}，+ 是这次）：")
        for c in changes:
            print(c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
