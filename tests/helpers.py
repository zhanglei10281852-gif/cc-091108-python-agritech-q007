"""测试辅助：在"米"坐标系里构造场景，再换算成 WGS84 经纬度。

锚点固定为 (118.5, 32.1)，与引擎登记地块时取边界首顶点为锚点的行为一致，
因此测试中的米制坐标可以精确往返，便于对面积做严格断言。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agrisettle import EngineConfig, LocalProjection, SettlementEngine  # noqa: E402

ANCHOR = (118.5, 32.1)
PROJ = LocalProjection(*ANCHOR)
TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 11, 8, 0, 0, tzinfo=TZ)

TEST_CONFIG = EngineConfig(
    cell_size_m=1.0,
    max_gap_s=60.0,
    max_bridge_m=25.0,
    joint_suppression_s=10.0,
    necessary_overlap_window_s=1800.0,
)


def ll(x_m: float, y_m: float) -> list[float]:
    """米坐标 -> [lon, lat]"""
    return list(PROJ.to_lonlat(x_m, y_m))


def boundary_m(*corners: tuple[float, float]) -> list[list[float]]:
    """米坐标多边形 -> 闭合的经纬度边界。"""
    ring = [ll(x, y) for x, y in corners]
    ring.append(list(ring[0]))
    return ring


def square_field_m(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return boundary_m((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def pt(boot: str, seq: int, t: datetime, x: float, y: float,
       quality: str = "fixed", state: str = "working") -> dict:
    return {
        "boot_id": boot,
        "sequence": seq,
        "at": t.isoformat(),
        "position": ll(x, y),
        "quality": quality,
        "implement_state": state,
    }


def pass_points(boot: str, seq0: int, t0: datetime, x: float, y0: float, y1: float,
                step_m: float = 1.0, dt_s: float = 2.0,
                quality: str = "fixed", state: str = "working") -> list[dict]:
    """沿 x 常量、从 y0 到 y1 的一趟作业点列。"""
    points = []
    n = int(round((y1 - y0) / step_m))
    for k in range(n + 1):
        points.append(pt(boot, seq0 + k, t0 + timedelta(seconds=dt_s * k),
                         x, y0 + step_m * k, quality, state))
    return points


def new_engine(store_path, config: EngineConfig = TEST_CONFIG) -> SettlementEngine:
    return SettlementEngine(store_path, config)


def make_field(engine: SettlementEngine, fid: str = "F1",
               boundary=None, width: float = 2.4) -> None:
    engine.register_field(fid, boundary or square_field_m(0, 0, 10, 10))
    engine.set_implement(fid, "ROTARY-1", width)
