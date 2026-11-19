"""和别家比转写：同一段公开会议录音，交给我们和腾讯会议、飞书妙记、企微，各自的逐字稿和人工转写比。

    uv run python tools/vendor_bench.py prepare <Eval_Ali 目录> <输出目录>   # 切录音、存人工转写、跑我们的整套流程
    uv run python tools/vendor_bench.py score <输出目录>                    # 汇总成表

输出目录里：
- 音频/：要上传的三段录音（wav 和 mp3 各一份，内容一样），两人、三人、四人各一段，约 8 分钟。
  取的是会议桌上阵列麦第 1 路、按浏览器自动增益的样子拉过响度（和 meeting_bench 一样），各家拿到的是同一份。
- 参照/：人工转写（不要上传）。
- 我们/：我们散会后的会议记录（逐句对话、纪要、整理稿）。
- 各家/<产品名>/：把那家导出的逐字稿（txt）放进来，文件名里带上「两人」「三人」「四人」。

比的是：错字率（整段拼起来逐字比，标点不算；人工转写里的数字写的是汉字，各家写阿拉伯数字的会多算一点，大家一样吃亏），
认人（导出里有说话人和时刻时，按时间重叠对上真人，算认对的说话时长占比）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from meeting_bench import RATE, agc, cer, channel0, sessions, speaker_match, textgrid

CLIPS = {"两人": "R8009_M8018", "三人": "R8008_M8013", "四人": "R8003_M8001"}
START, LENGTH = 300.0, 480.0        # 从第 5 分钟起取 8 分钟，两头挪到没人说话的地方


def quiet_point(rows, t: float, gap: float = 0.6) -> float:
    """t 之后第一个至少静 gap 秒的空档的中间（谁都没在说）。"""
    spans = sorted((s, e) for _, s, e, _ in rows)
    busy_until = 0.0
    for s, e in spans:
        if s - busy_until >= gap and busy_until >= t:
            return (busy_until + s) / 2
        busy_until = max(busy_until, e)
    return t


def cut(rows, pcm: bytes) -> tuple[bytes, list, float]:
    a = quiet_point(rows, START)
    b = quiet_point(rows, a + LENGTH)
    inside = [(spk, s - a, e - a, text) for spk, s, e, text in rows if s >= a and e <= b]
    return pcm[int(a * RATE) * 2:int(b * RATE) * 2], inside, a


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def mmss(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


async def prepare(data: Path, out: Path) -> int:
    from meeting_bench import pipeline

    src = sessions(data)
    for d in ("音频", "参照", "我们", "各家/腾讯会议", "各家/飞书妙记", "各家/企微"):
        (out / d).mkdir(parents=True, exist_ok=True)
    for label, name in CLIPS.items():
        wav, tg = src[name]
        clip, rows, a = cut(textgrid(tg), agc(channel0(wav)))
        write_wav(out / "音频" / f"测试-{label}.wav", clip)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(out / "音频" / f"测试-{label}.wav"),
                        "-b:a", "128k", str(out / "音频" / f"测试-{label}.mp3")], check=True)
        names = {spk: f"说话人{i}" for i, spk in enumerate(dict.fromkeys(r[0] for r in rows), 1)}
        (out / "参照" / f"{label}.json").write_text(json.dumps(
            {"会议": name, "从第几秒切": round(a, 2), "句子": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "参照" / f"{label}.txt").write_text(
            "\n".join(f"[{mmss(s)}] {names[spk]}：{text}" for spk, s, _e, text in rows), encoding="utf-8")
        print(f"{label}：{name} 第 {a:.0f} 秒起 {len(clip) / 2 / RATE / 60:.1f} 分钟，{len(names)} 人 {len(rows)} 句", flush=True)
    for label in CLIPS:               # 录音先全切好（可以先拿去上传），再一段段跑我们的流程
        if (out / "我们" / f"{label}.json").exists():
            continue
        with wave.open(str(out / "音频" / f"测试-{label}.wav")) as w:
            clip = w.readframes(w.getnframes())
        with tempfile.TemporaryDirectory() as tmp:
            got = await pipeline(clip, Path(tmp), polish=True)
        (out / "我们" / f"{label}.md").write_text(got["record"], encoding="utf-8")
        (out / "我们" / f"{label}.json").write_text(json.dumps(
            {k: got[k] for k in ("live", "final", "heard_seconds", "total_seconds")}, ensure_ascii=False), encoding="utf-8")
        print(f"  我们跑完：识别 {got['heard_seconds']} 秒，连纪要和整理稿共 {got['total_seconds']} 秒", flush=True)
    return 0


# ---------- 读各家导出的逐字稿 ----------
_TIME = r"(?P<t>\d{1,2}:\d{2}(?::\d{2})?)"
_HEAD = re.compile(rf"^\s*(?P<spk>[^\d:：\[\]（）()]{{1,16}}?\s*\d{{0,3}})\s*[\[(（]?{_TIME}[\])）]?\s*[:：]?\s*(?P<rest>.*)$")
_BRACKET = re.compile(rf"^\s*[\[(（]?{_TIME}[\])）]?\s*(?P<spk>[^:：]{{1,16}})[:：]\s*(?P<rest>.*)$")
_BODY = re.compile(r"^(Transcript|文字记录|逐字稿)\s*[:：]?$", re.IGNORECASE)


def seconds(t: str) -> float:
    parts = [int(p) for p in t.split(":")]
    return sum(v * 60 ** i for i, v in enumerate(reversed(parts)))


def parse_export(text: str) -> list[dict]:
    """导出的逐字稿 → [{说话人, 起, 文字}]。认得「说话人 1  00:12」下一行是话，也认得「[00:12] 说话人1：话」。
    认不出格式时整篇当一段（只能算错字率，算不了认人）。"""
    blocks: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line:
            continue
        if _BODY.match(line):          # 飞书妙记的导出前面有时间、关键词，到「Transcript:」才是正文
            blocks = []
            continue
        m = _BRACKET.match(line) or _HEAD.match(line)
        if m:
            blocks.append({"spk": m.group("spk").strip(), "at": seconds(m.group("t")),
                           "text": m.group("rest").strip()})
        elif blocks:
            blocks[-1]["text"] += line
        else:
            blocks.append({"spk": "", "at": None, "text": line})
    return blocks


def spans(blocks: list[dict], end: float) -> list[tuple[str, float, float]]:
    """每段从它的时刻到下一段开始（导出只有起点）。"""
    timed = [b for b in blocks if b["at"] is not None and b["spk"]]
    return [(b["spk"], b["at"], (timed[i + 1]["at"] if i + 1 < len(timed) else end)) for i, b in enumerate(timed)]


_FILLER = re.compile(r"[呃嗯啊哦唉诶额哎]")


def rates(ref: str, hyp: str) -> str:
    """原样比一个，两边都去掉语气词再比一个（人工转写留着「呃」「嗯」，有的产品默认删掉，不去掉就吃亏）。"""
    return f"{cer(ref, hyp) * 100:.1f}% | {cer(_FILLER.sub('', ref), _FILLER.sub('', hyp)) * 100:.1f}%"


def score(out: Path) -> int:
    rows_md = ["| 录音 | 谁转的 | 错字率 | 去掉语气词后 | 估出人数/真人数 | 认对的说话时长 |", "|---|---|---|---|---|---|"]
    for label in CLIPS:
        ref = json.loads((out / "参照" / f"{label}.json").read_text(encoding="utf-8"))
        rows = [tuple(r) for r in ref["句子"]]
        ref_text = "".join(r[3] for r in rows)
        end = max(r[2] for r in rows)
        people = len({r[0] for r in rows})
        ours = json.loads((out / "我们" / f"{label}.json").read_text(encoding="utf-8"))
        final = sorted(ours["final"], key=lambda l: l["at"] or 0)
        m = speaker_match(rows, [(l["speaker_id"], l["at"], l["at"] + l["seconds"]) for l in final
                                 if l["at"] is not None and l["speaker_id"]])
        rows_md.append(f"| {label} | 我们（会上） | {rates(ref_text, ''.join(l['text'] for l in ours['live']))} | | |")
        rows_md.append(f"| {label} | 我们（散会后） | {rates(ref_text, ''.join(l['text'] for l in final))} | "
                       f"{m['估出人数']}/{people} | {m['认对的时长占比'] * 100:.0f}% |")
        record = (out / "我们" / f"{label}.md").read_text(encoding="utf-8")
        polished = record.partition("\n## 整理稿\n")[2].partition("\n## ")[0]
        if polished.strip():
            body = re.sub(r"\[\d{2}:\d{2}:\d{2}\]\s*[^：]{1,16}：", "", polished)
            rows_md.append(f"| {label} | 我们（整理稿，书面化过） | {rates(ref_text, body)} | | |")
        for vendor_dir in sorted((out / "各家").iterdir()):
            for f in sorted(vendor_dir.glob(f"*{label}*.txt")):
                blocks = parse_export(f.read_text(encoding="utf-8", errors="replace"))
                hyp = "".join(b["text"] for b in blocks)
                sp = spans(blocks, end)
                who = speaker_match(rows, sp) if sp else None
                rows_md.append(f"| {label} | {vendor_dir.name} | {rates(ref_text, hyp)} | "
                               + (f"{who['估出人数']}/{people} | {who['认对的时长占比'] * 100:.0f}% |" if who else "认不出说话人 | |"))
    table = "\n".join(rows_md)
    (out / "结果.md").write_text(table + "\n", encoding="utf-8")
    print(table)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="和别家比转写")
    ap.add_argument("cmd", choices=["prepare", "score"])
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args()
    if args.cmd == "prepare":
        return asyncio.run(prepare(Path(args.paths[0]), Path(args.paths[1])))
    return score(Path(args.paths[0]))


if __name__ == "__main__":
    os.environ.setdefault("AVATAR_WORKSPACE", tempfile.mkdtemp(prefix="avatar-vendor-"))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
