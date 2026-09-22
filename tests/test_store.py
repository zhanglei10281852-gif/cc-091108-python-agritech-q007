import unittest

from agri_settle import TrackStore

from helpers import make_points


class TrackStoreTest(unittest.TestCase):
    def test_duplicate_upload_is_idempotent(self):
        store = TrackStore()
        pts = make_points("BOOT-A", [(0, 0), (1, 0), (2, 0)])
        r1 = store.upload("DEV-1", pts)
        r2 = store.upload("DEV-1", pts)
        self.assertEqual(r1.accepted, 3)
        self.assertEqual(r2.accepted, 0)
        self.assertEqual(r2.duplicates, 3)
        self.assertEqual(len(store), 3)

    def test_sequence_reset_after_reboot_does_not_collide(self):
        store = TrackStore()
        a = make_points("BOOT-A", [(0, 0), (1, 0)], seq0=98)
        b = make_points("BOOT-B", [(2, 0), (3, 0)], start="2026-09-11T08:10:00+08:00")
        store.upload("DEV-1", a)
        receipt = store.upload("DEV-1", b)
        self.assertEqual(receipt.accepted, 2)  # 序号归零但 boot_id 不同,不判重
        self.assertEqual(len(store), 4)

    def test_conflicting_reupload_keeps_first(self):
        store = TrackStore()
        pts = make_points("BOOT-A", [(0, 0), (1, 0)])
        store.upload("DEV-1", pts)
        tampered = [dict(p) for p in pts]
        tampered[0]["position"] = [tampered[0]["position"][0] + 0.001,
                                   tampered[0]["position"][1]]
        receipt = store.upload("DEV-1", tampered)
        self.assertEqual(len(receipt.conflicts), 1)
        self.assertEqual(receipt.duplicates, 1)
        # 原始点不被改写
        kept = store.points("DEV-1")
        self.assertEqual(kept[0].lon, pts[0]["position"][0])

    def test_canonical_order_independent_of_arrival(self):
        early = make_points("BOOT-A", [(0, 0), (1, 0)])
        late = make_points("BOOT-B", [(2, 0), (3, 0)],
                           start="2026-09-11T08:05:00+08:00")
        s1, s2 = TrackStore(), TrackStore()
        s1.upload("D", early)
        s1.upload("D", late)
        s2.upload("D", late)  # 后到的批次先传:断点续传乱序到达
        s2.upload("D", early)
        self.assertEqual([p.canonical() for p in s1.points("D")],
                         [p.canonical() for p in s2.points("D")])
        self.assertEqual(s1.fingerprint("D"), s2.fingerprint("D"))

    def test_ingest_status_ranges_support_resume(self):
        store = TrackStore()
        pts = (make_points("BOOT-A", [(x, 0) for x in range(3)], seq0=1)
               + make_points("BOOT-A", [(x, 0) for x in range(3)], seq0=5,
                             start="2026-09-11T08:10:00+08:00"))
        store.upload("D", pts)
        self.assertEqual(store.ingest_status()["D/BOOT-A"], [[1, 3], [5, 7]])

    def test_rejected_points_do_not_crash_ingest(self):
        store = TrackStore()
        receipt = store.upload("D", [{"boot_id": "B", "sequence": 1}])  # 缺字段
        self.assertEqual(receipt.accepted, 0)
        self.assertEqual(len(receipt.rejected), 1)
        bad_quality = make_points("B", [(0, 0, "unknown-quality")])
        receipt = store.upload("D", bad_quality)
        self.assertEqual(len(receipt.rejected), 1)


if __name__ == "__main__":
    unittest.main()
