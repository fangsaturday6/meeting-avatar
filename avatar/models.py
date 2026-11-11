"""本地语音模型：路径与首次下载。

用法：uv run python -m avatar.models   （缺哪个下哪个）

下载源里有国内镜像（比官方源快得多）。凡是从镜像来的大文件都用官方仓库公布的
SHA-256 校验，对不上就丢弃。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import child_env

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models"

_GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
_HF = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
_MS = "https://www.modelscope.cn/models/pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue/resolve/master"


@dataclass(frozen=True)
class ModelFile:
    name: str
    urls: tuple[str, ...]      # 按顺序尝试
    min_bytes: int             # 小于这个大小视为下载不完整
    sha256: str = ""           # 官方校验值；有就必须对上
    git_sha1: str = ""         # 小文件用官方仓库里的 git 对象号校验
    optional: bool = False     # 缺了也能开会，只是少一项能力

    @property
    def path(self) -> Path:
        return MODEL_DIR / self.name

    @property
    def ready(self) -> bool:
        return self.path.exists() and self.path.stat().st_size >= self.min_bytes


VAD = ModelFile("silero_vad.onnx", (f"{_GH}/asr-models/silero_vad.onnx",), 500_000)
ASR = ModelFile(
    "sense-voice.int8.onnx", (f"{_MS}/model.int8.onnx", f"{_HF}/model.int8.onnx"), 100_000_000,
    sha256="c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
)
ASR_TOKENS = ModelFile("sense-voice.tokens.txt", (f"{_HF}/tokens.txt",), 100_000)
SPEAKER = ModelFile(
    "speaker-campplus.onnx",
    (f"{_GH}/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
     "https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main/"
     "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"),
    20_000_000, optional=True,   # 缺了就不区分说话人
    sha256="f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11",
)

# 本地语音合成（MeloTTS 中英混读）。镜像下载，逐个文件对官方校验值。
_MELO_MS = "https://www.modelscope.cn/models/AdamLee/vits-melo-tts-zh_en_copy/resolve/master"
_MELO_HF = "https://huggingface.co/csukuangfj/vits-melo-tts-zh_en/resolve/main"


def _melo(name: str, min_bytes: int, sha256: str = "", git_sha1: str = "") -> ModelFile:
    return ModelFile(f"melo/{name}", (f"{_MELO_MS}/{name}", f"{_MELO_HF}/{name}"), min_bytes,
                     sha256=sha256, git_sha1=git_sha1, optional=True)   # 缺了就用在线或系统语音


TTS_MODEL = _melo("model.onnx", 100_000_000,
                  sha256="bf30582eb1b012250a35b1a4a80e7dfbcf8485e7bb9de0d95efbbeef0e4ad86d")
TTS_LEXICON = _melo("lexicon.txt", 1_000_000, git_sha1="1a1119efceca0c2dae243f84061e70e4e3d99710")
TTS_TOKENS = _melo("tokens.txt", 100, git_sha1="78ed7abf73cfa7955577a2894388dcd8f5a317c3")
TTS_FSTS = [
    _melo("date.fst", 1000, git_sha1="20ec7f4c077f9ec85ca9d01c4b4f4581f6e552ee"),
    _melo("number.fst", 1000, git_sha1="66c0bd468903fc98d212cd6c3c5b2793ce8eb2c3"),
    _melo("phone.fst", 1000, git_sha1="2eba908c27ec0767281cad25d322008b16869c5b"),
    _melo("new_heteronym.fst", 1000, git_sha1="478a809252067229211f7f414414f34f98314d9d"),
]
TTS = [TTS_MODEL, TTS_LEXICON, TTS_TOKENS, *TTS_FSTS]
ALL = [VAD, ASR, ASR_TOKENS, *TTS, SPEAKER]


def tts_ready() -> bool:
    return all(m.ready for m in TTS)


def missing(include_optional: bool = True) -> list[ModelFile]:
    return [m for m in ALL if not m.ready and (include_optional or not m.optional)]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _git_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def download(m: ModelFile) -> None:
    m.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = m.path.with_suffix(m.path.suffix + ".part")
    errors = []
    for url in m.urls:
        print(f"下载 {m.name} …", flush=True)
        r = subprocess.run(
            ["curl", "-fL", "--retry", "5", "--retry-all-errors", "--retry-delay", "3",
             "-C", "-", "--connect-timeout", "20", "-sS", "-o", str(tmp), url],
            capture_output=True, text=True, env=child_env(),
        )
        if r.returncode != 0:
            errors.append(r.stderr.strip()[-160:])
            continue
        if tmp.stat().st_size < m.min_bytes:
            tmp.unlink()
            errors.append("文件不完整")
            continue
        if (m.sha256 and _sha256(tmp) != m.sha256) or (m.git_sha1 and _git_sha1(tmp) != m.git_sha1):
            tmp.unlink()
            errors.append("校验值与官方不一致，已丢弃")
            continue
        tmp.replace(m.path)
        print(f"完成 {m.name}（{m.path.stat().st_size >> 20} MB）", flush=True)
        return
    raise RuntimeError(f"{m.name} 下载失败：{'；'.join(errors)}")


def ensure(include_optional: bool = True) -> None:
    for m in missing(include_optional):
        download(m)


if __name__ == "__main__":
    try:
        ensure()
    except Exception as e:  # noqa: BLE001
        print(f"模型下载失败：{e}", file=sys.stderr)
        sys.exit(1)
    print("模型齐全")
