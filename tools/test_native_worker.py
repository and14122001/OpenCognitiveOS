"""Hosted Linux checks for the production leased Python broker."""
import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from opencognitiveos.contracts.models import Budget
from opencognitiveos.errors import PolicyDeniedError

from opencognitiveos.contracts.models import TaskSpec, TaskStatus, canonical_json_hash
from opencognitiveos.execution.sandbox import GovernedSandboxRunner, SandboxExecutionRequest
from opencognitiveos.execution.worktree import WorkspaceManager
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.acceptance import AcceptanceSpec, AcceptanceSpecRegistry
from opencognitiveos.verification.completion import EvidenceCompletion
from opencognitiveos.verification.evidence_store import ContentAddressedEvidenceStore


class NativeWorker(unittest.TestCase):
    def run_candidate(self, source, timeout):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git","init","-q",str(repo)],check=True)
            (repo/"script.py").write_text(source)
            subprocess.run(["git","-C",str(repo),"add","script.py"],check=True)
            subprocess.run(["git","-C",str(repo),"-c","user.name=Test","-c","user.email=test@local",
                            "commit","-qm","fixture"],check=True)
            sha = subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()
            store = SQLiteStore(root/"tasks.db")
            store.migrate()
            auth = ExecutionAuthority(store)
            mgr = WorkspaceManager(authority=auth, repo_path=repo, workspaces_root=root/"workspaces")
            cas = ContentAddressedEvidenceStore(root/"cas")
            key, verifier_key, cap = secrets.token_bytes(32), secrets.token_bytes(32), object()
            export_dir = os.environ.get("OCOS_CI_FIXTURE_BACKUP")
            if export_dir:
                from restore_native_fixture import public_fixture_keys
                key, verifier_key = public_fixture_keys()
            policy = canonical_json_hash({"net":"deny"})
            auth.evidence_completion = EvidenceCompletion(mgr,cas,key,verifier_key,policy)
            reg = AcceptanceSpecRegistry(auth,controller_capability=cap,signing_key=key)
            spec = TaskSpec("p","r","t","test",canonical_json_hash({"goal":"test"}),base_sha=sha,
                            allowed_tools=("sandbox.python",),allowed_paths=("script.py",))
            parent = replace(spec, task_id="parent", allowed_paths=("script.py", "output.txt"))
            auth.submit_task(parent)
            spec = replace(spec, parent_id="parent", allowed_paths=parent.allowed_paths,
                           budget=Budget(max_retries=0, max_steps=2))
            rec,_ = auth.submit_task(spec)
            acceptance = AcceptanceSpec("p","r",spec.unique_key,sha,policy,"verifier",
                    {"command":"verify-file script.py","min_pass":1,"worker_script":"script.py"},auth.clock.now_iso())
            reg.register_spec(acceptance,task_spec=spec,capability=cap)
            for status in (TaskStatus.PLANNED,TaskStatus.READY):
                rec = auth.transition_task(spec.unique_key,status,expected_revision=rec.revision,actor_role="controller", controller_capability=auth._controller_capability)
            ws = mgr.create_workspace(workspace_id="ws",repo_id="r",task_id="t",base_sha=sha)
            lease = auth.acquire_lease(spec.unique_key,"worker",ttl_seconds=90)
            fence = dict(worker_id="worker",execution_id=lease.execution_id,epoch=lease.fencing_epoch)
            auth.transition_task(spec.unique_key,TaskStatus.RUNNING,expected_revision=rec.revision,**fence)
            runner = GovernedSandboxRunner(authority=auth,workspace_manager=mgr)
            result = runner.run(SandboxExecutionRequest(spec.unique_key,lease.execution_id,"worker",lease.fencing_epoch,
                                Path(ws.worktree_path),("sandbox.python","script.py"),timeout_seconds=timeout,workspace_id="ws"))
            self.assertTrue(result.is_os_enforced)
            self.assertEqual(cas.read_bytes(result.evidence_hash).decode(),result.stdout)
            if result.exit_code == 0:
                from opencognitiveos.verification.file_verifier import verify_file
                rec = auth.get_task(spec.unique_key)
                rec = auth.transition_task(spec.unique_key, TaskStatus.VERIFYING, expected_revision=rec.revision, **fence)
                manifest = verify_file(auth,task_key=spec.unique_key,workspace_id="ws",**fence)
                final, receipt = auth.verify_and_complete(spec.unique_key, expected_revision=rec.revision,
                                                          evidence_id=manifest.evidence_id, **fence)
                self.assertEqual(final.status, TaskStatus.SUCCEEDED)
                self.assertEqual(receipt.verifier_name,"independent-native-evidence-v1")
                self.assertTrue(auth.replay_verification(spec.unique_key))
                from opencognitiveos.kernel.candidates import stage_candidates
                proposals = [(spec.unique_key, "output.txt")]
                def stage(base, files):
                    return stage_candidates(auth, parent.unique_key, base, files,
                                            capability=auth._controller_capability)
                digest, bundle = stage(sha, proposals)
                self.assertEqual(bundle["files"]["output.txt"]["sha256"], result.evidence_hash)
                self.assertEqual(stage(sha, proposals)[0], digest)
                with self.assertRaises(PolicyDeniedError):
                    stage("0" * 40, proposals)
                with self.assertRaises(PolicyDeniedError):
                    stage(sha, proposals * 2)
                if export_dir:
                    from opencognitiveos.storage.evidence_backup import export_snapshot
                    snapshot = export_snapshot(store, cas, export_dir)
                    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as sink:
                        print("snapshot=" + snapshot, file=sink)
                print("Native sandbox.python -> signed manifest -> atomic SUCCEEDED -> replay OK", flush=True)
            return result

    def test_limits_and_io(self):
        result = self.run_candidate("""
import os,socket
from pathlib import Path
assert os.getuid()!=0 and not Path('/etc/passwd').exists() and not Path('/sys/fs/cgroup').exists()
assert {n for _,n in socket.if_nameindex()} <= {'lo'}
assert 'GITHUB_TOKEN' not in os.environ
Path('result').write_text('works')
print(Path('result').read_text())
""", 10)
        self.assertEqual(result.exit_code,0,result.stdout)
        self.assertIn("works",result.stdout)

    def test_failure_retains_evidence(self):
        result = self.run_candidate("print('failed check', flush=True)\nraise SystemExit(3)", 10)
        self.assertNotEqual(result.exit_code,0)
        self.assertIn("failed check",result.stdout)

    def test_timeout_drains_unit(self):
        result = self.run_candidate("import subprocess,time\nsubprocess.Popen(['/bin/sleep','30'])\ntime.sleep(30)", 2)
        self.assertNotEqual(result.exit_code,0)
        self.assertTrue(result.process_group_terminated)

if __name__ == "__main__":
    unittest.main()
