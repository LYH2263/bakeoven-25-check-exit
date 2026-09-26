"""判定步骤：只根据采集结果检查三件事，不接触数据库或文件。

三项检查：
1. 批次端点与甘特色块一致：每批的发酵、烘烤色块区间必须与
   [start, ferment_end)、[ferment_end, bake_end) 完全吻合，炉位一致，
   且没有多出来的色块。
2. 端点相接的两批不被报成重叠：一个半开区间的 end 恰好等于另一区间
   start 的相邻批次，绝不能出现在采集到的重叠清单里。
3. 半开真重叠一定能被找到：按半开区间几何独立推导出的真重叠批次对
   （start < other.end and other.start < end），必须与重叠判断的产出
   完全一致——真的能找到，且没有误报。

判定不引入任何新的排炉规则，只核对上面三件事。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.checks.collector import BatchEndpoint, Collection, GanttSwatch, OverlapPair
from app.services.oven_engine import Interval

PHASE_LABEL = {"ferment": "发酵段", "bake": "烘烤段"}


@dataclass
class Verdict:
    source: str
    endpoint_codes: list[str] = field(default_factory=list)
    inconsistencies: list[str] = field(default_factory=list)
    touching_pairs: list[tuple[str, str]] = field(default_factory=list)
    geometric_overlaps: list[tuple[str, str, int, int]] = field(default_factory=list)
    reported_overlaps: list[OverlapPair] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.inconsistencies and not self.geometric_overlaps


def _endpoint_intervals(ep: BatchEndpoint) -> dict[str, Interval]:
    return {
        "ferment": Interval(ep.start_min, ep.ferment_end),
        "bake": Interval(ep.ferment_end, ep.bake_end),
    }


def judge(coll: Collection) -> Verdict:
    v = Verdict(source=coll.source)
    v.reported_overlaps = list(coll.overlaps)
    v.endpoint_codes = [e.code for e in coll.endpoints]

    endpoints = {e.batch_id: e for e in coll.endpoints}

    # 把所有占炉段按 (炉位, 批次) 归集：端点推导一份、色块一份
    ep_segments: dict[tuple[int, int], dict[str, Interval]] = {
        (e.oven_id, e.batch_id): _endpoint_intervals(e) for e in coll.endpoints
    }
    sw_segments: dict[tuple[int, int], dict[str, Interval]] = defaultdict(dict)
    for s in coll.swatches:
        sw_segments[(s.oven_id, s.batch_id)][s.phase] = Interval(s.start_min, s.end_min)

    # 检查一：批次端点与甘特色块一致
    for key, phases in ep_segments.items():
        oven_id, batch_id = key
        ep = endpoints[batch_id]
        sw_phases = sw_segments.get(key)
        if sw_phases is None:
            v.inconsistencies.append(f"批次 {ep.code} 在炉位 {oven_id} 没有任何甘特色块")
            continue
        for phase, want in phases.items():
            got = sw_phases.get(phase)
            if got is None:
                v.inconsistencies.append(
                    f"批次 {ep.code} 缺少{PHASE_LABEL[phase]}色块"
                )
                continue
            if got != want:
                v.inconsistencies.append(
                    f"批次 {ep.code} {PHASE_LABEL[phase]}色块区间 "
                    f"[{got.start},{got.end}) 与端点 [{want.start},{want.end}) 不一致"
                )
        for phase in sw_phases:
            if phase not in phases:
                v.inconsistencies.append(
                    f"批次 {ep.code} 出现端点之外的色块阶段：{phase}"
                )
    for (oven_id, batch_id), sw_phases in sw_segments.items():
        if batch_id not in endpoints:
            code = next(
                (s.code for s in coll.swatches if s.batch_id == batch_id), f"#{batch_id}"
            )
            v.inconsistencies.append(
                f"甘特色块属于没有端点的批次 {code}（炉位 {oven_id}）"
            )
            continue
        if (oven_id, batch_id) not in ep_segments:
            ep = endpoints[batch_id]
            v.inconsistencies.append(
                f"批次 {ep.code} 的色块画在炉位 {oven_id}，端点却属于炉位 {ep.oven_id}"
            )

    # 端点推导出的全部占炉段（同一炉位），供检查二、三独立使用
    all_segments: list[tuple[int, int, str, Interval]] = []
    for e in coll.endpoints:
        for phase, iv in _endpoint_intervals(e).items():
            all_segments.append((e.oven_id, e.batch_id, phase, iv))

    def names(a: int, b: int) -> tuple[str, str]:
        ca = next((e.code for e in coll.endpoints if e.batch_id == a), f"#{a}")
        cb = next((e.code for e in coll.endpoints if e.batch_id == b), f"#{b}")
        return tuple(sorted((ca, cb)))

    reported_pairs = {frozenset((p.batch_id_a, p.batch_id_b)) for p in coll.overlaps}
    # 按“批次对”归类：任意占炉段真重叠 → 整对为真重叠；
    # 否则存在端对端相接 → 整对为相接对（段相接但别处真重叠的不算相接）。
    pair_overlap: dict[frozenset[int], tuple[int, int]] = {}
    pair_touch: set[frozenset[int]] = set()

    for i in range(len(all_segments)):
        oven_a, ba, phase_a, ia = all_segments[i]
        for j in range(i + 1, len(all_segments)):
            oven_b, bb, phase_b, ib = all_segments[j]
            if oven_a != oven_b or ba == bb:
                continue
            key = frozenset((ba, bb))
            if ia.overlaps(ib):
                lo = max(ia.start, ib.start)
                hi = min(ia.end, ib.end)
                old = pair_overlap.get(key)
                pair_overlap[key] = (
                    (min(old[0], lo), max(old[1], hi)) if old else (lo, hi)
                )
                pair_touch.discard(key)
            elif key not in pair_overlap and (
                ia.end == ib.start or ib.end == ia.start
            ):
                pair_touch.add(key)

    # 检查二：端点相接的两批不被报成重叠
    for key in sorted(pair_touch, key=lambda k: sorted(k)):
        a, b = sorted(key)
        ca, cb = names(a, b)
        v.touching_pairs.append((ca, cb))
        if key in reported_pairs:
            v.inconsistencies.append(
                f"端点相接的批次 {ca}、{cb} 被误报为重叠"
            )

    # 检查三：半开真重叠一定能被找到（独立几何推导 vs 重叠判断产出）
    for key, (lo, hi) in sorted(pair_overlap.items(), key=lambda kv: sorted(kv[0])):
        a, b = sorted(key)
        ca, cb = names(a, b)
        v.geometric_overlaps.append((ca, cb, lo, hi))
        if key not in reported_pairs:
            v.inconsistencies.append(
                f"批次 {ca}、{cb} 在 [{lo},{hi}) 真重叠，但重叠判断没有找到"
            )
    for p in coll.overlaps:
        if frozenset((p.batch_id_a, p.batch_id_b)) not in pair_overlap:
            v.inconsistencies.append(
                f"重叠判断误报批次 {p.code_a}、{p.code_b} 重叠"
                f"（{PHASE_LABEL.get(p.phase_a, p.phase_a)}/"
                f"{PHASE_LABEL.get(p.phase_b, p.phase_b)}）"
            )

    v.geometric_overlaps.sort()
    return v
