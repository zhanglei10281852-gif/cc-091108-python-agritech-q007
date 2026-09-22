import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import TEST_CONFIG
from agrisettle import SettlementEngine

DOMAIN = json.loads(
    (Path(__file__).resolve().parents[1] / "reference" / "domain.json").read_text(encoding="utf-8")
)


class ReferenceDataEndToEndTest(unittest.TestCase):
    """用随附的 domain.json 走一遍完整核算流程。

    样例数据只有两个点：第二个点提升器抬起且与前点相隔 31 分钟，
    因此无法构成作业航段，有效面积为零——这正是"按在线时长付款失真"的反面校验。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = SettlementEngine(Path(self.tmp.name) / "state.json", TEST_CONFIG)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reference_track_yields_zero_billable_area(self):
        field = DOMAIN["field"]
        history = {h["revision"]: h for h in DOMAIN["boundary_history"]}
        self.engine.register_field(
            field["id"], field["boundary"],
            revision=field["revision"],
            valid_from=history[field["revision"]]["valid_from"],
        )
        self.engine.set_implement(
            field["id"], DOMAIN["implement"]["id"], DOMAIN["implement"]["working_width_m"])

        res = self.engine.ingest_track(field["id"], DOMAIN["track"])
        self.assertEqual(res["added"], 2)
        self.assertEqual(res["conflicts"], [])

        self.assertEqual(self.engine.segments(field["id"]), [])

        bill = self.engine.compute_bill(field["id"], unit_price_per_mu=38.0)
        self.assertEqual(bill["revision"], 3)
        self.assertEqual(bill["areas_m2"]["first"], 0.0)
        self.assertEqual(bill["amount_yuan"], 0.0)
        self.assertEqual(bill["track_point_count"], 2)

        # 签认后冻结，账单仍可下钻（空覆盖）
        self.engine.sign_bill(bill["id"], "合作社")
        report = self.engine.bill_report(bill["id"])
        self.assertEqual(report["cell_counts"]["first"], 0)


if __name__ == "__main__":
    unittest.main()
