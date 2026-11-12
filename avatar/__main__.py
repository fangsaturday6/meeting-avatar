"""启动：uv run python -m avatar"""
import argparse
import logging

import uvicorn

from .api import create_app
from .config import WORKSPACE


def main() -> None:
    ap = argparse.ArgumentParser(description="会议数字人")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    # 决策日志：它每次听到什么、想了多久、为什么说或不说。排查「它怎么不说话」时看这个文件
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(WORKSPACE / "meeting.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger("avatar").addHandler(fh)
    print(f"\n  主窗口  http://{args.host}:{args.port}/\n  设置    http://{args.host}:{args.port}/panel\n")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
