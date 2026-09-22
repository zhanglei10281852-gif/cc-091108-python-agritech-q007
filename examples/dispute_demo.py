"""争议核算演示:旋耕机“在线 8 小时”账单的有效覆盖还原。

场景:北斗终端在线 08:00–16:00,农户异议——其中含道路转场与田头重叠。
本脚本演示:
  1. 幂等接收(分批、重发、终端重启序号归零);
  2. 有效覆盖还原:首次覆盖 / 必要重叠 / 重复碾压 / 越界作业;
  3. 定位漂移不凭插值跨河沟;
  4. 承包人带理由人工归类 → 合作社签认冻结;
  5. 边界更正只作用于未签认账单;
  6. 金额 → 覆盖网格 → 原始轨迹下钻;
  7. 服务重启后重放得到相同结果。

运行: python3 examples/dispute_demo.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from agri_settle import Engine, LocalFrame  # noqa: E402

FRAME = LocalFrame(118.5000, 32.1000)
T0 = datetime.fromisoformat("2026-09-11T08:00:00+08:00")
RATE = "900"  # 元/公顷
CLS_LABEL = {
    "first": "首次覆盖",
    "necessary_overlap": "必要重叠",
    "repeated_compaction": "重复碾压",
    "out_of_bounds": "越界作业",
}


def ll(x, y):
    lon, lat = FRAME.to_lonlat(x, y)
    return [round(lon, 8), round(lat, 8)]


def rect(x0, y0, x1, y1):
    return [ll(x0, y0), ll(x1, y0), ll(x1, y1), ll(x0, y1), ll(x0, y0)]


class TrackBuilder:
    """按时间轴生成轨迹点;boot 表示终端一次启动周期。"""

    def __init__(self):
        self.points = []
        self.boot = "BOOT-A"
        self.seq = 0
        self.t = T0

    def reboot(self):
        self.boot = "BOOT-B" if self.boot == "BOOT-A" else "BOOT-C"
        self.seq = 0  # 终端重启,序号归零

    def move(self, x, y, dt_s, state="working", quality="fixed"):
        self.t += timedelta(seconds=dt_s)
        self.seq += 1
        self.points.append(
            {
                "boot_id": self.boot,
                "sequence": self.seq,
                "at": self.t.isoformat(),
                "position": ll(x, y),
                "quality": quality,
                "implement_state": state,
            }
        )

    def run(self, x0, y0, x1, y1, step_m=2.0, dt_s=2.0, **kw):
        dist = max(abs(x1 - x0), abs(y1 - y0))
        n = max(int(dist / step_m), 1)
        for i in range(n + 1):
            self.move(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n, dt_s, **kw)


def build_day_track():
    tb = TrackBuilder()
    # 08:00 道路转场 20 分钟:提升器抬起,从机库到地头
    tb.run(-600, -30, -4, 2, step_m=8.0, dt_s=8.0, state="raised")
    # 上午:10 条东西向行程,间距 2.2m(接茬重叠 0.2m 属必要重叠)
    for k in range(10):
        y = 4 + k * 2.2
        if k % 2 == 0:
            tb.run(2, y, 98, y)
        else:
            tb.run(98, y, 2, y)
        if k == 4:  # 田头掉头忘提升:沿已覆盖的埂边空碾
            tb.run(98, y, 98, y - 1.0, state="working")
            tb.run(98, y - 1.0, 96, y - 1.0, state="working")
        if k < 9:
            nxt = 4 + (k + 1) * 2.2
            tb.run(98 if k % 2 == 0 else 2, nxt, 98 if k % 2 == 0 else 2, nxt,
                   state="raised")
    # 午间停机 40 分钟(无点,断档)
    tb.t += timedelta(minutes=40)
    tb.reboot()  # 午后终端重启,序号归零
    # 下午:继续 8 条行程;河沟 x∈[48,52] 纵贯地块,作业到两岸为止
    for k in range(8):
        y = 26.4 + k * 2.2
        if k == 3:
            tb.run(2, y, 46, y)  # 作业到河西岸
            tb.move(60, y, 2.0, quality="float")  # 漂移点跳到河对岸
            tb.run(54, y, 98, y)  # 恢复后继续,插值不得跨河
        else:
            tb.run(2, y, 98, y)
        if k < 7:
            tb.run(98, y, 98, y + 2.2, state="raised")
            tb.run(98, y + 2.2, 2, y + 2.2, state="raised")
    # 收工前误入界外:提升器未抬起,越出北边界
    tb.run(30, 46, 30, 52, state="working")
    tb.run(30, 52, 30, 46, state="raised")
    # 16:00 前返程(提升器抬起)
    tb.run(28, 44, -600, -30, step_m=8.0, dt_s=8.0, state="raised")
    return tb.points


def show_bill(engine, bill_id, title):
    r = engine.bill_report(bill_id)
    print(f"\n== {title} ==")
    print(f"  状态 {r['status']} | 边界版本 rev{r['boundary_revision']} | "
          f"指纹 {r['fingerprint'][:12]}…")
    for line in r["lines"]:
        mark = "计酬" if line["payable"] else "不计"
        print(f"  {CLS_LABEL[line['cls']]:<5} {line['area_m2']:>9.1f} m²  {mark}  "
              f"¥{line['amount']}")
    print(f"  有效面积 {r['total_payable_area_m2']:.1f} m² → 应付 ¥{r['total_amount']}")
    s = r["stats"]
    print(f"  (点 {s['points_used']} | 行程 {s['segments']} | 断轨 {s['breaks']} "
          f"{s['breaks_by_reason']} | 漏耕 {s['missed_cells']} 格)")
    return r


def main():
    log = Path(tempfile.mkdtemp()) / "events.jsonl"
    engine = Engine(log_path=log)
    engine.register_field("PLOT-88", rect(0, 0, 100, 46), revision=1)
    engine.register_implement("ROTARY-6", 2.4)
    engine.register_barrier("PLOT-88", "RIVER-1", rect(48, -5, 52, 51))

    points = build_day_track()
    # 分批 + 断点续传重发:第三批传了两遍,结果不变
    third = len(points) // 3
    engine.upload_points("BEIDOU-07", points[:third])
    engine.upload_points("BEIDOU-07", points[third:2 * third])
    engine.upload_points("BEIDOU-07", points[2 * third:])
    receipt = engine.upload_points("BEIDOU-07", points[2 * third:])
    print(f"上传 {len(points)} 点(末批重发:去重 {receipt['duplicates']} 条)")

    engine.create_bill("BILL-0911", "PLOT-88", "ROTARY-6", RATE,
                       device_id="BEIDOU-07", policy={"cell_size_m": 0.5})
    before = show_bill(engine, "BILL-0911", "初始核算(草稿)")

    # 承包人异议:田头忘提升的那一段,主张是合作社要求补压 → 提交带理由归类
    drill = engine.drill_down("BILL-0911", cls="repeated_compaction")
    disputed = sorted({c["seg_id"] for c in drill["cells"]})[:1]
    engine.submit_override(
        "BILL-0911",
        selector={"segments": disputed},
        target_class="necessary_overlap",
        reason="合作社现场要求田头补压,有微信记录为证",
        author="承包人-王",
    )
    show_bill(engine, "BILL-0911", "人工归类后(待签认)")

    # 合作社签认 → 冻结
    engine.sign_bill("BILL-0911", signed_by="合作社-赵", at="2026-09-12T09:30:00+08:00")
    signed = show_bill(engine, "BILL-0911", "合作社签认(冻结)")

    # 边界更正 rev2:只作用于未签认账单;已签认的 BILL-0911 不受影响
    engine.register_boundary_revision("PLOT-88", 2, rect(0, 0, 100, 44))
    engine.create_bill("BILL-0912", "PLOT-88", "ROTARY-6", RATE,
                       device_id="BEIDOU-07", policy={"cell_size_m": 0.5})
    show_bill(engine, "BILL-0912", "边界更正后新账单(rev2)")
    assert engine.bill_report("BILL-0911")["fingerprint"] == signed["fingerprint"]

    # 金额 → 网格 → 轨迹 下钻
    drill = engine.drill_down("BILL-0911", cls="first")
    cell = drill["cells"][0]
    seg = drill["segments"][cell["seg_id"]]
    print("\n== 下钻示例 ==")
    print(f"  ¥{signed['total_amount']} → 首次覆盖格 {cell['cell']} "
          f"@({cell['lon']}, {cell['lat']})")
    print(f"  → 行程 {cell['seg_id']} 长 {seg['length_m']:.1f}m "
          f"航向 {seg['heading_deg']:.1f}° 原始点 {len(seg['points'])} 个")
    print(f"  → 首点 {seg['points'][0]}")

    # 服务重启:重放事件日志,结果一致
    engine.close()
    restored = Engine.restore(log)
    again = restored.bill_report("BILL-0911")
    assert again["fingerprint"] == signed["fingerprint"], "重启后指纹必须一致"
    print(f"\n服务重启重放:指纹一致 ✓ ({again['fingerprint'][:12]}…)")
    restored.close()


if __name__ == "__main__":
    main()
