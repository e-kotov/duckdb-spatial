#!/usr/bin/env python3
"""Fetch immutable, hash-checked experiment inputs and prepare one GCC patch.

No installed compiler/archive is modified. The generated baseline.cc is the
exact GCC source; guarded.cc differs only by the candidate patch. Downloaded
GCC files retain GPLv3 + GCC Runtime Library Exception notices and licenses.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import urllib.request

GCC_COMMIT = "7ff47281ce4f3699185b06a3430968eac2a5b0c6"
DIAGNOSTIC_COMMIT = "fe62f8b04c3e879ed3d143956cf73095302f6f5f"
GCC_SOURCE = "libstdc++-v3/libsupc++/atexit_thread.cc"
SOURCES = {
    "gcc10-atexit_thread.cc": (
        "gcc-mirror/gcc", GCC_COMMIT, GCC_SOURCE,
        "4c786915c7e1f894c81409367dbab11e69f93f233a03da7ff24e692a9b9c3cc4"),
    "COPYING3": (
        "gcc-mirror/gcc", GCC_COMMIT, "COPYING3",
        "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"),
    "COPYING.RUNTIME": (
        "gcc-mirror/gcc", GCC_COMMIT, "COPYING.RUNTIME",
        "9d6b43ce4d8de0c878bf16b54d8e7a10d9bd42b75178153e3af6a815bdc90f74"),
    "windows-native-geometry.R": (
        "e-kotov/duckspatial", DIAGNOSTIC_COMMIT, ".github/windows-native-geometry.R",
        "3294b18430f2ccd2d0229aa246d899360227ed26475332d442dcc562627274be"),
    "windows-test-supervisor.py": (
        "e-kotov/duckspatial", DIAGNOSTIC_COMMIT, ".github/windows-test-supervisor.py",
        "a65124949af861d3effccd0c6d79df1f08feb44e597608b7a4bc913fd539b3e7"),
    "countries.geojson": (
        "e-kotov/duckspatial", DIAGNOSTIC_COMMIT, "inst/spatial/countries.geojson",
        "070053e9dfe7ca32de43243880008137da25029dbf92c33088326919ce4b8651"),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"Expected exactly one patch anchor: {old!r}")
    return source.replace(old, new, 1)


def guard_source(source: str) -> str:
    # Keep all user destructor lists, callbacks, and atexit registrations intact.
    source = replace_once(
        source,
        '#include "bits/gthr.h"\n#ifdef _GLIBCXX_THREAD_ATEXIT_WIN32\n',
        '#include "bits/gthr.h"\n#ifdef _WIN32\n',
    )
    source = replace_once(
        source,
        "  __gthread_key_t key;\n",
        """  __gthread_key_t key;
#ifdef _WIN32
  typedef BOOLEAN (NTAPI *dll_shutdown_function) ();
  dll_shutdown_function dll_shutdown_in_progress;
#endif
""",
    )
    source = replace_once(
        source,
        """      key_s() { __gthread_key_create (&key, run); }
      ~key_s() { __gthread_key_delete (key); }
""",
        """      key_s()
      {
#ifdef _WIN32
	// Resolve while initializing, never while running static destructors.
	HMODULE ntdll = GetModuleHandleW (L"ntdll.dll");
	if (ntdll)
	  dll_shutdown_in_progress = reinterpret_cast<dll_shutdown_function>
	    (GetProcAddress (ntdll, "RtlDllShutdownInProgress"));
#endif
	__gthread_key_create (&key, run);
      }
      ~key_s()
      {
#ifdef _WIN32
	// ExitProcess can terminate a thread holding winpthreads' key-table
	// read lock. Key deletion takes its write lock and cannot complete
	// then. Deleting this bookkeeping key invokes no user destructors.
	// Preserve deletion on ordinary DLL unload and before process exit.
	if (dll_shutdown_in_progress && dll_shutdown_in_progress ())
	  return;
#endif
	__gthread_key_delete (key);
      }
""",
    )
    return source


def fetch_inputs(output: Path, cache: Path | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"gcc_commit": GCC_COMMIT, "diagnostic_commit": DIAGNOSTIC_COMMIT,
                "sources": {}}
    for name, (repo, commit, path, expected) in SOURCES.items():
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
        cached = cache / name if cache else output / name
        if cached.is_file():
            data = cached.read_bytes()
        else:
            with urllib.request.urlopen(url, timeout=45) as response:
                data = response.read()
        actual = digest(data)
        if actual != expected:
            raise ValueError(f"SHA-256 mismatch for {name}: {actual}, expected {expected}")
        (output / name).write_bytes(data)
        manifest["sources"][name] = {"url": url, "sha256": actual, "bytes": len(data)}
    baseline = (output / "gcc10-atexit_thread.cc").read_bytes()
    guarded = guard_source(baseline.decode("utf-8")).encode("utf-8")
    (output / "baseline.cc").write_bytes(baseline)
    (output / "guarded.cc").write_bytes(guarded)
    patch = "".join(difflib.unified_diff(
        baseline.decode("utf-8").splitlines(keepends=True),
        guarded.decode("utf-8").splitlines(keepends=True),
        fromfile=f"a/{GCC_SOURCE}", tofile=f"b/{GCC_SOURCE}"))
    (output / "gcc-10.4-final-shutdown-key.patch").write_text(patch, encoding="utf-8", newline="\n")
    manifest["generated"] = {
        "baseline.cc": {"sha256": digest(baseline)},
        "guarded.cc": {"sha256": digest(guarded)},
    }
    (output / "inputs.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-cache", type=Path,
                        help="Optional offline cache; every byte is still hash-checked")
    args = parser.parse_args()
    print(json.dumps(fetch_inputs(args.output, args.source_cache), indent=2))
