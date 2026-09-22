"""持久化：全部状态保存在单个 JSON 文件中，原子写盘。

服务重启后从同一文件恢复，轨迹、边界版本、人工归类、账单与覆盖快照完整还原，
保证"服务重启仍得到相同有效面积"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {}
        self.state.setdefault("meta", {})
        self.state.setdefault("fields", {})
        self.state.setdefault("rulings", [])
        self.state.setdefault("bills", {})
        self.state.setdefault("snapshots", {})
        self.state.setdefault("counters", {"ruling": 0})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
