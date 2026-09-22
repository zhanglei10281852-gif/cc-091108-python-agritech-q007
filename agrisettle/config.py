"""核算引擎的判定参数。

所有影响核算结果的参数集中于此，并随账单快照一并留存，保证事后可审计、可复算。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    cell_size_m: float = 1.0
    """覆盖网格边长（米）。网格按投影原点绝对对齐，与数据范围无关。"""

    max_gap_s: float = 60.0
    """相邻有效定位点的最大时间间隔（秒），超过则断开航段，不做插值。"""

    max_bridge_m: float = 25.0
    """相邻有效定位点的最大平面距离（米）。

    超过该距离即使时间连续也不连成航段——短暂定位漂移不能凭插值跨过河沟等
    无作业区，断点续传留下的长缺口同样不被"脑补"为作业覆盖。
    """

    joint_suppression_s: float = 10.0
    """同一网格在多少秒内被相邻航段重复扫过时不重复计次。

    机具以正常速度作业时，前后两段航段的幅宽胶囊体在接头处天然重叠，这部分
    重叠属于同一趟作业的几何接缝，既不是必要重叠也不是重复碾压。
    """

    necessary_overlap_window_s: float = 1800.0
    """必要重叠判定时间窗（秒）。

    网格第二次被覆盖且距上次覆盖不超过该时间窗，判为必要重叠（邻接幅压边、
    田头转弯带过的正常交叠）；超过时间窗或覆盖次数更多，判为重复碾压。
    """

    break_on_boot_change: bool = False
    """终端重启（boot_id 变化）是否强制断段。默认否：重启本身不否定作业连续性，
    是否断段由时间/距离阈值决定；序号归零靠 (boot_id, sequence) 联合主键区分。"""
