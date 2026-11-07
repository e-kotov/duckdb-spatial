"""Reject ambiguous archive/link evidence; these checks do not run Windows code."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_pthread_build as verify


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive(entries: list[tuple[str, bytes]]) -> bytes:
    data = b"!<arch>\n"
    for name, payload in entries:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(payload):<10}`\n".encode()
        if len(header) != 60:
            raise ValueError("Fixture member name is too long")
        data += header + payload + (b"\n" if len(payload) % 2 else b"")
    return data


class PreparationChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = self.work / "prepared"
        self.root.mkdir()
        installed = self.work / "installed"
        installed.mkdir()
        compiler = installed / "g++.exe"
        compiler.write_bytes(b"fixture compiler, not an executable")
        compiler_record = {"path": str(compiler), "before_sha256": verify.digest(compiler),
                           "after_sha256": verify.digest(compiler), "unchanged": True}
        self.manifest = {
            "schema": 1, "status": "prepared", "native_preparation_passed": True,
            "installed_inputs_unchanged": True, "layout": {"passed": True},
            "installed_public_headers_match_v9_normalized": True,
            "installed_inputs": {str(compiler): compiler_record},
            "tools": {"g++": dict(compiler_record)}, "objects": {}, "variants": {},
        }
        pinned = {"g++.exe": verify.digest(compiler)}
        for arm in verify.ARMS:
            directory = self.root / arm / "lib"
            directory.mkdir(parents=True)
            variant = {"library_dir": str(directory), "archives": {}}
            payload = (arm + " thread code").encode()
            if arm != "original":
                obj = self.root / "objects" / arm / "thread.o"
                obj.parent.mkdir(parents=True)
                obj.write_bytes(payload)
                self.manifest["objects"][arm] = {"path": str(obj.relative_to(self.root)), "sha256": sha(payload)}
            for name in verify.ARCHIVE_NAMES:
                members = [("thread.o", payload), ("mutex.o", ("unchanged " + name).encode())]
                path = directory / name
                path.write_bytes(archive(members))
                record = {
                    "path": str(path.relative_to(self.root)), "sha256": verify.digest(path),
                    "members": verify.member_manifest(members), "thread_provider": "thread.o",
                    "thread_object_sha256": sha(payload),
                }
                if arm == "original":
                    record.update(exact_installed_copy=True, thread_global_symbols_match_rebuild=True)
                    pinned[name] = verify.digest(path)
                    original = installed / name
                    original.write_bytes(path.read_bytes())
                    self.manifest["installed_inputs"][str(original)] = {
                        "path": str(original), "before_sha256": pinned[name],
                        "after_sha256": pinned[name], "unchanged": True,
                    }
                else:
                    record.update(all_other_member_payloads_identical=True, member_names_and_order_identical=True)
                variant["archives"][name] = record
            self.manifest["variants"][arm] = variant
        pin_patch = patch.dict(verify.PINNED_INSTALLED, pinned)
        pin_patch.start()
        self.addCleanup(pin_patch.stop)
        self.save()

    def save(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n")

    def test_complete_preparation_checks_all_six_archives_and_exact_manifest(self):
        result = verify.inspect_preparation(self.root)
        self.assertTrue(result["passed"])
        self.assertEqual(result["prep_manifest_sha256"], verify.digest(self.root / "manifest.json"))
        self.assertEqual(result["installed_inputs_checked"], 3)
        self.assertEqual(set(result["variants"]), set(verify.ARMS))
        self.assertTrue(all(len(row["archives"]) == 2 for row in result["variants"].values()))

    def test_source_only_or_missing_native_checks_cannot_pass(self):
        initial = deepcopy(self.manifest)
        for key, value in (("status", "sources-prepared"), ("native_preparation_passed", False),
                           ("installed_inputs_unchanged", False), ("schema", True),
                           ("layout", {"passed": False}), ("installed_public_headers_match_v9_normalized", False)):
            self.manifest = {**deepcopy(initial), key: value}
            self.save()
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify.inspect_preparation(self.root)

    def test_changed_private_archive_is_rejected(self):
        path = self.root / "reverse-order/lib/libpthread.a"
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "archive hash changed"):
            verify.inspect_preparation(self.root)

    def test_changed_installed_input_is_rejected(self):
        compiler = self.work / "installed/g++.exe"
        compiler.write_bytes(b"different compiler")
        with self.assertRaisesRegex(ValueError, "Installed input changed"):
            verify.inspect_preparation(self.root)

    def test_unrelated_member_change_cannot_be_hidden_by_updated_manifest(self):
        arm, name = "reverse-order", "libpthread.a"
        path = self.root / arm / "lib" / name
        members = verify.archive_members(path.read_bytes())
        changed = [members[0], ("mutex.o", b"unexpected unrelated change")]
        path.write_bytes(archive(changed))
        record = self.manifest["variants"][arm]["archives"][name]
        record.update(sha256=verify.digest(path), members=verify.member_manifest(changed))
        self.save()
        with self.assertRaisesRegex(ValueError, "Unrelated archive member changed"):
            verify.inspect_preparation(self.root)

    def test_rebuilt_object_must_match_actual_provider_bytes(self):
        obj = self.root / "objects/reverse-order/thread.o"
        obj.write_bytes(b"wrong replacement object")
        with self.assertRaisesRegex(ValueError, "rebuilt object no longer matches"):
            verify.inspect_preparation(self.root)

    def test_wrong_directory_or_unrelated_library_cannot_shadow_runtime(self):
        self.manifest["variants"]["reverse-order"]["library_dir"] = str(self.work / "installed")
        self.save()
        with self.assertRaisesRegex(ValueError, "library directory disagrees"):
            verify.inspect_preparation(self.root)
        self.manifest["variants"]["reverse-order"]["library_dir"] = str(self.root / "reverse-order/lib")
        self.save()
        (self.root / "reverse-order/lib/libstdc++.a").write_bytes(b"unexpected compiler runtime")
        with self.assertRaisesRegex(ValueError, "unrelated link libraries"):
            verify.inspect_preparation(self.root)

    def test_preparation_cli_retains_failed_check_and_returns_failure(self):
        self.manifest["native_preparation_passed"] = False
        self.save()
        output = self.work / "failure.json"
        argv = ["verify_pthread_build.py", "--preparation", str(self.root),
                "--check-preparation", "--output", str(output)]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            code = verify.main()
        report = json.loads(output.read_text())
        self.assertEqual(code, 1)
        self.assertIs(report["passed"], False)
        self.assertIn("Native preparation", report["error"])


class LinkProviderChecks(unittest.TestCase):
    def setUp(self):
        self.archives = {name: {"path": f"D:/private/reverse-order/lib/{name}", "sha256": "0" * 64,
                               "thread_provider": "libwinpthread_la-thread.o"}
                         for name in verify.ARCHIVE_NAMES}
        self.provider = self.archives["libpthread.a"]["path"]
        self.map = f"{self.provider}(libwinpthread_la-thread.o)\r\nLOAD {self.provider}\r\n"
        self.symbols = "\r\n".join(f"0000000180001000 T {name}" for name in verify.PROVIDER_SYMBOLS) + "\r\n"

    def test_actual_selected_member_and_five_unique_apis_pass(self):
        result = verify.inspect_link(self.map, self.symbols, self.archives)
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["selected_thread_members"]), 1)
        self.assertEqual(result["pthread_api_definition_counts"], {name: 1 for name in verify.PROVIDER_SYMBOLS})

    def test_search_flag_or_load_entry_without_member_is_insufficient(self):
        for mapping in (" -LD:/private/reverse-order/lib", f"LOAD {self.provider}\n",
                        self.map.replace("libwinpthread_la-thread.o", "different-thread.o")):
            with self.subTest(mapping=mapping):
                self.assertFalse(verify.inspect_link(mapping, self.symbols, self.archives)["passed"])

    def test_outside_or_dynamic_archive_fallback_is_rejected(self):
        for outside in ("C:/rtools42/lib/libpthread.a", "C:/rtools42/lib/LIBPTHREAD.A",
                        "D:/private/reverse-order/lib/libpthread.dll.a"):
            mapping = self.map + f"{outside}(other.o)\n"
            with self.subTest(outside=outside):
                self.assertFalse(verify.inspect_link(mapping, self.symbols, self.archives)["passed"])

    def test_each_api_must_have_exactly_one_strong_code_definition(self):
        for name in verify.PROVIDER_SYMBOLS:
            for changed in (self.symbols.replace(f"0000000180001000 T {name}\r\n", ""),
                            self.symbols + f"0000000180002000 T {name}\r\n",
                            self.symbols + f"0000000180002000 W {name}\r\n"):
                with self.subTest(name=name, changed=changed):
                    self.assertFalse(verify.inspect_link(self.map, changed, self.archives)["passed"])

    def test_two_selected_thread_members_are_ambiguous(self):
        second = self.archives["libwinpthread.a"]["path"]
        mapping = self.map + f"{second}(libwinpthread_la-thread.o)\n"
        self.assertFalse(verify.inspect_link(mapping, self.symbols, self.archives)["passed"])


if __name__ == "__main__":
    unittest.main()
