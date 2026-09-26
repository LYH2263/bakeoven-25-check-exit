"""采集步骤：只从三处取数，不做任何判定。

三处数据源：
1. 批次端点（``/api/batches`` 的产出：start/ferment_end/bake_end）
2. 甘特色块（``/api/gantt`` 的产出：发酵、烘烤两个半开区间色块）
3. 重叠判断（``oven_engine.find_conflicts`` 对占炉区间的半开判断）

当前库直接复用 API 路由里的取数函数；夹具模式从 JSON 读出前两处，
第三处仍交给同一个 ``find_conflicts`` 计算，保证三处口径一致。

采集期只检查“缺没缺”：端点缺字段、色块少一段都记入 ``missing``；
数值对不对由判定步骤负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.router import _all_occupancies, _batch_out, gantt
from app.models.models import Batch, Oven, Product
from app.services.oven_engine import Interval, Occupancy, find_conflicts

REQUIRED_PHASES = ("ferment", "bake")
ENDPOINT_FIELDS = ("oven_id", "start_min", "ferment_end", "bake_end")
BLOCK_FIELDS = ("oven_id", "phase", "start_min", "end_min")


@dataclass(frozen=True)
class BatchEndpoint:
    batch_id: int
    code: str
    oven_id: int
    start_min: int
    ferment_end: int
    bake_end: int


@dataclass(frozen=True)
class GanttSwatch:
    batch_id: int
    code: str
    oven_id: int
    phase: str
    start_min: int
    end_min: int


@dataclass(frozen=True)
class OverlapPair:
    """重叠判断（第三处取数）报出的一对批次。"""

    batch_id_a: int
    batch_id_b: int
    code_a: str
    code_b: str
    oven_id: int
    overlap_start: int
    overlap_end: int
    phase_a: str
    phase_b: str


@dataclass
class Collection:
    source: str
    endpoints: list[BatchEndpoint] = field(default_factory=list)
    swatches: list[GanttSwatch] = field(default_factory=list)
    overlaps: list[OverlapPair] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _overlaps_from_occupancies(
    occupancies: list[Occupancy], code_of: dict[int, str]
) -> list[OverlapPair]:
    """对同一组占炉区间逐批调用生产的 find_conflicts，成对去重。"""
    pairs: list[OverlapPair] = []
    seen: set[frozenset[int]] = set()
    batch_ids = {o.batch_id for o in occupancies}
    for bid in batch_ids:
        candidates = [o for o in occupancies if o.batch_id == bid]
        existing = [o for o in occupancies if o.batch_id != bid]
        for ex, cand in find_conflicts(existing, candidates):
            key = frozenset({ex.batch_id, cand.batch_id})
            if key in seen:
                continue
            seen.add(key)
            pairs.append(
                OverlapPair(
                    batch_id_a=ex.batch_id,
                    batch_id_b=cand.batch_id,
                    code_a=code_of.get(ex.batch_id, f"#{ex.batch_id}"),
                    code_b=code_of.get(cand.batch_id, f"#{cand.batch_id}"),
                    oven_id=cand.oven_id,
                    overlap_start=max(ex.interval.start, cand.interval.start),
                    overlap_end=min(ex.interval.end, cand.interval.end),
                    phase_a=ex.phase,
                    phase_b=cand.phase,
                )
            )
    pairs.sort(key=lambda p: (p.overlap_start, p.batch_id_a, p.batch_id_b))
    return pairs


def _mark_missing_swatches(coll: Collection) -> None:
    """采集期清点：每个有端点的批次，发酵/烘烤两块色快是否都在。"""
    by_batch: dict[int, set[str]] = {}
    for s in coll.swatches:
        by_batch.setdefault(s.batch_id, set()).add(s.phase)
    code_of = {e.batch_id: e.code for e in coll.endpoints}
    for s in coll.swatches:
        if s.batch_id not in code_of:
            continue  # 色块没有对应端点，留给判定步骤报不一致
    for ep in coll.endpoints:
        have = by_batch.get(ep.batch_id, set())
        for phase in REQUIRED_PHASES:
            if phase not in have:
                label = "发酵段" if phase == "ferment" else "烘烤段"
                coll.missing.append(f"批次 {ep.code} 缺少色块（{label}）")


def collect_current(db: Session) -> Collection:
    """从当前库采集：直接调用批次端点、甘特端点与引擎重叠判断。"""
    coll = Collection(source="当前库")
    batches = db.scalars(select(Batch).order_by(Batch.start_min, Batch.id)).all()

    # 第一处：批次端点（/api/batches 的产出）
    for b in batches:
        product = db.get(Product, b.product_id)
        oven = db.get(Oven, b.oven_id)
        if product is None or oven is None:
            coll.missing.append(f"批次 {b.code} 缺少端点（产品或炉位缺失）")
            continue
        out = _batch_out(db, b)
        coll.endpoints.append(
            BatchEndpoint(
                batch_id=out.id,
                code=out.code,
                oven_id=out.oven_id,
                start_min=out.start_min,
                ferment_end=out.ferment_end,
                bake_end=out.bake_end,
            )
        )

    # 第二处：甘特色块（/api/gantt 的产出）
    for block in gantt(db):
        coll.swatches.append(
            GanttSwatch(
                batch_id=block.batch_id,
                code=block.code,
                oven_id=block.oven_id,
                phase=block.phase,
                start_min=block.start_min,
                end_min=block.end_min,
            )
        )

    _mark_missing_swatches(coll)

    # 第三处：重叠判断（生产引擎对全炉占炉区间的半开判断）
    occupancies = _all_occupancies(db)
    code_of = {e.batch_id: e.code for e in coll.endpoints}
    coll.overlaps = _overlaps_from_occupancies(occupancies, code_of)
    return coll


def collect_fixture(path: str | Path) -> Collection:
    """从夹具 JSON 采集。批次缺端点或色块时记入 missing，由入口区分退出码。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    coll = Collection(source=f"夹具 {Path(path).name}")

    occupancies: list[Occupancy] = []

    # 第一处：批次端点
    for row in data.get("batches", []):
        bid = row["batch_id"]
        code = row.get("code", f"#{bid}")
        missing_fields = [f for f in ENDPOINT_FIELDS if f not in row]
        if missing_fields:
            coll.missing.append(f"批次 {code} 缺少端点（{', '.join(missing_fields)}）")
            continue
        ep = BatchEndpoint(
            batch_id=bid,
            code=code,
            oven_id=row["oven_id"],
            start_min=row["start_min"],
            ferment_end=row["ferment_end"],
            bake_end=row["bake_end"],
        )
        coll.endpoints.append(ep)
        occupancies.append(Occupancy(ep.oven_id, Interval(ep.start_min, ep.ferment_end), "ferment", bid))
        occupancies.append(Occupancy(ep.oven_id, Interval(ep.ferment_end, ep.bake_end), "bake", bid))

    # 第二处：甘特色块
    for row in data.get("blocks", []):
        bid = row["batch_id"]
        missing_fields = [f for f in BLOCK_FIELDS if f not in row]
        if missing_fields:
            coll.missing.append(
                f"批次 {row.get('code', f'#{bid}')} 色块字段不全（{', '.join(missing_fields)}）"
            )
            continue
        coll.swatches.append(
            GanttSwatch(
                batch_id=bid,
                code=row.get("code", f"#{bid}"),
                oven_id=row["oven_id"],
                phase=row["phase"],
                start_min=row["start_min"],
                end_min=row["end_min"],
            )
        )

    _mark_missing_swatches(coll)

    # 第三处：重叠判断（与当前库走同一个生产引擎）
    code_of = {e.batch_id: e.code for e in coll.endpoints}
    coll.overlaps = _overlaps_from_occupancies(occupancies, code_of)
    return coll
