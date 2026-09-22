"""覆盖网格核算：把作业航段按机具幅宽栅格化到绝对对齐的网格，并逐事件分类。

核算口径：
- 每段航段是以航迹线为轴、幅宽一半为半径的胶囊体，扫过的网格记一次覆盖事件；
- 网格按投影原点绝对对齐，与数据范围、上传批次无关，保证幂等；
- 同一网格在 joint_suppression_s 内被相邻航段重复扫过，视为同一趟作业的接缝，
  不重复计次；
- 田内网格：首次覆盖 → 必要重叠（时间窗内第二次）→ 重复碾压（更晚或更多次）；
- 田外网格一律记越界作业，不参与田内计次；
- 人工归类以航段为粒度覆盖自动分类，只改标签，不改原始点。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .config import EngineConfig
from .geo import dist_point_segment, point_in_ring, ring_area
from .models import DISPLAY_PRECEDENCE, CoverClass, Segment


@dataclass
class CoverageResult:
    cell_size_m: float
    swath_width_m: float
    cells: dict[tuple[int, int], list[tuple[str, str]]]  # 网格 -> [(航段id, 分类)]
    segments: dict[str, Segment]
    segment_cells: dict[str, list[tuple[int, int]]]
    areas_m2: dict[str, float]
    field_area_m2: float
    field_cell_count: int
    covered_field_cell_count: int
    applied_rulings: list[dict] = field(default_factory=list)

    @property
    def cell_area_m2(self) -> float:
        return self.cell_size_m * self.cell_size_m

    @staticmethod
    def display_class(events: list[tuple[str, str]]) -> str:
        classes = {klass for _, klass in events}
        for klass in DISPLAY_PRECEDENCE:
            if klass.value in classes:
                return klass.value
        return CoverClass.OUT.value

    def class_counts(self) -> dict[str, int]:
        counts = {klass.value: 0 for klass in CoverClass}
        for events in self.cells.values():
            for _, klass in events:
                counts[klass] += 1
        return counts

    def to_snapshot(self) -> dict:
        cells = {
            f"{i},{j}": {"events": [[seg, klass] for seg, klass in events],
                         "class": self.display_class(events)}
            for (i, j), events in sorted(self.cells.items())
        }
        segments = {}
        for seg_id, seg in sorted(self.segments.items()):
            class_counts = {klass.value: 0 for klass in CoverClass}
            for cell in self.segment_cells.get(seg_id, []):
                for ev_seg, ev_klass in self.cells.get(cell, []):
                    if ev_seg == seg_id:
                        class_counts[ev_klass] += 1
            segments[seg_id] = {
                **seg.to_dict(),
                "cells": [[i, j] for i, j in sorted(self.segment_cells.get(seg_id, []))],
                "class_counts": class_counts,
            }
        return {
            "cell_size_m": self.cell_size_m,
            "swath_width_m": self.swath_width_m,
            "cells": cells,
            "segments": segments,
            "areas_m2": dict(self.areas_m2),
            "field_area_m2": self.field_area_m2,
            "field_cell_count": self.field_cell_count,
            "covered_field_cell_count": self.covered_field_cell_count,
            "applied_rulings": list(self.applied_rulings),
        }


def compute_coverage(
    segments: list[Segment],
    swath_width_m: float,
    boundary_xy: list[tuple[float, float]],
    config: EngineConfig,
    rulings: dict[str, str] | None = None,
    applied_rulings: list[dict] | None = None,
) -> CoverageResult:
    """对一组航段做覆盖核算。rulings: {航段id: 强制分类}。"""
    rulings = rulings or {}
    radius = swath_width_m / 2.0
    cs = config.cell_size_m
    cells: dict[tuple[int, int], list[tuple[str, str]]] = {}
    segment_cells: dict[str, list[tuple[int, int]]] = {}
    cover_count: dict[tuple[int, int], int] = {}
    last_event_at: dict[tuple[int, int], datetime] = {}
    areas = {klass.value: 0.0 for klass in CoverClass}
    cell_area = cs * cs

    for seg in sorted(segments, key=lambda s: (s.t0, s.id)):
        i0 = math.floor((min(seg.ax, seg.bx) - radius) / cs)
        i1 = math.floor((max(seg.ax, seg.bx) + radius) / cs)
        j0 = math.floor((min(seg.ay, seg.by) - radius) / cs)
        j1 = math.floor((max(seg.ay, seg.by) + radius) / cs)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                cx, cy = (i + 0.5) * cs, (j + 0.5) * cs
                # 1e-6 m 容差吸收投影往返的浮点噪声，判定本身保持确定性
                if dist_point_segment(cx, cy, seg.ax, seg.ay, seg.bx, seg.by) > radius + 1e-6:
                    continue
                cell = (i, j)
                segment_cells.setdefault(seg.id, []).append(cell)
                last_at = last_event_at.get(cell)
                if last_at is not None and (
                    (seg.t0 - last_at).total_seconds() <= config.joint_suppression_s
                ):
                    continue  # 同趟作业的航段接缝，不重复计次
                last_event_at[cell] = seg.t0
                if not point_in_ring(cx, cy, boundary_xy):
                    klass = CoverClass.OUT
                else:
                    n = cover_count.get(cell, 0)
                    prev_at = last_at
                    if n == 0:
                        klass = CoverClass.FIRST
                    elif (
                        n == 1
                        and prev_at is not None
                        and (seg.t0 - prev_at).total_seconds() <= config.necessary_overlap_window_s
                    ):
                        klass = CoverClass.NECESSARY
                    else:
                        klass = CoverClass.REPEAT
                    cover_count[cell] = n + 1
                if seg.id in rulings:
                    klass = CoverClass(rulings[seg.id])
                cells.setdefault(cell, []).append((seg.id, klass.value))
                areas[klass.value] += cell_area

    # 田内网格统计（含未被覆盖的漏耕网格），用于出具体积与覆盖率。
    field_cell_count = 0
    if boundary_xy:
        xs = [p[0] for p in boundary_xy]
        ys = [p[1] for p in boundary_xy]
        for i in range(math.floor(min(xs) / cs), math.floor(max(xs) / cs) + 1):
            for j in range(math.floor(min(ys) / cs), math.floor(max(ys) / cs) + 1):
                if point_in_ring((i + 0.5) * cs, (j + 0.5) * cs, boundary_xy):
                    field_cell_count += 1

    covered_field = sum(1 for cell, n in cover_count.items() if n > 0)
    return CoverageResult(
        cell_size_m=cs,
        swath_width_m=swath_width_m,
        cells=cells,
        segments={seg.id: seg for seg in segments},
        segment_cells=segment_cells,
        areas_m2=areas,
        field_area_m2=ring_area(boundary_xy) if boundary_xy else 0.0,
        field_cell_count=field_cell_count,
        covered_field_cell_count=covered_field,
        applied_rulings=list(applied_rulings or []),
    )
