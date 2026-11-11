"""启动：uv run python -m avatar"""
import argparse
import logging

import uvicorn

from .api import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="会议数字人")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    print(f"\n  控制台  http://{args.host}:{args.port}/\n  形象页  http://{args.host}:{args.port}/stage\n")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
