"""Exercise the real Windows updater against disposable local Git repositories."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(
    os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required"
)
class WindowsUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pdf2zh-update-test-")
        self.folder = Path(self.temp.name)
        self.seed = self.folder / "seed"
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "main")
        self.git(self.seed, "config", "user.name", "Deployment Test")
        self.git(self.seed, "config", "user.email", "deploy@example.invalid")
        script = self.seed / "script"
        script.mkdir()
        for name in ("update_windows.ps1", "windows_common.ps1"):
            shutil.copy2(ROOT / "script" / name, script / name)
        (script / "setup_windows.ps1").write_text(
            "param([switch]$NoStart,[switch]$SkipAssets,[switch]$SkipOllama)\n"
            "if (-not ($NoStart -and $SkipAssets -and $SkipOllama)) { exit 7 }\n"
            'Set-Content (Join-Path $PSScriptRoot "../deployment-ran.txt") "ok"\n',
            encoding="ascii",
        )
        (self.seed / "tracked.txt").write_text("initial", encoding="utf8")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "initial")
        self.remote = self.folder / "remote.git"
        self.git(self.folder, "clone", "--bare", str(self.seed), str(self.remote))
        self.local = self.folder / "customer copy"
        self.git(self.folder, "clone", str(self.remote), str(self.local))
        self.git(self.seed, "remote", "add", "origin", str(self.remote))

    def tearDown(self):
        self.temp.cleanup()

    def git(self, cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def advance(self):
        (self.seed / "tracked.txt").write_text("upstream", encoding="utf8")
        self.git(self.seed, "add", "tracked.txt")
        self.git(self.seed, "commit", "-m", "update")
        self.git(self.seed, "push", "origin", "main")

    def update(self):
        return subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.local / "script/update_windows.ps1"),
                "-NoStart",
                "-SkipAssets",
                "-SkipOllama",
            ],
            cwd=self.folder,
            capture_output=True,
            text=True,
            timeout=45,
        )

    def test_fast_forward_from_another_directory_preserves_user_files_and_reruns_setup(
        self,
    ):
        library = self.local / "pdf2zh/kernel/PDFMathTranslate-next.git/pdf2zh_files"
        library.mkdir(parents=True)
        for name in ("_webapp_settings.json", "_jobs.json", "customer.pdf"):
            (library / name).write_bytes(b"preserve these user bytes")
        self.advance()
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.local / "tracked.txt").read_text(), "upstream")
        self.assertTrue((self.local / "deployment-ran.txt").exists())
        self.assertEqual(self.git(self.local, "branch", "--show-current"), "main")
        for path in library.iterdir():
            self.assertEqual(path.read_bytes(), b"preserve these user bytes")

    def test_dirty_tracked_file_is_not_discarded(self):
        self.advance()
        (self.local / "tracked.txt").write_text("customer edit", encoding="utf8")
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.local / "tracked.txt").read_text(), "customer edit")
        self.assertFalse((self.local / "deployment-ran.txt").exists())

    def test_local_commits_are_not_reset(self):
        self.git(self.local, "config", "user.name", "Deployment Test")
        self.git(self.local, "config", "user.email", "deploy@example.invalid")
        (self.local / "tracked.txt").write_text("local commit", encoding="utf8")
        self.git(self.local, "commit", "-am", "local change")
        before = self.git(self.local, "rev-parse", "HEAD")
        self.advance()
        self.assertNotEqual(self.update().returncode, 0)
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)

    def test_untracked_file_collision_is_preserved(self):
        (self.seed / "customer-config.txt").write_text("upstream file", encoding="utf8")
        self.git(self.seed, "add", "customer-config.txt")
        self.git(self.seed, "commit", "-m", "new tracked file")
        self.git(self.seed, "push", "origin", "main")
        path = self.local / "customer-config.txt"
        path.write_text("customer file", encoding="utf8")
        before = self.git(self.local, "rev-parse", "HEAD")
        self.assertNotEqual(self.update().returncode, 0)
        self.assertEqual(path.read_text(), "customer file")
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)

    def test_running_workbench_lock_blocks_update(self):
        import msvcrt

        lock = (
            self.local
            / "pdf2zh/kernel/PDFMathTranslate-next.git/pdf2zh_files/_server.lock"
        )
        lock.parent.mkdir(parents=True)
        with lock.open("w+") as handle:
            handle.write("0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            result = self.update()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Close the workbench", result.stdout)
        self.assertFalse((self.local / "deployment-ran.txt").exists())


if __name__ == "__main__":
    unittest.main()
