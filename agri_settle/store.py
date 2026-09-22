"""轨迹存储:幂等接收、断点续传状态、规范时序输出。

- 点的唯一身份是 (device_id, boot_id, sequence);终端重启序号归零后,
  新 boot_id 的点不会与旧点冲突。
- 同一身份重复上传且内容一致 → 幂等去重;内容不一致 → 保留首条并记入冲突,
  原始点绝不改写。
- 输出永远按 (时间, device_id, boot_id, sequence) 规范排序(全序),
  与到达顺序无关,因此分批上传、重复上传、服务重启后重放都得到相同的核算输入。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from .models import TrackPoint


@dataclass
class UploadReceipt:
    accepted: int = 0
    duplicates: int = 0
    conflicts: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "rejected": self.rejected,
        }


class TrackStore:
    def __init__(self) -> None:
        self._points: dict[tuple[str, str, int], TrackPoint] = {}
        self.conflicts: list[dict] = []

    def upload(self, device_id: str, raw_points: list[dict]) -> UploadReceipt:
        receipt = UploadReceipt()
        for raw in raw_points:
            try:
                point = TrackPoint.from_dict(device_id, raw)
            except ValueError as exc:
                receipt.rejected.append({"point": raw, "reason": str(exc)})
                continue
            existing = self._points.get(point.key)
            if existing is None:
                self._points[point.key] = point
                receipt.accepted += 1
            elif existing.canonical() == point.canonical():
                receipt.duplicates += 1
            else:
                conflict = {
                    "key": list(point.key),
                    "kept": existing.canonical(),
                    "dropped": point.canonical(),
                }
                self.conflicts.append(conflict)
                receipt.conflicts.append(conflict)
        return receipt

    def points(
        self,
        device_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[TrackPoint]:
        """规范时序:与上传批次、上传顺序无关。"""
        out = []
        for p in self._points.values():
            if device_id is not None and p.device_id != device_id:
                continue
            if start is not None and p.at < start:
                continue
            if end is not None and p.at > end:
                continue
            out.append(p)
        out.sort(key=lambda p: (p.at, p.device_id, p.boot_id, p.sequence))
        return out

    def ingest_status(self) -> dict:
        """每台设备每个启动周期已收到的序号区间,供断点续传续传比对。"""
        by_boot: dict[tuple[str, str], list[int]] = {}
        for device_id, boot_id, seq in self._points:
            by_boot.setdefault((device_id, boot_id), []).append(seq)
        status = {}
        for (device_id, boot_id), seqs in sorted(by_boot.items()):
            seqs.sort()
            ranges = []
            lo = hi = seqs[0]
            for s in seqs[1:]:
                if s == hi + 1:
                    hi = s
                else:
                    ranges.append([lo, hi])
                    lo = hi = s
            ranges.append([lo, hi])
            status[f"{device_id}/{boot_id}"] = ranges
        return status

    def fingerprint(self, device_id: str | None = None) -> str:
        h = hashlib.sha256()
        for p in self.points(device_id=device_id):
            h.update(p.canonical().encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    def __len__(self) -> int:
        return len(self._points)
