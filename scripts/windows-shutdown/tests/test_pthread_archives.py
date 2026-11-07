"""Portable checks for archive selection and isolation gates; no Windows claim."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "prepare_pthread_archives.py"
SPEC = importlib.util.spec_from_file_location("prepare_pthread_archives", SCRIPT)
prep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prep)


def archive(entries: list[tuple[str, bytes]]) -> bytes:
    data = b"!<arch>\n"
    for name, payload in entries:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(payload):<10}`\n".encode()
        if len(header) != 60:
            raise ValueError("Test archive name too long")
        data += header + payload + (b"\n" if len(payload) % 2 else b"")
    return data


class ArchiveIsolationTests(unittest.TestCase):
    def test_regular_archive_and_long_names(self):
        data = archive([("/", b"index"), ("//", b"long-thread-object.o/\n"),
                        ("/0", b"thread"), ("other.o/", b"other")])
        self.assertEqual(prep.archive_members(data),
                         [("long-thread-object.o", b"thread"), ("other.o", b"other")])

    def test_reject_thin_or_linker_script(self):
        for data in (b"!<thin>\n", b"INPUT(libwinpthread.a)\n"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                prep.archive_members(data)

    def test_reject_truncated_member_or_bad_name(self):
        for data in (archive([("thread.o/", b"odd")])[:-1],
                     archive([("../thread.o/", b"x")]),
                     archive([("/9", b"x")])):
            with self.subTest(data=data), self.assertRaises(ValueError):
                prep.archive_members(data)

    def test_reject_duplicate_provider_member_names(self):
        with self.assertRaises(ValueError):
            prep.archive_members(archive([("thread.o/", b"one"), ("thread.o/", b"two")]))

    def test_unrelated_member_cannot_change(self):
        before = [("thread.o", b"old"), ("other.o", b"keep")]
        passed = prep.verify_replacement(before, [("thread.o", b"new"), ("other.o", b"keep")],
                                         "thread.o", b"new")
        self.assertEqual(passed["changed_members"], ["thread.o"])
        for after in ([("thread.o", b"new"), ("other.o", b"changed")],
                      [("thread.o", b"old"), ("other.o", b"keep"), ("new.o", b"new")],
                      [("other.o", b"keep"), ("thread.o", b"new")],
                      [("thread.o", b"wrong"), ("other.o", b"keep")]):
            with self.subTest(after=after), self.assertRaises(ValueError):
                prep.verify_replacement(before, after, "thread.o", b"new")

    def test_provider_must_be_one_real_code_member(self):
        symbols = {name: [("libwinpthread_la-thread.o", "T")] for name in prep.PROVIDER_SYMBOLS}
        self.assertEqual(prep.select_provider(symbols), "libwinpthread_la-thread.o")
        for replacement in ([], [("one.o", "T"), ("two.o", "T")], [("stub.o", "I")], [("different.o", "T")]):
            altered = {**symbols, "pthread_key_delete": replacement}
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                prep.select_provider(altered)

    def test_nm_windows_drive_and_global_types(self):
        text = "\n".join(f"C:/rtools42/lib/libpthread.a[thread.o]: {name} T 123 40"
                         for name in prep.PROVIDER_SYMBOLS)
        self.assertEqual(prep.select_provider(prep.parse_archive_nm(text)), "thread.o")
        with self.assertRaises(ValueError):
            prep.parse_archive_nm("unexpected output that must not be ignored")

    def test_layout_rejects_wrong_offset_missing_extra_or_boolean(self):
        prep.validate_layout(dict(prep.EXPECTED_LAYOUT))
        for altered in ({**prep.EXPECTED_LAYOUT, "thread_tid": 108},
                        {key: value for key, value in prep.EXPECTED_LAYOUT.items() if key != "thread_tid"},
                        {**prep.EXPECTED_LAYOUT, "extra": 0},
                        {**prep.EXPECTED_LAYOUT, "foreign_bitfield_changed_bytes": True}):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                prep.validate_layout(altered)

    def test_predefines_accept_native_crlf_and_lf(self):
        lines = (b"#define __SEH__ 1", b"#define __SIZEOF_POINTER__ 8", b"#define _WIN64 1")
        for newline in (b"\n", b"\r\n"):
            for final_newline in (b"", newline):
                with self.subTest(newline=newline, final_newline=final_newline):
                    prep.validate_predefines(newline.join(lines) + final_newline)

    def test_predefines_reject_wrong_missing_or_ambiguous_macros(self):
        valid = b"#define __SEH__ 1\r\n#define __SIZEOF_POINTER__ 8\r\n"
        for altered in (valid.replace(b"__SEH__ 1", b"__SEH__ 0"),
                        valid.replace(b"__SIZEOF_POINTER__ 8", b"__SIZEOF_POINTER__ 4"),
                        valid.replace(b"#define __SEH__ 1\r\n", b""),
                        valid.replace(b"#define __SIZEOF_POINTER__ 8\r\n", b""),
                        valid.replace(b"__SEH__ 1", b"__SEH__ 10"),
                        valid + b"#define __SEH__ 1\r\n",
                        valid + b"#define\t__SIZEOF_POINTER__\t4\r\n"):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                prep.validate_predefines(altered)

    def test_patch_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            prep.reverse_source(b"void _pthread_cleanup_dest(void) {}\n")

    @unittest.skipUnless(shutil.which("ar"), "Host ar required for real binary extraction")
    def test_real_archive_extraction_preserves_every_binary_byte(self):
        payload = bytes(range(256)) * 3 + b"\n\x00\r\n\r\r\n\x1a"
        original = archive([("thread.o/", payload), ("other.o/", b"untouched")])
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "original.a"
            source.write_bytes(original)
            output = work / "output"
            output.mkdir()
            preparation = prep.Preparation(output)
            directory = output / "objects"
            extracted = preparation.extract_archive_member(
                source, "extract", Path(shutil.which("ar")), "thread.o", payload, directory)
            self.assertEqual(extracted.read_bytes(), payload)
            self.assertEqual(set(directory.iterdir()), {extracted})
            self.assertEqual(source.read_bytes(), original)

    @unittest.skipUnless(shutil.which("ar"), "Host ar required for real binary extraction")
    def test_real_extraction_rejects_disagreement_without_normalizing_bytes(self):
        payload = b"binary\x00\n\r\n\r\r\n\x1a"
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "original.a"
            source.write_bytes(archive([("thread.o/", payload)]))
            output = work / "output"
            output.mkdir()
            preparation = prep.Preparation(output)
            for index, incorrect in enumerate((payload.replace(b"\n", b"\r\n"), payload + b"x")):
                directory = output / f"objects-{index}"
                with self.subTest(expected=incorrect), self.assertRaisesRegex(
                        ValueError, "ar/parser extracted object disagreement"):
                    preparation.extract_archive_member(source, f"extract-{index}",
                                                       Path(shutil.which("ar")), "thread.o",
                                                       incorrect, directory)
                self.assertEqual((directory / "thread.o").read_bytes(), payload)

    @unittest.skipUnless(all(shutil.which(name) for name in ("cc", "ar", "nm")),
                         "Host toolchain required for portable real-archive roundtrip")
    def test_real_host_archive_selection_and_member_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            names = prep.PROVIDER_SYMBOLS
            source = "\n".join(f"int {name}(void) {{ return 1; }}" for name in names)
            (work / "thread.c").write_text(source)
            (work / "other.c").write_text("int untouched(void) { return 3; }\n")
            thread = "libwinpthread_la-thread.o"
            def run(*args):
                return subprocess.run(args, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      check=True, timeout=30).stdout
            run("cc", "-c", "thread.c", "-o", thread)
            run("cc", "-c", "other.c", "-o", "other.o")
            run("ar", "rcs", "libpthread.a", thread, "other.o")
            before = prep.archive_members((work / "libpthread.a").read_bytes())
            symbols = prep.parse_archive_nm(run("nm", "-A", "-P", "-g", "--defined-only", "libpthread.a").decode())
            self.assertEqual(prep.select_provider(symbols), thread)
            self.assertEqual(run("ar", "t", "libpthread.a").decode().splitlines(), [name for name, _ in before])
            (work / "thread.c").write_text(source.replace("return 1", "return 2"))
            run("cc", "-c", "thread.c", "-o", thread)
            run("ar", "r", "libpthread.a", thread)
            run("ar", "s", "libpthread.a")
            after = prep.archive_members((work / "libpthread.a").read_bytes())
            proof = prep.verify_replacement(before, after, thread, (work / thread).read_bytes())
            self.assertTrue(proof["all_other_member_payloads_identical"])


if __name__ == "__main__":
    unittest.main()
