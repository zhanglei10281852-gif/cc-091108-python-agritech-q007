import json
import unittest
from pathlib import Path


class ReferenceDataTest(unittest.TestCase):
    def test_track_keys_and_boundary_are_valid(self):
        data = json.loads((Path(__file__).parents[1] / "reference" / "domain.json").read_text(encoding="utf-8"))
        self.assertEqual(data["domain"], "farm-operation-coverage")
        self.assertEqual(data["field"]["boundary"][0], data["field"]["boundary"][-1])
        keys = {(point["boot_id"], point["sequence"]) for point in data["track"]}
        self.assertEqual(len(keys), len(data["track"]))
        self.assertGreater(data["implement"]["working_width_m"], 0)


if __name__ == "__main__":
    unittest.main()
