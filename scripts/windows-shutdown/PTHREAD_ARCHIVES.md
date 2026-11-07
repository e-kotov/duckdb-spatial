# Isolated pthread archive comparison

`prepare_pthread_archives.py` prepares three private archive directories for the
native cleanup and TLS storage-order experiments. It does not install a runtime
or change DuckDB Spatial's production build.

The fork-only [Windows ordering workflow](../../.github/workflows/windows-tls-storage-order.yml)
prepares these three arms and runs six storage-order observations plus the
existing twelve cleanup cases against each arm. It preserves all inputs and
outcomes for 90 days. Its diagnostic success requires observing the old
ordering hazard in both controls and callback-before-release with the accepted
patch. The six deliberately blocked-reader cases remain failed software exits,
and the twelve old-order observations remain failed storage-lifetime contracts.
This workflow does not execute the Spatial/R reproducer.

```powershell
python scripts/windows-shutdown/prepare_pthread_archives.py `
  --cxx C:/rtools42/x86_64-w64-mingw32.static.posix/bin/g++.exe `
  --output build/pthread-order
```

The output directory must be fresh. Each arm contains exactly two real files,
`lib/libpthread.a` and `lib/libwinpthread.a`, for the runners' existing
`--pthread-library-dir` option:

| Arm | Thread implementation | Expected storage-order diagnostic |
| --- | --- | --- |
| `original` | Exact copies of the pinned installed archives | Storage release before GCC callback |
| `rebuilt-baseline` | Unmodified MinGW v9 `thread.c`, rebuilt with recorded flags | Storage release before GCC callback |
| `reverse-order` | Same rebuild with the two changes from MinGW commit `8e06daa36dfcea4bb491acf4b350658f40738f02` | GCC callback before storage release |

The original-versus-rebuilt comparison is essential. The recorded flags follow
the pinned upstream static-library build definitions, but are not asserted to
reproduce every Rtools vendor build option. Only the difference between the two
rebuilt arms is attributed to the reverse-key patch. The runners must still
measure actual callback order and preserve unexpected outcomes as failures.

Preparation rejects an unknown compiler/archive hash, unexpected archive form,
ambiguous API provider, mismatched public header, private include closure, ABI
layout or external symbol set. It discovers the provider with `ar` and `nm`,
replaces that exact member, and checks that every other member payload and the
member order remain identical. Each installed input is rehashed afterward.
Installed public headers are used for compilation; downloaded public headers
serve only as immutable comparison references. Private headers and `thread.c`
come from the resolved v9 commit
`acc9b9d9eb63a13d8122cbac4882eb5f4ee2f679`, with individual SHA-256 checks.

`manifest.json` records the source identities, generated patch, tools, flags,
included headers, observed layout, original and replacement objects, archive
members and before/after hashes. Raw libraries, objects, preprocessed source,
header copies, disassembly, commands and logs remain available for independent
review, including when a subsequent check fails. A workflow must require
`native_preparation_passed: true` before executing comparisons and retain all
outputs even on failure.

For portable source checks only, use `--prepare-only` and optionally
`--source-cache <directory>`. This reports `sources-prepared` and leaves
`native_preparation_passed` false. Local unit checks exercise archive selection
and rejection gates; they do not establish Windows runtime behavior.

The storage-order probe and the ordinary cleanup/abandoned-reader controls answer
different questions. Reversing keys does not change the key-table lock around
callbacks or GCC's final key deletion. It is not, by itself, proof that the
previously captured shutdown wait is fixed, nor proof of which thread abandoned
the captured read count.
