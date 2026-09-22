import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import T0, make_field, new_engine, pass_points, pt


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "state.json"
        self.engine = new_engine(self.store)
        make_field(self.engine)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedup_and_conflict_on_reupload(self):
        points = pass_points("BOOT-A", 1, T0, x=5, y0=0, y1=5)
        first = self.engine.ingest_track("F1", points)
        self.assertEqual(first["added"], 6)
        self.assertEqual(first["duplicates"], 0)

        again = self.engine.ingest_track("F1", points)
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["duplicates"], 6)
        self.assertEqual(again["total"], 6)

        # 同主键不同内容：保留先到的记录，并如实报告冲突
        tampered = dict(points[2], position=[points[2]["position"][0] + 0.001,
                                             points[2]["position"][1]])
        res = self.engine.ingest_track("F1", [tampered])
        self.assertEqual(res["conflicts"], [f"{points[2]['boot_id']}#{points[2]['sequence']}"])
        self.assertEqual(self.engine.track_points("F1")[2], points[2])

    def test_batch_upload_equals_single_shot(self):
        batch1 = pass_points("BOOT-A", 1, T0, x=5, y0=0, y1=10)
        batch2 = pass_points("BOOT-A", 101, T0 + timedelta(minutes=10), x=6.5, y0=0, y1=10)

        self.engine.ingest_track("F1", batch1)
        self.engine.ingest_track("F1", batch2)
        self.engine.ingest_track("F1", batch1)  # 重复上传
        areas_batched = self.engine.compute("F1").areas_m2

        other = new_engine(Path(self.tmp.name) / "one-shot.json")
        make_field(other)
        other.ingest_track("F1", batch1 + batch2)
        areas_oneshot = other.compute("F1").areas_m2

        self.assertEqual(areas_batched, areas_oneshot)

    def test_restart_persistence(self):
        self.engine.ingest_track("F1", pass_points("BOOT-A", 1, T0, x=5, y0=0, y1=10))
        bill = self.engine.compute_bill("F1", unit_price_per_mu=30.0)

        reopened = new_engine(self.store)  # 模拟服务重启
        self.assertEqual(reopened.track_points("F1"), self.engine.track_points("F1"))
        bill2 = reopened.compute_bill("F1", unit_price_per_mu=30.0)
        self.assertEqual(bill2["id"], bill["id"])
        self.assertEqual(bill2["areas_m2"], bill["areas_m2"])

    def test_sequence_reset_across_boots(self):
        # 终端重启后序号归零：BOOT-A 与 BOOT-B 的序号各自从 1 开始
        pts_a = pass_points("BOOT-A", 1, T0, x=5, y0=0, y1=2)
        pts_b = pass_points("BOOT-B", 1, T0 + timedelta(seconds=6), x=5, y0=3, y1=5)
        res = self.engine.ingest_track("F1", pts_a + pts_b)
        self.assertEqual(res["added"], 6)  # (boot_id, sequence) 联合主键，互不覆盖

        segments = self.engine.segments("F1")
        self.assertEqual(len(segments), 5)  # 6 个点连成 5 段，重启本身不断链
        # 幅宽 2.4m：x∈[4,6) 两列 × y∈[0,6) 六行（端帽探入第 6 行）= 12 格
        self.assertEqual(self.engine.compute("F1").areas_m2["first"], 12.0)


if __name__ == "__main__":
    unittest.main()
