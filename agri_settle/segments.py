"""由规范时序轨迹点构建作业航段,并把连续共线的航段合并为行程。

只有相邻两个点都满足“定位质量可接受 + 机具入土”才尝试连线;
连线还要通过三道闸:
  1. 时间差不超过 max_gap_s(断点续传造成的空洞不插值);
  2. 距离不超过 max_segment_m(漂移跳点不插值);
  3. 连线不接触任何河沟屏障(短暂漂移不能凭插值跨过河沟)。
每一次被拒绝的连线都记录为 TrackBreak,供结算主管审计。

合并:逐点采样下相邻航段的幅宽胶囊沿航向大面积重叠,若按单航段
栅格化会把同一行程内部误计为重复碾压。因此把链式相连、航向与
横向漂移都在阈值内的连续航段合并为一个行程,行程是覆盖分类的
最小单位;田头转弯等弯曲处会自然断开为短行程。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .geo import point_line_distance, segment_crosses_polygon
from .models import (
    QUALITY_FLOAT,
    STATE_WORKING,
    Policy,
    TrackPoint,
)

BREAK_GAP = "gap"  # 时间断档
BREAK_JUMP = "jump"  # 距离跳变(疑似漂移)
BREAK_BARRIER = "barrier"  # 连线接触河沟
BREAK_QUALITY = "quality"  # 定位质量不可接受
BREAK_STATE = "state"  # 机具未入土


@dataclass(frozen=True)
class Segment:
    """一条作业行程(可能由多个相邻航段合并而成)。"""

    seg_id: str
    device_id: str
    a_key: tuple[str, str, int]
    b_key: tuple[str, str, int]
    point_keys: tuple[tuple[str, str, int], ...]  # 行程上的全部原始点
    start: datetime
    end: datetime
    ax: float
    ay: float
    bx: float
    by: float
    length_m: float  # 沿轨迹的行程长度
    heading_deg: float  # 无向航向 [0, 180)
    low_confidence: bool  # 任一点为浮点解

    @property
    def midpoint(self) -> tuple[float, float]:
        return ((self.ax + self.bx) / 2.0, (self.ay + self.by) / 2.0)


@dataclass(frozen=True)
class TrackBreak:
    reason: str
    a_key: tuple[str, str, int] | None
    b_key: tuple[str, str, int] | None
    barrier_id: str | None = None
    detail: str = ""


def _heading_of(ax: float, ay: float, bx: float, by: float) -> float:
    return math.degrees(math.atan2(by - ay, bx - ax)) % 180.0


def _heading_diff(h1: float, h2: float) -> float:
    d = abs(h1 - h2) % 180.0
    return min(d, 180.0 - d)


def _seg_id(device_id: str, a_key: tuple, b_key: tuple) -> str:
    return f"{device_id}:{a_key[1]}:{a_key[2]}:{b_key[1]}:{b_key[2]}"


def _make_segment(
    p: TrackPoint, px: float, py: float, q: TrackPoint, qx: float, qy: float
) -> Segment:
    return Segment(
        seg_id=_seg_id(p.device_id, p.key, q.key),
        device_id=p.device_id,
        a_key=p.key,
        b_key=q.key,
        point_keys=(p.key, q.key),
        start=p.at,
        end=q.at,
        ax=px,
        ay=py,
        bx=qx,
        by=qy,
        length_m=math.hypot(qx - px, qy - py),
        heading_deg=_heading_of(px, py, qx, qy),
        low_confidence=QUALITY_FLOAT in (p.quality, q.quality),
    )


def _try_merge(run: Segment, nxt: Segment, policy: Policy) -> Segment | None:
    """若 nxt 能顺接 run(链式相连、近乎共线),返回合并后的行程,否则 None。"""
    if run.b_key != nxt.a_key:
        return None
    if _heading_diff(run.heading_deg, nxt.heading_deg) > policy.merge_bend_deg:
        return None
    drift = point_line_distance(
        (nxt.bx, nxt.by), (run.ax, run.ay), (run.bx, run.by)
    )
    if drift > policy.merge_drift_m:
        return None
    return Segment(
        seg_id=_seg_id(run.device_id, run.a_key, nxt.b_key),
        device_id=run.device_id,
        a_key=run.a_key,
        b_key=nxt.b_key,
        point_keys=run.point_keys + (nxt.b_key,),
        start=run.start,
        end=nxt.end,
        ax=run.ax,
        ay=run.ay,
        bx=nxt.bx,
        by=nxt.by,
        length_m=run.length_m + nxt.length_m,
        heading_deg=_heading_of(run.ax, run.ay, nxt.bx, nxt.by),
        low_confidence=run.low_confidence or nxt.low_confidence,
    )


def build_segments(
    points_xy: list[tuple[TrackPoint, float, float]],
    policy: Policy,
    barriers_xy: list[tuple[str, list[tuple[float, float]]]],
) -> tuple[list[Segment], list[TrackBreak]]:
    """points_xy 必须已按规范时序排序。返回 (行程列表, 断轨事件)。

    多设备点流交错到达时,每台设备各自维护前一点与未闭合行程,
    设备之间绝不连线。
    """
    runs: list[Segment] = []
    breaks: list[TrackBreak] = []
    prev_by_device: dict[str, tuple[TrackPoint, float, float]] = {}
    open_run: dict[str, int] = {}  # device_id -> runs 下标

    for q, qx, qy in points_xy:
        prev = prev_by_device.get(q.device_id)
        prev_by_device[q.device_id] = (q, qx, qy)
        if prev is None:
            continue
        p, px, py = prev
        both_working = (
            p.implement_state == STATE_WORKING and q.implement_state == STATE_WORKING
        )
        if not both_working:
            if p.implement_state != q.implement_state:
                breaks.append(
                    TrackBreak(BREAK_STATE, p.key, q.key, detail="机具状态切换")
                )
            continue
        quality_ok = (
            p.quality in policy.accepted_qualities
            and q.quality in policy.accepted_qualities
        )
        if not quality_ok:
            breaks.append(
                TrackBreak(
                    BREAK_QUALITY,
                    p.key,
                    q.key,
                    detail=f"{p.quality}->{q.quality}",
                )
            )
            continue
        dt = (q.at - p.at).total_seconds()
        if dt > policy.max_gap_s:
            breaks.append(
                TrackBreak(BREAK_GAP, p.key, q.key, detail=f"间隔 {dt:.1f}s")
            )
            continue
        dist = math.hypot(qx - px, qy - py)
        if dist > policy.max_segment_m:
            breaks.append(
                TrackBreak(BREAK_JUMP, p.key, q.key, detail=f"跳变 {dist:.1f}m")
            )
            continue
        crossing = next(
            (
                bid
                for bid, ring in barriers_xy
                if segment_crosses_polygon((px, py), (qx, qy), ring)
            ),
            None,
        )
        if crossing is not None:
            breaks.append(
                TrackBreak(
                    BREAK_BARRIER, p.key, q.key, barrier_id=crossing, detail="连线接触河沟"
                )
            )
            continue
        if dist == 0.0:
            continue  # 原地不动不产生航段
        seg = _make_segment(p, px, py, q, qx, qy)
        idx = open_run.get(q.device_id)
        if idx is not None:
            merged = _try_merge(runs[idx], seg, policy)
            if merged is not None:
                runs[idx] = merged
                continue
        open_run[q.device_id] = len(runs)
        runs.append(seg)
    return runs, breaks
