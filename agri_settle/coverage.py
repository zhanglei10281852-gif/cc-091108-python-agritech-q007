"""覆盖栅格化与覆盖分类。

分类规则(对每条航段、每个栅格单元,按航段开始时间先后处理):
  - 首次覆盖: 单元格第一次被入土作业的幅宽扫到,且在地块内、不在河沟内;
  - 必要重叠: 航段与早先平行行程的横向重叠不超过 necessary_overlap_max_m,
    且该航段同时扫到了新的单元格(是生产性行程而非纯重跑);
  - 重复碾压: 其余一切二次覆盖——航向交叉(田头转弯)、横向重叠超限、
    或整条航段完全没有扫到新单元格;
  - 越界作业: 幅宽扫到地块边界以外的单元格(河沟内的单元格不算越界,直接排除)。

人工归类只允许在“必要重叠 / 重复碾压”之间改判,且必须附理由;
首次覆盖与越界属几何事实,不接受裁定。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geo import GridSpec, point_in_polygon, point_line_distance
from .models import (
    CLS_FIRST,
    CLS_NECESSARY,
    CLS_OUT_OF_BOUNDS,
    CLS_REPEATED,
    OVERRIDABLE_CLASSES,
    Policy,
)
from .segments import Segment, TrackBreak


@dataclass(frozen=True)
class CellCovering:
    seg_id: str
    cls: str


@dataclass
class SegmentCoverage:
    seg_id: str
    first_cells: list[tuple[int, int]] = field(default_factory=list)
    recovered_cells: list[tuple[int, int]] = field(default_factory=list)
    out_cells: list[tuple[int, int]] = field(default_factory=list)
    overlap_class: str | None = None  # 二次覆盖部分的类别


@dataclass
class CoverageResult:
    field_id: str
    boundary_revision: int
    cell_size_m: float
    coverings: dict[tuple[int, int], list[CellCovering]]
    segments: dict[str, SegmentCoverage]
    coverable_cells: set[tuple[int, int]]
    breaks: list[TrackBreak]

    def class_areas(self) -> dict[str, float]:
        """各类覆盖面积(平方米)。首次按唯一单元格计,其余按覆盖事件计。"""
        area = self.cell_size_m * self.cell_size_m
        counts = {
            c: 0 for c in (CLS_FIRST, CLS_NECESSARY, CLS_REPEATED, CLS_OUT_OF_BOUNDS)
        }
        for cell_coverings in self.coverings.values():
            for cov in cell_coverings:
                counts[cov.cls] += 1
        return {c: n * area for c, n in counts.items()}

    def missed_cells(self) -> set[tuple[int, int]]:
        covered = set(self.coverings)
        return {c for c in self.coverable_cells if c not in covered}


def _heading_diff(h1: float, h2: float) -> float:
    d = abs(h1 - h2) % 180.0
    return min(d, 180.0 - d)


def compute_coverage(
    field_id: str,
    boundary_revision: int,
    boundary_ring_xy: list[tuple[float, float]],
    barriers_xy: list[tuple[str, list[tuple[float, float]]]],
    segments: list[Segment],
    width_m: float,
    policy: Policy,
    breaks: list[TrackBreak] | None = None,
    overrides: list[dict] | None = None,
) -> CoverageResult:
    grid = GridSpec(policy.cell_size_m)
    half_w = width_m / 2.0

    # 1. 可覆盖单元格:地块内且不在任何河沟内
    coverable: set[tuple[int, int]] = set()
    for cell in grid.polygon_cells(boundary_ring_xy):
        cx, cy = grid.cell_center(*cell)
        if any(point_in_polygon(cx, cy, ring) for _, ring in barriers_xy):
            continue
        coverable.add(cell)

    def in_barrier(cell: tuple[int, int]) -> bool:
        cx, cy = grid.cell_center(*cell)
        return any(point_in_polygon(cx, cy, ring) for _, ring in barriers_xy)

    # 2. 逐航段栅格化并按时间先后分类
    ordered = sorted(segments, key=lambda s: (s.start.isoformat(), s.seg_id))
    coverings: dict[tuple[int, int], list[CellCovering]] = {}
    seg_coverage: dict[str, SegmentCoverage] = {}
    order_index = {s.seg_id: k for k, s in enumerate(ordered)}
    by_id = {s.seg_id: s for s in ordered}

    for seg in ordered:
        sc = SegmentCoverage(seg_id=seg.seg_id)
        for cell in grid.capsule_cells(
            (seg.ax, seg.ay), (seg.bx, seg.by), half_w
        ):
            if cell in coverable:
                if cell in coverings:
                    sc.recovered_cells.append(cell)
                else:
                    sc.first_cells.append(cell)
            elif not in_barrier(cell):
                sc.out_cells.append(cell)
            # 河沟内单元格:既不是覆盖也不是越界,直接排除

        # 3. 该航段二次覆盖部分的类别
        if sc.recovered_cells:
            if not sc.first_cells:
                sc.overlap_class = CLS_REPEATED  # 纯重跑,没有扫到任何新单元格
            else:
                # 找共享二次覆盖单元格最多的早先航段,按它与本航段的几何关系裁定
                share: dict[str, int] = {}
                for cell in sc.recovered_cells:
                    for cov in coverings[cell]:
                        share[cov.seg_id] = share.get(cov.seg_id, 0) + 1
                best_id = max(
                    share, key=lambda sid: (share[sid], -order_index[sid])
                )
                prior = by_id[best_id]
                if _heading_diff(seg.heading_deg, prior.heading_deg) > (
                    policy.parallel_heading_deg
                ):
                    sc.overlap_class = CLS_REPEATED  # 航向交叉(田头转弯等)
                else:
                    offset = point_line_distance(
                        seg.midpoint, (prior.ax, prior.ay), (prior.bx, prior.by)
                    )
                    overlap_depth = width_m - offset
                    if overlap_depth > policy.necessary_overlap_max_m:
                        sc.overlap_class = CLS_REPEATED  # 横向重叠超限
                    else:
                        sc.overlap_class = CLS_NECESSARY

        # 4. 人工归类:只允许在必要重叠/重复碾压之间改判
        if overrides and sc.overlap_class in OVERRIDABLE_CLASSES:
            for ov in overrides:
                if seg.seg_id in ov.get("_matched_seg_ids", ()):
                    sc.overlap_class = ov["target_class"]

        # 5. 落账
        for cell in sc.first_cells:
            coverings[cell] = [CellCovering(seg.seg_id, CLS_FIRST)]
        for cell in sc.recovered_cells:
            coverings[cell].append(CellCovering(seg.seg_id, sc.overlap_class))
        for cell in sc.out_cells:
            coverings.setdefault(cell, []).append(
                CellCovering(seg.seg_id, CLS_OUT_OF_BOUNDS)
            )
        seg_coverage[seg.seg_id] = sc

    return CoverageResult(
        field_id=field_id,
        boundary_revision=boundary_revision,
        cell_size_m=policy.cell_size_m,
        coverings=coverings,
        segments=seg_coverage,
        coverable_cells=coverable,
        breaks=list(breaks or ()),
    )
