import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import T0, make_field, new_engine, pt


class SegmentBuildingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = new_engine(Path(self.tmp.name) / "state.json")
        make_field(self.engine, boundary=None, width=2.0)

    def tearDown(self):
        self.tmp.cleanup()

    def _segments(self, points):
        self.engine.ingest_track("F1", points)
        return self.engine.segments("F1")

    def test_normal_chain(self):
        points = [pt("B1", k, T0 + timedelta(seconds=2 * k), 5, k) for k in range(4)]
        self.assertEqual(len(self._segments(points)), 3)

    def test_raised_implement_breaks_chain(self):
        # 提升器抬起（转场/掉头）的点不成段，且切断链条
        points = [
            pt("B1", 1, T0, 5, 0),
            pt("B1", 2, T0 + timedelta(seconds=2), 5, 1, state="raised"),
            pt("B1", 3, T0 + timedelta(seconds=4), 5, 2),
        ]
        self.assertEqual(len(self._segments(points)), 0)

    def test_invalid_quality_breaks_chain_no_interpolation(self):
        # 河沟两侧定位漂移（无效解）：不允许凭插值跨沟连成一段
        points = [pt("B1", k + 1, T0 + timedelta(seconds=2 * k), 5, k) for k in range(6)]
        points += [
            pt("B1", 7, T0 + timedelta(seconds=12), 5, 6, quality="invalid"),
            pt("B1", 8, T0 + timedelta(seconds=14), 5, 7, quality="invalid"),
            pt("B1", 9, T0 + timedelta(seconds=16), 5, 8, quality="invalid"),
        ]
        points += [pt("B1", 10 + k, T0 + timedelta(seconds=18 + 2 * k), 5, 9 + k)
                   for k in range(6)]
        segments = self._segments(points)
        self.assertEqual(len(segments), 10)  # 沟两侧各 5 段，中间无桥接

        result = self.engine.compute("F1")
        covered_rows = {j for (i, j) in result.cells}
        # 沟面（第 6、7 行）无覆盖；第 8 行在下游端点幅宽端帽内，属正常作业幅面
        self.assertFalse(any(6 <= j <= 7 for j in covered_rows))

    def test_long_distance_gap_not_bridged(self):
        # 时间连续但距离突变（断点续传缺口/漂移）：不成段
        points = [
            pt("B1", 1, T0, 5, 0),
            pt("B1", 2, T0 + timedelta(seconds=10), 5, 100),
            pt("B1", 3, T0 + timedelta(seconds=12), 5, 101),
        ]
        self.assertEqual(len(self._segments(points)), 1)

    def test_long_time_gap_not_bridged(self):
        points = [
            pt("B1", 1, T0, 5, 0),
            pt("B1", 2, T0 + timedelta(seconds=120), 5, 1),
            pt("B1", 3, T0 + timedelta(seconds=122), 5, 2),
        ]
        self.assertEqual(len(self._segments(points)), 1)

    def test_zero_length_segment_skipped(self):
        points = [
            pt("B1", 1, T0, 5, 0),
            pt("B1", 2, T0 + timedelta(seconds=2), 5, 0),  # 原地不动不成段
            pt("B1", 3, T0 + timedelta(seconds=4), 5, 1),
        ]
        self.assertEqual(len(self._segments(points)), 1)


if __name__ == "__main__":
    unittest.main()
