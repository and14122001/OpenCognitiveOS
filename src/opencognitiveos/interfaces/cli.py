"""CLI for OpenCognitiveOS M0-M1.6 offline kernel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from opencognitiveos import __version__
from opencognitiveos.contracts.models import EffectOutcome, SystemClock, TaskSpec, TaskStatus, canonical_json_hash
from opencognitiveos.execution.runner import OfflineRunner
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.providers.mock_provider import MockProvider
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.verifier import IndependentVerifier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencognitiveos", description="OpenCognitiveOS M0-M1.6 CLI")
    parser.add_argument("--version", action="version", version=f"opencognitiveos {__version__}")
    parser.add_argument("--db", help="Path to SQLite database file.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Run SQLite migrations and integrity check.")
    sub.add_parser("integrity-check", help="Run SQLite integrity_check and foreign_key_check.")
    p_audit = sub.add_parser("audit-consistency", help="Audit cross-table DB consistency for a task.")
    p_audit.add_argument("--unique-key", required=True)

    media = sub.add_parser("attach-local", help="Attach bounded local text, PCM audio or RGB bitmap context.")
    media.add_argument("--source", required=True)
    media.add_argument("--media-type", required=True)
    register = sub.add_parser("register-acceptance", help="Sign an operator-owned acceptance specification.")
    register.add_argument("--spec", required=True, help="Canonical path to an operator-owned mode-0600 JSON file.")
    stage = sub.add_parser("stage-candidates", help="Stage signed child outputs for independent review.")
    stage.add_argument("--base-sha", required=True)
    stage.add_argument("--candidate", nargs=2, action="append", metavar=("CHILD", "PATH"), required=True)
    replay = sub.add_parser("replay-evidence", help="Replay the sealed receipt and referenced CAS evidence.")
    complete = sub.add_parser("complete-evidence", help="Atomically verify and commit a governed task.")
    verify_file = sub.add_parser("verify-file", help="Check the registered Git blob in a native Linux sandbox.")
    approved = sub.add_parser("run-approved-python", help="Run one explicitly approved, pinned script in Linux isolation.")
    approved.add_argument("--script", required=True)
    approved.add_argument("--timeout", type=float, default=30)
    for command in (verify_file, approved):
        command.add_argument("--workspace", required=True)
    for name, typ in (("worker", str), ("execution", str), ("epoch", int)):
        for command in (verify_file, approved):
            command.add_argument("--" + name, type=typ, required=True)
    for command in (media, register, stage, replay, complete, verify_file, approved):
        command.add_argument("--config", required=True, help="Canonical path to operator-owned mode-0600 configuration.")
        command.add_argument("--unique-key", required=True)
    complete.add_argument("--worker", required=True)
    complete.add_argument("--execution", required=True)
    complete.add_argument("--epoch", type=int, required=True)
    complete.add_argument("--revision", type=int, required=True)
    complete.add_argument("--manifest", required=True)
    complete.add_argument("--output", required=True, help="Path to worker output JSON.")

    p_run = sub.add_parser("run-mock", help="Submit and run a task offline using MockProvider.")
    p_run.add_argument("--project-id", default="default-project")
    p_run.add_argument("--repo-id", required=True)
    p_run.add_argument("--task-id", required=True)
    p_run.add_argument("--goal", required=True)
    p_run.add_argument("--simulate-lost-response", action="store_true")

    p_status = sub.add_parser("status", help="Inspect persisted task status.")
    p_status.add_argument("--unique-key", required=True, help="Task key in repo_id::task_id format.")

    p_cancel = sub.add_parser("cancel", help="Cancel a non-terminal task.")
    p_cancel.add_argument("--unique-key", required=True)
    p_cancel.add_argument("--reason", default="Cancelled via CLI")

    sub.add_parser("recover", help="Run restart recovery scan on persisted SQLite state.")

    p_backup = sub.add_parser("backup", help="Create consistent SQLite backup.")
    p_backup.add_argument("--dest", required=True)

    benchmark = sub.add_parser("compare-benchmark", help="Compare paired offline observations against a pinned plan.")
    benchmark.add_argument("--plan", required=True)
    benchmark.add_argument("--sha256", required=True)
    benchmark.add_argument("--observations", required=True)
    bundle = sub.add_parser("backup-evidence", help="Publish a database and referenced CAS snapshot.")
    bundle.add_argument("--cas", required=True)
    bundle.add_argument("--dest", required=True)
    restore_bundle = sub.add_parser("restore-evidence", help="Restore a verified snapshot into a new directory.")
    restore_bundle.add_argument("--source", required=True)
    restore_bundle.add_argument("--sha256", required=True)
    restore_bundle.add_argument("--dest", required=True)

    p_restore = sub.add_parser("restore", help="Restore a verified SQLite backup to --db.")
    p_restore.add_argument("--source", required=True)

    p_reconcile = sub.add_parser("reconcile", help="Reconcile a task in NEEDS_RECONCILIATION.")
    p_reconcile.add_argument("--unique-key", required=True)
    p_reconcile.add_argument("--intent-id", required=True)
    p_reconcile.add_argument("--outcome", choices=["SUCCEEDED", "FAILED"], required=True)
    p_reconcile.add_argument("--external-ref", default="reconciled-manual")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compare-benchmark":
        from opencognitiveos.verification.benchmark import compare
        rows = json.loads(Path(args.observations).read_text(encoding="utf-8"))
        print(json.dumps(compare(args.plan, args.sha256, rows), ensure_ascii=False))
        return 0
    if not args.db and args.command != "restore-evidence":
        parser.error("--db is required for this command")
    clock = SystemClock()
    if args.command in {"backup-evidence", "restore-evidence"}:
        from opencognitiveos.storage.evidence_backup import export_snapshot, restore_snapshot
        from opencognitiveos.verification.evidence_store import ContentAddressedEvidenceStore

        if args.command == "restore-evidence":
            restore_snapshot(args.source, args.dest, args.sha256)
            print(json.dumps({"restored": args.dest}))
        else:
            db = SQLiteStore(Path(args.db).resolve(strict=True), clock=clock)
            digest = export_snapshot(db, ContentAddressedEvidenceStore(Path(args.cas).resolve(strict=True)), args.dest)
            print(json.dumps({"snapshot": args.dest, "manifest_sha256": digest}))
        return 0
    if args.command == "restore":
        restored = SQLiteStore.restore_from(args.source, args.db, clock=clock)
        print(json.dumps(restored.integrity_check()))
        return 0
    store = SQLiteStore(Path(args.db), clock=clock)

    if args.command == "init-db":
        applied = store.migrate()
        integrity = store.integrity_check()
        print(
            json.dumps(
                {"applied_migrations": applied, "schema_version": store.schema_version(), "integrity": integrity},
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "integrity-check":
        print(json.dumps(store.integrity_check(), ensure_ascii=False))
        return 0

    store.migrate()
    authority = ExecutionAuthority(store, clock=clock)

    if args.command in ("attach-local", "register-acceptance", "stage-candidates", "complete-evidence", "replay-evidence", "verify-file", "run-approved-python"):
        from opencognitiveos.verification.bootstrap import configure_evidence
        configure_evidence(authority, args.config)
        if args.command == "attach-local":
            from opencognitiveos.contracts.media import attach
            from opencognitiveos.verification.bootstrap import _private_read
            identity = attach(authority, args.unique_key, args.media_type, _private_read(args.source),
                              capability=authority._controller_capability)
            print(json.dumps({"attachment": identity}))
            return 0
        if args.command == "register-acceptance":
            from opencognitiveos.errors import PolicyDeniedError
            from opencognitiveos.verification.acceptance import AcceptanceSpec, AcceptanceSpecRegistry
            from opencognitiveos.verification.bootstrap import _private_read
            gate = authority.evidence_completion
            source = Path(args.spec)
            if any(source.is_relative_to(root) for root in (gate.manager.repo_path, gate.manager.workspaces_root)):
                raise PolicyDeniedError("Acceptance input must be outside worker-controlled roots.")
            approved_spec = AcceptanceSpec.from_dict(json.loads(_private_read(source)))
            if approved_spec.policy_hash != gate.policy_hash:
                raise PolicyDeniedError("Acceptance policy differs from the configured policy.")
            cap = authority._controller_capability
            registry = AcceptanceSpecRegistry(authority, controller_capability=cap, signing_key=gate.controller_key)
            digest = registry.register_spec(approved_spec, task_spec=authority.get_task(args.unique_key).spec, capability=cap)
            print(json.dumps({"spec_sha256": digest}))
            return 0
        if args.command == "stage-candidates":
            from opencognitiveos.kernel.candidates import stage_candidates
            digest, bundle = stage_candidates(authority, args.unique_key, args.base_sha, args.candidate,
                                              capability=authority._controller_capability)
            print(json.dumps({"sha256": digest, "bundle": bundle}))
            return 0
        if args.command == "run-approved-python":
            from opencognitiveos.execution.sandbox import GovernedSandboxRunner, SandboxExecutionRequest
            manager = authority.evidence_completion.manager
            identity = manager.load_identity(args.workspace)
            runner = GovernedSandboxRunner(authority=authority, workspace_manager=manager)
            result = runner.run(SandboxExecutionRequest(args.unique_key,args.execution,args.worker,args.epoch,
                    Path(identity.worktree_path),("sandbox.python",args.script),timeout_seconds=args.timeout,
                    workspace_id=args.workspace))
            print(json.dumps({"exit_code":result.exit_code,"timed_out":result.timed_out,"log_digest":result.evidence_hash}))
            return 0 if result.exit_code == 0 and not result.timed_out else 1
        if args.command == "verify-file":
            from opencognitiveos.verification.file_verifier import verify_file
            manifest = verify_file(authority, task_key=args.unique_key, workspace_id=args.workspace,
                                   worker_id=args.worker, execution_id=args.execution, epoch=args.epoch)
            print(json.dumps({"evidence_id": manifest.evidence_id, "manifest_hash": manifest.manifest_hash()}))
            return 0
        if args.command == "replay-evidence":
            valid = authority.replay_verification(args.unique_key)
            print(json.dumps({"valid": valid}))
            return 0 if valid else 1
        task, receipt = authority.verify_and_complete(args.unique_key, evidence_id=args.manifest,
                    expected_revision=args.revision, worker_id=args.worker, execution_id=args.execution,
                    epoch=args.epoch, output_payload=json.loads(Path(args.output).read_text(encoding="utf-8")))
        print(json.dumps({"status": task.status.value, "receipt": receipt.verification_id}))
        return 0

    if args.command == "run-mock":
        spec = TaskSpec(
            project_id=args.project_id,
            repo_id=args.repo_id,
            task_id=args.task_id,
            goal=args.goal,
            acceptance_hash=canonical_json_hash({"goal": args.goal}),
        )
        runner = OfflineRunner(authority, MockProvider(), authority.verifier)
        record = runner.run_task(spec, simulate_lost_response=args.simulate_lost_response)
        print(
            json.dumps(
                {
                    "unique_key": record.unique_key,
                    "status": record.status.value,
                    "revision": record.revision,
                    "fencing_epoch": record.fencing_epoch,
                    "result": record.result,
                    "error_message": record.error_message,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "audit-consistency":
        print(json.dumps(authority.audit_database_consistency(args.unique_key), ensure_ascii=False))
        return 0

    if args.command == "status":
        record = authority.get_task(args.unique_key)
        print(
            json.dumps(
                {
                    "unique_key": record.unique_key,
                    "status": record.status.value,
                    "revision": record.revision,
                    "fencing_epoch": record.fencing_epoch,
                    "retry_count": record.retry_count,
                    "result": record.result,
                    "error_message": record.error_message,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "cancel":
        record = authority.get_task(args.unique_key)
        updated = authority.cancel_task(args.unique_key, expected_revision=record.revision, reason=args.reason)
        print(
            json.dumps(
                {"unique_key": updated.unique_key, "status": updated.status.value, "revision": updated.revision},
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "recover":
        print(json.dumps(authority.recover_on_restart(), ensure_ascii=False))
        return 0

    if args.command == "backup":
        dest = store.backup_to(args.dest)
        print(json.dumps({"backup_path": str(dest)}, ensure_ascii=False))
        return 0

    if args.command == "reconcile":
        updated = authority.reconcile_effect(
            args.unique_key,
            intent_id=args.intent_id,
            resolved_outcome=EffectOutcome(args.outcome),
            external_ref=args.external_ref,
            evidence_hash=canonical_json_hash({"intent_id": args.intent_id, "outcome": args.outcome}),
        )
        print(
            json.dumps(
                {"unique_key": updated.unique_key, "status": updated.status.value, "revision": updated.revision},
                ensure_ascii=False,
            )
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
