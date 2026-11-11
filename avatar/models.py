"""本地语音模型：路径与首次下载。

用法：uv run python -m avatar.models   （缺哪个下哪个）
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models"

_GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
_HF = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    min_bytes: int  # 小于这个大小视为下载不完整

    @property
    def path(self) -> Path:
        return MODEL_DIR / self.name


VAD = ModelFile("silero_vad.onnx", f"{_GH}/asr-models/silero_vad.onnx", 500_000)
ASR = ModelFile("sense-voice.int8.onnx", f"{_HF}/model.int8.onnx", 100_000_000)
ASR_TOKENS = ModelFile("sense-voice.tokens.txt", f"{_HF}/tokens.txt", 100_000)
SPEAKER = ModelFile(
    "speaker-campplus.onnx",
    f"{_GH}/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    5_000_000,
)
ALL = [VAD, ASR, ASR_TOKENS, SPEAKER]


def missing() -> list[ModelFile]:
    return [m for m in ALL if not m.path.exists() or m.path.stat().st_size < m.min_bytes]


def download(m: ModelFile) -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = m.path.with_suffix(m.path.suffix + ".part")
    print(f"下载 {m.name} …", flush=True)
    # 用 curl：走系统代理更稳，支持断点续传和重试
    r = subprocess.run(
        ["curl", "-fL", "--retry", "4", "--retry-all-errors", "--retry-delay", "2",
         "-C", "-", "--connect-timeout", "20", "-sS", "-o", str(tmp), m.url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"{m.name} 下载失败：{r.stderr.strip()[-200:]}")
    if tmp.stat().st_size < m.min_bytes:
        tmp.unlink()
        raise RuntimeError(f"{m.name} 下载不完整")
    tmp.replace(m.path)
    print(f"完成 {m.name}（{m.path.stat().st_size >> 20} MB）", flush=True)


def ensure() -> None:
    for m in missing():
        download(m)


if __name__ == "__main__":
    try:
        ensure()
    except Exception as e:  # noqa: BLE001
        print(f"模型下载失败：{e}", file=sys.stderr)
        sys.exit(1)
    print("模型齐全")
