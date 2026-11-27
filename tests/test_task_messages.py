import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from opencognitiveos.contracts.models import Budget, InjectableClock, TaskSpec, TaskStatus
from opencognitiveos.errors import CASConflictError, PolicyDeniedError
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.kernel.messages import TaskMessages
from opencognitiveos.storage.sqlite_store import SQLiteStore


class RoutingTests(unittest.TestCase):
    def test_scoped_delivery_restart_and_revocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = InjectableClock()
            store = SQLiteStore(Path(tmp) / "bus.db", clock=clock)
            store.migrate()
            auth = ExecutionAuthority(store, clock=clock)
            control = object()
            bus = TaskMessages(auth, controller_capability=control)
            root = TaskSpec("project", "repo", "root", "plan", "hash")
            auth.submit_task(root)
            members = []
            for name in ("a", "b"):
                task = replace(root, task_id=name, parent_id="root", budget=Budget(max_steps=2, max_retries=0))
                rec, _ = auth.submit_task(task)
                for status in (TaskStatus.PLANNED, TaskStatus.READY):
                    rec = auth.transition_task(task.unique_key, status, expected_revision=rec.revision, actor_role="controller")
                lease = auth.acquire_lease(task.unique_key, name, ttl_seconds=600)
                fence = dict(worker_id=name, execution_id=lease.execution_id, epoch=lease.fencing_epoch)
                auth.transition_task(task.unique_key, TaskStatus.RUNNING, expected_revision=rec.revision, **fence)
                members.append((task.unique_key, fence))
            (a, sender), (b, recipient) = members
            from opencognitiveos.kernel.inference import InferenceCoordinator
            from opencognitiveos.errors import BudgetExceededError

            slots = InferenceCoordinator(auth, controller_capability=control)
            from opencognitiveos.kernel.evolution import EvolutionLab
            lab = EvolutionLab(store, controller_capability=control)
            lab.initialize({"model":"local-fixture","tokenizer":"v1","strategy":"single"}, capability=control)
            slots.allocate_current(root.unique_key, "policy", 10, capability=control)
            rid, snap = slots.begin(a, request_id="first", max_tokens=7, **sender)
            self.assertEqual(snap["task_key"], a)
            self.assertEqual(snap["release_generation"], 1)
            slots = InferenceCoordinator(ExecutionAuthority(store, clock=clock), controller_capability=control)
            with self.assertRaises(BudgetExceededError):
                slots.begin(b, request_id="second", max_tokens=2, **recipient)
            slots.settle(rid, None, capability=control)
            with self.assertRaises(BudgetExceededError):
                slots.begin(b, request_id="second", max_tokens=4, **recipient)
            rid, _ = slots.begin(b, request_id="second", max_tokens=3, **recipient)
            slots.settle(rid, 3, capability=control)
            version = bus.replace_routes(root.unique_key, 0, [(a,b)], capability=control)
            args = dict(root=root.unique_key, version=version, sender=a, recipient=b, kind="proposal", body="review",
                        sequence=1, **sender)
            digest = bus.publish(**args)
            with self.assertRaises(CASConflictError):
                bus.publish(**args)
            with self.assertRaises(PolicyDeniedError):
                bus.publish(**{**args, "sender": b, "recipient": a, **recipient})
            restarted = TaskMessages(ExecutionAuthority(store, clock=clock), controller_capability=control)
            query = dict(root=root.unique_key, recipient=b, **recipient)
            self.assertEqual([x["digest"] for x in restarted.inbox(**query)], [digest])
            restarted.acknowledge(**query, source_worker_task=a, source_execution=sender["execution_id"],
                                  sequence=1, digest=digest)
            self.assertEqual(bus.inbox(**query), [])
            with self.assertRaises(CASConflictError):
                restarted.replace_routes(root.unique_key, 0, [], capability=control)
            restarted.replace_routes(root.unique_key, version, [], capability=control)
            self.assertEqual(restarted.inbox(**query), [])
            with self.assertRaises(PolicyDeniedError):
                bus.publish(**{**args, "sequence": 2})
            root_rec = auth.get_task(root.unique_key)
            auth.cancel_task(root.unique_key, expected_revision=root_rec.revision)
            for member, _ in members:
                self.assertEqual(auth.get_task(member).status, TaskStatus.CANCELLED)
