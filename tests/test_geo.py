import math
import unittest

from agri_settle.geo import (
    GridSpec,
    LocalFrame,
    point_in_polygon,
    point_line_distance,
    point_segment_distance,
    polygon_centroid,
    segment_crosses_polygon,
    segments_intersect,
)

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]


class GeoTest(unittest.TestCase):
    def test_frame_round_trip(self):
        frame = LocalFrame(118.5, 32.1)
        lon, lat = 118.503, 32.104
        x, y = frame.to_xy(lon, lat)
        lon2, lat2 = frame.to_lonlat(x, y)
        self.assertAlmostEqual(lon, lon2, places=10)
        self.assertAlmostEqual(lat, lat2, places=10)
        # 纬度方向 0.001 度约 111 米
        _, y1 = frame.to_xy(118.5, 32.101)
        self.assertAlmostEqual(y1, 111.19, delta=0.5)

    def test_point_in_polygon(self):
        self.assertTrue(point_in_polygon(5, 5, SQUARE))
        self.assertTrue(point_in_polygon(0, 5, SQUARE))  # 边界算内部
        self.assertFalse(point_in_polygon(10.5, 5, SQUARE))
        self.assertFalse(point_in_polygon(-1, -1, SQUARE))

    def test_segments_intersect(self):
        self.assertTrue(segments_intersect((0, 0), (4, 4), (0, 4), (4, 0)))
        self.assertFalse(segments_intersect((0, 0), (1, 1), (2, 0), (3, 1)))
        self.assertTrue(segments_intersect((0, 0), (4, 0), (4, 0), (4, 4)))  # 端点接触

    def test_segment_crosses_polygon(self):
        self.assertTrue(segment_crosses_polygon((-1, 5), (5, 5), SQUARE))
        self.assertTrue(segment_crosses_polygon((2, 2), (8, 8), SQUARE))
        self.assertFalse(segment_crosses_polygon((-3, -3), (-1, -1), SQUARE))

    def test_distances(self):
        self.assertAlmostEqual(point_segment_distance((0, 3), (0, 0), (4, 0)), 3.0)
        self.assertAlmostEqual(point_segment_distance((9, 3), (0, 0), (4, 0)), math.hypot(5, 3))
        self.assertAlmostEqual(point_line_distance((0, 3), (0, 0), (4, 0)), 3.0)

    def test_centroid_ignores_closing_point(self):
        cx, cy = polygon_centroid(SQUARE)
        self.assertEqual((cx, cy), (5.0, 5.0))

    def test_capsule_cells(self):
        grid = GridSpec(1.0)
        cells = set(grid.capsule_cells((0.5, 0.5), (4.5, 0.5), 1.0))
        self.assertIn((0, 0), cells)
        self.assertIn((4, 0), cells)
        self.assertIn((2, 1), cells)  # 中心距线段 0.5m
        self.assertNotIn((2, 3), cells)  # 中心距线段 2.5m


if __name__ == "__main__":
    unittest.main()
