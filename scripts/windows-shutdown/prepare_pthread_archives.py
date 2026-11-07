#!/usr/bin/env python3
"""Prepare isolated Rtools42 pthread archives for a bounded native experiment.

The installed archives are read-only inputs. Each private archive retains every
member except the verified thread.c provider. The rebuilt-baseline and
reverse-order arms use identical flags; the latter differs only by the two
changes in MinGW commit 8e06daa36dfcea4bb491acf4b350658f40738f02.

This is diagnostic preparation, not a production runtime installer. A successful
preparation proves input, ABI and member-selection checks, not cleanup safety.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import urllib.request


MINGW_COMMIT = "acc9b9d9eb63a13d8122cbac4882eb5f4ee2f679"
REVERSE_COMMIT = "8e06daa36dfcea4bb491acf4b350658f40738f02"
SOURCE_ROOT = "mingw-w64-libraries/winpthreads"
PATCHED_SHA256 = "1726a2bb0b1feeed57d5f4448b49254f615c9d883043f6a1d427e5146e135d2d"
SOURCES = {
    "Makefile.am": "4ce2d152bafc67206ea27f2631c4ff19b315c04421b84ca968e94ba1c906b90c",
    "src/thread.c": "1e366d8e2dc774665c2e4d3332fd8c853137a4abef6bf6192195eb2b15b36b32",
    "src/thread.h": "efea946574310311d58542b2826e7abe44711a21990df6d788d157d9db8b1ca7",
    "src/rwlock.h": "2a0cb96652a83efa00256c695707c6e0b32bfcb8173d9414dc28ff270a50c3e1",
    "src/misc.h": "f9533b18e4a78c79b1c94b527458a875602735a16485b14f8c2254b158a74c60",
    "src/winpthread_internal.h": "3cf8a27a0d51a9c009bd8d3109696d1489b5e2308637debff352f90295ce61d0",
    "include/pthread.h": "a193b89b9f6ba100af64af40a65612b5461df8df4e729329a0b773a4da59c90a",
    "include/pthread_compat.h": "7c42868767b70f45ebbd41829b01d17cc4c50df1894791d729ae057c77ece393",
    "include/pthread_unistd.h": "920925577b990df78791e757b557bf83c2c4f9a6b91e87b3178a97258b011da2",
    "include/pthread_time.h": "f5523cd6f58a1e77c822b3db4961acb9e2d5f07e8454a0b4cde3a992dfd3f867",
    "include/pthread_signal.h": "46915f25fb94f61ac4bdfa9b38736e6524a5b36bc42b00f7e87526fbc24287a6",
    "include/sched.h": "9af39f7a9d87a4e5efb9f39eec495c4483680ec501a4bdce69bcbde36cbf0f19",
    "include/semaphore.h": "513868e6d45ceae605542ad6659bae0bf5d21530b22d95ed41afcf96243afc68",
}
ARCHIVE_NAMES = ("libpthread.a", "libwinpthread.a")
PINNED_INSTALLED = {
    "g++.exe": "427db0e9383b2abb628b99ebcaff80d635351353dc67cf051b43de73cfaa868c",
    "libstdc++.a": "a51ab30b812a682ba6a8db469ee9c48e74c902349e3372f009fcd7ee3c4a5de9",
    "libgcc.a": "95acd6de95cdf0fa93d7439dbae0bcd21cc3e26510be8a5ace9b384d3a7e6031",
    "libpthread.a": "c9be6014ca8f70e5f3de095089f2cd4f0ad0e15d7fefe335caaf4928075f3745",
    "libwinpthread.a": "9f4e33d7a96f88e07adedabe1b56151f59a90334934fa3e77deb09067242c3b8",
}
PROVIDER_SYMBOLS = ("_pthread_cleanup_dest", "pthread_key_create", "pthread_key_delete",
                    "pthread_create", "pthread_join")
COMPILE_FLAGS = ["-std=gnu11", "-O2", "-g", "-Wall", "-DWIN32_LEAN_AND_MEAN",
                 "-DIN_WINPTHREAD", "-DWINPTHREAD_DBG=1", "-D__USE_MINGW_ANSI_STDIO=0"]
# _pthread_v offsets/size and foreign bit are independently mapped from the
# preserved 3de3f298... Spatial DLL. Scalar/rwlock sizes additionally follow the
# pinned v9 declarations under the Windows x64 ABI. No vendor flag identity is
# inferred from these checks; the unmodified rebuild is a separate control.
EXPECTED_LAYOUT = {
    "pointer_size": 8, "pthread_t_size": 8, "pthread_key_t_size": 4,
    "pthread_mutex_t_size": 8, "pthread_spinlock_t_size": 8, "pthread_rwlock_t_size": 8,
    "DWORD_size": 4, "LONG_size": 4, "thread_size": 480, "thread_h": 40,
    "thread_keymax": 72, "thread_keyval": 80, "thread_keyval_set": 88,
    "thread_spin_keys": 104, "thread_tid": 112, "thread_rwlc": 116,
    "thread_ended": 188, "foreign_bitfield_changed_bytes": 1,
    "foreign_bitfield_offset": 64, "foreign_bitfield_mask": 16,
    "rwlock_size": 48, "rwlock_valid": 0, "rwlock_busy": 4,
    "rwlock_nex_count": 8, "rwlock_nsh_count": 12, "rwlock_ncomplete": 16,
    "rwlock_mex": 24, "rwlock_mcomplete": 32, "rwlock_ccomplete": 40,
    "destructor_iterations": 256,
}
LAYOUT_REFERENCE = {
    "diagnostic_report": "https://github.com/e-kotov/duckspatial/blob/fe62f8b04c3e879ed3d143956cf73095302f6f5f/.github/windows-native-exit-capture-report.md",
    "preserved_spatial_sha256": "3de3f298b4dcaa10f3e081d6ff1e411cf44d487d19317be28bd20303494a1415",
    "additional_preserved_binary_mapping": {
        "allocation_function": "pop_pthread_mem",
        "preferred_calloc_call_address": "0x293be0db2",
        "allocation_bytes": 480,
        "foreign_flag_byte_offset": 64,
        "foreign_flag_mask": 16,
    },
    "scope": "Checks compiled v9 private layout against mapped preserved-binary fields and Windows x64 scalar ABI; does not claim identical vendor build flags.",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    return digest(path.read_bytes())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def reverse_source(data: bytes) -> bytes:
    require(digest(data) == SOURCES["src/thread.c"], "Unrecognized baseline thread.c")
    source = data.decode("utf-8")
    changes = (
        ("_pthread_cleanup_dest (pthread_t t)\n{\n\t_pthread_v *tv;\n\tunsigned int i, j;",
         "_pthread_cleanup_dest (pthread_t t)\n{\n\t_pthread_v *tv;\n\tunsigned int j;\n\tint i;"),
        ("for (i = 0; i < tv->keymax; i++)", "for (i = tv->keymax - 1; i >= 0; i--)"),
    )
    for old, new in changes:
        require(source.count(old) == 1, f"Patch anchor not unique: {old!r}")
        source = source.replace(old, new, 1)
    patched = source.encode("utf-8")
    require(digest(patched) == PATCHED_SHA256, "Unexpected reverse-order source bytes")
    return patched


def fetch_sources(output: Path, cache: Path | None) -> dict:
    result = {"commit": MINGW_COMMIT, "reverse_commit": REVERSE_COMMIT, "files": {}}
    for name, expected in SOURCES.items():
        url = f"https://raw.githubusercontent.com/mingw-w64/mingw-w64/{MINGW_COMMIT}/{SOURCE_ROOT}/{name}"
        cached = cache / name if cache else None
        if cached is not None and cached.is_file():
            data = cached.read_bytes()
        else:
            with urllib.request.urlopen(url, timeout=45) as response:
                data = response.read()
        require(digest(data) == expected, f"Immutable input hash mismatch: {name}")
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        result["files"][name] = {"url": url, "sha256": expected, "bytes": len(data)}
    original = (output / "src/thread.c").read_bytes()
    patched = reverse_source(original)
    (output / "reverse-thread.c").write_bytes(patched)
    patch = "".join(difflib.unified_diff(
        original.decode().splitlines(keepends=True), patched.decode().splitlines(keepends=True),
        fromfile=f"a/{SOURCE_ROOT}/src/thread.c", tofile=f"b/{SOURCE_ROOT}/src/thread.c"))
    (output / "reverse-order.patch").write_text(patch, encoding="utf-8", newline="\n")
    result["generated"] = {"reverse-thread.c": {"sha256": digest(patched)},
                           "reverse-order.patch": {"sha256": digest(patch.encode())}}
    return result


def archive_members(data: bytes) -> list[tuple[str, bytes]]:
    """Read normal GNU/COFF ar payloads, independent of ar's symbol index."""
    require(data.startswith(b"!<arch>\n"), "Expected a regular archive, not thin/import script")
    offset, longnames, raw = 8, b"", []
    while offset < len(data):
        require(offset + 60 <= len(data), "Truncated archive header")
        header = data[offset:offset + 60]
        require(header[58:60] == b"`\n", "Invalid archive header terminator")
        size_text = header[48:58].decode("ascii").strip()
        require(size_text.isdecimal(), "Invalid archive member size")
        size = int(size_text)
        start, end = offset + 60, offset + 60 + size
        require(end <= len(data), "Truncated archive member")
        name, payload = header[:16].decode("ascii").rstrip(), data[start:end]
        if name == "//":
            require(not longnames, "Duplicate GNU archive name table")
            longnames = payload
        elif name not in ("/", "/SYM64/"):
            raw.append((name, payload))
        offset = end + (size % 2)
        require(offset <= len(data), "Missing archive padding")
    require(offset == len(data), "Trailing partial archive data")
    members, seen = [], set()
    for name, payload in raw:
        if re.fullmatch(r"/\d+", name):
            position = int(name[1:])
            require(position < len(longnames), "Archive name offset outside table")
            end = longnames.find(b"/\n", position)
            require(end >= position, "Unterminated GNU archive name")
            name = longnames[position:end].decode("utf-8")
        elif name.endswith("/"):
            name = name[:-1]
        require(bool(re.fullmatch(r"[A-Za-z0-9_.+-]+", name)) and name not in (".", ".."),
                f"Unsafe/unsupported archive member name: {name!r}")
        require(name not in seen, f"Duplicate archive member name: {name}")
        seen.add(name)
        members.append((name, payload))
    require(bool(members), "Archive contains no object members")
    return members


def member_manifest(members: list[tuple[str, bytes]]) -> list[dict]:
    return [{"name": name, "sha256": digest(payload), "bytes": len(payload)}
            for name, payload in members]


def verify_replacement(before: list[tuple[str, bytes]], after: list[tuple[str, bytes]],
                       provider: str, replacement: bytes) -> dict:
    require([name for name, _ in before] == [name for name, _ in after],
            "Archive member names/order changed")
    changed = []
    for (name, old), (_, new) in zip(before, after):
        if name == provider:
            require(new == replacement, "Selected provider is not the compiled replacement")
        else:
            require(old == new, f"Unrelated archive member changed: {name}")
        if old != new:
            changed.append(name)
    require(changed == [provider], "Expected exactly the selected member to change")
    return {"changed_members": changed, "all_other_member_payloads_identical": True,
            "member_names_and_order_identical": True}


def parse_archive_nm(text: str) -> dict[str, list[tuple[str, str]]]:
    """Parse GNU nm -A -P output (archive[member]: symbol type ...)."""
    symbols: dict[str, list[tuple[str, str]]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r".+\[([^\[\]]+)\]:\s+(\S+)\s+([A-Za-z?])(?:\s+.*)?", line)
        require(match is not None, f"Unrecognized archive nm record: {line!r}")
        member, name, kind = match.groups()
        symbols.setdefault(name, []).append((member, kind))
    return symbols


def select_provider(symbols: dict[str, list[tuple[str, str]]]) -> str:
    providers = []
    for name in PROVIDER_SYMBOLS:
        definitions = symbols.get(name, [])
        require(len(definitions) == 1 and definitions[0][1] == "T",
                f"Expected exactly one strong code definition of {name}: {definitions}")
        providers.append(definitions[0][0])
    require(len(set(providers)) == 1, f"Thread APIs have different archive providers: {providers}")
    return providers[0]


def parse_object_nm(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        require(len(fields) >= 2 and len(fields[1]) == 1,
                f"Unrecognized object nm record: {line!r}")
        name, kind = fields[:2]
        require(name not in result, f"Duplicate object symbol: {name}")
        result[name] = kind
    return result


def validate_layout(observed: dict) -> None:
    require(set(observed) == set(EXPECTED_LAYOUT), "Layout field set changed")
    for name, expected in EXPECTED_LAYOUT.items():
        require(type(observed[name]) is int and observed[name] == expected,
                f"ABI mismatch: {name} is {observed[name]!r}, expected {expected}")


def validate_predefines(data: bytes) -> None:
    # Windows GCC writes CRLF. Keep the raw command output unchanged, but compare
    # complete macro definitions after normalizing only that line ending.
    lines = data.replace(b"\r\n", b"\n").split(b"\n")
    for name, value, message in (
        (b"__SEH__", b"1", "Expected SEH exception model"),
        (b"__SIZEOF_POINTER__", b"8", "Expected x64 pointer ABI"),
    ):
        definitions = [line for line in lines
                       if re.match(rb"#define[ \t]+" + name + rb"(?:[ \t]|$)", line)]
        require(definitions == [b"#define " + name + b" " + value], message)


class Preparation:
    def __init__(self, output: Path):
        self.output = output
        self.manifest = {"schema": 1, "status": "preparing", "native_preparation_passed": False,
                         "commands": [], "installed_inputs": {}, "variants": {}}
        (output / "logs").mkdir()

    def save(self) -> None:
        (self.output / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    def run(self, label: str, arguments: list[str | Path], *, cwd: Path | None = None,
            input_bytes: bytes | None = None) -> bytes:
        args = [str(item) for item in arguments]
        record = {"label": label, "argv": args, "cwd": str(cwd or Path.cwd())}
        self.manifest["commands"].append(record)
        self.save()
        result = subprocess.run(args, cwd=cwd, input=input_bytes, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=60, check=False)
        for stream, data in (("stdout", result.stdout), ("stderr", result.stderr)):
            path = self.output / "logs" / f"{label}.{stream}.log"
            path.write_bytes(data)
            record[stream] = {"path": str(path.relative_to(self.output)), "sha256": digest(data)}
        record["returncode"] = result.returncode
        self.save()
        require(result.returncode == 0, f"Command {label} failed ({result.returncode}); see raw logs")
        return result.stdout

    def track(self, path: Path, *, expected: str | None = None) -> dict:
        path = path.resolve(strict=True)
        require(path.is_file(), f"Expected installed file: {path}")
        actual = file_digest(path)
        if expected is not None:
            require(actual == expected, f"Installed input hash mismatch: {path.name}: {actual}")
        record = {"path": str(path), "before_sha256": actual, "bytes": path.stat().st_size}
        self.manifest["installed_inputs"].setdefault(str(path), record)
        return record

    def audit_installed_inputs(self) -> None:
        changed = []
        for record in self.manifest["installed_inputs"].values():
            path = Path(record["path"])
            actual = file_digest(path) if path.is_file() else None
            record["after_sha256"] = actual
            record["unchanged"] = actual == record["before_sha256"]
            if not record["unchanged"]:
                changed.append(str(path))
        self.manifest["installed_inputs_unchanged"] = not changed
        require(not changed, f"Installed inputs changed during preparation: {changed}")

    def inspect_archive(self, path: Path, label: str, ar: Path, nm: Path) -> tuple[list, str]:
        members = archive_members(path.read_bytes())
        listing = self.run(f"{label}-ar-list", [ar, "t", path]).decode().splitlines()
        require(listing == [name for name, _ in members], "ar/parser member disagreement")
        listing_nm = self.run(f"{label}-nm", [nm, "-A", "-P", "-g", "--defined-only", path])
        provider = select_provider(parse_archive_nm(listing_nm.decode()))
        require(provider in dict(members), "nm provider absent from archive")
        return members, provider

    def extract_archive_member(self, path: Path, label: str, ar: Path, provider: str,
                               expected: bytes, directory: Path) -> Path:
        # Rtools42 ar p expands LF bytes to CRLF on stdout, including in COFF
        # object data. Extract to a fresh private file; never normalize binaries.
        directory.mkdir(parents=True)
        self.run(label, [ar, "x", path.resolve(strict=True), provider], cwd=directory)
        extracted = directory / provider
        require(extracted.is_file() and not extracted.is_symlink(),
                "ar did not extract a regular provider object")
        require(extracted.read_bytes() == expected, "ar/parser extracted object disagreement")
        return extracted


def included_headers(preprocessed: str) -> list[Path]:
    paths = set()
    for match in re.finditer(r'^#\s+\d+\s+("(?:[^"\\]|\\.)*")', preprocessed, re.MULTILINE):
        # GCC emits C-escaped paths, including doubled backslashes on Windows.
        name = json.loads(match.group(1))
        if name.startswith("<"):
            continue
        path = Path(name).resolve(strict=True)
        if path.suffix.lower() == ".h":
            paths.add(path)
    require(bool(paths), "No actual preprocessor headers recorded")
    return sorted(paths, key=str)


def native_prepare(prep: Preparation, cxx: Path) -> None:
    require(os.name == "nt", "Archive rebuilding and ABI execution require actual Windows")
    cxx = cxx.resolve(strict=True)
    toolchain = cxx.parent.parent
    require(not prep.output.is_relative_to(toolchain), "Output must be outside installed toolchain")
    tools = {"g++": cxx}
    for name in ("gcc", "ar", "nm", "objdump"):
        tools[name] = (cxx.parent / f"{name}.exe").resolve(strict=True)
    prep.manifest["tools"] = {}
    for name, path in tools.items():
        info = prep.track(path, expected=PINNED_INSTALLED["g++.exe"] if name == "g++" else None)
        version = prep.run(f"{name.replace('+', 'x')}-version", [path, "--version"]).decode()
        prep.manifest["tools"][name] = {**info, "version": version}
    for name in ("gcc", "g++"):
        version = prep.run(f"{name.replace('+', 'x')}-numeric-version",
                           [tools[name], "-dumpfullversion"]).decode().strip()
        target = prep.run(f"{name.replace('+', 'x')}-target",
                          [tools[name], "-dumpmachine"]).decode().strip()
        require(version == "10.4.0", f"Expected GCC 10.4.0, got {version}")
        require(target in ("x86_64-w64-mingw32", "x86_64-w64-mingw32.static.posix"),
                f"Unexpected compiler target: {target}")
        prep.manifest["tools"][name].update(target=target, numeric_version=version)
    require(prep.manifest["tools"]["gcc"]["target"] == prep.manifest["tools"]["g++"]["target"],
            "C and C++ compiler targets differ")
    for name in ("cc1", "collect2", "ld"):
        resolved = prep.run(f"program-{name}", [tools["gcc"], f"-print-prog-name={name}"]).decode().strip()
        path = Path(resolved)
        if not path.is_absolute():
            path = cxx.parent / path
        if not path.is_file() and not path.suffix:
            path = path.with_suffix(".exe")
        path = path.resolve(strict=True)
        require(path.is_relative_to(toolchain), f"Program outside selected toolchain: {path}")
        prep.manifest["tools"][name] = prep.track(path)

    installed = {}
    for name in ("libstdc++.a", "libgcc.a", *ARCHIVE_NAMES):
        raw = prep.run(f"lookup-{name}", [cxx, f"-print-file-name={name}"]).decode().strip()
        path = Path(raw).resolve(strict=True)
        require(path.is_relative_to(toolchain), f"Archive outside selected toolchain: {path}")
        prep.track(path, expected=PINNED_INSTALLED[name])
        installed[name] = path
    # Preserve these read-only input copies even if a later header/layout check
    # fails, so an independent review can inspect the exact rejected inputs.
    original_dir = prep.output / "original/lib"
    original_dir.mkdir(parents=True)
    for name in ARCHIVE_NAMES:
        shutil.copyfile(installed[name], original_dir / name)
        require(file_digest(original_dir / name) == PINNED_INSTALLED[name], "Original private copy differs")

    inputs = prep.output / "inputs"
    private_headers = inputs / "src"
    flags = [*COMPILE_FLAGS, "-I", str(private_headers)]
    prep.manifest["compile_flags"] = flags
    prep.manifest["vendor_flag_equivalence_claimed"] = False
    prep.manifest["control_scope"] = (
        "Original installed archive copies versus an unmodified v9 thread.c rebuild distinguish "
        "rebuild/source/vendor-flag effects; the reverse-order arm differs from that rebuild only "
        "by the accepted two-line upstream change. No installed library is patched.")
    preprocessed = prep.run("thread-preprocessed", [tools["gcc"], *flags, "-E", "-dD", inputs / "src/thread.c"])
    (inputs / "thread.preprocessed.c").write_bytes(preprocessed)
    headers = included_headers(preprocessed.decode())
    header_records, actual_public = [], {}
    prep.manifest["headers"] = header_records
    source_public = {Path(name).name: name for name in SOURCES if name.startswith("include/")}
    for path in headers:
        data = path.read_bytes()
        is_private = path.is_relative_to(private_headers)
        require(is_private or path.is_relative_to(toolchain), f"Unexpected header provider: {path}")
        record = {"path": str(path), "sha256": digest(data), "bytes": len(data), "private": is_private}
        destination = prep.output / "headers" / record["sha256"] / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        record["copy"] = str(destination.relative_to(prep.output))
        if not is_private:
            prep.track(path)
        if path.name in source_public:
            reference = (inputs / source_public[path.name]).read_bytes()
            require(not is_private and data.replace(b"\r\n", b"\n") == reference.replace(b"\r\n", b"\n"),
                    f"Installed public header differs from pinned v9 reference: {path}")
            require(path.name not in actual_public, f"Multiple public header providers: {path.name}")
            actual_public[path.name] = record
        header_records.append(record)
    # pthread_time.h is included by the MSVC compatibility branch only, not by
    # this GCC build. Validate it if actually included, without inventing an
    # include dependency absent from the pinned GCC preprocessor path.
    require({"pthread.h", "pthread_compat.h", "pthread_unistd.h"}.issubset(actual_public),
            "Required installed public pthread headers were not observed")
    require({path.name for path in headers if path.is_relative_to(private_headers)} ==
            {"thread.h", "rwlock.h", "misc.h", "winpthread_internal.h"}, "Private include closure changed")
    prep.manifest["installed_public_headers_match_v9_normalized"] = True
    macros = prep.run("compiler-predefines", [tools["gcc"], *flags, "-dM", "-E", "-x", "c", "-"], input_bytes=b"")
    validate_predefines(macros)

    layout_dir = prep.output / "layout"
    layout_dir.mkdir()
    layout_source = Path(__file__).resolve().parent / "runtime_tests/pthread_layout.c"
    shutil.copyfile(layout_source, layout_dir / "pthread_layout.c")
    layout_exe = layout_dir / "pthread_layout.exe"
    prep.run("layout-build", [tools["gcc"], *flags, layout_dir / "pthread_layout.c", "-o", layout_exe])
    observed = json.loads(prep.run("layout-run", [layout_exe]).decode())
    validate_layout(observed)
    prep.manifest["layout"] = {"observed": observed, "expected": EXPECTED_LAYOUT,
                               "reference": LAYOUT_REFERENCE, "passed": True,
                               "source_sha256": file_digest(layout_source), "executable_sha256": file_digest(layout_exe)}

    objects = prep.output / "objects"
    objects.mkdir()
    rebuilt_objects, rebuilt_symbols = {}, {}
    for arm, source in (("rebuilt-baseline", inputs / "src/thread.c"),
                        ("reverse-order", inputs / "reverse-thread.c")):
        directory = objects / arm
        directory.mkdir()
        obj = directory / "thread.o"
        prep.run(f"{arm}-compile", [tools["gcc"], *flags, "-c", source, "-o", obj])
        require(len(obj.read_bytes()) >= 20 and struct.unpack_from("<H", obj.read_bytes())[0] == 0x8664,
                f"Replacement is not an AMD64 COFF object: {obj}")
        definitions = prep.run(f"{arm}-defined", [tools["nm"], "-P", "-g", "--defined-only", obj])
        rebuilt_symbols[arm] = parse_object_nm(definitions.decode())
        prep.run(f"{arm}-undefined", [tools["nm"], "-P", "-g", "--undefined-only", obj])
        prep.run(f"{arm}-disassembly", [tools["objdump"], "-dr", "-Mintel", obj])
        rebuilt_objects[arm] = obj
    require(rebuilt_symbols["rebuilt-baseline"] == rebuilt_symbols["reverse-order"],
            "Two-line patch changed global symbol definitions")
    prep.manifest["objects"] = {arm: {"path": str(path.relative_to(prep.output)),
                                     "sha256": file_digest(path), "defined_symbols": rebuilt_symbols[arm]}
                                for arm, path in rebuilt_objects.items()}

    for arm in ("original", "rebuilt-baseline", "reverse-order"):
        (prep.output / arm / "lib").mkdir(parents=True, exist_ok=True)
        prep.manifest["variants"][arm] = {"library_dir": str(prep.output / arm / "lib"), "archives": {}}
    for name in ARCHIVE_NAMES:
        original = prep.output / "original/lib" / name
        require(file_digest(original) == PINNED_INSTALLED[name], "Original private copy differs")
        before, provider = prep.inspect_archive(original, f"original-{name}", tools["ar"], tools["nm"])
        payload = dict(before)[provider]
        require(len(payload) >= 20 and struct.unpack_from("<H", payload)[0] == 0x8664,
                "Original thread provider is not a regular AMD64 COFF object")
        object_dir = objects / "original" / name
        original_obj = prep.extract_archive_member(original, f"original-{name}-extract", tools["ar"],
                                                   provider, payload, object_dir)
        definitions = prep.run(f"original-{name}-defined", [tools["nm"], "-P", "-g", "--defined-only", original_obj])
        original_symbols = parse_object_nm(definitions.decode())
        require(original_symbols == rebuilt_symbols["rebuilt-baseline"],
                f"Original/rebuilt global symbol definitions differ: {name}; inspect raw nm output")
        prep.run(f"original-{name}-undefined", [tools["nm"], "-P", "-g", "--undefined-only", original_obj])
        prep.run(f"original-{name}-disassembly", [tools["objdump"], "-dr", "-Mintel", original_obj])
        prep.manifest["variants"]["original"]["archives"][name] = {
            "path": str(original.relative_to(prep.output)), "sha256": file_digest(original),
            "members": member_manifest(before), "thread_provider": provider,
            "thread_object_sha256": digest(payload), "exact_installed_copy": True,
            "thread_global_symbols_match_rebuild": True}
        for arm, compiled in rebuilt_objects.items():
            target = prep.output / arm / "lib" / name
            shutil.copyfile(original, target)
            staging = objects / arm / name
            staging.mkdir()
            replacement = staging / provider
            shutil.copyfile(compiled, replacement)
            # Match the actual existing member name; otherwise ar appends an
            # unused new object and the unchanged implementation can still win.
            prep.run(f"{arm}-{name}-replace", [tools["ar"], "r", target, replacement])
            prep.run(f"{arm}-{name}-index", [tools["ar"], "s", target])
            after, chosen = prep.inspect_archive(target, f"{arm}-{name}", tools["ar"], tools["nm"])
            require(chosen == provider, "Rebuilt archive selected a different provider")
            proof = verify_replacement(before, after, provider, compiled.read_bytes())
            prep.manifest["variants"][arm]["archives"][name] = {
                "path": str(target.relative_to(prep.output)), "sha256": file_digest(target),
                "members": member_manifest(after), "thread_provider": provider,
                "thread_object_sha256": file_digest(compiled), **proof}
    prep.manifest["native_preparation_passed"] = True
    prep.manifest["status"] = "prepared"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", type=Path, help="Exact pinned Rtools42 g++.exe (required for native preparation)")
    parser.add_argument("--output", required=True, type=Path, help="Fresh output directory outside the installed toolchain")
    parser.add_argument("--source-cache", type=Path, help="Optional offline immutable-input cache; all bytes are hash checked")
    parser.add_argument("--prepare-only", action="store_true", help="Fetch and patch sources only; never claims native preparation")
    args = parser.parse_args()
    if not args.prepare_only and args.cxx is None:
        parser.error("--cxx is required unless --prepare-only is used")
    output = args.output.resolve()
    require(not output.exists() or (output.is_dir() and not any(output.iterdir())),
            "Output must be a fresh or empty directory")
    if args.cxx is not None:
        require(not output.is_relative_to(args.cxx.resolve().parent.parent),
                "Output must be outside installed toolchain")
    output.mkdir(parents=True, exist_ok=True)
    prep = Preparation(output)
    try:
        prep.manifest["sources"] = fetch_sources(output / "inputs", args.source_cache)
        prep.manifest["script_sha256"] = file_digest(Path(__file__))
        if args.prepare_only:
            prep.manifest["status"] = "sources-prepared"
        else:
            native_prepare(prep, args.cxx)
    except Exception as error:
        prep.manifest.update(status="failed", native_preparation_passed=False,
                             error=f"{type(error).__name__}: {error}")
    finally:
        try:
            prep.audit_installed_inputs()
        except Exception as error:
            prep.manifest.update(status="failed", native_preparation_passed=False,
                                 installed_input_audit_error=f"{type(error).__name__}: {error}")
        prep.save()
    print(json.dumps({"status": prep.manifest["status"], "manifest": str(output / "manifest.json"),
                      "native_preparation_passed": prep.manifest["native_preparation_passed"],
                      "error": prep.manifest.get("error"),
                      "installed_input_audit_error": prep.manifest.get("installed_input_audit_error")}, indent=2))
    return 1 if prep.manifest["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
