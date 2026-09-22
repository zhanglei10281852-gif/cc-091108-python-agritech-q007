"""农机作业覆盖结算核算引擎。

由北斗轨迹还原有效作业覆盖，区分首次覆盖、必要重叠、重复碾压与越界作业，
支撑"按有效面积结算"的账单出具、人工归类、合作社签认与逐级下钻。
"""

from .config import EngineConfig
from .coverage import CoverageResult
from .engine import SettlementEngine
from .geo import LocalProjection
from .models import CoverClass, ImplementState, Quality, Segment, TrackPoint

__all__ = [
    "CoverClass",
    "CoverageResult",
    "EngineConfig",
    "ImplementState",
    "LocalProjection",
    "Quality",
    "Segment",
    "SettlementEngine",
    "TrackPoint",
]
