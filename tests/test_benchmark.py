import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from opencognitiveos.verification.benchmark import compare


class BenchmarkTests(unittest.TestCase):
    def test_paired_budget_and_safety_gate(self):
        plan = dict(tasks=[str(n) for n in range(30)], seeds=[1, 2], baseline="a", candidate="b",
                    mode="tokens", limit=100, model="fixture")
        rows = [{"task": t, "seed": s, "arm": arm, "model": "fixture", "pass": arm == "b", "safe": True,
                 "tokens": 100, "wall_ms": 7, "message_bytes": 0, "ram_bytes": 20}
                for t in plan["tasks"] for s in plan["seeds"] for arm in "ab"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            pin = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertTrue(compare(path, pin, rows)["eligible_for_review"])
            rows[-1]["safe"] = False
            self.assertFalse(compare(path, pin, rows)["eligible_for_review"])
            with self.assertRaises(ValueError):
                compare(path, pin, rows[:-1])
            rows[-1]["tokens"] += 1
            with self.assertRaises(ValueError):
                compare(path, pin, rows)
