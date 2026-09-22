"""农机作业覆盖核算引擎。

按地块多边形、机具幅宽、定位质量、提升器状态与作业轨迹还原有效覆盖,
区分首次覆盖、必要重叠、重复碾压与越界作业,支撑账单核算、
人工归类、合作社签认冻结与金额下钻。
"""
from .billing import compute_bill, drill_down, match_override
from .engine import BILL_DRAFT, BILL_SIGNED, Engine
from .geo import GridSpec, LocalFrame
from .models import (
    CLASSES,
    CLS_FIRST,
    CLS_NECESSARY,
    CLS_OUT_OF_BOUNDS,
    CLS_REPEATED,
    OVERRIDABLE_CLASSES,
    QUALITIES,
    STATES,
    Barrier,
    DomainError,
    FieldBoundary,
    ImplementSpec,
    Policy,
    TrackPoint,
)
from .segments import Segment, TrackBreak, build_segments
from .store import TrackStore, UploadReceipt

__all__ = [
    "Barrier",
    "BILL_DRAFT",
    "BILL_SIGNED",
    "CLASSES",
    "CLS_FIRST",
    "CLS_NECESSARY",
    "CLS_OUT_OF_BOUNDS",
    "CLS_REPEATED",
    "DomainError",
    "Engine",
    "FieldBoundary",
    "GridSpec",
    "ImplementSpec",
    "LocalFrame",
    "OVERRIDABLE_CLASSES",
    "Policy",
    "QUALITIES",
    "STATES",
    "Segment",
    "TrackBreak",
    "TrackPoint",
    "TrackStore",
    "UploadReceipt",
    "build_segments",
    "compute_bill",
    "drill_down",
    "match_override",
]
