import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import T0, make_field, new_engine, pass_points, square_field_m
from test_coverage import three_pass_points


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "state.json"
        self.engine = new_engine(self.store)
        make_field(self.engine)

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest(self, *batches):
        for batch in batches:
            self.engine.ingest_track("F1", batch)

    def test_amount_and_breakdown(self):
        self._ingest(*three_pass_points())
        bill = self.engine.compute_bill("F1", unit_price_per_mu=30.0)
        self.assertEqual(bill["status"], "draft")
        self.assertEqual(bill["areas_m2"]["first"], 40.0)
        self.assertEqual(bill["areas_m2"]["necessary_overlap"], 10.0)
        self.assertEqual(bill["areas_m2"]["repeat"], 20.0)
        self.assertEqual(bill["areas_m2"]["out_of_bounds"], 12.0)
        # 只按首次覆盖计费：40 m² = 0.06 亩 × 30 元/亩
        self.assertEqual(bill["billable_area_mu"], 0.06)
        self.assertEqual(bill["amount_yuan"], 1.8)
        # 幂等：同输入重复出账得到同一账单
        self.assertEqual(self.engine.compute_bill("F1", 30.0)["id"], bill["id"])

    def test_drill_down_from_amount_to_track(self):
        self._ingest(*three_pass_points())
        bill = self.engine.compute_bill("F1", unit_price_per_mu=30.0)

        report = self.engine.bill_report(bill["id"])
        self.assertEqual(report["cell_counts"]["repeat"], 20)

        repeat_cells = self.engine.bill_cells(bill["id"], klass="repeat")
        self.assertEqual(len(repeat_cells), 20)

        i, j = repeat_cells[0]["cell"]
        detail = self.engine.cell_detail(bill["id"], i, j)
        # 该网格既有首次覆盖事件、又有后来的重复碾压事件；展示分类取首次覆盖
        self.assertEqual(detail["class"], "first")
        repeat_events = [e for e in detail["events"] if e["class"] == "repeat"]
        self.assertTrue(repeat_events)
        self.assertIn("center_lonlat", detail)

        seg_id = repeat_events[0]["segment_id"]
        seg = self.engine.segment_detail(bill["id"], seg_id)
        self.assertEqual(len(seg["points"]), 2)  # 航段两端点即对应原始轨迹
        for raw in seg["points"]:
            self.assertIn(raw["boot_id"], ("B1",))
            self.assertEqual(raw["quality"], "fixed")
            self.assertEqual(raw["implement_state"], "working")
        self.assertTrue(seg["cells"])

    def test_manual_ruling_reclassifies_but_never_touches_points(self):
        p1, p2, _ = three_pass_points()
        self._ingest(p1, p2)
        before = self.engine.track_points("F1")

        seg_id = self.engine.segments("F1")[0].id  # 第一趟的首段
        with self.assertRaises(ValueError):
            self.engine.submit_ruling("F1", seg_id, "repeat", "", "承包人")  # 无理由
        with self.assertRaises(ValueError):
            self.engine.submit_ruling("F1", "SEG-000000000000", "repeat", "x", "承包人")
        with self.assertRaises(ValueError):
            self.engine.submit_ruling("F1", seg_id, "half", "x", "承包人")

        ruling = self.engine.submit_ruling("F1", seg_id, "repeat",
                                           "该段实为上一班次重复碾压", "承包人老王")
        self.assertEqual(ruling["segment_id"], seg_id)

        bill = self.engine.compute_bill("F1", unit_price_per_mu=30.0)
        # 该段 4 格首次覆盖被改判重复碾压，2 格越界端帽一并改判
        self.assertEqual(bill["areas_m2"]["first"], 36.0)
        self.assertEqual(bill["areas_m2"]["repeat"], 6.0)
        self.assertEqual(bill["areas_m2"]["out_of_bounds"], 6.0)
        self.assertEqual(bill["amount_yuan"], 1.62)
        self.assertEqual(bill["applied_rulings"], [ruling["id"]])
        # 原始轨迹点未被修改
        self.assertEqual(self.engine.track_points("F1"), before)

    def test_sign_freezes_bill(self):
        p1, p2, _ = three_pass_points()
        self._ingest(p1, p2)
        bill = self.engine.compute_bill("F1", unit_price_per_mu=30.0)
        signed = self.engine.sign_bill(bill["id"], "合作社李会计")
        self.assertEqual(signed["status"], "signed")

        with self.assertRaises(ValueError):
            self.engine.sign_bill(bill["id"], "合作社李会计")  # 重复签认

        # 签认后新增人工归类只产生新的草稿版本，固定版本不受影响
        seg_id = self.engine.segments("F1")[0].id
        self.engine.submit_ruling("F1", seg_id, "repeat", "补充争议说明", "承包人老王")
        new_bill = self.engine.compute_bill("F1", unit_price_per_mu=30.0)
        self.assertNotEqual(new_bill["id"], bill["id"])
        self.assertEqual(new_bill["status"], "draft")
        frozen = self.engine.get_bill(bill["id"])
        self.assertEqual(frozen["status"], "signed")
        self.assertEqual(frozen["areas_m2"]["first"], 40.0)
        self.assertEqual(frozen["amount_yuan"], 1.8)

    def test_boundary_revision_applies_only_to_unsigned_bills(self):
        # 作业越出 v1 边界（x∈[0,10]），全部判越界
        points = pass_points("B1", 1, T0, x=11.0, y0=0, y1=10)
        self._ingest(points)
        draft = self.engine.compute_bill("F1", unit_price_per_mu=30.0)
        self.assertEqual(draft["areas_m2"]["first"], 0.0)
        self.assertEqual(draft["areas_m2"]["out_of_bounds"], 24.0)

        # 边界更正为 x∈[0,12]：未签认的草稿自动按新边界重算
        res = self.engine.register_boundary_revision(
            "F1", square_field_m(0, 0, 12, 10), revision=2)
        self.assertEqual(res["superseded_bills"], [draft["id"]])
        self.assertEqual(len(res["recomputed_bills"]), 1)
        recomputed = self.engine.get_bill(res["recomputed_bills"][0])
        self.assertEqual(recomputed["revision"], 2)
        self.assertEqual(recomputed["areas_m2"]["first"], 20.0)
        self.assertEqual(recomputed["areas_m2"]["out_of_bounds"], 4.0)
        self.assertEqual(self.engine.get_bill(draft["id"])["status"], "superseded")

    def test_boundary_revision_leaves_signed_bills_frozen(self):
        points = pass_points("B1", 1, T0, x=11.0, y0=0, y1=10)
        self._ingest(points)
        bill = self.engine.compute_bill("F1", unit_price_per_mu=30.0)
        self.engine.sign_bill(bill["id"], "合作社李会计")

        res = self.engine.register_boundary_revision(
            "F1", square_field_m(0, 0, 12, 10), revision=2)
        self.assertEqual(res["superseded_bills"], [])
        self.assertEqual(res["recomputed_bills"], [])

        frozen = self.engine.get_bill(bill["id"])
        self.assertEqual(frozen["status"], "signed")
        self.assertEqual(frozen["revision"], 1)
        self.assertEqual(frozen["areas_m2"]["out_of_bounds"], 24.0)

    def test_immutable_revision_and_monotonic_numbers(self):
        with self.assertRaises(ValueError):
            self.engine.register_boundary_revision(
                "F1", square_field_m(0, 0, 12, 10), revision=1)  # 版本号不增
        self.engine.register_boundary_revision("F1", square_field_m(0, 0, 12, 10), revision=2)
        with self.assertRaises(ValueError):
            self.engine.register_boundary_revision(
                "F1", square_field_m(0, 0, 14, 10), revision=2)  # 同号不同内容


if __name__ == "__main__":
    unittest.main()
