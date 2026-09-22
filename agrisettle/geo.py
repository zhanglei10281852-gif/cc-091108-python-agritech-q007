"""局部平面投影与几何工具。

WGS84 经纬度通过等距圆柱投影（按锚点纬度校正经度比例）换算为局部平面坐标。
单个地块尺度（百米至公里级）内，投影误差远小于覆盖网格分辨率，满足结算核算要求。

锚点一旦确定必须保持不变：网格按投影原点绝对对齐，锚点漂移会导致同一批轨迹
得到不同的网格划分，破坏"分批上传 / 服务重启后结果一致"的幂等性要求。
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_378_137.0  # WGS84 长半轴


class LocalProjection:
    """以固定锚点为原点的局部平面投影（x 向东，y 向北，单位米）。"""

    def __init__(self, anchor_lon: float, anchor_lat: float) -> None:
        self.anchor_lon = float(anchor_lon)
        self.anchor_lat = float(anchor_lat)
        self._cos_lat = math.cos(math.radians(self.anchor_lat))

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        x = math.radians(lon - self.anchor_lon) * self._cos_lat * EARTH_RADIUS_M
        y = math.radians(lat - self.anchor_lat) * EARTH_RADIUS_M
        return x, y

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        lon = self.anchor_lon + math.degrees(x / (self._cos_lat * EARTH_RADIUS_M))
        lat = self.anchor_lat + math.degrees(y / EARTH_RADIUS_M)
        return lon, lat


def dist_point_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """点到线段的平面距离（米）。"""
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """射线法判断点是否在多边形内（ring 为 (x, y) 序列，允许首尾闭合）。"""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def ring_area(ring: list[tuple[float, float]]) -> float:
    """鞋带公式求多边形面积（平方米）。"""
    area = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0
