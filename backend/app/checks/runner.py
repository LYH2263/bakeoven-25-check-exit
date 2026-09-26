"""入口步骤：只负责选择当前库或夹具，然后依次调用采集、判定。

用法：
    python -m app.checks                 # 核对当前库
    python -m app.checks --fixture FILE  # 核对夹具 JSON

退出码：
    0  核对通过
    1  发现半开真重叠（输出点名两个批次号）
    2  采集缺项（缺少端点或色块，输出写明缺的是哪一项）
    3  端点与色块不一致 / 相接被误报 / 真重叠没找到

入口不做存活或依赖探测，也不导出任何文件；只打印结论文本。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from sqlalchemy.orm import Session

from app.checks.collector import Collection, collect_current, collect_fixture
from app.checks.judge import judge

EXIT_OK = 0
EXIT_OVERLAP = 1
EXIT_MISSING = 2
EXIT_MISMATCH = 3


def _render(coll: Collection, verdict, out: TextIO) -> None:
    print(f"排炉核对（{coll.source}）", file=out)
    print("批次：" + "、".join(verdict.endpoint_codes), file=out)

    if coll.missing:
        print("采集缺项：", file=out)
        for m in coll.missing:
            print(f"  - {m}", file=out)
        return

    for ca, cb in verdict.touching_pairs:
        print(f"端点相接：{ca} 与 {cb} 半开相接，不计重叠", file=out)

    if verdict.inconsistencies:
        print("核对不通过：", file=out)
        for m in verdict.inconsistencies:
            print(f"  - {m}", file=out)
        return

    if verdict.geometric_overlaps:
        for ca, cb, lo, hi in verdict.geometric_overlaps:
            print(f"发现重叠：{ca} 与 {cb} 在 [{lo},{hi}) 半开真重叠", file=out)
        return

    print("核对通过：端点与色块一致，相接无误报，真重叠无遗漏。", file=out)


def run_check(
    argv: Sequence[str] | None = None,
    db: Session | None = None,
    out: TextIO | None = None,
) -> int:
    """采集 → 判定 → 按结果分流退出码。返回退出码，便于重复调用。"""
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="app.checks", description="排炉核对")
    parser.add_argument("--fixture", help="夹具 JSON 路径；缺省核对当前库")
    args = parser.parse_args(argv)

    if args.fixture:
        try:
            coll = collect_fixture(args.fixture)
        except (OSError, ValueError, KeyError) as exc:
            print(f"夹具读取失败：{exc}", file=out)
            return EXIT_MISSING
    else:
        own_session = db is None
        if own_session:
            from app.database import SessionLocal

            db = SessionLocal()
        try:
            coll = collect_current(db)
        finally:
            if own_session:
                db.close()

    verdict = judge(coll)
    _render(coll, verdict, out)

    # 采集缺项优先：数据不全时后续结论不可信，退出码必须与发现重叠不同
    if coll.missing:
        return EXIT_MISSING
    if verdict.inconsistencies:
        return EXIT_MISMATCH
    if verdict.geometric_overlaps:
        return EXIT_OVERLAP
    return EXIT_OK


def main() -> None:
    raise SystemExit(run_check())
