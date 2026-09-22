"""领域模型：轨迹点、航段、覆盖分类。

原始轨迹点一经入库不可修改（append-only）；承包人的人工归类只作用于航段的
分类标签，永不回写原始点。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class Quality(str, Enum):
    """定位质量：厘米级固定解 / 浮点解 / 无效解。无效解不参与覆盖生成。"""

    FIXED = "fixed"
    FLOAT = "float"
    INVALID = "invalid"


class ImplementState(str, Enum):
    """提升器状态：入土作业 / 提升（转场、掉头）。只有入土状态才可能形成覆盖。"""

    WORKING = "working"
    RAISED = "raised"


class CoverClass(str, Enum):
    """覆盖事件的四种核算分类。"""

    FIRST = "first"  # 首次覆盖
    NECESSARY = "necessary_overlap"  # 必要重叠
    REPEAT = "repeat"  # 重复碾压
    OUT = "out_of_bounds"  # 越界作业


# 网格展示分类的优先序：一个网格可能被多个航段扫过，取最具结算意义的类别。
DISPLAY_PRECEDENCE = (CoverClass.FIRST, CoverClass.NECESSARY, CoverClass.REPEAT, CoverClass.OUT)


def _parse_at(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        # 终端数据规范要求带时区；缺失时按 UTC 处理，保证可比较、可排序。
        at = at.replace(tzinfo=timezone.utc)
    return at


@dataclass(frozen=True)
class TrackPoint:
    """一条原始定位记录。主键为 (boot_id, sequence)：终端重启后序号归零，
    单靠 sequence 会撞号，必须联合启动周期标识。"""

    boot_id: str
    sequence: int
    at: datetime
    lon: float
    lat: float
    quality: Quality
    implement_state: ImplementState

    @property
    def key(self) -> str:
        return f"{self.boot_id}#{self.sequence}"

    @classmethod
    def from_dict(cls, d: dict) -> "TrackPoint":
        try:
            boot_id = str(d["boot_id"])
            sequence = int(d["sequence"])
            at = _parse_at(str(d["at"]))
            lon, lat = d["position"]
            quality = Quality(str(d["quality"]))
            state = ImplementState(str(d["implement_state"]))
        except KeyError as exc:
            raise ValueError(f"轨迹点缺少字段: {exc}") from exc
        except ValueError as exc:
            key = f"{d.get('boot_id')}#{d.get('sequence')}"
            raise ValueError(f"轨迹点字段非法 ({key}): {exc}") from exc
        if not (-180.0 <= float(lon) <= 180.0 and -90.0 <= float(lat) <= 90.0):
            raise ValueError(f"经纬度越界: {d['position']}")
        if sequence < 0:
            raise ValueError(f"序号不能为负: {sequence}")
        return cls(boot_id, sequence, at, float(lon), float(lat), quality, state)

    def to_dict(self) -> dict:
        return {
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "at": self.at.isoformat(),
            "position": [self.lon, self.lat],
            "quality": self.quality.value,
            "implement_state": self.implement_state.value,
        }

    @property
    def usable_for_coverage(self) -> bool:
        """只有定位有效且机具入土的点才参与覆盖生成。"""
        return (
            self.quality is not Quality.INVALID
            and self.implement_state is ImplementState.WORKING
        )


@dataclass(frozen=True)
class Segment:
    """两个相邻有效作业点连成的一段作业航段。

    id 由两端点主键派生，内容确定则 id 确定——与上传批次、上传顺序无关，
    这是人工归类可以稳定引用航段、且重算结果幂等的基础。
    """

    id: str
    start_key: str
    end_key: str
    t0: datetime
    t1: datetime
    ax: float
    ay: float
    bx: float
    by: float

    @staticmethod
    def make_id(start_key: str, end_key: str) -> str:
        digest = hashlib.sha1(f"{start_key}|{end_key}".encode("utf-8")).hexdigest()
        return f"SEG-{digest[:12]}"

    @property
    def length_m(self) -> float:
        return ((self.bx - self.ax) ** 2 + (self.by - self.ay) ** 2) ** 0.5

    @property
    def duration_s(self) -> float:
        return (self.t1 - self.t0).total_seconds()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start_key": self.start_key,
            "end_key": self.end_key,
            "t0": self.t0.isoformat(),
            "t1": self.t1.isoformat(),
            "a": [self.ax, self.ay],
            "b": [self.bx, self.by],
            "length_m": round(self.length_m, 3),
        }
