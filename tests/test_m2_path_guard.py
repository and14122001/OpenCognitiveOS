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

    def test_reserved_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("nul.txt", "COM¹.txt", "LPT²", "COM³", "CONIN$", "CONOUT$", "CON .txt", "bad?name", "bad*name", "bad<name", "bad>name", "bad|name", 'bad"name', "bad\nname", "a "):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    checked_path(Path(tmp), name)

    def test_reparse_attribute(self):
        import stat
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = Path.lstat
            def fake(path, *args, **kwargs):
                if path.name == "junction":
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                return original(path, *args, **kwargs)
            with patch.object(Path, "lstat", fake), self.assertRaises(ValueError):
                checked_path(root, "junction/file")
