"""核算引擎门面:状态装配、账单生命周期、事件日志与重放。

所有变更类操作先执行、后追加到 JSONL 事件日志;服务重启后
Engine.restore(log_path) 按序重放即可恢复到完全一致的状态。
由于轨迹接收幂等、核算为纯函数,重放/重传/分批上传都得到相同指纹。
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .billing import compute_bill, drill_down
from .geo import LocalFrame, polygon_centroid
from .models import (
    OVERRIDABLE_CLASSES,
    Barrier,
    DomainError,
    FieldBoundary,
    ImplementSpec,
    Policy,
)
from .store import TrackStore

BILL_DRAFT = "draft"
BILL_SIGNED = "signed"


class Engine:
    def __init__(self, log_path: str | Path | None = None) -> None:
        self.store = TrackStore()
        self.fields: dict[str, dict] = {}
        self.implements: dict[str, ImplementSpec] = {}
        self.bills: dict[str, dict] = {}
        self._log_path = Path(log_path) if log_path else None
        self._log_fh = (
            open(self._log_path, "a", encoding="utf-8") if self._log_path else None
        )

    # ------------------------------------------------------------------ 日志
    def _log(self, op: str, payload: dict) -> None:
        if self._log_fh is not None:
            self._log_fh.write(
                json.dumps({"op": op, "payload": payload}, ensure_ascii=False) + "\n"
            )
            self._log_fh.flush()

    def _apply(self, op: str, payload: dict):
        return getattr(self, f"_do_{op}")(**payload)

    def _command(self, op: str, payload: dict):
        result = self._apply(op, payload)
        self._log(op, payload)
        return result

    @classmethod
    def restore(cls, log_path: str | Path) -> "Engine":
        """服务重启:重放事件日志恢复状态。"""
        engine = cls()
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    record = json.loads(line)
                    engine._apply(record["op"], record["payload"])
        engine._log_path = Path(log_path)
        engine._log_fh = open(engine._log_path, "a", encoding="utf-8")
        return engine

    def close(self) -> None:
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None

    # ---------------------------------------------------------- 基础档案登记
    def register_field(
        self,
        field_id: str,
        boundary: list[list[float]],
        revision: int = 1,
        valid_from: str | None = None,
    ) -> None:
        self._command(
            "register_field",
            {
                "field_id": field_id,
                "boundary": boundary,
                "revision": revision,
                "valid_from": valid_from,
            },
        )

    def _do_register_field(
        self, field_id: str, boundary: list[list[float]], revision: int, valid_from
    ) -> None:
        if field_id in self.fields:
            raise DomainError(f"地块已登记: {field_id}")
        bound = FieldBoundary.of(field_id, revision, boundary, valid_from)
        # 投影参考点在首次登记时确定(取整到 1e-4 度),此后不随边界修订改变
        cx, cy = polygon_centroid(list(bound.ring))
        frame = LocalFrame(round(cx, 4), round(cy, 4))
        self.fields[field_id] = {
            "frame": frame,
            "revisions": {bound.revision: bound},
            "barriers": {},
        }

    def register_boundary_revision(
        self,
        field_id: str,
        revision: int,
        boundary: list[list[float]],
        valid_from: str | None = None,
    ) -> dict:
        return self._command(
            "register_boundary_revision",
            {
                "field_id": field_id,
                "revision": revision,
                "boundary": boundary,
                "valid_from": valid_from,
            },
        )

    def _do_register_boundary_revision(
        self, field_id: str, revision: int, boundary: list[list[float]], valid_from
    ) -> dict:
        field = self._field(field_id)
        current = max(field["revisions"])
        if revision <= current:
            raise DomainError(
                f"边界修订号必须递增: 当前 {current}, 收到 {revision}"
            )
        field["revisions"][revision] = FieldBoundary.of(
            field_id, revision, boundary, valid_from
        )
        # 边界更正版只作用于尚未签认的账单
        recomputed = []
        for bill_id, bill in self.bills.items():
            if bill["field_id"] == field_id and bill["status"] != BILL_SIGNED:
                self._recompute(bill)
                recomputed.append(bill_id)
        return {"recomputed": recomputed}

    def register_barrier(
        self, field_id: str, barrier_id: str, ring: list[list[float]]
    ) -> None:
        self._command(
            "register_barrier",
            {"field_id": field_id, "barrier_id": barrier_id, "ring": ring},
        )

    def _do_register_barrier(
        self, field_id: str, barrier_id: str, ring: list[list[float]]
    ) -> None:
        self._field(field_id)["barriers"][barrier_id] = Barrier.of(barrier_id, ring)

    def register_implement(self, implement_id: str, working_width_m: float) -> None:
        self._command(
            "register_implement",
            {"implement_id": implement_id, "working_width_m": working_width_m},
        )

    def _do_register_implement(self, implement_id: str, working_width_m: float) -> None:
        self.implements[implement_id] = ImplementSpec(implement_id, working_width_m)

    # -------------------------------------------------------------- 轨迹接收
    def upload_points(self, device_id: str, points: list[dict]) -> dict:
        return self._command(
            "upload_points", {"device_id": device_id, "points": points}
        )

    def _do_upload_points(self, device_id: str, points: list[dict]) -> dict:
        return self.store.upload(device_id, points).as_dict()

    def ingest_status(self) -> dict:
        return self.store.ingest_status()

    # ------------------------------------------------------------ 账单生命周期
    def create_bill(
        self,
        bill_id: str,
        field_id: str,
        implement_id: str,
        rate_per_ha: str,
        period_start: str | None = None,
        period_end: str | None = None,
        device_id: str | None = None,
        policy: dict | None = None,
    ) -> dict:
        return self._command(
            "create_bill",
            {
                "bill_id": bill_id,
                "field_id": field_id,
                "implement_id": implement_id,
                "rate_per_ha": rate_per_ha,
                "period_start": period_start,
                "period_end": period_end,
                "device_id": device_id,
                "policy": policy,
            },
        )

    def _do_create_bill(
        self,
        bill_id: str,
        field_id: str,
        implement_id: str,
        rate_per_ha: str,
        period_start: str | None,
        period_end: str | None,
        device_id: str | None,
        policy: dict | None,
    ) -> dict:
        if bill_id in self.bills:
            raise DomainError(f"账单已存在: {bill_id}")
        self._field(field_id)
        if implement_id not in self.implements:
            raise DomainError(f"机具未登记: {implement_id}")
        bill = {
            "bill_id": bill_id,
            "field_id": field_id,
            "implement_id": implement_id,
            "rate_per_ha": str(Decimal(str(rate_per_ha))),
            "period_start": period_start,
            "period_end": period_end,
            "device_id": device_id,
            "policy": Policy.from_dict(policy),
            "status": BILL_DRAFT,
            "overrides": [],
            "signed_by": None,
            "signed_at": None,
            "result": None,
        }
        self.bills[bill_id] = bill
        self._recompute(bill)
        return self.bill_report(bill_id)

    def recompute_bill(self, bill_id: str) -> dict:
        return self._command("recompute_bill", {"bill_id": bill_id})

    def _do_recompute_bill(self, bill_id: str) -> dict:
        bill = self._unsigned_bill(bill_id)
        self._recompute(bill)
        return self.bill_report(bill_id)

    def submit_override(
        self,
        bill_id: str,
        selector: dict,
        target_class: str,
        reason: str,
        author: str,
    ) -> dict:
        return self._command(
            "submit_override",
            {
                "bill_id": bill_id,
                "selector": selector,
                "target_class": target_class,
                "reason": reason,
                "author": author,
            },
        )

    def _do_submit_override(
        self, bill_id: str, selector: dict, target_class: str, reason: str, author: str
    ) -> dict:
        bill = self._unsigned_bill(bill_id)
        if target_class not in OVERRIDABLE_CLASSES:
            raise DomainError(
                f"人工归类只能在 {list(OVERRIDABLE_CLASSES)} 之间裁定, 收到 {target_class}"
            )
        if not reason or not reason.strip():
            raise DomainError("人工归类必须附理由")
        override = {
            "override_id": f"OV-{len(bill['overrides']) + 1:03d}",
            "selector": selector,
            "target_class": target_class,
            "reason": reason,
            "author": author,
        }
        bill["overrides"].append(override)
        try:
            self._recompute(bill)  # 选择器非法(指向不存在的航段)会在此抛错
        except Exception:
            bill["overrides"].pop()
            raise
        return self.bill_report(bill_id)

    def sign_bill(self, bill_id: str, signed_by: str, at: str) -> dict:
        return self._command(
            "sign_bill", {"bill_id": bill_id, "signed_by": signed_by, "at": at}
        )

    def _do_sign_bill(self, bill_id: str, signed_by: str, at: str) -> dict:
        bill = self._unsigned_bill(bill_id)
        datetime.fromisoformat(at)  # 校验时间格式
        bill["status"] = BILL_SIGNED
        bill["signed_by"] = signed_by
        bill["signed_at"] = at
        # 签认即冻结:结果快照不再随新轨迹、边界修订或人工归类改变
        bill["snapshot"] = json.loads(json.dumps(bill["result"]))
        return self.bill_report(bill_id)

    # ------------------------------------------------------------------ 查询
    def bill_report(self, bill_id: str) -> dict:
        bill = self._bill(bill_id)
        result = bill["snapshot"] if bill["status"] == BILL_SIGNED else bill["result"]
        return {
            "bill_id": bill_id,
            "field_id": bill["field_id"],
            "implement_id": bill["implement_id"],
            "status": bill["status"],
            "boundary_revision": result["boundary_revision"],
            "lines": result["lines"],
            "total_payable_area_m2": result["total_payable_area_m2"],
            "total_amount": result["total_amount"],
            "rate_per_ha": result["rate_per_ha"],
            "stats": result["stats"],
            "overrides": result["overrides_applied"],
            "fingerprint": result["fingerprint"],
            "signed_by": bill["signed_by"],
            "signed_at": bill["signed_at"],
        }

    def drill_down(self, bill_id: str, cls: str | None = None) -> dict:
        bill = self._bill(bill_id)
        result = bill["snapshot"] if bill["status"] == BILL_SIGNED else bill["result"]
        return drill_down(result, self._field(bill["field_id"])["frame"], cls)

    # ------------------------------------------------------------------ 内部
    def _recompute(self, bill: dict) -> None:
        field = self._field(bill["field_id"])
        revision = max(field["revisions"])
        bound = field["revisions"][revision]
        frame: LocalFrame = field["frame"]
        policy: Policy = bill["policy"]
        start = (
            datetime.fromisoformat(bill["period_start"]) if bill["period_start"] else None
        )
        end = datetime.fromisoformat(bill["period_end"]) if bill["period_end"] else None
        points = self.store.points(
            device_id=bill["device_id"], start=start, end=end
        )
        points_xy = [(p, *frame.to_xy(p.lon, p.lat)) for p in points]
        barriers_xy = [
            (bid, [frame.to_xy(lon, lat) for lon, lat in barrier.ring])
            for bid, barrier in field["barriers"].items()
        ]
        bill["result"] = compute_bill(
            field_id=bill["field_id"],
            boundary_revision=revision,
            boundary_ring_xy=[frame.to_xy(lon, lat) for lon, lat in bound.ring],
            boundary_ring_lonlat=bound.ring,
            barriers_xy=barriers_xy,
            points=points,
            points_xy=points_xy,
            width_m=self.implements[bill["implement_id"]].working_width_m,
            policy=policy,
            overrides=bill["overrides"],
            rate_per_ha=Decimal(bill["rate_per_ha"]),
        )

    def _field(self, field_id: str) -> dict:
        if field_id not in self.fields:
            raise DomainError(f"地块未登记: {field_id}")
        return self.fields[field_id]

    def _bill(self, bill_id: str) -> dict:
        if bill_id not in self.bills:
            raise DomainError(f"账单不存在: {bill_id}")
        return self.bills[bill_id]

    def _unsigned_bill(self, bill_id: str) -> dict:
        bill = self._bill(bill_id)
        if bill["status"] == BILL_SIGNED:
            raise DomainError(f"账单已签认冻结, 不可变更: {bill_id}")
        return bill
