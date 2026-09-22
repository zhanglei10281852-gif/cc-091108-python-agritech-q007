import unittest

from agri_settle import (
    CLS_NECESSARY,
    CLS_REPEATED,
    DomainError,
)

from helpers import line_map, make_engine, rect
from test_coverage import upload_scenario

CELL = {"cell_size_m": 0.5}
RATE = "900"
REDRIVE_RANGE = ["2026-09-11T08:02:00+08:00", "2026-09-11T08:02:36+08:00"]


def fresh_engine_with_bill():
    engine = make_engine()
    upload_scenario(engine)
    report = engine.create_bill("BILL-1", "F1", "R1", RATE, policy=CELL)
    return engine, report


class OverrideTest(unittest.TestCase):
    def test_override_flips_repeated_to_necessary_with_reason(self):
        engine, before = fresh_engine_with_bill()
        after = engine.submit_override(
            "BILL-1",
            selector={"time_range": REDRIVE_RANGE},
            target_class=CLS_NECESSARY,
            reason="合作社要求对该航段补压漏耕,确认为必要重叠",
            author="承包人-张",
        )
        self.assertLess(
            line_map(after)[CLS_REPEATED]["area_m2"],
            line_map(before)[CLS_REPEATED]["area_m2"],
        )
        self.assertGreater(
            line_map(after)[CLS_NECESSARY]["area_m2"],
            line_map(before)[CLS_NECESSARY]["area_m2"],
        )
        self.assertGreater(after["total_amount"], before["total_amount"])
        self.assertEqual(after["overrides"][0]["reason"], "合作社要求对该航段补压漏耕,确认为必要重叠")
        self.assertEqual(after["overrides"][0]["author"], "承包人-张")

    def test_override_requires_reason(self):
        engine, _ = fresh_engine_with_bill()
        with self.assertRaises(DomainError):
            engine.submit_override(
                "BILL-1",
                selector={"time_range": REDRIVE_RANGE},
                target_class=CLS_NECESSARY,
                reason="  ",
                author="承包人-张",
            )

    def test_override_cannot_touch_geometric_facts(self):
        engine, _ = fresh_engine_with_bill()
        with self.assertRaises(DomainError):
            engine.submit_override(
                "BILL-1",
                selector={"time_range": REDRIVE_RANGE},
                target_class="first",  # 首次覆盖是几何事实,不接受裁定
                reason="想改成首次",
                author="承包人-张",
            )

    def test_override_with_unknown_segment_is_rejected_and_rolled_back(self):
        engine, before = fresh_engine_with_bill()
        with self.assertRaises(DomainError):
            engine.submit_override(
                "BILL-1",
                selector={"segments": ["DEV-1:BOOT-A:999:BOOT-A:1000"]},
                target_class=CLS_NECESSARY,
                reason="不存在的航段",
                author="承包人-张",
            )
        after = engine.bill_report("BILL-1")
        self.assertEqual(after["fingerprint"], before["fingerprint"])  # 未留下痕迹
        self.assertEqual(after["overrides"], [])

    def test_override_does_not_mutate_raw_points(self):
        engine, _ = fresh_engine_with_bill()
        before_fp = engine.store.fingerprint("DEV-1")
        engine.submit_override(
            "BILL-1",
            selector={"time_range": REDRIVE_RANGE},
            target_class=CLS_NECESSARY,
            reason="裁定不影响原始点",
            author="承包人-张",
        )
        self.assertEqual(engine.store.fingerprint("DEV-1"), before_fp)


class SignOffTest(unittest.TestCase):
    def test_signed_bill_is_frozen(self):
        engine, before = fresh_engine_with_bill()
        signed = engine.sign_bill("BILL-1", signed_by="合作社-李", at="2026-09-12T10:00:00+08:00")
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(signed["signed_by"], "合作社-李")
        with self.assertRaises(DomainError):
            engine.recompute_bill("BILL-1")
        with self.assertRaises(DomainError):
            engine.submit_override(
                "BILL-1",
                selector={"time_range": REDRIVE_RANGE},
                target_class=CLS_NECESSARY,
                reason="签认后试图改判",
                author="承包人-张",
            )
        with self.assertRaises(DomainError):
            engine.sign_bill("BILL-1", signed_by="合作社-李", at="2026-09-12T11:00:00+08:00")
        # 签认后新到轨迹也不改变账单
        engine.upload_points(
            "DEV-1",
            [{"boot_id": "BOOT-C", "sequence": 1, "at": "2026-09-11T09:00:00+08:00",
              "position": [118.5001, 32.1001], "quality": "fixed",
              "implement_state": "working"}],
        )
        self.assertEqual(engine.bill_report("BILL-1")["fingerprint"], before["fingerprint"])

    def test_boundary_revision_only_applies_to_unsigned_bills(self):
        engine, before = fresh_engine_with_bill()
        engine.create_bill("BILL-2", "F1", "R1", RATE, policy=CELL)
        engine.sign_bill("BILL-1", signed_by="合作社-李", at="2026-09-12T10:00:00+08:00")
        signed_fp = engine.bill_report("BILL-1")["fingerprint"]
        # 边界更正:地块南侧收缩(rev2)
        result = engine.register_boundary_revision("F1", 2, rect(0, 0, 40, 6))
        self.assertEqual(result["recomputed"], ["BILL-2"])  # 只重算未签认的
        self.assertEqual(engine.bill_report("BILL-1")["fingerprint"], signed_fp)
        self.assertEqual(engine.bill_report("BILL-1")["boundary_revision"], 1)
        bill2 = engine.bill_report("BILL-2")
        self.assertEqual(bill2["boundary_revision"], 2)
        self.assertNotEqual(bill2["fingerprint"], before["fingerprint"])
        # 收缩后大部分作业跑到界外:越界面积上升、首次覆盖下降
        self.assertGreater(
            line_map(bill2)["out_of_bounds"]["area_m2"],
            line_map(before)["out_of_bounds"]["area_m2"],
        )
        self.assertLess(
            line_map(bill2)["first"]["area_m2"],
            line_map(before)["first"]["area_m2"],
        )

    def test_revision_number_must_increase(self):
        engine, _ = fresh_engine_with_bill()
        with self.assertRaises(DomainError):
            engine.register_boundary_revision("F1", 1, rect(0, 0, 40, 6))


class DrillDownAfterSignTest(unittest.TestCase):
    def test_drill_down_served_from_frozen_snapshot(self):
        engine, before = fresh_engine_with_bill()
        engine.sign_bill("BILL-1", signed_by="合作社-李", at="2026-09-12T10:00:00+08:00")
        engine.register_boundary_revision("F1", 2, rect(0, 0, 40, 6))
        drill = engine.drill_down("BILL-1", cls="first")
        self.assertEqual(drill["fingerprint"], before["fingerprint"])
        self.assertEqual(drill["boundary_revision"], 1)
        self.assertTrue(drill["cells"])


if __name__ == "__main__":
    unittest.main()
