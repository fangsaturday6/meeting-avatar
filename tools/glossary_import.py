"""把一份名单或一个公司本体仓库并进会议数字人的常用词表。后台没开着也能用；开着的话设置窗口里「导入文件」是同一件事。

    uv run python tools/glossary_import.py 通讯录.csv                       # 表格：第一列是词，第二列是说明
    uv run python tools/glossary_import.py 客户名单.txt                     # 一行一个词，后面可以跟「：说明」
    uv run python tools/glossary_import.py ~/公司本体   # 本体仓库：里面所有 YAML
    uv run python tools/glossary_import.py <文件或目录> <词表文件>          # 默认 workspace/glossary.txt

词表里已经有的行（包括你手写的）原样保留，只在末尾追加新词，所以可以反复跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from avatar import glossary  # noqa: E402


def terms_of(source: Path) -> dict[str, str]:
    """词 → 说明。source 是一个文件，或者一个本体仓库的目录。"""
    files = [source] if source.is_file() else sorted(p for p in source.rglob("*.y*ml") if ".git" not in p.parts)
    found: dict[str, str] = {}
    for f in files:
        for term, note in glossary.from_file(f.name, f.read_text(encoding="utf-8")).items():
            if not found.get(term):
                found[term] = note
    return found


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    source = Path(sys.argv[1]).expanduser()
    target = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else glossary.GLOSSARY_FILE
    if not source.exists():
        sys.exit(f"没有这个文件或目录：{source}")
    try:
        terms = terms_of(source)
    except (ValueError, UnicodeDecodeError) as e:
        sys.exit(f"没读懂：{e}")
    added = glossary.add(terms, heading=f"从 {source.name} 导入", path=target)
    print(f"里面有 {len(terms)} 个词，新加了 {len(added)} 个，词表在 {target}")
