"""Installer checks using disposable directories and fixture passwords only."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import stat
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash

SPEC = importlib.util.spec_from_file_location("playnow_setup", Path(__file__).parent / "scripts/setup.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="playnow-installer-")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.passwords = {"viewer": "fixture-viewer-password", "admin": "fixture-admin-password"}
        self.files = {}
        for role, password in self.passwords.items():
            path = self.root / (role + ".password")
            path.write_text(password + "\n")
            path.chmod(0o600)
            self.files[role] = path
        self.output = io.StringIO()
        self.error = io.StringIO()

    def tearDown(self):
        self.temp.cleanup()

    def run_setup(self, extra=()):
        args = ["--port", "18080", "--public-url", "http://localhost:18080",
                "--viewer-password-file", str(self.files["viewer"]),
                "--admin-password-file", str(self.files["admin"])]
        if os.geteuid() == 0:
            args.extend(["--uid", "1000", "--gid", "1000"])
        with patch.object(setup, "PROJECT", self.project), contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.error):
            return setup.main(args + list(extra))

    def test_fresh_setup_creates_private_config_usable_hashes_and_mount_marker(self):
        self.assertEqual(self.run_setup(), 0, self.error.getvalue())
        path = self.project / "runtime.env"
        values = dict(line.split("=", 1) for line in shlex.split(path.read_text(), comments=True))
        self.assertGreaterEqual(len(values["PLAYNOW_SECRET_KEY"]), 32)
        for role, password in self.passwords.items():
            encoded = values[f"PLAYNOW_{role.upper()}_PASSWORD_HASH"]
            self.assertTrue(check_password_hash(encoded, password))
            self.assertNotIn(password, path.read_text())
            self.assertNotIn(password, self.output.getvalue() + self.error.getvalue())
        for name in ("runtime.env", ".env"):
            self.assertEqual(stat.S_IMODE((self.project / name).stat().st_mode), 0o600)
        media = self.project / "var/media"
        self.assertEqual((media / ".playnow-storage").read_bytes(), b"playnow-storage-v1\n")
        for folder in ("Originaly", "Prehravani", "Nahravani"):
            self.assertTrue((media / folder).is_dir())
        before = {name: (self.project / name).read_bytes() for name in ("runtime.env", ".env")}
        self.assertEqual(self.run_setup(), 1)
        self.assertEqual(before, {name: (self.project / name).read_bytes() for name in before})

    def test_existing_media_is_refused_without_mutating_it_or_creating_data(self):
        media = self.root / "existing-media"
        media.mkdir()
        sentinel = media / "keep.txt"
        sentinel.write_text("existing material")
        self.assertEqual(self.run_setup(["--media-dir", str(media)]), 1)
        self.assertEqual(sentinel.read_text(), "existing material")
        self.assertEqual(list(media.iterdir()), [sentinel])
        self.assertFalse((self.project / "var").exists())
        self.assertFalse((self.project / "runtime.env").exists())

    def test_unsafe_password_file_and_public_plain_http_fail_before_writes(self):
        self.files["admin"].chmod(0o644)
        self.assertEqual(self.run_setup(), 1)
        self.assertFalse((self.project / "var").exists())
        self.files["admin"].chmod(0o600)
        self.assertEqual(self.run_setup(["--public-url", "http://video.example.com"]), 1)
        self.assertFalse((self.project / "runtime.env").exists())
        self.assertFalse((self.project / "var").exists())

    def test_unicode_password_and_normalized_https_origin(self):
        self.passwords["viewer"] = "fixture-české-rodinné-heslo"
        self.files["viewer"].write_text(self.passwords["viewer"] + "\n", encoding="utf-8")
        self.assertEqual(self.run_setup(["--public-url", "https://VIDEO.EXAMPLE.COM:443/"]), 0, self.error.getvalue())
        values = dict(line.split("=", 1) for line in shlex.split((self.project / "runtime.env").read_text(), comments=True))
        self.assertTrue(check_password_hash(values["PLAYNOW_VIEWER_PASSWORD_HASH"], self.passwords["viewer"]))
        self.assertEqual(values["PLAYNOW_PUBLIC_URL"], "https://video.example.com")
        self.assertEqual(values["PLAYNOW_COOKIE_SECURE"], "1")
        self.assertEqual(values["PLAYNOW_TRUSTED_PROXY_COUNT"], "1")
        self.assertNotIn(self.passwords["viewer"], self.output.getvalue() + self.error.getvalue())


if __name__ == "__main__":
    unittest.main()
