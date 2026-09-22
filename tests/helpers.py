"""测试共享的场景构造工具:在局部米制坐标里设计轨迹,再换算成 WGS84。"""
from __future__ import annotations

from datetime import datetime, timedelta

from agri_settle import Engine, LocalFrame

FRAME = LocalFrame(118.50, 32.10)
START = "2026-09-11T08:00:00+08:00"


def ll(x: float, y: float) -> list[float]:
    """局部米制坐标 → [lon, lat]。"""
    lon, lat = FRAME.to_lonlat(x, y)
    return [round(lon, 8), round(lat, 8)]


def rect(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [ll(x0, y0), ll(x1, y0), ll(x1, y1), ll(x0, y1), ll(x0, y0)]


def make_points(boot: str, items: list[tuple], start: str = START, seq0: int = 1):
    """items: (x, y) 或 (x, y, quality) 或 (x, y, quality, state) 或
    (x, y, quality, state, dt_from_prev_s)。"""
    t = datetime.fromisoformat(start)
    out = []
    for k, item in enumerate(items):
        x, y = item[0], item[1]
        quality = item[2] if len(item) > 2 else "fixed"
        state = item[3] if len(item) > 3 else "working"
        dt = item[4] if len(item) > 4 else (0.0 if k == 0 else 1.0)
        t = t + timedelta(seconds=dt)
        out.append(
            {
                "boot_id": boot,
                "sequence": seq0 + k,
                "at": t.isoformat(),
                "position": ll(x, y),
                "quality": quality,
                "implement_state": state,
            }
        )
    return out


def pass_along_x(boot: str, y: float, x0: float = 2.0, x1: float = 38.0,
                 start: str = START, seq0: int = 1, quality: str = "fixed",
                 state: str = "working"):
    """一条沿 x 方向的作业行程,1 秒一点、每秒 1 米。"""
    n = int(round(x1 - x0)) + 1
    items = [(x0 + i, y, quality, state) for i in range(n)]
    return make_points(boot, items, start=start, seq0=seq0)


def make_engine(field=None, width: float = 2.4, barriers: dict | None = None,
                log_path=None, revision: int = 1) -> Engine:
    engine = Engine(log_path=log_path)
    engine.register_field("F1", field if field is not None else rect(0, 0, 40, 20),
                          revision=revision)
    engine.register_implement("R1", width)
    for barrier_id, ring in (barriers or {}).items():
        engine.register_barrier("F1", barrier_id, ring)
    return engine


def line_map(report: dict) -> dict:
    return {line["cls"]: line for line in report["lines"]}
