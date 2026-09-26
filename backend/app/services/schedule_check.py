"""可重复执行的排炉核对：采集 → 判定 → 入口。

- 采集：从批次端点、甘特色块、重叠判断三处取数（当前库或夹具）。
- 判定：只根据采集结果检查三件事——
  1. 批次端点与甘特色块一致；
  2. 端点相接的两批不被报成重叠；
  3. 半开真重叠一定能被找到。
- 入口：只负责选择当前库或夹具并调用前两步。

退出码：0 通过；1 发现重叠；2 采集缺项；3 判定不一致。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from app.services.oven_engine import Interval, Occupancy, RecipeDurations, build_occupancies

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

EXIT_OK = 0
EXIT_OVERLAP = 1
EXIT_INCOMPLETE = 2
EXIT_MISMATCH = 3

_PHASES = ("ferment", "bake")


# ---------------------------------------------------------------------------
# 采集结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchEndpoints:
    """批次端点：一批在炉上的起止。"""

    code: str
    oven_id: int
    start: int
    end: int  # 半开，不含


@dataclass(frozen=True)
class GanttColorBlock:
    """甘特色块：一批某阶段在甘特图上的色块。"""

    code: str
    oven_id: int
    phase: str  # ferment | bake
    start: int
    end: int  # 半开，不含


@dataclass
class Collected:
    """三处取数的汇总；missing 非空表示采集缺项。"""

    endpoints: list[BatchEndpoints] = field(default_factory=list)
    blocks: list[GanttColorBlock] = field(default_factory=list)
    occupancies: list[Occupancy] = field(default_factory=list)
    occupancy_codes: dict[int, str] = field(default_factory=dict)  # 占用区间 batch_id → 批次号
    missing: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 采集：当前库
# ---------------------------------------------------------------------------
# 数据库相关导入集中在各函数内部：夹具路径不连接、也不依赖数据库驱动。


def _batch_codes(db: Session) -> list[tuple[int, str]]:
    from sqlalchemy import select

    from app.models.models import Batch

    rows = db.execute(select(Batch.id, Batch.code).order_by(Batch.id)).all()
    return [(row.id, row.code) for row in rows]


def _collect_endpoints_db(db: Session, codes: list[tuple[int, str]], out: Collected) -> None:
    """批次端点：与 GET /api/batches 同一来源（批次行 + 产品配方）。"""
    from app.models.models import Batch, Product

    for batch_id, code in codes:
        batch = db.get(Batch, batch_id)
        product = db.get(Product, batch.product_id) if batch else None
        if not batch or not product:
            out.missing.append(f"批次端点:{code}")
            continue
        out.endpoints.append(
            BatchEndpoints(
                code=code,
                oven_id=batch.oven_id,
                start=batch.start_min,
                end=batch.start_min + product.ferment_min + product.bake_min,
            )
        )


def _collect_blocks_db(db: Session, codes: list[tuple[int, str]], out: Collected) -> None:
    """甘特色块：与 GET /api/gantt 同一来源（逐批逐阶段色块）。"""
    from app.models.models import Batch, Product

    for batch_id, code in codes:
        batch = db.get(Batch, batch_id)
        product = db.get(Product, batch.product_id) if batch else None
        if not batch or not product:
            out.missing.append(f"甘特色块:{code}")
            continue
        recipe = RecipeDurations(product.ferment_min, product.bake_min)
        for occ in build_occupancies(batch.oven_id, batch.id, batch.start_min, recipe):
            out.blocks.append(
                GanttColorBlock(
                    code=code,
                    oven_id=occ.oven_id,
                    phase=occ.phase,
                    start=occ.interval.start,
                    end=occ.interval.end,
                )
            )


def _collect_occupancies_db(db: Session, codes: list[tuple[int, str]], out: Collected) -> None:
    """重叠判断取数：与冲突检测同一来源（每批的半开占用区间）。"""
    from app.models.models import Batch, Product

    for batch_id, code in codes:
        batch = db.get(Batch, batch_id)
        product = db.get(Product, batch.product_id) if batch else None
        if not batch or not product:
            out.missing.append(f"重叠判断:{code}")
            continue
        recipe = RecipeDurations(product.ferment_min, product.bake_min)
        out.occupancy_codes[batch.id] = code
        out.occupancies.extend(build_occupancies(batch.oven_id, batch.id, batch.start_min, recipe))


def collect_from_db(db: Session) -> Collected:
    """从当前库采集三处数据；缺哪项记哪项，不中断。"""
    out = Collected()
    codes = _batch_codes(db)
    _collect_endpoints_db(db, codes, out)
    _collect_blocks_db(db, codes, out)
    _collect_occupancies_db(db, codes, out)
    return out


# ---------------------------------------------------------------------------
# 采集：夹具
# ---------------------------------------------------------------------------
#
# 夹具是一个 dict，三个键对应三处取数，可分别缺失以模拟采集缺项：
# {
#   "endpoints":   [{"code", "oven_id", "start", "end"}],
#   "gantt":       [{"code", "oven_id", "phase", "start", "end"}],
#   "occupancies": [{"code", "oven_id", "phase", "start", "end"}],
# }


def _collect_endpoints_fixture(fx: dict[str, Any], out: Collected) -> None:
    rows = fx.get("endpoints")
    if rows is None:
        out.missing.append("批次端点:<夹具缺 endpoints 段>")
        return
    for row in rows:
        try:
            out.endpoints.append(
                BatchEndpoints(
                    code=str(row["code"]),
                    oven_id=int(row["oven_id"]),
                    start=int(row["start"]),
                    end=int(row["end"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            out.missing.append(f"批次端点:{row.get('code', '?') if isinstance(row, dict) else '?'}")


def _collect_blocks_fixture(fx: dict[str, Any], out: Collected) -> None:
    rows = fx.get("gantt")
    if rows is None:
        out.missing.append("甘特色块:<夹具缺 gantt 段>")
        return
    for row in rows:
        try:
            out.blocks.append(
                GanttColorBlock(
                    code=str(row["code"]),
                    oven_id=int(row["oven_id"]),
                    phase=str(row["phase"]),
                    start=int(row["start"]),
                    end=int(row["end"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            out.missing.append(f"甘特色块:{row.get('code', '?') if isinstance(row, dict) else '?'}")


def _collect_occupancies_fixture(fx: dict[str, Any], out: Collected) -> None:
    rows = fx.get("occupancies")
    if rows is None:
        out.missing.append("重叠判断:<夹具缺 occupancies 段>")
        return
    code_to_id = {code: batch_id for batch_id, code in out.occupancy_codes.items()}
    for row in rows:
        try:
            code = str(row["code"])
            if code not in code_to_id:
                new_id = max(out.occupancy_codes, default=0) + 1
                out.occupancy_codes[new_id] = code
                code_to_id[code] = new_id
            out.occupancies.append(
                Occupancy(
                    oven_id=int(row["oven_id"]),
                    interval=Interval(int(row["start"]), int(row["end"])),
                    phase=str(row["phase"]),
                    batch_id=code_to_id[code],
                )
            )
        except (KeyError, TypeError, ValueError):
            out.missing.append(f"重叠判断:{row.get('code', '?') if isinstance(row, dict) else '?'}")


def collect_from_fixture(fx: dict[str, Any]) -> Collected:
    """从夹具采集三处数据；缺哪项记哪项，不中断。"""
    out = Collected()
    _collect_endpoints_fixture(fx, out)
    _collect_blocks_fixture(fx, out)
    _collect_occupancies_fixture(fx, out)
    return out


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def _overlap_pairs(col: Collected) -> set[frozenset[str]]:
    """重叠判断：同炉半开占用区间相交的批次对（与引擎同一判定）。"""
    pairs: set[frozenset[str]] = set()
    occs = col.occupancies
    for i in range(len(occs)):
        for j in range(i + 1, len(occs)):
            a, b = occs[i], occs[j]
            if a.oven_id != b.oven_id or a.batch_id == b.batch_id:
                continue
            if a.interval.overlaps(b.interval):
                pairs.add(
                    frozenset(
                        (
                            col.occupancy_codes.get(a.batch_id, str(a.batch_id)),
                            col.occupancy_codes.get(b.batch_id, str(b.batch_id)),
                        )
                    )
                )
    return pairs


def judge(col: Collected) -> tuple[list[str], list[str], list[str]]:
    """只根据采集结果检查三件事。

    返回 (mismatches, overlaps, codes)：
    - mismatches：端点与色块不一致、端点相接被报重叠等判定不一致；
    - overlaps：半开真重叠的批次号对（"A 与 B" 形式）；
    - codes：本次覆盖到的批次号（按采集顺序去重）。
    """
    mismatches: list[str] = []
    overlaps: list[str] = []

    codes: list[str] = []
    for ep in col.endpoints:
        if ep.code not in codes:
            codes.append(ep.code)

    # 检查一：批次端点与甘特色块一致。
    blocks_by_code: dict[str, list[GanttColorBlock]] = {}
    for blk in col.blocks:
        blocks_by_code.setdefault(blk.code, []).append(blk)
    for ep in col.endpoints:
        batch_blocks = [b for b in blocks_by_code.get(ep.code, []) if b.oven_id == ep.oven_id]
        if sorted(b.phase for b in batch_blocks) != sorted(_PHASES):
            mismatches.append(f"{ep.code}: 甘特色块与批次端点阶段不齐")
            continue
        spans = {b.phase: (b.start, b.end) for b in batch_blocks}
        f_start, f_end = spans["ferment"]
        b_start, b_end = spans["bake"]
        if f_start != ep.start or b_end != ep.end or f_end != b_start:
            mismatches.append(
                f"{ep.code}: 端点 [{ep.start},{ep.end}) 与色块 "
                f"酵[{f_start},{f_end}) 烤[{b_start},{b_end}) 不一致"
            )

    # 检查二、三：以端点为准，对照重叠判断的结果。
    reported = _overlap_pairs(col)
    eps = col.endpoints
    for i in range(len(eps)):
        for j in range(i + 1, len(eps)):
            x, y = eps[i], eps[j]
            if x.oven_id != y.oven_id:
                continue
            pair = frozenset((x.code, y.code))
            touching = x.end == y.start or y.end == x.start
            true_overlap = x.start < y.end and y.start < x.end
            if touching and pair in reported:
                mismatches.append(f"{x.code} 与 {y.code}: 端点相接被报成重叠")
            if true_overlap:
                if pair not in reported:
                    mismatches.append(f"{x.code} 与 {y.code}: 半开真重叠未被找到")
                else:
                    overlaps.append(f"{x.code} 与 {y.code}")

    return mismatches, overlaps, codes


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_check(source: str, fixture_path: str | None = None) -> int:
    """选择当前库或夹具，调用采集与判定，打印结果并返回退出码。"""
    if source == "fixture":
        if not fixture_path:
            print("采集缺项: 未指定夹具文件")
            return EXIT_INCOMPLETE
        try:
            fx = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"采集缺项: 夹具不可读 {fixture_path} ({exc})")
            return EXIT_INCOMPLETE
        col = collect_from_fixture(fx)
    else:
        from app.database import SessionLocal  # 延迟导入，夹具路径不连库

        db = SessionLocal()
        try:
            col = collect_from_db(db)
        finally:
            db.close()

    if col.missing:
        for item in col.missing:
            print(f"采集缺项: {item}")
        return EXIT_INCOMPLETE

    mismatches, overlaps, codes = judge(col)

    if mismatches:
        for msg in mismatches:
            print(f"判定不一致: {msg}")
        return EXIT_MISMATCH

    if overlaps:
        print("发现重叠:")
        for pair in overlaps:
            print(f"  {pair}")
        return EXIT_OVERLAP

    print(f"排炉核对通过，共 {len(codes)} 批: {', '.join(codes)}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="排炉核对：采集 → 判定 → 退出码")
    parser.add_argument(
        "--source",
        choices=["db", "fixture"],
        default="db",
        help="取数来源：db=当前库（默认），fixture=夹具文件",
    )
    parser.add_argument("--fixture", help="夹具 JSON 文件路径（--source fixture 时必填）")
    args = parser.parse_args(argv)
    return run_check(args.source, args.fixture)


if __name__ == "__main__":
    sys.exit(main())
