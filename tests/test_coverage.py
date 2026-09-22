import unittest

from agri_settle import (
    CLS_FIRST,
    CLS_NECESSARY,
    CLS_OUT_OF_BOUNDS,
    CLS_REPEATED,
)

from helpers import FRAME, line_map, make_engine, make_points, pass_along_x, rect

CELL = {"cell_size_m": 0.5}
RATE = "900"  # 元/公顷


def hop(boot, x, y, at, seq0):
    """提升器抬起状态下的转场点。"""
    return make_points(boot, [(x, y, "fixed", "raised")], start=at, seq0=seq0)


def upload_scenario(engine, device="DEV-1"):
    """两条平行行程(必要重叠) + 一次重跑(重复碾压) + 一次斜穿(重复碾压)
    + 界外空驶(越界) + 提升器抬起的道路转场(不产生覆盖)。"""
    pts = []
    pts += pass_along_x("BOOT-A", y=5.0, start="2026-09-11T08:00:00+08:00", seq0=1)
    pts += hop("BOOT-A", 2, 6.92, "2026-09-11T08:00:50+08:00", seq0=100)
    pts += pass_along_x("BOOT-A", y=6.92, start="2026-09-11T08:01:00+08:00", seq0=101)
    pts += hop("BOOT-A", 2, 5.1, "2026-09-11T08:01:50+08:00", seq0=200)
    pts += pass_along_x("BOOT-A", y=5.1, start="2026-09-11T08:02:00+08:00", seq0=201)
    pts += hop("BOOT-A", 10, 2, "2026-09-11T08:02:50+08:00", seq0=300)
    # 斜穿已覆盖区域:航向约 50°,与平行行程交叉
    diag = [(10 + i * 10 / 12, 2 + i * 12 / 12) for i in range(13)]
    pts += make_points("BOOT-A", diag, start="2026-09-11T08:03:00+08:00", seq0=301)
    pts += hop("BOOT-A", 2, 23, "2026-09-11T08:03:50+08:00", seq0=400)
    pts += pass_along_x("BOOT-A", y=23.0, start="2026-09-11T08:04:00+08:00", seq0=401)
    # 道路转场:提升器抬起,长距离移动,不产生任何覆盖
    pts += make_points(
        "BOOT-A",
        [(x, 30.0, "fixed", "raised") for x in range(2, 39)],
        start="2026-09-11T08:05:00+08:00",
        seq0=500,
    )
    return engine.upload_points(device, pts)


class CoverageClassificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()
        upload_scenario(cls.engine)
        cls.report = cls.engine.create_bill(
            "BILL-1", "F1", "R1", RATE, policy=CELL
        )
        cls.lines = line_map(cls.report)

    def test_all_four_classes_present(self):
        for cls in (CLS_FIRST, CLS_NECESSARY, CLS_REPEATED, CLS_OUT_OF_BOUNDS):
            self.assertGreater(self.lines[cls]["area_m2"], 0, cls)

    def test_adjacent_pass_overlap_is_necessary(self):
        # y=6.92 行程与 y=5.0 行程横向重叠约 0.48m,不超过阈值 → 必要重叠
        necessary = self.lines[CLS_NECESSARY]["area_m2"]
        repeated = self.lines[CLS_REPEATED]["area_m2"]
        self.assertGreater(necessary, 0)
        self.assertLess(necessary, repeated)  # 重跑+斜穿的重复碾压占大头

    def test_raised_transit_produces_no_coverage(self):
        # 越界只能来自 y=23 的界外行程;y=30 的转场(提升器抬起)不产生任何覆盖
        drill = self.engine.drill_down("BILL-1", cls=CLS_OUT_OF_BOUNDS)
        self.assertTrue(drill["cells"])
        for cell in drill["cells"]:
            _, y = FRAME.to_xy(cell["lon"], cell["lat"])
            self.assertTrue(
                20.5 < y < 25.0, f"越界应只来自 y=23 的界外行程, 实际 y={y:.2f}"
            )

    def test_payable_is_first_plus_necessary(self):
        payable = self.report["total_payable_area_m2"]
        expect = (
            self.lines[CLS_FIRST]["area_m2"] + self.lines[CLS_NECESSARY]["area_m2"]
        )
        self.assertAlmostEqual(payable, expect, places=4)
        self.assertFalse(self.lines[CLS_REPEATED]["payable"])
        self.assertFalse(self.lines[CLS_OUT_OF_BOUNDS]["payable"])

    def test_amount_matches_rate(self):
        # 应付 = 有效面积(公顷) × 单价,保留到分
        from decimal import Decimal

        expect = (
            Decimal(str(self.report["total_payable_area_m2"]))
            / Decimal(10000)
            * Decimal(RATE)
        ).quantize(Decimal("0.01"))
        self.assertEqual(self.report["total_amount"], str(expect))

    def test_drill_down_from_amount_to_cells_to_track_points(self):
        drill = self.engine.drill_down("BILL-1", cls=CLS_FIRST)
        self.assertEqual(drill["total_amount"], self.report["total_amount"])
        self.assertTrue(drill["cells"])
        for cell in drill["cells"][:50]:
            self.assertEqual(cell["cls"], CLS_FIRST)
            seg = drill["segments"][cell["seg_id"]]
            self.assertGreaterEqual(len(seg["points"]), 2)
            for raw in seg["points"]:
                # 下钻到原始定位点:坐标、质量、提升器状态都在
                self.assertIn("position", raw)
                self.assertIn("quality", raw)
                self.assertIn("implement_state", raw)
        # 首次覆盖单元格数与账单行一致
        self.assertEqual(len(drill["cells"]), int(self.lines[CLS_FIRST]["events"]))


class BarrierCoverageTest(unittest.TestCase):
    def test_river_cells_excluded_and_no_interpolation_across(self):
        river = {"RIVER-1": rect(19, -1, 21, 21)}
        engine = make_engine(barriers=river)
        pts = pass_along_x("BOOT-A", y=10.0)
        # 一个漂到对岸的坏点:不能凭插值跨河
        pts += make_points(
            "BOOT-A", [(26, 10)], start="2026-09-11T08:00:40+08:00", seq0=100
        )
        engine.upload_points("DEV-1", pts)
        report = engine.create_bill("B1", "F1", "R1", RATE, policy=CELL)
        breaks = engine.drill_down("B1")["breaks"]
        self.assertTrue(any(b["reason"] == "barrier" for b in breaks))
        drill = engine.drill_down("B1", cls=CLS_FIRST)
        for cell in drill["cells"]:
            x, _ = FRAME.to_xy(cell["lon"], cell["lat"])
            self.assertFalse(19.0 <= x <= 21.0, "河沟内的单元格不应被覆盖")
        # 河沟占据的单元格不计入可覆盖,也不算漏耕
        stats = report["stats"]
        full_coverable = 80 * 40  # 40m×20m ÷ 0.25m²
        self.assertLess(stats["coverable_cells"], full_coverable)


if __name__ == "__main__":
    unittest.main()
