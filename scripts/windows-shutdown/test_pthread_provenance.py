"""Reject private-archive fallback and preserve the standalone default entry."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from pthread_provenance import isolated_pthread_archives, pthread_link_proof


SELECTED = ["D:/a/private/lib/libpthread.a", "D:/a/private/lib/libwinpthread.a"]
SYMBOLS = "\n".join(f"00000001800000{index:02x} T {name}" for index, name in enumerate(
    ("pthread_key_create", "pthread_key_delete", "_pthread_cleanup_dest")))
MAP = "D:/a/private/lib/libpthread.a(libwinpthread_la-thread.o)\nLOAD D:/a/private/lib/libwinpthread.a\n"


class PrivatePthreadProvenanceTests(unittest.TestCase):
    def test_selected_provider_and_unique_definitions_pass(self):
        self.assertTrue(pthread_link_proof(MAP, SYMBOLS, SELECTED)["passed"])

    def test_original_provider_fallback_fails(self):
        added = "C:/rtools42/lib/libpthread.a(libwinpthread_la-thread.o)\n"
        proof = pthread_link_proof(MAP + added, SYMBOLS, SELECTED)
        self.assertFalse(proof["passed"])
        self.assertFalse(proof["only_selected_pthread_archives"])

    def test_dynamic_import_archive_fails(self):
        added = "D:/a/private/lib/libwinpthread.dll.a(import.o)\n"
        self.assertFalse(pthread_link_proof(MAP + added, SYMBOLS, SELECTED)["passed"])

    def test_missing_provider_or_implementation_fails(self):
        self.assertFalse(pthread_link_proof("", SYMBOLS, SELECTED)["passed"])
        self.assertFalse(pthread_link_proof(MAP, "", SELECTED)["passed"])

    def test_load_without_actual_archive_member_does_not_prove_provider(self):
        self.assertFalse(pthread_link_proof("LOAD D:/a/private/lib/libpthread.a\n", SYMBOLS, SELECTED)["passed"])

    def test_duplicate_key_implementation_fails(self):
        duplicate = SYMBOLS + "\n0000000180000010 T pthread_key_create\n"
        self.assertFalse(pthread_link_proof(MAP, duplicate, SELECTED)["passed"])

    def test_both_archive_files_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "libpthread.a").write_bytes(b"archive-one")
            with self.assertRaises(FileNotFoundError):
                isolated_pthread_archives(root)
            (root / "libwinpthread.a").write_bytes(b"archive-two")
            self.assertEqual(set(isolated_pthread_archives(root)), {"libpthread.a", "libwinpthread.a"})

    def test_unrelated_runtime_shadowing_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("libpthread.a", "libwinpthread.a", "libstdc++.a"):
                (root / name).write_bytes(b"archive")
            with self.assertRaisesRegex(ValueError, "unrelated link libraries"):
                isolated_pthread_archives(root)

    def test_default_entry_help_does_not_require_new_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "run_runtime.py"
            shutil.copyfile(Path(__file__).with_name("run_runtime.py"), copy)
            result = subprocess.run([sys.executable, str(copy), "--help"], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--pthread-library-dir", result.stdout)


if __name__ == "__main__":
    unittest.main()
