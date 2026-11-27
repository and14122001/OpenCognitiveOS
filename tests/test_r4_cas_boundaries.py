import tempfile
import unittest
from pathlib import Path
from opencognitiveos.verification import ContentAddressedEvidenceStore, EvidenceIntegrityError, EvidenceKind

class TestR4CASBoundaries(unittest.TestCase):
    def test_digest_rejects_trailing_newline(self):
        with self.assertRaises(EvidenceIntegrityError):
            ContentAddressedEvidenceStore.validate_digest('a'*64+'\n')

    def test_staging_symlink_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            store=ContentAddressedEvidenceStore(root/'cas')
            outside=root/'outside'; outside.mkdir()
            store.staging_dir.rmdir()
            store.staging_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(EvidenceIntegrityError):
                store.store_bytes(b'example',kind=EvidenceKind.RAW_EVIDENCE)
            self.assertEqual(list(outside.iterdir()),[])
