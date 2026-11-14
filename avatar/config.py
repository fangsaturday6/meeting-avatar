"""可调设置。存在 workspace/settings.json，控制台和接口都能改。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
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
# room：大家围着这台电脑；call：你在这台电脑上开线上会，它在旁边听（麦克风是你，这台电脑放出的声音是线上的人）；
# online：它用自己的账号单独入会，靠虚拟声卡听和说
SCENES = {"room": "当面开会", "call": "我在开线上会", "online": "它单独入会"}
ROLES = {"companion": "陪我开会", "delegate": "替我开会"}
# 参与度：只在被叫到时说 / 有实质补充才说 / 每个议题都主动给看法
ENGAGEMENTS = {"quiet": "少说", "normal": "适中", "active": "多说"}
# 每场会的原始录音留多久。留着才能会后重算说话人、拿真实的会比较识别参数
KEEP_AUDIO = {"two_hours": "留 2 小时", "always": "一直留着", "until_polished": "整理完就删", "never": "不留"}
KEEP_HOURS = 2.0             # 「留 2 小时」从散会算起


@dataclass
class Settings:
    name: str = "小克"
    owner: str = ""              # 它是谁的分身（你的名字）。会上别人提到这个名字，它知道是在说你
    role: str = "companion"      # companion = 你也在场；delegate = 你不在，它替你去
    engagement: str = "normal"   # 参与度，见 ENGAGEMENTS
    open_book: bool = False      # 替你开会时，能不能谈这场会之外的事（你的其他项目、别的会、Claude 的记忆）
    voice: str = "local"
    speed: float = 1.1           # 语速，1.0 为正常
    scene: str = "room"
    project_dir: str = ""        # 这场会主要讨论的项目
    projects_root: str = ""      # 你所有项目所在的目录；留空 = 项目目录的上一级
    topic: str = ""              # 会议背景，开会前告诉数字人
    fast_model: str = "claude-haiku-5-5"
    slow_model: str = ""         # 干活用的模型；留空 = 本机 Claude 的默认模型（最稳）
    use_my_skills: bool = False  # 做设计时是否加载你本机 Claude 的技能和插件（更强但更慢）
    silence_nudge: float = 6.0   # 会场安静多少秒后提示数字人一次
    gap: float = 0.4             # 别人说完后至少等多久再开口
    hold: float = 4.0            # 被问到后，旁边一直有别的声音时最多等多久就顶着说
    barge_delay: float = 0.9     # 别人持续说多久算打断。太短的话一声「嗯」就会把它打断
    barge_grace: float = 0.5     # 刚开口的这段时间内不算打断（防回声误触发）
    keep_audio: str = "two_hours"  # 录音留多久，见 KEEP_AUDIO。文字和声纹总是留着
    announce: bool = True        # 开会时先说一句「这场会我会记录」

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def guarded(self) -> bool:
        """你不在场、又没放开：它只知道、只能查这场会的项目。会上的人没法通过它打听你别的事。"""
        return self.role == "delegate" and not self.open_book

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
            if f.name == "projects_root" and new and not Path(new).expanduser().is_dir():
                raise ValueError("这个目录不存在")
            if f.name == "role" and new not in ROLES:
                raise ValueError("没有这种角色")
            if f.name == "keep_audio" and new not in KEEP_AUDIO:
                raise ValueError("录音只能：" + "、".join(KEEP_AUDIO.values()))
            if f.name == "engagement" and new not in ENGAGEMENTS:
                raise ValueError("参与度只能是：" + "、".join(ENGAGEMENTS))
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
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return s  # 文件坏了就用默认值
            for key, value in (data.items() if isinstance(data, dict) else ()):
                try:
                    s.update({key: value})
                except ValueError:
                    pass  # 只跳过不合法的这一项（比如登记的目录已经不在了），别连累其他设置
        return s


# ---------- 子进程的网络环境 ----------
log = logging.getLogger("avatar.config")
_PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
RECHECK = 15.0                      # 记下的代理不通、也没找到通的：隔这么多秒再找，别每起一个子进程都翻一遍终端配置
_child_env: dict | None = None
_looked_at = 0.0                    # 上一次「找了一圈没有一个通的」是什么时候；找到通的就清零
_env_lock = threading.Lock()


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


def _proxy_in(env: dict) -> str | None:
    """env 里子进程实际会用的那个代理。"""
    return env.get("HTTPS_PROXY") or env.get("https_proxy") or env.get("ALL_PROXY") or env.get("all_proxy") or None


def _answers(env: dict) -> bool:
    """env 里的代理是不是还有人在听。没配代理算通。

    只试本机上的（连一下，不通立刻就知道）；别的机器上的一时不通多半是网络抖，不是换了地方，不试。
    """
    import socket
    from urllib.parse import urlsplit
    url = _proxy_in(env) or env.get("HTTP_PROXY") or env.get("http_proxy")
    if not url:
        return True
    try:
        parts = urlsplit(url if "://" in url else f"http://{url}")
        host, port = parts.hostname, parts.port
    except ValueError:
        return True
    if host not in ("127.0.0.1", "localhost", "::1") or not port:
        return True
    try:
        socket.create_connection((host, port), timeout=0.5).close()
    except OSError:
        return False
    return True


def _with_proxy(base: dict, found: dict) -> dict:
    """把 base 里的代理整个换成 found 的（大小写两种写法都换）。found 没说哪些地址不走代理时，原来的留着。"""
    swapped = {k.upper() for k in _PROXY_KEYS[:3]} | {k.upper() for k in found}
    env = {k: v for k, v in base.items() if k.upper() not in swapped}
    env.update(found)
    env.update({k.lower(): v for k, v in found.items()})
    return env


def _lookup() -> dict:
    """现在该用的环境：自己带着的代理通就用它；不通或没有，依次去终端配置、系统设置里找一个通的。

    一个通的都没有时不乱换：自己带着的留着；自己没带的，用先找到的那个。
    """
    env = dict(os.environ)
    own = any(env.get(k) or env.get(k.lower()) for k in _PROXY_KEYS[:3])
    if own and _answers(env):
        return env
    spare = None
    for look in (_proxy_from_shell, _proxy_from_system):
        found = look()
        if not found:
            continue
        candidate = _with_proxy(env, found)
        if _answers(candidate):
            return candidate
        spare = spare or candidate
    return env if own else (spare or env)


def child_env() -> dict:
    """给子进程（本机 claude、curl）用的环境变量。

    从图形界面启动的程序拿不到终端里配置的代理，本机 claude 就会连不上、一直卡住；
    开着不关的后台记着的代理也会过期（换了代理软件、换了端口）。所以每次要用时先看记着的那个还通不通，
    不通就重新找（见 _lookup）。找一次要读终端配置，可能一两秒：事件循环里用 asyncio.to_thread 调。
    """
    global _child_env, _looked_at
    with _env_lock:
        stale = _child_env is not None and not _answers(_child_env)
        if _child_env is None or (stale and time.monotonic() - _looked_at >= RECHECK):
            before = _proxy_in(_child_env) if _child_env else None
            _child_env = _lookup()
            _looked_at = 0.0 if _answers(_child_env) else time.monotonic()
            if stale and _proxy_in(_child_env) != before:
                log.warning("代理换了地方，之后起的子进程改用新的")
        return dict(_child_env)


def proxy_url(env: dict | None = None) -> str | None:
    """现在该用的代理；给了 env 就只读它（看某个子进程当初带的是哪个）。"""
    return _proxy_in(child_env() if env is None else env)
