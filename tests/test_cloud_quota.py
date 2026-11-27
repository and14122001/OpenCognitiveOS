import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from opencognitiveos.contracts.models import DataClass, InjectableClock, TaskSpec, TaskStatus
from opencognitiveos.errors import BudgetExceededError, PolicyDeniedError
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.providers.cloud_quota import CloudQuotaLedger
from opencognitiveos.storage.sqlite_store import SQLiteStore


class CloudQuotaTests(unittest.TestCase):
    def test_shared_pool_replay_and_unknown_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = InjectableClock()
            store = SQLiteStore(Path(tmp) / "quota.db", clock=clock)
            store.migrate()
            auth = ExecutionAuthority(store, clock=clock)
            controller = object()
            ledger = CloudQuotaLedger(auth, controller_capability=controller)
            template = TaskSpec("p", "r", "a", "goal", "hash", data_class=DataClass.PUBLIC,
                                allowed_providers=("test-provider",), cloud_approved=True)
            calls = []
            for task_id in ("a", "b"):
                task = replace(template, task_id=task_id)
                rec, _ = auth.submit_task(task)
                ledger.grant(capability=controller, task=rec.unique_key, provider="test-provider",
                             pool="shared", pool_tokens=10, task_tokens=10, expires="2099-01-01T00:00:00Z")
                for status in (TaskStatus.PLANNED, TaskStatus.READY):
                    rec = auth.transition_task(rec.unique_key, status, expected_revision=rec.revision,
                                               actor_role="controller")
                lease = auth.acquire_lease(rec.unique_key, worker_id="w")
                auth.transition_task(rec.unique_key, TaskStatus.RUNNING, expected_revision=rec.revision,
                                     worker_id="w", execution_id=lease.execution_id, epoch=lease.fencing_epoch)
                calls.append(dict(capability=controller, task=rec.unique_key, provider="test-provider", request_id="1",
                                  max_tokens=6, guaranteed_cost_microusd=0, worker_id="w",
                                  execution_id=lease.execution_id, epoch=lease.fencing_epoch))
            with self.assertRaises(PolicyDeniedError):
                ledger.reserve(**{**calls[0], "capability": object()})
            with self.assertRaises(PolicyDeniedError):
                ledger.reserve(**{**calls[0], "guaranteed_cost_microusd": None})
            self.assertTrue(ledger.reserve(**calls[0]))
            self.assertFalse(ledger.reserve(**calls[0]))
            with self.assertRaises(BudgetExceededError):
                ledger.reserve(**calls[1])
