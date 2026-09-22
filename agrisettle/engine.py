"""结算核算引擎门面：轨迹入库、边界版本、人工归类、账单生命周期与下钻。

核心保证：
- 幂等：轨迹按 (boot_id, sequence) 去重，航段与网格由内容确定性派生，
  分批上传、重复上传、服务重启后核算出相同的有效面积；
- 边界更正版只作用于尚未签认的账单：登记新修订时，草稿账单自动按新边界重算，
  已签认账单保持冻结；
- 人工归类只给航段换分类标签且必须附理由，原始轨迹点永不修改；
- 合作社签认后账单及其覆盖快照成为固定版本，任何后续操作不再改变它；
- 从账单金额可逐级下钻：账单 → 覆盖网格 → 覆盖事件 → 航段 → 原始轨迹点。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import EngineConfig
from .coverage import CoverageResult, compute_coverage
from .geo import LocalProjection, ring_area
from .models import CoverClass, Segment, TrackPoint
from .segments import build_segments
from .store import Store

MU_PER_M2 = 3.0 / 2000.0  # 1 亩 = 2000/3 平方米


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _close_ring(boundary: list) -> list[list[float]]:
    ring = [[float(p[0]), float(p[1])] for p in boundary]
    if len(ring) < 3:
        raise ValueError("地块边界至少需要 3 个顶点")
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    if len(ring) < 4:
        raise ValueError("地块边界至少需要 3 个顶点")
    return ring


class SettlementEngine:
    def __init__(self, store_path: str | Path, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self.store = Store(store_path)

    # ------------------------------------------------------------------ 基础

    def _field(self, field_id: str) -> dict:
        try:
            return self.store.state["fields"][field_id]
        except KeyError:
            raise ValueError(f"未登记的地块: {field_id}") from None

    def _projection(self) -> LocalProjection:
        anchor = self.store.state["meta"].get("anchor")
        if anchor is None:
            raise ValueError("尚未登记任何地块，投影锚点不存在")
        return LocalProjection(anchor[0], anchor[1])

    def _current_revision(self, field: dict) -> dict:
        if not field["revisions"]:
            raise ValueError(f"地块 {field['id']} 没有可用的边界版本")
        return max(field["revisions"], key=lambda r: r["revision"])

    def _points(self, field_id: str) -> list[TrackPoint]:
        raw = self._field(field_id)["points"].values()
        return [TrackPoint.from_dict(d) for d in raw]

    # ------------------------------------------------------- 地块与边界版本

    def register_field(
        self,
        field_id: str,
        boundary: list,
        revision: int = 1,
        valid_from: str | None = None,
    ) -> dict:
        """登记地块及其初始边界版本。首个地块的首个顶点固定为投影锚点。"""
        fields = self.store.state["fields"]
        if field_id in fields:
            raise ValueError(f"地块已登记: {field_id}")
        ring = _close_ring(boundary)
        proj_anchor = self.store.state["meta"].get("anchor")
        if proj_anchor is None:
            self.store.state["meta"]["anchor"] = list(ring[0])
        proj = LocalProjection(*self.store.state["meta"]["anchor"])
        area = ring_area([proj.to_xy(lon, lat) for lon, lat in ring])
        if area <= 0:
            raise ValueError("地块边界面积为零")
        entry = {
            "revision": int(revision),
            "boundary": ring,
            "valid_from": valid_from,
            "registered_at": _now_iso(),
        }
        fields[field_id] = {"id": field_id, "revisions": [entry], "points": {}, "implement": None}
        self.store.save()
        return entry

    def register_boundary_revision(
        self,
        field_id: str,
        boundary: list,
        revision: int,
        valid_from: str | None = None,
    ) -> dict:
        """登记边界更正版。只作用于尚未签认的账单：草稿自动按新边界重算，
        已签认的账单保持冻结。边界版本号必须单调递增且内容不可变。"""
        field = self._field(field_id)
        ring = _close_ring(boundary)
        proj = self._projection()
        if ring_area([proj.to_xy(lon, lat) for lon, lat in ring]) <= 0:
            raise ValueError("地块边界面积为零")
        for existing in field["revisions"]:
            if existing["revision"] == int(revision):
                if existing["boundary"] != ring:
                    raise ValueError(f"边界版本 {revision} 已存在且内容不同，版本不可变")
                return {"revision": existing, "superseded_bills": [], "recomputed_bills": []}
        if int(revision) <= max(r["revision"] for r in field["revisions"]):
            raise ValueError("边界版本号必须递增")
        entry = {
            "revision": int(revision),
            "boundary": ring,
            "valid_from": valid_from,
            "registered_at": _now_iso(),
        }
        field["revisions"].append(entry)

        superseded, recomputed = [], []
        for bill in list(self.store.state["bills"].values()):
            if bill["field_id"] == field_id and bill["status"] == "draft":
                bill["status"] = "superseded"
                bill["superseded_at"] = _now_iso()
                superseded.append(bill["id"])
                recomputed.append(self._create_bill(field_id, bill["unit_price_per_mu"])["id"])
        self.store.save()
        return {"revision": entry, "superseded_bills": superseded, "recomputed_bills": recomputed}

    def set_implement(self, field_id: str, implement_id: str, working_width_m: float) -> dict:
        """登记作业机具及幅宽。幅宽参与账单指纹，换机具即得到新账单版本。"""
        if working_width_m <= 0:
            raise ValueError("机具幅宽必须为正")
        field = self._field(field_id)
        field["implement"] = {"id": implement_id, "working_width_m": float(working_width_m)}
        self.store.save()
        return field["implement"]

    # ------------------------------------------------------------- 轨迹入库

    def ingest_track(self, field_id: str, points: list[dict]) -> dict:
        """批量入库轨迹点。按 (boot_id, sequence) 去重：重复上传不产生新数据；
        同主键但内容冲突的记录保留先到的，并在返回值中如实报告。"""
        field = self._field(field_id)
        stored = field["points"]
        added, duplicates, conflicts = 0, 0, []
        for raw in points:
            point = TrackPoint.from_dict(raw)
            existing = stored.get(point.key)
            if existing is None:
                stored[point.key] = point.to_dict()
                added += 1
            elif existing != point.to_dict():
                conflicts.append(point.key)
            else:
                duplicates += 1
        self.store.save()
        return {"added": added, "duplicates": duplicates, "conflicts": conflicts,
                "total": len(stored)}

    def track_points(self, field_id: str) -> list[dict]:
        """按时间排序导出全部原始轨迹点（只读）。"""
        points = sorted(self._points(field_id), key=lambda p: (p.at, p.boot_id, p.sequence))
        return [p.to_dict() for p in points]

    # ------------------------------------------------------------- 核算

    def segments(self, field_id: str) -> list[Segment]:
        return build_segments(self._points(field_id), self._projection(), self.config)

    def _applicable_rulings(self, field_id: str, segments: list[Segment]) -> tuple[dict, list]:
        known = {s.id for s in segments}
        rulings, applied = {}, []
        for r in self.store.state["rulings"]:
            if r["field_id"] == field_id and r["segment_id"] in known:
                rulings[r["segment_id"]] = r["class"]
                applied.append(r)
        return rulings, applied

    def compute(self, field_id: str) -> CoverageResult:
        """按当前边界版本对全部已入库轨迹做覆盖核算（不落账单）。"""
        field = self._field(field_id)
        implement = field.get("implement")
        if not implement:
            raise ValueError(f"地块 {field_id} 尚未登记机具幅宽")
        proj = self._projection()
        revision = self._current_revision(field)
        ring_xy = [proj.to_xy(lon, lat) for lon, lat in revision["boundary"]]
        segments = self.segments(field_id)
        rulings, applied = self._applicable_rulings(field_id, segments)
        return compute_coverage(
            segments, implement["working_width_m"], ring_xy, self.config,
            rulings=rulings, applied_rulings=applied,
        )

    # ------------------------------------------------------------- 人工归类

    def submit_ruling(self, field_id: str, segment_id: str, klass: str,
                      reason: str, author: str) -> dict:
        """承包人对争议航段提交人工归类。必须附理由；只改分类标签，不改原始点。"""
        self._field(field_id)
        try:
            CoverClass(klass)
        except ValueError:
            raise ValueError(f"非法分类: {klass}") from None
        if not reason or not reason.strip():
            raise ValueError("人工归类必须填写理由")
        if not author or not author.strip():
            raise ValueError("人工归类必须填写提交人")
        known = {s.id for s in self.segments(field_id)}
        if segment_id not in known:
            raise ValueError(f"未知航段: {segment_id}")
        counters = self.store.state["counters"]
        counters["ruling"] += 1
        ruling = {
            "id": f"RLG-{counters['ruling']:04d}",
            "field_id": field_id,
            "segment_id": segment_id,
            "class": klass,
            "reason": reason.strip(),
            "author": author.strip(),
            "at": _now_iso(),
        }
        self.store.state["rulings"].append(ruling)
        self.store.save()
        return ruling

    # ------------------------------------------------------------- 账单

    def _fingerprint(self, field_id: str, unit_price_per_mu: float) -> tuple[str, dict, list]:
        field = self._field(field_id)
        revision = self._current_revision(field)
        implement = field.get("implement") or {}
        segments = self.segments(field_id)
        _, applied = self._applicable_rulings(field_id, segments)
        payload = {
            "field_id": field_id,
            "revision": revision["revision"],
            "boundary": revision["boundary"],
            "working_width_m": implement.get("working_width_m"),
            "points": sorted(field["points"].keys()),
            "rulings": [[r["id"], r["segment_id"], r["class"]] for r in applied],
            "config": asdict(self.config),
            "unit_price_per_mu": unit_price_per_mu,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return digest, revision, applied

    def _create_bill(self, field_id: str, unit_price_per_mu: float) -> dict:
        field = self._field(field_id)
        fingerprint, revision, applied = self._fingerprint(field_id, unit_price_per_mu)
        bill_id = f"B-{field_id}-r{revision['revision']}-{fingerprint[:10]}"
        existing = self.store.state["bills"].get(bill_id)
        if existing is not None:
            return existing

        result = self.compute(field_id)
        areas_m2 = {k: round(v, 2) for k, v in result.areas_m2.items()}
        areas_mu = {k: round(v * MU_PER_M2, 4) for k, v in result.areas_m2.items()}
        billable_m2 = result.areas_m2[CoverClass.FIRST.value]
        bill = {
            "id": bill_id,
            "field_id": field_id,
            "revision": revision["revision"],
            "status": "draft",
            "currency": "CNY",
            "unit_price_per_mu": float(unit_price_per_mu),
            "areas_m2": areas_m2,
            "areas_mu": areas_mu,
            "billable_area_m2": round(billable_m2, 2),
            "billable_area_mu": round(billable_m2 * MU_PER_M2, 4),
            "amount_yuan": round(billable_m2 * MU_PER_M2 * unit_price_per_mu, 2),
            "field_area_m2": round(result.field_area_m2, 2),
            "field_cell_count": result.field_cell_count,
            "covered_field_cell_count": result.covered_field_cell_count,
            "uncovered_field_cell_count": result.field_cell_count - result.covered_field_cell_count,
            "segment_count": len(result.segments),
            "track_point_count": len(field["points"]),
            "total_segment_length_m": round(sum(s.length_m for s in result.segments.values()), 2),
            "working_time_s": round(sum(s.duration_s for s in result.segments.values()), 1),
            "applied_rulings": [r["id"] for r in applied],
            "fingerprint": fingerprint,
            "created_at": _now_iso(),
            "signed_at": None,
            "signer": None,
        }
        # 新账单版本产生时，同地块其余草稿作废；已签认账单永不动。
        for other in self.store.state["bills"].values():
            if other["field_id"] == field_id and other["status"] == "draft":
                other["status"] = "superseded"
                other["superseded_at"] = _now_iso()
        snapshot = result.to_snapshot()
        snapshot.update({
            "bill_id": bill_id,
            "field_id": field_id,
            "revision": revision["revision"],
            "config": asdict(self.config),
            "anchor": list(self.store.state["meta"]["anchor"]),
        })
        self.store.state["snapshots"][bill_id] = snapshot
        self.store.state["bills"][bill_id] = bill
        self.store.save()
        return bill

    def compute_bill(self, field_id: str, unit_price_per_mu: float = 0.0) -> dict:
        """按当前数据与边界版本出账。输入相同则返回同一账单（幂等）。"""
        return self._create_bill(field_id, unit_price_per_mu)

    def sign_bill(self, bill_id: str, signer: str) -> dict:
        """合作社签认：账单连同覆盖快照冻结为固定版本。"""
        bill = self.store.state["bills"].get(bill_id)
        if bill is None:
            raise ValueError(f"未知账单: {bill_id}")
        if bill["status"] != "draft":
            raise ValueError(f"账单 {bill_id} 当前状态为 {bill['status']}，不能签认")
        if not signer or not signer.strip():
            raise ValueError("签认人不能为空")
        bill["status"] = "signed"
        bill["signer"] = signer.strip()
        bill["signed_at"] = _now_iso()
        self.store.save()
        return bill

    def get_bill(self, bill_id: str) -> dict:
        bill = self.store.state["bills"].get(bill_id)
        if bill is None:
            raise ValueError(f"未知账单: {bill_id}")
        return bill

    def list_bills(self, field_id: str | None = None, status: str | None = None) -> list[dict]:
        bills = self.store.state["bills"].values()
        out = [b for b in bills
               if (field_id is None or b["field_id"] == field_id)
               and (status is None or b["status"] == status)]
        return sorted(out, key=lambda b: (b["created_at"], b["id"]))

    # ------------------------------------------------------------- 下钻

    def _snapshot(self, bill_id: str) -> dict:
        snap = self.store.state["snapshots"].get(bill_id)
        if snap is None:
            raise ValueError(f"账单 {bill_id} 没有覆盖快照")
        return snap

    def bill_report(self, bill_id: str) -> dict:
        """账单总览：金额、四类面积、覆盖率与作业统计——下钻入口。

        cell_counts 按覆盖事件计数，与 areas_m2 严格对应（每格每次 = 一格面积），
        保证"金额 → 面积 → 网格 → 事件 → 航段 → 轨迹点"逐级对得上。
        """
        bill = self.get_bill(bill_id)
        snap = self._snapshot(bill_id)
        cell_counts = {klass.value: 0 for klass in CoverClass}
        for cell in snap["cells"].values():
            for _, klass in cell["events"]:
                cell_counts[klass] += 1
        return {
            "bill": bill,
            "cell_counts": cell_counts,
            "segment_count": len(snap["segments"]),
            "config": snap["config"],
        }

    def bill_cells(self, bill_id: str, klass: str | None = None) -> list[dict]:
        """账单的覆盖网格清单；按分类过滤时返回含有该类覆盖事件的网格。"""
        if klass is not None:
            CoverClass(klass)  # 非法分类尽早报错
        snap = self._snapshot(bill_id)
        out = []
        for key, cell in snap["cells"].items():
            class_counts = {k.value: 0 for k in CoverClass}
            for _, k in cell["events"]:
                class_counts[k] += 1
            if klass is not None and class_counts.get(klass, 0) == 0:
                continue
            i, j = key.split(",")
            out.append({"cell": [int(i), int(j)], "class": cell["class"],
                        "event_count": len(cell["events"]), "class_counts": class_counts})
        return sorted(out, key=lambda c: (c["cell"][0], c["cell"][1]))

    def cell_detail(self, bill_id: str, i: int, j: int) -> dict:
        """单个覆盖网格：分类、覆盖事件及其航段，附网格中心经纬度。"""
        snap = self._snapshot(bill_id)
        cell = snap["cells"].get(f"{i},{j}")
        if cell is None:
            raise ValueError(f"账单 {bill_id} 中网格 ({i},{j}) 无覆盖记录")
        proj = LocalProjection(*snap["anchor"])
        cs = snap["cell_size_m"]
        lon, lat = proj.to_lonlat((i + 0.5) * cs, (j + 0.5) * cs)
        return {
            "cell": [i, j],
            "class": cell["class"],
            "center_lonlat": [lon, lat],
            "events": [{"segment_id": seg, "class": klass} for seg, klass in cell["events"]],
        }

    def segment_detail(self, bill_id: str, segment_id: str) -> dict:
        """单段航段：几何、分类统计、扫过的网格，以及对应的原始轨迹点。"""
        snap = self._snapshot(bill_id)
        seg = snap["segments"].get(segment_id)
        if seg is None:
            raise ValueError(f"账单 {bill_id} 中无航段 {segment_id}")
        field = self._field(snap["field_id"])
        points = [field["points"][k] for k in (seg["start_key"], seg["end_key"])]
        return {
            "segment": {k: v for k, v in seg.items() if k != "cells"},
            "cells": seg["cells"],
            "points": points,
        }
