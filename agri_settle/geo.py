"""纯 Python 平面几何:WGS84 局部投影、多边形判定、线段关系与覆盖栅格。

所有核算都在以地块参考点为原点的局部平面坐标(米)中进行。
参考点在地块首次登记时确定并随地块档案保存,边界修订不会改变它,
因此栅格单元在不同边界版本、不同重算之间保持稳定。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

EARTH_RADIUS_M = 6_378_137.0

_EPS = 1e-9


@dataclass(frozen=True)
class LocalFrame:
    """以固定参考点为中心的等距圆柱投影(米)。小范围作业内形变可忽略。"""

    lon0: float
    lat0: float
    _cos0: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_cos0", math.cos(math.radians(self.lat0)))

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        x = math.radians(lon - self.lon0) * EARTH_RADIUS_M * self._cos0
        y = math.radians(lat - self.lat0) * EARTH_RADIUS_M
        return (x, y)

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        lon = self.lon0 + math.degrees(x / (EARTH_RADIUS_M * self._cos0))
        lat = self.lat0 + math.degrees(y / EARTH_RADIUS_M)
        return (lon, lat)


def polygon_centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """顶点平均中心(确定性,用于选取投影参考点)。ring 首尾闭合。"""
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


def _on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> bool:
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    scale = max(abs(bx - ax), abs(by - ay), 1.0)
    if abs(cross) > _EPS * scale * scale:
        return False
    return (
        min(ax, bx) - _EPS <= px <= max(ax, bx) + _EPS
        and min(ay, by) - _EPS <= py <= max(ay, by) + _EPS
    )


def point_in_polygon(
    px: float, py: float, ring: list[tuple[float, float]]
) -> bool:
    """射线法;落在边界上视为内部。ring 首尾闭合。"""
    for i in range(len(ring) - 1):
        ax, ay = ring[i]
        bx, by = ring[i + 1]
        if _on_segment(px, py, ax, ay, bx, by):
            return True
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > py) != (y2 > py):
            xin = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if xin > px:
                inside = not inside
    return inside


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    """两线段是否相交(含端点接触与共线重叠)。"""
    o1 = _orient(a[0], a[1], b[0], b[1], c[0], c[1])
    o2 = _orient(a[0], a[1], b[0], b[1], d[0], d[1])
    o3 = _orient(c[0], c[1], d[0], d[1], a[0], a[1])
    o4 = _orient(c[0], c[1], d[0], d[1], b[0], b[1])
    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)):
        return True
    for px, py, (s1, s2) in (
        (c[0], c[1], (a, b)),
        (d[0], d[1], (a, b)),
        (a[0], a[1], (c, d)),
        (b[0], b[1], (c, d)),
    ):
        if _on_segment(px, py, s1[0], s1[1], s2[0], s2[1]):
            return True
    return False


def segment_crosses_polygon(
    a: tuple[float, float],
    b: tuple[float, float],
    ring: list[tuple[float, float]],
) -> bool:
    """线段是否与多边形内部有任何接触(端点在内或与任一边相交)。"""
    if point_in_polygon(a[0], a[1], ring) or point_in_polygon(b[0], b[1], ring):
        return True
    for i in range(len(ring) - 1):
        if segments_intersect(a, b, ring[i], ring[i + 1]):
            return True
    return False


def point_segment_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """点到线段的距离(米)。"""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_line_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """点到无限直线的距离(米),用于相邻行程的横向偏移。"""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    norm = math.hypot(dx, dy)
    if norm == 0.0:
        return math.hypot(p[0] - ax, p[1] - ay)
    return abs(dx * (p[1] - ay) - dy * (p[0] - ax)) / norm


@dataclass(frozen=True)
class GridSpec:
    """覆盖栅格。锚定在投影原点,单元格只取决于 cell_size,与边界版本无关。"""

    cell_size_m: float = 1.0

    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        return (math.floor(x / self.cell_size_m), math.floor(y / self.cell_size_m))

    def cell_center(self, i: int, j: int) -> tuple[float, float]:
        return ((i + 0.5) * self.cell_size_m, (j + 0.5) * self.cell_size_m)

    @property
    def cell_area_m2(self) -> float:
        return self.cell_size_m * self.cell_size_m

    def capsule_cells(
        self,
        a: tuple[float, float],
        b: tuple[float, float],
        half_width: float,
    ):
        """枚举距线段 ab 不超过 half_width 的所有单元格(以中心点判定)。"""
        cs = self.cell_size_m
        i0 = math.floor((min(a[0], b[0]) - half_width) / cs)
        i1 = math.floor((max(a[0], b[0]) + half_width) / cs)
        j0 = math.floor((min(a[1], b[1]) - half_width) / cs)
        j1 = math.floor((max(a[1], b[1]) + half_width) / cs)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                cx, cy = (i + 0.5) * cs, (j + 0.5) * cs
                if point_segment_distance((cx, cy), a, b) <= half_width:
                    yield (i, j)

    def polygon_cells(self, ring: list[tuple[float, float]]):
        """枚举中心点落在多边形内的所有单元格。"""
        cs = self.cell_size_m
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        i0, i1 = math.floor(min(xs) / cs), math.floor(max(xs) / cs)
        j0, j1 = math.floor(min(ys) / cs), math.floor(max(ys) / cs)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                cx, cy = (i + 0.5) * cs, (j + 0.5) * cs
                if point_in_polygon(cx, cy, ring):
                    yield (i, j)
