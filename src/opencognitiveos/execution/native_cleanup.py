"""Proofs for reclaiming worktrees explicitly provisioned for the isolated native broker."""
import hmac
import json
from pathlib import Path
import re
import subprocess

from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.verification.seals import seal


def workspace_tag(key, identity):
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot):
        raise ValueError("Unknown Linux boot identity.")
    return seal(key, "private-native-worktree", canonical_json_hash([identity, boot]))


def ready(manager, conn, identity, dispatch):
    gate = manager.authority.evidence_completion
    if gate is None or gate.manager is not manager:
        return False
    marker = conn.execute("SELECT tag FROM native_private_workspaces WHERE workspace_id=?",
                          (identity.workspace_id,)).fetchone()
    if marker is None or not hmac.compare_digest(marker[0], workspace_tag(gate.controller_key, identity.identity_hash)):
        return False
    task = conn.execute("SELECT * FROM tasks WHERE unique_key=?", (identity.task_unique_key,)).fetchone()
    if task is None or task["status"] != "SUCCEEDED" or dispatch["epoch"] != task["fencing_epoch"]:
        return False
    verified = conn.execute("""SELECT output_payload_json FROM verification_receipts WHERE task_unique_key=?
                 AND execution_id=? AND epoch=? AND verifier_name='independent-native-evidence-v1'""",
                            (identity.task_unique_key, dispatch["execution_id"], dispatch["epoch"])).fetchone()
    if verified is None or not manager.authority.replay_verification(identity.task_unique_key):
        return False
    binding = json.loads(verified[0]).get("verified_evidence", {})
    manifest = conn.execute("SELECT manifest_json FROM evidence_manifests WHERE evidence_id=?",
                            (binding.get("manifest_id"),)).fetchone()
    if manifest is None or json.loads(manifest[0]).get("workspace_id") != identity.workspace_id:
        return False
    effects = conn.execute("SELECT * FROM effects WHERE task_unique_key=?", (identity.task_unique_key,)).fetchall()
    if len(effects) != 1:
        return False
    effect = effects[0]
    if (effect["tool"] != "sandbox.python" or effect["outcome"] != "SUCCEEDED"
            or effect["execution_id"] != dispatch["execution_id"] or effect["epoch"] != dispatch["epoch"]
            or effect["evidence_hash"] != binding.get("worker_log")
            or not re.fullmatch(r"ocos-worker-[0-9a-f]{32}\.service", effect["external_ref"] or "")):
        return False
    state = subprocess.run(["/usr/bin/systemctl", "show", effect["external_ref"], "--property=ActiveState,SubState"],
                           text=True, capture_output=True, timeout=5, env={"PATH":"/usr/bin:/bin","LC_ALL":"C"})
    return set(state.stdout.splitlines()) == {"ActiveState=inactive", "SubState=dead"}
