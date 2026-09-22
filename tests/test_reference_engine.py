import json
import unittest
from pathlib import Path

from agri_settle import Engine

DOMAIN = json.loads(
    (Path(__file__).parents[1] / "reference" / "domain.json").read_text(encoding="utf-8")
)


class ReferenceEngineTest(unittest.TestCase):
    """用随附资料跑通全流程:边界修订历史、boot_id/sequence、提升器状态。"""

    def test_domain_data_flows_through_engine(self):
        field = DOMAIN["field"]
        engine = Engine()
        # 边界修订历史:rev2 已失效,rev3 现行(此处 rev2 边界用缩小版示意)
        shrunk = [[lon - 0.001, lat - 0.001] for lon, lat in field["boundary"]]
        shrunk[0] = shrunk[-1] = shrunk[0]
        engine.register_field(field["id"], shrunk, revision=2)
        engine.register_boundary_revision(field["id"], field["revision"], field["boundary"])
        engine.register_implement(
            DOMAIN["implement"]["id"], DOMAIN["implement"]["working_width_m"]
        )
        receipt = engine.upload_points("BEIDOU-1", DOMAIN["track"])
        self.assertEqual(receipt["accepted"], 2)
        # 序号归零:两个启动周期的序号区间各自独立
        status = engine.ingest_status()
        self.assertEqual(status["BEIDOU-1/BOOT-A"], [[991, 991]])
        self.assertEqual(status["BEIDOU-1/BOOT-B"], [[1, 1]])

        report = engine.create_bill(
            "BILL-R",
            field["id"],
            DOMAIN["implement"]["id"],
            rate_per_ha="900",
            policy={"cell_size_m": 2.0},
        )
        self.assertEqual(report["boundary_revision"], 3)
        self.assertEqual(report["stats"]["points_used"], 2)
        # 一点入土、一点提升:连不成作业航段,有效面积为零
        self.assertEqual(report["stats"]["segments"], 0)
        self.assertEqual(report["total_amount"], "0.00")
        breaks = engine.drill_down("BILL-R")["breaks"]
        self.assertTrue(any(b["reason"] == "state" for b in breaks))
        engine.close()


if __name__ == "__main__":
    unittest.main()
