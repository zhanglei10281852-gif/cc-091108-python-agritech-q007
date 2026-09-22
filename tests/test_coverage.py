import random
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import T0, make_field, new_engine, pass_points


def three_pass_points():
    """三趟作业：正常一趟、邻接压边一趟（间距 1.4m < 幅宽 2.4m）、一小时后重复碾压一趟。"""
    p1 = pass_points("B1", 1, T0, x=5.0, y0=0, y1=10)
    p2 = pass_points("B1", 101, T0 + timedelta(minutes=10), x=6.4, y0=0, y1=10)
    p3 = pass_points("B1", 201, T0 + timedelta(hours=1), x=5.0, y0=0, y1=10)
    return p1, p2, p3


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = new_engine(Path(self.tmp.name) / "state.json")
        make_field(self.engine)  # 10m x 10m 地块，幅宽 2m，网格 1m

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_pass_first_coverage(self):
        p1, _, _ = three_pass_points()
        self.engine.ingest_track("F1", p1)
        result = self.engine.compute("F1")
        # 幅宽 2.4m 覆盖 x∈[4,6) 两列、y∈[0,10) 十行 = 20 格首次覆盖
        self.assertEqual(result.areas_m2["first"], 20.0)
        self.assertEqual(result.areas_m2["necessary_overlap"], 0.0)
        self.assertEqual(result.areas_m2["repeat"], 0.0)
        # 胶囊体两端各探出半径 1.2m：田外 4 格越界
        self.assertEqual(result.areas_m2["out_of_bounds"], 4.0)
        self.assertEqual(result.field_cell_count, 100)
        self.assertEqual(result.covered_field_cell_count, 20)

    def test_adjacent_pass_necessary_overlap(self):
        p1, p2, _ = three_pass_points()
        self.engine.ingest_track("F1", p1 + p2)
        result = self.engine.compute("F1")
        # 第二趟与第一趟横向重叠 1.0m：x=5 一列 10 格为时间窗内第二次覆盖
        self.assertEqual(result.areas_m2["first"], 40.0)
        self.assertEqual(result.areas_m2["necessary_overlap"], 10.0)
        self.assertEqual(result.areas_m2["repeat"], 0.0)
        # 端帽角格按勾股距判定：第二趟 x=6.4 的田外端帽只覆盖 x∈[5,7) 两列
        self.assertEqual(result.areas_m2["out_of_bounds"], 8.0)

    def test_later_repass_is_repeat_compaction(self):
        p1, p2, p3 = three_pass_points()
        self.engine.ingest_track("F1", p1 + p2 + p3)
        result = self.engine.compute("F1")
        self.assertEqual(result.areas_m2["first"], 40.0)
        self.assertEqual(result.areas_m2["necessary_overlap"], 10.0)
        # 一小时后原线重耕：20 格判重复碾压
        self.assertEqual(result.areas_m2["repeat"], 20.0)
        self.assertEqual(result.areas_m2["out_of_bounds"], 12.0)
        # 首次覆盖面积不因重耕而膨胀
        self.assertEqual(result.covered_field_cell_count, 40)

    def test_out_of_bounds_pass(self):
        points = pass_points("B1", 1, T0, x=-2.0, y0=0, y1=10)
        self.engine.ingest_track("F1", points)
        result = self.engine.compute("F1")
        self.assertEqual(result.areas_m2["first"], 0.0)
        # x∈[-3,-1) 两列、y∈[-1,11] 共 24 格全部越界
        self.assertEqual(result.areas_m2["out_of_bounds"], 24.0)
        self.assertEqual(result.covered_field_cell_count, 0)

    def test_determinism_under_shuffled_ingest(self):
        p1, p2, p3 = three_pass_points()
        points = p1 + p2 + p3

        self.engine.ingest_track("F1", points)
        expected = self.engine.compute("F1").areas_m2

        for trial in range(3):
            shuffled = list(points)
            random.Random(trial).shuffle(shuffled)
            other = new_engine(Path(self.tmp.name) / f"shuffled-{trial}.json")
            make_field(other)
            # 随机顺序、随机分批、夹带重复上传
            other.ingest_track("F1", shuffled[:20])
            other.ingest_track("F1", shuffled[10:])
            other.ingest_track("F1", shuffled[:15])
            self.assertEqual(other.compute("F1").areas_m2, expected)


if __name__ == "__main__":
    unittest.main()
