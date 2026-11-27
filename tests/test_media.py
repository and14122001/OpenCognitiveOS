import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import wave

from opencognitiveos.contracts.media import attach, describe
from opencognitiveos.contracts.models import TaskSpec, TaskStatus
from opencognitiveos.errors import PolicyDeniedError
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.kernel.inference import InferenceCoordinator
from opencognitiveos.storage.evidence_backup import export_snapshot, restore_snapshot
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.evidence_store import ContentAddressedEvidenceStore


class MediaInputs(unittest.TestCase):
    def test_private_inputs_frozen_and_restored(self):
        audio = io.BytesIO()
        with wave.open(audio, "wb") as pcm:
            pcm.setparams((1, 1, 8000, 0, "NONE", "not compressed"))
            pcm.writeframes(bytes(16))
        self.assertEqual(describe("audio/wav", audio.getvalue())["frames"], 16)
        image = b"P6\n1 1\n255\nabc"
        with self.assertRaises(ValueError):
            describe("image/x-portable-pixmap", image[:-1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteStore(root/"state.db")
            store.migrate()
            auth = ExecutionAuthority(store)
            cas = ContentAddressedEvidenceStore(root/"cas")
            auth.evidence_completion = SimpleNamespace(cas=cas)
            task = TaskSpec("p", "r", "context", "offline", "fixture", allowed_tools=("context.attach",))
            rec, _ = auth.submit_task(task)
            cap = auth._controller_capability
            def bind(capability):
                return attach(auth, task.unique_key, "image/x-portable-pixmap", image, capability=capability)
            with self.assertRaises(PolicyDeniedError):
                bind(object())
            identity = bind(cap)
            self.assertEqual(bind(cap), identity)
            pin = export_snapshot(store, cas, root/"backup")
            restore_snapshot(root/"backup", root/"restored", pin)
            restored = ContentAddressedEvidenceStore(root/"restored"/"cas")
            import hashlib
            self.assertEqual(restored.read_bytes(hashlib.sha256(image).hexdigest()), image)
            slots = InferenceCoordinator(auth, controller_capability=cap)
            slots.allocate(task.unique_key, "local", "v1", "policy", 32, capability=cap)
            for state in (TaskStatus.PLANNED, TaskStatus.READY):
                rec = auth.transition_task(task.unique_key, state, expected_revision=rec.revision,
                                           actor_role="controller", controller_capability=cap)
            with self.assertRaises(PolicyDeniedError):
                bind(cap)
            lease = auth.acquire_lease(task.unique_key, "worker")
            fence = dict(worker_id="worker", execution_id=lease.execution_id, epoch=lease.fencing_epoch)
            auth.transition_task(task.unique_key, TaskStatus.RUNNING, expected_revision=rec.revision, **fence)
            _, snapshot = slots.begin(task.unique_key, request_id="media", max_tokens=2, **fence)
            self.assertEqual(snapshot["local_attachments"], [identity])
