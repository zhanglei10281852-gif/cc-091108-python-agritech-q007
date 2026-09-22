"""由轨迹点构建作业航段。

断链规则（保守、可解释）：
- 无效定位点、提升器未入土的点不参与成段，并直接切断链条——
  短暂定位漂移不能凭插值跨过河沟，抬起机具的转场也不能算作业；
- 相邻有效点的时间间隔超过 max_gap_s 或平面距离超过 max_bridge_m 时断链，
  断点续传留下的长缺口不被"脑补"为作业覆盖；
- 终端重启（boot_id 变化、序号归零）本身不断链，连续性由时间/距离阈值判定。
"""

from __future__ import annotations

from .config import EngineConfig
from .geo import LocalProjection
from .models import Segment, TrackPoint


def build_segments(
    points: list[TrackPoint], projection: LocalProjection, config: EngineConfig
) -> list[Segment]:
    ordered = sorted(points, key=lambda p: (p.at, p.boot_id, p.sequence))
    segments: list[Segment] = []
    prev: TrackPoint | None = None
    prev_xy: tuple[float, float] | None = None

    for point in ordered:
        if not point.usable_for_coverage:
            prev = None
            prev_xy = None
            continue
        xy = projection.to_xy(point.lon, point.lat)
        if prev is not None and prev_xy is not None:
            dt = (point.at - prev.at).total_seconds()
            dist = ((xy[0] - prev_xy[0]) ** 2 + (xy[1] - prev_xy[1]) ** 2) ** 0.5
            boot_ok = not config.break_on_boot_change or point.boot_id == prev.boot_id
            if boot_ok and 0.0 <= dt <= config.max_gap_s and 0.0 < dist <= config.max_bridge_m:
                segments.append(
                    Segment(
                        id=Segment.make_id(prev.key, point.key),
                        start_key=prev.key,
                        end_key=point.key,
                        t0=prev.at,
                        t1=point.at,
                        ax=prev_xy[0],
                        ay=prev_xy[1],
                        bx=xy[0],
                        by=xy[1],
                    )
                )
        prev = point
        prev_xy = xy

    segments.sort(key=lambda s: (s.t0, s.id))
    return segments
