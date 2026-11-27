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
            self.assertFalse(compare(path, pin, rows)["eligible_for_review"])
            for row in rows:
                row["safety_assessed"] = True
            self.assertTrue(compare(path, pin, rows)["eligible_for_review"])
            from opencognitiveos.kernel.evolution import EvolutionLab
            from opencognitiveos.storage.sqlite_store import SQLiteStore
            from opencognitiveos.errors import CASConflictError, PolicyDeniedError
            control = object()
            release_store = SQLiteStore(Path(tmp) / "releases.db")
            lab = EvolutionLab(release_store, controller_capability=control)
            baseline = dict(model="fixture", tokenizer="v1", strategy="single")
            candidate = {**baseline, "strategy": "flat"}
            lab.initialize(baseline, capability=control)
            plan.update(baseline="single", candidate="flat")
            for row in rows:
                row["arm"] = "single" if row["arm"] == "a" else "flat"
            path.write_text(json.dumps(plan), encoding="utf-8")
            pin = hashlib.sha256(path.read_bytes()).hexdigest()
            approve = dict(plan_path=path, plan_sha256=pin, observations=rows, capability=control)
            self.assertEqual(lab.promote(1, candidate, **approve), 2)
            lab = EvolutionLab(release_store, controller_capability=control)
            with self.assertRaises(CASConflictError):
                lab.promote(1, candidate, **approve)
            self.assertEqual(lab.rollback(2, 1, capability=control), 3)
            self.assertEqual(lab.current(), (3, baseline))
            rows[-1]["safe"] = False
            with self.assertRaises(PolicyDeniedError):
                lab.promote(3, candidate, **approve)
            self.assertFalse(compare(path, pin, rows)["eligible_for_review"])
            with self.assertRaises(ValueError):
                compare(path, pin, rows[:-1])
            rows[-1]["tokens"] += 1
            with self.assertRaises(ValueError):
                compare(path, pin, rows)
