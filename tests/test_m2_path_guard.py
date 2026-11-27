import tempfile
import unittest
from pathlib import Path
from opencognitiveos.execution.path_guard import checked_path

class TestM2PathGuard(unittest.TestCase):
    def test_valid_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(checked_path(root, "src/new.py"), root.resolve() / "src/new.py")
            for name in ("", "../x", "/tmp/x", "C:/x", "a\\b", "a//b", "a/./b", ".git/config", "a/.GIT", "x:stream", "NUL", "a."):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    checked_path(root, name)
    def test_link_outside(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(ValueError):
                checked_path(root, "link/file")
    def test_hardlink(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "original").write_text("test")
            os.link(root / "original", root / "alias")
            with self.assertRaises(ValueError):
                checked_path(root, "alias")
