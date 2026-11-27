"""Controller-side evidence checks inside the task's final write transaction."""
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path

from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.errors import VerificationFailedError
from opencognitiveos.execution.worktree import WorkspaceIdentity, WorkspaceManager
from opencognitiveos.verification.acceptance import AcceptanceSpec, _parse_utc
from opencognitiveos.verification.evidence_store import ContentAddressedEvidenceStore
from opencognitiveos.verification.manifest import EvidenceManifest, capture_trusted_git_state
from opencognitiveos.verification.seals import seal, spec_seal


def require(condition, reason):
    if not condition:
        raise VerificationFailedError(reason)


@dataclass(frozen=True)
class EvidenceCompletion:
    manager: WorkspaceManager
    cas: ContentAddressedEvidenceStore
    controller_key: bytes = field(repr=False)
    verifier_key: bytes = field(repr=False)
    policy_hash: str

    def _sealed_records(self, conn, task, evidence_id, execution_id, epoch):
        row = conn.execute("SELECT * FROM evidence_manifests WHERE evidence_id=?", (evidence_id,)).fetchone()
        require(row is not None, "Missing manifest.")
        manifest = EvidenceManifest.from_dict(json.loads(row["manifest_json"]))
        mhash = manifest.manifest_hash()
        spec_row = conn.execute("SELECT * FROM acceptance_specs WHERE task_unique_key=?", (task.unique_key,)).fetchone()
        require(spec_row is not None, "Missing acceptance specification.")
        spec = AcceptanceSpec.from_dict(json.loads(spec_row["spec_json"]))
        shash = spec.compute_spec_hash()
        for query, kind, digest, key in (
                ("SELECT tag FROM acceptance_seals WHERE digest=?", "spec", shash, self.controller_key),
                ("SELECT tag FROM manifest_seals WHERE digest=?", "manifest", mhash, self.verifier_key)):
            tag = conn.execute(query, (digest,)).fetchone()
            expected_tag = spec_seal(key, digest, task) if kind == "spec" else seal(key, kind, digest)
            require(tag and hmac.compare_digest(tag[0], expected_tag), "Untrusted evidence seal.")
        require(manifest.evidence_id == evidence_id and mhash == row["manifest_hash"]
                and manifest.acceptance_hash == spec_row["spec_hash"] == shash
                and manifest.task_unique_key == spec.task_unique_key == task.unique_key
                and manifest.project_id == spec.project_id == task.project_id
                and manifest.repo_id == spec.repo_id == task.repo_id
                and manifest.base_sha == spec.base_sha == task.base_sha
                and manifest.policy_hash == spec.policy_hash == self.policy_hash
                and manifest.verifier_id == spec.verifier_id
                and manifest.execution_id == execution_id
                and manifest.fencing_epoch == epoch, "Evidence binding mismatch.")
        artifacts = manifest.artifact_hashes
        require(artifacts.get("test_log") and artifacts.get("environment_manifest") == manifest.environment_hash,
                "Missing bound test log or environment.")
        for digest in artifacts.values():
            require(self.cas.verify_digest(digest), "Missing CAS object.")
        env = json.loads(self.cas.read_bytes(manifest.environment_hash))
        criteria = spec.criteria
        require(set(criteria) in ({"command", "min_pass"}, {"command", "min_pass", "worker_script"}) and type(criteria["min_pass"]) is int
                and criteria["min_pass"] > 0 and isinstance(criteria["command"], str) and criteria["command"]
                and env["test_command"] == criteria["command"] and env["sandbox_backend"] == "linux-bwrap"
                and type(env["passed_count"]) is int and env["passed_count"] >= criteria["min_pass"]
                and all(type(env[k]) is int and env[k] == 0 for k in ("exit_code", "failed_count", "skipped_count")),
                "Acceptance criteria failed or unsupported.")
        binding = {"manifest_id": evidence_id, "manifest_hash": mhash, "spec_hash": shash}
        if "worker_script" in criteria:
            log = artifacts.get("worker_log")
            rows = conn.execute("""SELECT e.*,b.tag FROM effects e JOIN broker_seals b ON e.intent_id=b.intent
                WHERE e.task_unique_key=? AND e.execution_id=? AND e.epoch=? AND e.approval_ref=?
                AND e.tool='sandbox.python' AND e.outcome='SUCCEEDED' AND e.evidence_hash=?""",
                (task.unique_key, execution_id, epoch, shash, log)).fetchall()
            require(log and len(rows) == 1, "Missing uniquely attested native effect.")
            effect = rows[0]
            digest = canonical_json_hash([task.unique_key, execution_id, epoch, effect["intent_id"], effect["args_hash"],
                                          shash, effect["outcome"], effect["external_ref"], log])
            require(hmac.compare_digest(effect["tag"], seal(self.verifier_key, "effect", digest)), "Invalid broker seal.")
            binding["worker_log"] = log
        return spec, manifest, binding

    def replay(self, conn, task, binding, execution_id, epoch):
        _, _, original = self._sealed_records(conn, task, binding["manifest_id"], execution_id, epoch)
        return binding == original

    def verify(self, conn, task, evidence_id, worker_id, execution_id, epoch):
        spec, manifest, binding = self._sealed_records(conn, task, evidence_id, execution_id, epoch)
        require(manifest.worker_id == worker_id, "Wrong manifest worker.")
        if spec.expires_at_utc:
            require(_parse_utc(spec.expires_at_utc) > self.manager.clock.now_utc(), "Acceptance expired.")
        workspace = conn.execute("""SELECT w.* FROM workspaces w JOIN workspace_dispatch d
            ON d.workspace_id=w.workspace_id WHERE w.workspace_id=? AND w.task_unique_key=?
            AND w.status='ACTIVE' AND d.execution_id=? AND d.worker_id=? AND d.epoch=?""",
            (manifest.workspace_id, task.unique_key, execution_id, worker_id, epoch)).fetchone()
        require(workspace is not None, "No matching workspace dispatch.")
        identity = WorkspaceIdentity.from_row(workspace)
        self.manager.verify_worktree(identity)
        git = capture_trusted_git_state(Path(identity.worktree_path), base_sha=task.base_sha,
                                        evidence_store=self.cas, claimed_clean=True)
        require(not manifest.dirty_workspace and manifest.uncommitted_snapshot_hash is None
                and git.tested_commit_sha == manifest.tested_commit_sha == task.base_sha
                and git.tested_tree_sha == manifest.tested_tree_sha, "Tested tree differs from workspace.")
        return binding
