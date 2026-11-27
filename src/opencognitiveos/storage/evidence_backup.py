"""Publish and restore a verified SQLite snapshot plus its referenced CAS objects."""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from opencognitiveos.errors import DatabaseIntegrityError
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.evidence_store import ContentAddressedEvidenceStore, EvidenceKind


def _file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _references(db):
    with closing(sqlite3.connect(db.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_manifests'").fetchone():
            return []
        refs = set()
        for (raw,) in conn.execute("SELECT manifest_json FROM evidence_manifests"):
            for digest in json.loads(raw)["artifact_hashes"].values():
                refs.add(ContentAddressedEvidenceStore.validate_digest(digest))
        return sorted(refs)


@contextmanager
def _publish(dest):
    dest = Path(dest).absolute()
    dest.parent.mkdir(parents=True, exist_ok=True)
    lock = dest.with_name(dest.name + ".publish-lock")
    lock.mkdir(mode=0o700)
    try:
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(dest)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            stage = Path(tmp) / "snapshot"
            stage.mkdir(mode=0o700)
            yield stage
            os.rename(stage, dest)
    finally:
        lock.rmdir()


def export_snapshot(store, cas, destination):
    with _publish(destination) as stage:
        db = store.backup_to(stage / "state.sqlite3")
        SQLiteStore(db).integrity_check()
        refs = _references(db)
        objects = stage / "objects"
        objects.mkdir()
        for digest in refs:
            (objects / digest).write_bytes(cas.read_bytes(digest))
        manifest = {"version": 1, "database_sha256": _file_hash(db), "objects": refs}
        manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
        (stage / "manifest.json").write_bytes(manifest_bytes)
        return hashlib.sha256(manifest_bytes).hexdigest()


def restore_snapshot(source, destination, manifest_sha256):
    root = Path(source).resolve(strict=True)
    raw = (root / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise DatabaseIntegrityError("Snapshot manifest does not match trusted digest.")
    manifest = json.loads(raw)
    if manifest.get("version") != 1:
        raise DatabaseIntegrityError("Unsupported snapshot.")
    with _publish(destination) as stage:
        # Pin the database bytes before validation so a changing source cannot substitute data.
        db = stage / "state.sqlite3"
        shutil.copyfile(root / "state.sqlite3", db)
        if _file_hash(db) != manifest["database_sha256"] or _references(db) != manifest["objects"]:
            raise DatabaseIntegrityError("Database digest or evidence inventory mismatch.")
        SQLiteStore(db).integrity_check()
        cas = ContentAddressedEvidenceStore(stage / "cas")
        for digest in manifest["objects"]:
            with (root / "objects" / digest).open("rb") as stream:
                result = cas.store_stream(stream, kind=EvidenceKind.RAW_EVIDENCE)
            if result.digest != digest:
                raise DatabaseIntegrityError("Restored evidence digest mismatch.")
