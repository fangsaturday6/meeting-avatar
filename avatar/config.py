"""可调设置。存在 workspace/settings.json，控制台和接口都能改。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace"
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
    barge_delay: float = 0.35    # 别人持续说多久算打断

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
