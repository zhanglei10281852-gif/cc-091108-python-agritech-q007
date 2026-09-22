"""端到端演示：从北斗轨迹到签认账单。

场景：PLOT-88 地块（reference/domain.json），旋耕机幅宽 2.4m。
覆盖故事线的全部环节：
  分批上传 / 重复上传 / 服务重启  → 幂等面积
  道路转场（提升器抬起）、田头压边、重复碾压、越界作业、河沟漂移断链
  → 四类覆盖核算 → 账单 → 人工归类（带理由、不改原始点）→ 合作社签认冻结
  → 边界更正版只作用于未签认账单 → 金额下钻到网格与轨迹

运行：python3 examples/demo.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agrisettle import EngineConfig, LocalProjection, SettlementEngine

DOMAIN = json.loads(
    (Path(__file__).resolve().parents[1] / "reference" / "domain.json").read_text(encoding="utf-8")
)
FIELD_ID = DOMAIN["field"]["id"]
ANCHOR = DOMAIN["field"]["boundary"][0]
PROJ = LocalProjection(*ANCHOR)
TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 11, 8, 0, 0, tzinfo=TZ)
CONFIG = EngineConfig(cell_size_m=1.0)
PRICE = 38.0  # 元/亩


def pt(boot, seq, t, x, y, quality="fixed", state="working"):
    return {"boot_id": boot, "sequence": seq, "at": t.isoformat(),
            "position": list(PROJ.to_lonlat(x, y)), "quality": quality,
            "implement_state": state}


def pass_points(boot, seq0, t0, x, y0, y1, step_m=5.0, dt_s=2.0, **kw):
    n = int(round((y1 - y0) / step_m))
    return [pt(boot, seq0 + k, t0 + timedelta(seconds=dt_s * k), x, y0 + step_m * k, **kw)
            for k in range(n + 1)]


def build_track():
    """构造一天的作业轨迹，包含各种争议情形。"""
    batches = []
    # 批次一：5 趟南北向作业，其中第 2、3 趟间距 2.1m（田头压边 → 必要重叠）
    b1, seq, t = [], 1, T0
    for x in (100.0, 102.4, 104.5, 106.9, 109.3):
        b1 += pass_points("BOOT-A", seq, t, x, 80, 380)
        seq += 61
        t += timedelta(minutes=12)
    batches.append(b1)

    # 批次二：过河沟时定位漂移（无效解），随后断点续传；接着抬起提升器道路转场
    b2 = pass_points("BOOT-A", seq, t, 111.7, 80, 200)
    seq += 26
    t += timedelta(seconds=52)
    drift_start = t
    for k in range(3):  # 河沟上空 6 秒无效解，不允许插值跨沟
        b2.append(pt("BOOT-A", seq + k, drift_start + timedelta(seconds=2 * k),
                     111.7, 210 + 5 * k, quality="invalid"))
    seq += 3
    t = drift_start + timedelta(seconds=6)
    b2 += pass_points("BOOT-A", seq, t, 111.7, 230, 380)  # 续传：沟后恢复
    seq += 31
    t += timedelta(seconds=62)
    for k in range(20):  # 提升器抬起，道路转场去加油，不计覆盖
        b2.append(pt("BOOT-A", seq + k, t + timedelta(seconds=30 * k),
                     111.7 + 15 * k, 380 + 2.5 * k, state="raised"))
    batches.append(b2)

    # 批次三：终端重启（序号归零）后越界作业；两小时后按农户要求重耕一趟
    b3, seq, t = [], 1, t + timedelta(minutes=15)
    b3 += pass_points("BOOT-B", seq, t, 570.0, 100, 200)  # 越出东边界（x≈565.7）
    t += timedelta(hours=2)
    b3 += pass_points("BOOT-B", 100, t, 104.5, 80, 380)  # 重复碾压原航线
    batches.append(b3)
    return batches


def show_bill(engine, bill_id, title):
    report = engine.bill_report(bill_id)
    bill = report["bill"]
    a = bill["areas_m2"]
    print(f"\n【{title}】账单 {bill['id']}（边界 v{bill['revision']}，{bill['status']}）")
    print(f"  首次覆盖 {a['first']:>9.1f} m² | 必要重叠 {a['necessary_overlap']:>7.1f} m² | "
          f"重复碾压 {a['repeat']:>7.1f} m² | 越界 {a['out_of_bounds']:>6.1f} m²")
    print(f"  计费面积 {bill['billable_area_mu']:.3f} 亩 × {bill['unit_price_per_mu']:.0f} 元/亩"
          f" = {bill['amount_yuan']:.2f} 元"
          f"（地块 {bill['field_area_m2']:.0f} m²，漏耕 {bill['uncovered_field_cell_count']} 格）")
    return report


def main():
    tmp = tempfile.TemporaryDirectory()
    store = Path(tmp.name) / "settlement.json"
    engine = SettlementEngine(store, CONFIG)
    engine.register_field(FIELD_ID, DOMAIN["field"]["boundary"],
                          revision=DOMAIN["field"]["revision"])
    engine.set_implement(FIELD_ID, DOMAIN["implement"]["id"],
                         DOMAIN["implement"]["working_width_m"])

    batches = build_track()
    print("== 轨迹入库：分批上传，中间夹杂重复上传与服务重启 ==")
    print("批次一:", engine.ingest_track(FIELD_ID, batches[0]))
    print("批次二:", engine.ingest_track(FIELD_ID, batches[1]))
    print("批次二重传:", engine.ingest_track(FIELD_ID, batches[1]))

    engine = SettlementEngine(store, CONFIG)  # 模拟服务重启后恢复
    print("批次三(重启后):", engine.ingest_track(FIELD_ID, batches[2]))

    points = engine.track_points(FIELD_ID)
    online_h = (datetime.fromisoformat(points[-1]["at"])
                - datetime.fromisoformat(points[0]["at"])).total_seconds() / 3600
    draft = engine.compute_bill(FIELD_ID, PRICE)
    print(f"\n终端在线时长 {online_h:.2f} 小时，而有效作业时长仅 "
          f"{draft['working_time_s'] / 3600:.2f} 小时——按在线时长付款确实失真")
    show_bill(engine, draft["id"], "草稿账单（按有效覆盖核算）")

    print("\n== 边界更正：东边界外扩 10m（rev 4），只作用于未签认账单 ==")
    east = [list(p) for p in DOMAIN["field"]["boundary"]]
    for p in east:
        if p[0] > 118.504:
            p[0] = 118.5071
    res = engine.register_boundary_revision(FIELD_ID, east, revision=4)
    print(f"  作废草稿 {res['superseded_bills']} → 自动重算 {res['recomputed_bills']}")
    draft2 = engine.get_bill(res["recomputed_bills"][0])
    show_bill(engine, draft2["id"], "更正后草稿（越界条带转入田内）")

    print("\n== 承包人对争议航段提交人工归类（带理由，不改原始点）==")
    repeat_cell = engine.bill_cells(draft2["id"], klass="repeat")[0]["cell"]
    seg_id = engine.cell_detail(draft2["id"], *repeat_cell)["events"][0]["segment_id"]
    ruling = engine.submit_ruling(
        FIELD_ID, seg_id, "first",
        "应农户要求对该区域二次旋耕，属合同约定作业，请按首耕计费", "承包人老王")
    print(f"  归类单 {ruling['id']}：航段 {seg_id} → first")
    draft3 = engine.compute_bill(FIELD_ID, PRICE)
    show_bill(engine, draft3["id"], "人工归类后草稿")

    print("\n== 合作社签认，形成固定版本 ==")
    signed = engine.sign_bill(draft3["id"], "合作社李会计")
    print(f"  {signed['id']} 已由 {signed['signer']} 签认")

    print("\n== 再次边界更正（rev 5）：已签认账单保持冻结 ==")
    res = engine.register_boundary_revision(FIELD_ID, east, revision=5)
    print(f"  作废 {res['superseded_bills']}，重算 {res['recomputed_bills']}（均为空）")
    frozen = engine.get_bill(signed["id"])
    assert frozen["revision"] == 4 and frozen["status"] == "signed"

    print("\n== 结算主管下钻：账单金额 → 覆盖网格 → 航段 → 原始轨迹 ==")
    report = show_bill(engine, signed["id"], "已签认账单")
    print("  各分类覆盖事件格数:", report["cell_counts"])
    cell = engine.bill_cells(signed["id"], klass="first")[0]["cell"]
    detail = engine.cell_detail(signed["id"], *cell)
    print(f"  网格 {cell}（中心 {['%.6f' % v for v in detail['center_lonlat']]}）"
          f" 分类 {detail['class']}，事件 {detail['events']}")
    seg = engine.segment_detail(signed["id"], detail["events"][0]["segment_id"])
    print(f"  航段 {seg['segment']['id']} 长 {seg['segment']['length_m']:.1f}m，"
          f"分类统计 {seg['segment']['class_counts']}")
    for raw in seg["points"]:
        print(f"    原始点 {raw['boot_id']}#{raw['sequence']} {raw['at']} "
              f"{raw['position']} {raw['quality']}/{raw['implement_state']}")

    print("\n== 幂等校验：重启 + 全量重传，结果不变 ==")
    engine2 = SettlementEngine(store, CONFIG)  # 再次模拟服务重启
    total_dup = sum(engine2.ingest_track(FIELD_ID, b)["duplicates"] for b in batches)
    again = engine2.compute_bill(FIELD_ID, PRICE)
    again2 = SettlementEngine(store, CONFIG).compute_bill(FIELD_ID, PRICE)
    assert again["id"] == again2["id"]  # 重启后重算 → 同一账单
    assert again["areas_m2"] == signed["areas_m2"]  # rev5 与 rev4 边界同内容 → 面积一致
    frozen = engine2.get_bill(signed["id"])
    assert frozen["status"] == "signed" and frozen["revision"] == 4  # 签认版冻结在 r4
    print(f"  重传 {total_dup} 点全部判重；重算账单 {again['id']}（rev 5）")
    print(f"  面积与已签认的 {signed['id']}（rev 4，冻结）完全一致 ✓")
    tmp.cleanup()


if __name__ == "__main__":
    main()
