"""可调设置。存在 workspace/settings.json，控制台和接口都能改。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 运行时数据（设置、设计稿）。测试用 AVATAR_WORKSPACE 指到临时目录，免得改掉真实设置
WORKSPACE = Path(os.environ.get("AVATAR_WORKSPACE") or ROOT / "workspace")
SETTINGS_FILE = WORKSPACE / "settings.json"
DESIGN_DIR = WORKSPACE / "designs"

# local = 本机合成，不联网，延迟最稳；其余是在线嗓音，更自然但依赖网络
VOICES = {
    "local": "本地嗓音（不联网，最稳）",
    "zh-CN-YunxiNeural": "在线·云希（男，年轻）",
    "zh-CN-YunjianNeural": "在线·云健（男，沉稳）",
    "zh-CN-XiaoxiaoNeural": "在线·晓晓（女，亲切）",
    "zh-CN-XiaoyiNeural": "在线·晓伊（女，活泼）",
}
SCENES = {"room": "当面开会", "online": "线上会议"}


@dataclass
class Settings:
    name: str = "小克"
    voice: str = "local"
    speed: float = 1.1           # 语速，1.0 为正常
    scene: str = "room"
    project_dir: str = ""        # 「查代码」「做设计」的工作目录
    topic: str = ""              # 会议背景，开会前告诉数字人
    fast_model: str = "claude-haiku-5-5"
    slow_model: str = ""         # 干活用的模型；留空 = 本机 Claude 的默认模型（最稳）
    use_my_skills: bool = False  # 做设计时是否加载你本机 Claude 的技能和插件（更强但更慢）
    silence_nudge: float = 6.0   # 会场安静多少秒后提示数字人一次
    gap: float = 0.4             # 别人说完后至少等多久再开口
    hold: float = 4.0            # 被问到后，旁边一直有别的声音时最多等多久就顶着说
    barge_delay: float = 0.35    # 别人持续说多久算打断
    barge_grace: float = 0.5     # 刚开口的这段时间内不算打断（防回声误触发）

    def to_dict(self) -> dict:
        return asdict(self)

    def update(self, data: dict) -> list[str]:
        """只接受已知字段且类型匹配的值，返回实际改动的字段名。"""
        changed = []
        for f in fields(self):
            if f.name not in data:
                continue
            cur, new = getattr(self, f.name), data[f.name]
            if isinstance(cur, float) and isinstance(new, (int, float)) and not isinstance(new, bool):
                new = float(new)
            if type(new) is not type(cur):
                raise ValueError(f"{f.name} 的类型不对")
            if f.name == "voice" and new not in VOICES:
                raise ValueError("没有这个嗓音")
            if f.name == "scene" and new not in SCENES:
                raise ValueError("没有这个场景")
            if f.name == "project_dir" and new and not Path(new).expanduser().is_dir():
                raise ValueError("项目目录不存在")
            if f.name == "name" and not new.strip():
                raise ValueError("名字不能为空")
            if f.name == "speed" and not 0.6 <= new <= 1.8:
                raise ValueError("语速要在 0.6 到 1.8 之间")
            if new != cur:
                setattr(self, f.name, new)
                changed.append(f.name)
        return changed

    def save(self, path: Path = SETTINGS_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = SETTINGS_FILE) -> "Settings":
        s = cls()
        if path.exists():
            try:
                s.update(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass  # 文件坏了就用默认值
        return s


# ---------- 子进程的网络环境 ----------
_PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
_child_env: dict | None = None


def _proxy_from_shell() -> dict:
    """终端里（~/.zshrc 等）配置的代理。"""
    import subprocess
    script = 'print -r -- "__PROXY__${HTTPS_PROXY:-$https_proxy}|${HTTP_PROXY:-$http_proxy}|${ALL_PROXY:-$all_proxy}|${NO_PROXY:-$no_proxy}"'
    try:
        out = subprocess.run(["zsh", "-ilc", script], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    for line in out.splitlines():
        if "__PROXY__" in line:
            vals = line.split("__PROXY__", 1)[1].split("|")
            return {k: v for k, v in zip(_PROXY_KEYS, vals) if v}
    return {}


def _proxy_from_system() -> dict:
    """macOS「系统设置 › 网络 › 代理」里配置的代理。"""
    import re
    import subprocess
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    kv = dict(re.findall(r"^\s*(\w+)\s*:\s*(\S+)\s*$", out, flags=re.M))
    found = {}
    for scheme, key in (("HTTPS", "HTTPS_PROXY"), ("HTTP", "HTTP_PROXY")):
        if kv.get(f"{scheme}Enable") == "1" and kv.get(f"{scheme}Proxy"):
            found[key] = f"http://{kv[f'{scheme}Proxy']}:{kv.get(f'{scheme}Port', '80')}"
    return found


def child_env() -> dict:
    """给子进程（本机 claude、curl）用的环境变量。

    从图形界面启动的程序拿不到终端里配置的代理，本机 claude 就会连不上、一直卡住。
    所以环境里没有代理时，依次去终端配置和系统设置里找，找到就补上。结果只算一次。
    """
    global _child_env
    if _child_env is None:
        env = dict(os.environ)
        if not any(env.get(k) or env.get(k.lower()) for k in _PROXY_KEYS[:3]):
            found = _proxy_from_shell() or _proxy_from_system()
            env.update(found)
            env.update({k.lower(): v for k, v in found.items()})
        _child_env = env
    return dict(_child_env)


def proxy_url() -> str | None:
    env = child_env()
    return env.get("HTTPS_PROXY") or env.get("https_proxy") or env.get("ALL_PROXY") or env.get("all_proxy") or None

