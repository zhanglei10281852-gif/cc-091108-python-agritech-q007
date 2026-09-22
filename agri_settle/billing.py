"""账单核算:由覆盖结果汇总金额、人工归类的解析与应用、下钻。

账单结果整体是一个可 JSON 序列化的字典:签认时直接冻结为快照,
结算主管可沿 金额 → 覆盖分类 → 栅格单元 → 航段 → 原始定位点 逐级下钻。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .coverage import compute_coverage
from .geo import LocalFrame
from .models import (
    CLASSES,
    DomainError,
    Policy,
    TrackPoint,
)
from .segments import Segment, build_segments

CENT = Decimal("0.01")
M2_PER_HA = Decimal(10000)


def _sha(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _money(area_m2: float, rate_per_ha: Decimal) -> Decimal:
    area = Decimal(str(round(area_m2, 4)))
    return (area / M2_PER_HA * rate_per_ha).quantize(CENT, rounding=ROUND_HALF_UP)


def match_override(selector: dict, segments: list[Segment]) -> set[str]:
    """把人工归类的选择器解析为航段集合。支持按航段号或按时间窗。"""
    if "segments" in selector:
        wanted = set(selector["segments"])
        known = {s.seg_id for s in segments}
        unknown = wanted - known
        if unknown:
            raise DomainError(f"归类指向不存在的航段: {sorted(unknown)}")
        return wanted
    if "time_range" in selector:
        t0 = datetime.fromisoformat(selector["time_range"][0])
        t1 = datetime.fromisoformat(selector["time_range"][1])
        return {s.seg_id for s in segments if t0 <= s.start <= t1}
    raise DomainError("归类选择器需要 segments 或 time_range")


def compute_bill(
    *,
    field_id: str,
    boundary_revision: int,
    boundary_ring_xy: list[tuple[float, float]],
    boundary_ring_lonlat,
    barriers_xy: list[tuple[str, list[tuple[float, float]]]],
    points: list[TrackPoint],
    points_xy: list[tuple[TrackPoint, float, float]],
    width_m: float,
    policy: Policy,
    overrides: list[dict],
    rate_per_ha: Decimal,
) -> dict:
    """纯函数:同一输入永远得到同一结果(指纹相同)。"""
    segments, breaks = build_segments(points_xy, policy, barriers_xy)

    applied = []
    for ov in overrides:
        matched = match_override(ov["selector"], segments)
        applied.append({**ov, "_matched_seg_ids": matched})

    coverage = compute_coverage(
        field_id=field_id,
        boundary_revision=boundary_revision,
        boundary_ring_xy=boundary_ring_xy,
        barriers_xy=barriers_xy,
        segments=segments,
        width_m=width_m,
        policy=policy,
        breaks=breaks,
        overrides=applied,
    )

    areas = coverage.class_areas()
    lines = []
    total_payable = 0.0
    for cls in CLASSES:
        payable = cls in policy.payable_classes
        amount = _money(areas[cls], rate_per_ha) if payable else Decimal("0.00")
        if payable:
            total_payable += areas[cls]
        lines.append(
            {
                "cls": cls,
                "events": round(areas[cls] / (policy.cell_size_m**2), 0),
                "area_m2": round(areas[cls], 4),
                "payable": payable,
                "amount": str(amount),
            }
        )
    total_payable = round(total_payable, 4)
    total_amount = _money(total_payable, rate_per_ha)

    by_key = {p.key: p for p in points}
    seg_dicts = {}
    for s in sorted(segments, key=lambda s: (s.start.isoformat(), s.seg_id)):
        sc = coverage.segments[s.seg_id]
        seg_dicts[s.seg_id] = {
            "start": s.start.isoformat(),
            "end": s.end.isoformat(),
            "length_m": round(s.length_m, 4),
            "heading_deg": round(s.heading_deg, 4),
            "low_confidence": s.low_confidence,
            "overlap_class": sc.overlap_class,
            "first_cells": len(sc.first_cells),
            "recovered_cells": len(sc.recovered_cells),
            "out_cells": len(sc.out_cells),
            # 行程上的全部原始定位点,支撑“金额 → 网格 → 轨迹”下钻
            "points": [by_key[k].as_dict() for k in s.point_keys],
        }

    result = {
        "field_id": field_id,
        "boundary_revision": boundary_revision,
        "cell_size_m": policy.cell_size_m,
        "lines": lines,
        "total_payable_area_m2": round(total_payable, 4),
        "total_amount": str(total_amount),
        "rate_per_ha": str(rate_per_ha),
        "stats": {
            "points_used": len(points),
            "segments": len(segments),
            "low_confidence_segments": sum(1 for s in segments if s.low_confidence),
            "breaks": len(breaks),
            "breaks_by_reason": _count_by(breaks, lambda b: b.reason),
            "coverable_cells": len(coverage.coverable_cells),
            "missed_cells": len(coverage.missed_cells()),
        },
        "breaks": [
            {
                "reason": b.reason,
                "a_key": list(b.a_key) if b.a_key else None,
                "b_key": list(b.b_key) if b.b_key else None,
                "barrier_id": b.barrier_id,
                "detail": b.detail,
            }
            for b in breaks
        ],
        "segments": seg_dicts,
        "coverings": {
            f"{i},{j}": [[c.seg_id, c.cls] for c in covs]
            for (i, j), covs in sorted(coverage.coverings.items())
        },
        "overrides_applied": [
            {k: v for k, v in ov.items() if not k.startswith("_")} for ov in applied
        ],
    }
    result["fingerprint"] = _sha(
        {
            "field_id": field_id,
            "boundary_revision": boundary_revision,
            "boundary_ring": [[round(x, 6), round(y, 6)] for x, y in boundary_ring_lonlat],
            "width_m": width_m,
            "policy": policy.as_dict(),
            "points_sha": _sha([p.canonical() for p in points]),
            "rate_per_ha": str(rate_per_ha),
            "overrides": result["overrides_applied"],
            "lines": result["lines"],
            "total_amount": result["total_amount"],
        }
    )
    return result


def _count_by(items, key) -> dict:
    out: dict[str, int] = {}
    for it in items:
        k = key(it)
        out[k] = out.get(k, 0) + 1
    return out


def drill_down(result: dict, frame: LocalFrame, cls: str | None = None) -> dict:
    """金额 → 覆盖分类 → 栅格单元(经纬度) → 航段 → 原始定位点。"""
    if cls is not None and cls not in CLASSES:
        raise DomainError(f"未知覆盖分类: {cls}")
    cells = []
    for key, covs in result["coverings"].items():
        i, j = (int(v) for v in key.split(","))
        lon, lat = frame.to_lonlat(
            (i + 0.5) * result["cell_size_m"], (j + 0.5) * result["cell_size_m"]
        )
        for seg_id, cov_cls in covs:
            if cls is None or cov_cls == cls:
                cells.append(
                    {
                        "cell": [i, j],
                        "lon": round(lon, 7),
                        "lat": round(lat, 7),
                        "cls": cov_cls,
                        "seg_id": seg_id,
                        "at": result["segments"][seg_id]["start"],
                    }
                )
    cells.sort(key=lambda c: (c["at"], c["cell"][0], c["cell"][1], c["cls"]))
    return {
        "field_id": result["field_id"],
        "boundary_revision": result["boundary_revision"],
        "fingerprint": result["fingerprint"],
        "lines": result["lines"],
        "total_payable_area_m2": result["total_payable_area_m2"],
        "total_amount": result["total_amount"],
        "filter_cls": cls,
        "cells": cells,
        "segments": result["segments"],
        "breaks": result["breaks"],
        "stats": result["stats"],
    }
