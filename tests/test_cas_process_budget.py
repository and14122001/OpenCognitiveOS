import multiprocessing
import tempfile
import unittest

from opencognitiveos.verification import ContentAddressedEvidenceStore, EvidenceIntegrityError, EvidenceKind


def publish(root, start, payload, results):
    store = ContentAddressedEvidenceStore(root, max_object_bytes=8, max_total_bytes=8)
    start.wait(10)
    try:
        store.store_bytes(payload, kind=EvidenceKind.RAW_EVIDENCE)
    except EvidenceIntegrityError:
        results.put("quota")
    else:
        results.put("written")


class ProcessBudgetTests(unittest.TestCase):
    def test_competing_writers_share_capacity(self):
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as root:
            start, results = ctx.Barrier(2), ctx.Queue()
            writers = [ctx.Process(target=publish, args=(root, start, data, results))
                       for data in (b"12345678", b"abcdefgh")]
            for writer in writers:
                writer.start()
            try:
                self.assertCountEqual([results.get(timeout=30) for _ in writers], ["quota", "written"])
                for writer in writers:
                    writer.join(10)
                    self.assertEqual(writer.exitcode, 0)
                self.assertEqual(ContentAddressedEvidenceStore(root).current_total_bytes(), 8)
            finally:
                for writer in writers:
                    if writer.is_alive():
                        writer.kill()
                    writer.join()
                results.close()
                results.join_thread()
