import os
import tempfile
import unittest

from agri_settle import Engine

from helpers import make_engine, make_points, pass_along_x, rect
from test_coverage import upload_scenario

CELL = {"cell_size_m": 0.5}
RATE = "900"


def scenario_points():
    """含终端重启(序号归零)的两条行程。"""
    pts = []
    pts += pass_along_x("BOOT-A", y=5.0, start="2026-09-11T08:00:00+08:00", seq0=991)
    pts += make_points(
        "BOOT-A", [(2, 6.92, "fixed", "raised")],
        start="2026-09-11T08:00:50+08:00", seq0=2000,
    )
    # 终端重启,序号从 1 重新计数
    pts += pass_along_x("BOOT-B", y=6.92, start="2026-09-11T08:01:00+08:00", seq0=1)
    return pts


class IdempotencyTest(unittest.TestCase):
    def _bill_fp(self, engine):
        return engine.create_bill("BILL-X", "F1", "R1", RATE, policy=CELL)["fingerprint"]

    def test_batched_duplicate_and_reordered_uploads_give_same_area(self):
        pts = scenario_points()
        # 一次性上传
        e1 = make_engine()
        e1.upload_points("DEV-1", pts)
        # 分三批乱序上传,且其中一批重复传了两次(断点续传重发)
        e2 = make_engine()
        third = len(pts) // 3
        batches = [pts[:third], pts[third:2 * third], pts[2 * third:]]
        e2.upload_points("DEV-1", batches[2])
        e2.upload_points("DEV-1", batches[0])
        e2.upload_points("DEV-1", batches[0])  # 重发
        e2.upload_points("DEV-1", batches[1])
        self.assertEqual(e1.store.fingerprint("DEV-1"), e2.store.fingerprint("DEV-1"))
        self.assertEqual(self._bill_fp(e1), self._bill_fp(e2))
        # 有效面积一致
        r1 = e1.bill_report("BILL-X")
        r2 = e2.bill_report("BILL-X")
        self.assertEqual(r1["total_payable_area_m2"], r2["total_payable_area_m2"])
        self.assertEqual(r1["total_amount"], r2["total_amount"])

    def test_multi_device_interleaved_uploads_give_same_result(self):
        # 两台设备同刻作业、交错分批到达:结果与到达顺序无关
        from helpers import pass_along_x

        d1 = pass_along_x("BOOT-A", y=5.0, seq0=1)
        d2 = pass_along_x("BOOT-A", y=10.0, seq0=1)
        e1 = make_engine()
        e1.upload_points("DEV-1", d1)
        e1.upload_points("DEV-2", d2)
        e2 = make_engine()
        e2.upload_points("DEV-2", d2[:10])
        e2.upload_points("DEV-1", d1[20:])
        e2.upload_points("DEV-2", d2[10:])
        e2.upload_points("DEV-1", d1[:20])
        e2.upload_points("DEV-1", d1)  # 全量重发
        fp1 = self._bill_fp(e1)
        fp2 = self._bill_fp(e2)
        self.assertEqual(fp1, fp2)

    def test_service_restart_replay_gives_same_result(self):
        pts = scenario_points()
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "events.jsonl")
            engine = Engine(log_path=log)
            engine.register_field("F1", rect(0, 0, 40, 20))
            engine.register_implement("R1", 2.4)
            engine.upload_points("DEV-1", pts[:40])
            engine.upload_points("DEV-1", pts[40:])
            engine.create_bill("BILL-X", "F1", "R1", RATE, policy=CELL)
            engine.submit_override(
                "BILL-X",
                selector={"time_range": ["2026-09-11T08:01:00+08:00",
                                         "2026-09-11T08:01:36+08:00"]},
                target_class="repeated_compaction",
                reason="合作社认定第二行程为重压",
                author="合作社-李",
            )
            engine.sign_bill("BILL-X", signed_by="合作社-李",
                             at="2026-09-12T10:00:00+08:00")
            before = engine.bill_report("BILL-X")
            engine.close()
            # 服务重启:重放事件日志
            restored = Engine.restore(log)
            after = restored.bill_report("BILL-X")
            restored.close()
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(after["status"], "signed")
        self.assertEqual(after["total_amount"], before["total_amount"])
        self.assertEqual(after["overrides"], before["overrides"])

    def test_recompute_is_stable(self):
        engine = make_engine()
        upload_scenario(engine)
        r1 = engine.create_bill("BILL-1", "F1", "R1", RATE, policy=CELL)
        r2 = engine.recompute_bill("BILL-1")
        self.assertEqual(r1["fingerprint"], r2["fingerprint"])


if __name__ == "__main__":
    unittest.main()
