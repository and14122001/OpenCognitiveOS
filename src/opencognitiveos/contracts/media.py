"""Bounded local context attachments. The controller accepts lossless, uncompressed fixtures."""
import io
import hashlib
import json
import re
import wave

from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.errors import PolicyDeniedError
from opencognitiveos.verification.evidence_store import EvidenceKind


def describe(mime, data):
    if not isinstance(data, bytes) or not 0 < len(data) <= 65536:
        raise ValueError("Expected 1 through 65536 bytes.")
    if mime == "text/plain":
        return {"characters": len(data.decode("utf-8", errors="strict"))}
    if mime == "image/x-portable-pixmap":
        match = re.match(rb"P6\n([1-9][0-9]{0,3}) ([1-9][0-9]{0,3})\n255\n", data)
        if not match:
            raise ValueError("Expected an RGB8 P6 image.")
        width, height = map(int, match.groups())
        if width * height * 3 != len(data) - match.end():
            raise ValueError("Image dimensions differ from payload.")
        return {"width": width, "height": height, "channels": 3}
    if mime == "audio/wav":
        with wave.open(io.BytesIO(data), "rb") as audio:
            channels, size, rate, count = audio.getparams()[:4]
            if (channels not in (1, 2) or size not in (1, 2) or not 8000 <= rate <= 48000
                    or not 0 < count <= 65536 or len(audio.readframes(count)) != count * channels * size):
                raise ValueError("Unsupported or truncated PCM audio.")
            return {"frames": count, "rate": rate, "channels": channels, "sample_bytes": size}
    raise PolicyDeniedError("Unsupported local context media type.")


def attach(authority, task_key, mime, data, *, capability):
    gate = authority.evidence_completion
    if gate is None or capability is not authority._controller_capability:
        raise PolicyDeniedError("Local attachments require operator authorization.")
    metadata = describe(mime, data)
    with authority.store.transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE unique_key=?", (task_key,)).fetchone()
        if row is None or row["status"] != "SUBMITTED":
            raise PolicyDeniedError("The task input is closed.")
        task = authority._spec_from_row(row)
        authority.policy_gate.check_tool(task, "context.attach")
        conn.execute("""CREATE TABLE IF NOT EXISTS local_attachments (
            id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(unique_key), digest TEXT NOT NULL,
            envelope TEXT NOT NULL)""")
        digest = hashlib.sha256(data).hexdigest()
        envelope = {"version": 1, "task": task_key, "task_hash": task.content_hash(),
                    "media_type": mime, "metadata": metadata, "size": len(data),
                    "sha256": digest, "data_class": task.data_class.value}
        identity = canonical_json_hash(envelope)
        if conn.execute("SELECT id FROM local_attachments WHERE id=?", (identity,)).fetchone():
            if not gate.cas.verify_digest(digest):
                raise PolicyDeniedError("Attachment integrity failure.")
            return identity
        if conn.execute("SELECT COUNT(*) FROM local_attachments WHERE task=?", (task_key,)).fetchone()[0] >= 8:
            raise PolicyDeniedError("Task attachment limit reached.")
        gate.cas.store_bytes(data, kind=EvidenceKind.RAW_EVIDENCE)
        conn.execute("INSERT INTO local_attachments VALUES (?,?,?,?)", (identity, task_key, digest,
                     json.dumps(envelope, sort_keys=True)))
        authority._append_event_and_outbox(conn, event_type="LOCAL_CONTEXT_ATTACHED", task_unique_key=task_key,
                    execution_id="controller", epoch=row["fencing_epoch"], payload={"id": identity})
        return identity
