import unittest
from pathlib import Path
from opencognitiveos.execution.sandbox import (build_sanitized_env, SandboxPolicyError, LinuxUnshareSandboxBackend, MacOSSeatbeltSandboxBackend, SandboxUnavailableError)
class QuarantineTests(unittest.TestCase):
    def test_unshare_disabled(self):
        backend = LinuxUnshareSandboxBackend()
        self.assertFalse(backend.detect_capabilities().available)
        with self.assertRaises(SandboxUnavailableError):
            backend.execute(None)
    def test_env_override_denied(self):
        for kwargs in ({'env_whitelist': ('GITHUB_TOKEN',)}, {'extra_env': (('LD_PRELOAD','x'),)}):
            with self.assertRaises(SandboxPolicyError):
                build_sanitized_env(workspace_path=Path('/tmp'), **kwargs)
    def test_macos_quarantined(self):
        self.assertFalse(MacOSSeatbeltSandboxBackend().detect_capabilities().available)
