"""核算域模型:轨迹点、机具、边界版本、河沟屏障与核算策略。"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

# 定位质量
QUALITY_FIXED = "fixed"  # 厘米级固定解
QUALITY_FLOAT = "float"  # 浮点解
QUALITY_INVALID = "invalid"  # 无效解
QUALITIES = (QUALITY_FIXED, QUALITY_FLOAT, QUALITY_INVALID)

# 提升器/机具状态
STATE_WORKING = "working"  # 入土作业
STATE_RAISED = "raised"  # 提升(转场、掉头不作业)
STATES = (STATE_WORKING, STATE_RAISED)

# 覆盖分类
CLS_FIRST = "first"  # 首次覆盖
CLS_NECESSARY = "necessary_overlap"  # 必要重叠(邻接行程正常接茬)
CLS_REPEATED = "repeated_compaction"  # 重复碾压
CLS_OUT_OF_BOUNDS = "out_of_bounds"  # 越界作业
CLASSES = (CLS_FIRST, CLS_NECESSARY, CLS_REPEATED, CLS_OUT_OF_BOUNDS)

# 人工归类只允许在“必要重叠 / 重复碾压”之间裁定,几何事实(首次、越界)不可裁定
OVERRIDABLE_CLASSES = (CLS_NECESSARY, CLS_REPEATED)


class DomainError(Exception):
    """违反核算域规则(如对已签认账单做变更)。"""


@dataclass(frozen=True)
class TrackPoint:
    """一条原始定位记录。原始点只增不改,任何归类裁定都不触碰它。"""

    device_id: str
    boot_id: str  # 终端一次启动周期;重启后 sequence 归零,靠 boot_id 区分
    sequence: int
    at: datetime
    lon: float
    lat: float
    quality: str
    implement_state: str

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.device_id, self.boot_id, self.sequence)

    @staticmethod
    def from_dict(device_id: str, raw: dict) -> "TrackPoint":
        try:
            at = raw["at"]
            if isinstance(at, str):
                at = datetime.fromisoformat(at)
            if at.tzinfo is None:
                raise ValueError("时间戳缺少时区")
            point = TrackPoint(
                device_id=device_id,
                boot_id=str(raw["boot_id"]),
                sequence=int(raw["sequence"]),
                at=at,
                lon=float(raw["position"][0]),
                lat=float(raw["position"][1]),
                quality=str(raw["quality"]),
                implement_state=str(raw["implement_state"]),
            )
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError(f"字段缺失或结构错误: {exc}") from exc
        if point.quality not in QUALITIES:
            raise ValueError(f"未知定位质量: {point.quality}")
        if point.implement_state not in STATES:
            raise ValueError(f"未知机具状态: {point.implement_state}")
        if not (-180.0 <= point.lon <= 180.0 and -90.0 <= point.lat <= 90.0):
            raise ValueError(f"坐标越界: {point.lon}, {point.lat}")
        return point

    def as_dict(self) -> dict:
        return {
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "at": self.at.isoformat(),
            "position": [self.lon, self.lat],
            "quality": self.quality,
            "implement_state": self.implement_state,
        }

    def canonical(self) -> str:
        """用于去重比对与指纹的稳定字符串表示。"""
        return (
            f"{self.device_id}|{self.boot_id}|{self.sequence}|{self.at.isoformat()}"
            f"|{self.lon:.7f}|{self.lat:.7f}|{self.quality}|{self.implement_state}"
        )


@dataclass(frozen=True)
class ImplementSpec:
    implement_id: str
    working_width_m: float

    def __post_init__(self) -> None:
        if self.working_width_m <= 0:
            raise DomainError("机具幅宽必须为正")


@dataclass(frozen=True)
class Barrier:
    """河沟等不可跨越屏障。作业插值不得凭插值跨过它。"""

    barrier_id: str
    ring: tuple[tuple[float, float], ...]  # WGS84 闭合环

    @staticmethod
    def of(barrier_id: str, ring: list[list[float]]) -> "Barrier":
        pts = tuple((float(p[0]), float(p[1])) for p in ring)
        if pts[0] != pts[-1]:
            pts = pts + (pts[0],)
        return Barrier(barrier_id=barrier_id, ring=pts)


@dataclass(frozen=True)
class FieldBoundary:
    """地块边界的一个修订版本。revision 单调递增。"""

    field_id: str
    revision: int
    ring: tuple[tuple[float, float], ...]  # WGS84 闭合环
    valid_from: str | None = None

    @staticmethod
    def of(
        field_id: str,
        revision: int,
        ring: list[list[float]],
        valid_from: str | None = None,
    ) -> "FieldBoundary":
        pts = tuple((float(p[0]), float(p[1])) for p in ring)
        if len(pts) < 4:
            raise DomainError("边界多边形至少需要 3 个顶点")
        if pts[0] != pts[-1]:
            pts = pts + (pts[0],)
        return FieldBoundary(
            field_id=field_id, revision=revision, ring=pts, valid_from=valid_from
        )


@dataclass(frozen=True)
class Policy:
    """核算策略。所有阈值显式给出,保证核算可解释、可复算。"""

    cell_size_m: float = 1.0  # 覆盖栅格边长
    accepted_qualities: tuple[str, ...] = (QUALITY_FIXED, QUALITY_FLOAT)
    max_gap_s: float = 30.0  # 相邻点时间差超过则断轨,不插值
    max_segment_m: float = 60.0  # 相邻点距离超过则视为漂移跳点,断轨
    parallel_heading_deg: float = 45.0  # 航向差在此范围内视为平行行程
    necessary_overlap_max_m: float = 0.5  # 平行行程横向重叠不超过此值计为必要重叠
    merge_bend_deg: float = 10.0  # 连续航段航向偏差在此范围内可合并为一个行程
    merge_drift_m: float = 0.5  # 合并时新点偏离行程直线的最大横向距离
    payable_classes: tuple[str, ...] = (CLS_FIRST, CLS_NECESSARY)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(raw: dict | None) -> "Policy":
        if raw is None:
            return Policy()
        return Policy(
            **{k: (tuple(v) if isinstance(v, list) else v) for k, v in raw.items()}
        )
