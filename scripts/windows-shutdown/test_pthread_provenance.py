"""Reject private-archive fallback and preserve the standalone default entry."""

from pathlib import Path
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pthread_provenance import isolated_pthread_archives, pthread_link_proof
import run_runtime


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

    def check_driver_inputs(self, use_private):
        """Exercise pre-link selection with the observed compiler lookup behavior.

        Stop at the first native build: this checks driver bookkeeping only,
        while the separate map/symbol tests enforce actual provider evidence.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed, private = root / "installed", root / "private"
            installed.mkdir()
            private.mkdir()
            for name in ("libstdc++.a", "libpthread.a", "libwinpthread.a"):
                (installed / name).write_bytes(("installed " + name).encode())
                if name != "libstdc++.a":
                    (private / name).write_bytes(("private " + name).encode())
            cxx = root / "g++.exe"
            cxx.write_bytes(b"compiler identity fixture")
            baseline, guarded = root / "baseline.cc", root / "guarded.cc"
            baseline.write_text("baseline fixture")
            guarded.write_text("guarded fixture")
            output = root / "output"
            calls = []

            def compiler(argv, **kwargs):
                calls.append(argv)
                if "-dumpfullversion" in argv:
                    value = "10.4.0\r\n"
                elif "-dumpmachine" in argv:
                    value = "x86_64-w64-mingw32.static.posix\r\n"
                elif any(arg.startswith("-print-file-name=") for arg in argv):
                    name = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("-print-file-name="))
                    # Deliberately ignore -L, as the real Rtools42 query did.
                    value = str(installed / name) + "\r\n"
                else:
                    return {"command": argv, "returncode": 123, "stdout": "", "stderr": "stop before native build"}
                return {"command": argv, "returncode": 0, "stdout": value, "stderr": ""}

            argv = ["run_runtime.py", "--cxx", str(cxx), "--baseline-runtime-source", str(baseline),
                    "--guarded-runtime-source", str(guarded), "--output-dir", str(output)]
            if use_private:
                argv.extend(["--pthread-library-dir", str(private)])
            with patch.object(run_runtime, "os", SimpleNamespace(name="nt")), \
                    patch.object(run_runtime, "command", side_effect=compiler), \
                    patch.object(run_runtime.shutil, "which", side_effect=lambda value: value), \
                    patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_runtime.main(), 1)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["error"], "RuntimeError: Native C host failed to compile")
            self.assertFalse(summary["candidate_validated_for_runtime_fixture"])
            for name in ("libpthread.a", "libwinpthread.a"):
                for field, folder in (("compiler_default_thread_runtime_archives", installed),
                                      ("thread_runtime_archives", private if use_private else installed)):
                    # Windows TEMP may use an 8.3 alias such as RUNNER~1.
                    path = (folder / name).resolve(strict=True)
                    self.assertEqual(summary[field][name], {
                        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    })
            queries = [call for call in calls if any(arg.startswith("-print-file-name=") for arg in call)]
            self.assertTrue(queries)
            self.assertFalse(any(arg.startswith("-L") for call in queries for arg in call))
            self.assertEqual(f"-L{private.resolve(strict=True)}" in calls[-1], use_private)

    def test_private_driver_input_is_independent_of_compiler_default_lookup(self):
        self.check_driver_inputs(True)

    def test_default_driver_input_still_uses_installed_lookup(self):
        self.check_driver_inputs(False)


if __name__ == "__main__":
    unittest.main()
