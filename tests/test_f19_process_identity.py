import unittest
from unittest.mock import patch
from opencognitiveos.execution.sandbox import (
    LinuxUnshareSandboxBackend, SandboxHandle, SandboxUnavailableError,
)

class TestF19(unittest.TestCase):
    def test_untrusted_numeric_handle_never_signals(self):
        backend = LinuxUnshareSandboxBackend()
        handle = SandboxHandle(backend.name, 123, 123, '/tmp/ws', '2026-01-01T00:00:00Z')
        with patch('os.killpg', create=True) as kill:
            for operation in (backend.cancel, backend.inspect, backend.cleanup):
                with self.subTest(operation=operation.__name__), self.assertRaises(SandboxUnavailableError):
                    operation(handle)
            kill.assert_not_called()
