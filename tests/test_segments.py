import unittest

from agri_settle import Policy, TrackStore
from agri_settle.segments import build_segments

from helpers import FRAME, make_points, rect


def build(raw_by_device, barriers=None, policy=None):
    store = TrackStore()
    for device, pts in raw_by_device.items():
        store.upload(device, pts)
    points = store.points()
    points_xy = [(p, *FRAME.to_xy(p.lon, p.lat)) for p in points]
    barriers_xy = [
        (bid, [FRAME.to_xy(lon, lat) for lon, lat in ring])
        for bid, ring in (barriers or {}).items()
    ]
    return build_segments(points_xy, policy or Policy(), barriers_xy)


class SegmentsTest(unittest.TestCase):
    def test_raised_implement_breaks_run(self):
        pts = make_points("B", [(0, 0), (1, 0), (2, 0, "fixed", "raised"), (3, 0)])
        segments, breaks = build({"D": pts})
        self.assertEqual(len(segments), 1)  # 只有 (0,0)-(1,0)
        self.assertTrue(any(b.reason == "state" for b in breaks))

    def test_time_gap_is_not_interpolated(self):
        pts = make_points("B", [(0, 0), (1, 0), (2, 0, "fixed", "working", 120.0),
                                (3, 0)])
        segments, breaks = build({"D": pts})
        self.assertEqual(len(segments), 2)  # 断档两侧各自成段
        self.assertTrue(any(b.reason == "gap" for b in breaks))

    def test_position_jump_is_not_interpolated(self):
        pts = make_points("B", [(0, 0), (1, 0), (80, 0), (81, 0)])
        segments, breaks = build({"D": pts})
        self.assertEqual(len(segments), 2)
        self.assertTrue(any(b.reason == "jump" for b in breaks))

    def test_invalid_quality_breaks_run(self):
        pts = make_points("B", [(0, 0), (1, 0, "invalid"), (2, 0)])
        segments, breaks = build({"D": pts})
        self.assertEqual(len(segments), 0)
        self.assertTrue(any(b.reason == "quality" for b in breaks))

    def test_drift_cannot_interpolate_across_river(self):
        # 河沟 x∈[19,21];正常作业到 x=18,漂移点跳到对岸 (26,10),随后回到 x=22
        river = {"RIVER-1": rect(19, -1, 21, 21)}
        pts = make_points(
            "B",
            [(16, 10), (17, 10), (18, 10), (26, 10), (22, 10), (23, 10)],
        )
        segments, breaks = build({"D": pts}, barriers=river)
        barrier_breaks = [b for b in breaks if b.reason == "barrier"]
        self.assertTrue(barrier_breaks)
        for seg in segments:
            # 没有任何航段伸入河沟 x∈[19,21](所有点 y=10,位于河道纵跨范围内)
            lo, hi = min(seg.ax, seg.bx), max(seg.ax, seg.bx)
            self.assertFalse(lo < 21 and hi > 19, f"航段 {seg.seg_id} 伸入河沟")

    def test_segments_never_chain_across_devices(self):
        # 两台机具同时作业、点位交错到达:行程各建各的,不跨设备连线
        from helpers import pass_along_x

        pts1 = pass_along_x("BOOT-A", y=5.0, seq0=1)
        pts2 = pass_along_x("BOOT-A", y=10.0, seq0=1)
        segments, _ = build({"DEV-1": pts1, "DEV-2": pts2})
        self.assertEqual(len(segments), 2)  # 每台设备合并为一条行程
        for seg in segments:
            self.assertAlmostEqual(seg.length_m, 36.0, delta=0.01)

    def test_segment_ids_are_deterministic(self):
        pts = make_points("B", [(0, 0), (1, 0), (2, 0)])
        s1, _ = build({"D": pts})
        s2, _ = build({"D": list(reversed(pts))})  # 上传顺序不同
        self.assertEqual([s.seg_id for s in s1], [s.seg_id for s in s2])


if __name__ == "__main__":
    unittest.main()
