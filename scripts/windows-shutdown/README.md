# Windows shutdown: runtime-key experiment

## Status, 7 November 2026

**Candidate implemented; actual Windows compilation and execution are pending.**
This is an opt-in experiment, not a demonstrated fix or an upstream submission.
The ordinary Spatial build and distribution workflow are unchanged when the
new experiment options are unset. No thread default, existing test, or cleanup
callback is disabled. No change was made to duckspatial GeoParquet PR #5.

Experiment branch: `fix/windows-tls-key-shutdown-20261107`.
Upstream base: `d332807f4ffdcc4da0092fe760ef48266f548941`, default branch
`v2.0-cyanoptera`. The user created `e-kotov/duckdb-spatial` after preparation,
and access to that fork is now verified. All experiment branches, workflow runs
and any draft PR belong exclusively in that fork. Do not open a PR against or
push changes to `duckdb/duckdb-spatial`. No merge is authorized.
Do not reinterpret local checks as Windows validation or create a
production-fix PR on that basis.

## Evidence reused

The original investigation remains authoritative:

- [Technical report at diagnostic HEAD](https://github.com/e-kotov/duckspatial/blob/fe62f8b04c3e879ed3d143956cf73095302f6f5f/.github/windows-native-exit-capture-report.md).
- [Standalone R workload](https://github.com/e-kotov/duckspatial/blob/fe62f8b04c3e879ed3d143956cf73095302f6f5f/.github/windows-native-geometry.R).
- [Native capture run 42314733984](https://github.com/e-kotov/duckspatial/actions/runs/42314733984).
- [Artifact 13550570990](https://github.com/e-kotov/duckspatial/actions/runs/42314733984/artifacts/13550570990).

The original 196,937,717-byte ZIP was downloaded, independently checked and
preserved separately from the expiring Actions artifact. All 52 members pass
CRC checks. Its SHA-256 is
`d0a923d4039166bc988ec44ccb0d2eb59747cd63da1fc507ee9300cb439449d8`.
The original GitHub artifact expires on 6 December 2026 at 23:56:21 UTC.

The full 363,038,440-byte R dump matches
`78e2305c29e4f1e54fc02fa174599ea3e07e7e818f246ddc6ca8ff968349e8ec`.
The exact Spatial DLL matches
`3de3f298b4dcaa10f3e081d6ff1e411cf44d487d19317be28bd20303494a1415`.
Independent Memory64/ThreadList/ThreadInfo/ModuleList parsing confirms PID 6496,
one live thread (TID 212), an instruction pointer in ntdll, the recorded module
base and the exact key lock fields `[0xBAB1F0ED, 1, 0, 1, -1]`.

A bounded additional examination of that same dump found that Spatial's
`mtx_pthr_locked` mutex is unlocked and has no recorded owner. Its pthread
registry retains records for TID 212 and vanished TID 1688. Both have
`thread_noposix = 1`: they were registered as foreign threads in this particular
statically bundled pthread runtime. TID 1688's two key slots are cleared. The
key destructor table maps key 0 to `emutls_destroy` and key 1 to GCC's cleanup
runner. This is a useful trace lead; it does **not** establish that TID 1688
owned the abandoned reader, which callback held it, or why its cleanup stopped.
The audit script and machine-readable results are included in the companion
experiment package, alongside the unchanged original report.

## Source assessment and appropriate target

The wait site is GCC's fallback C++ TLS cleanup-key destructor, using
winpthreads' key-table lock. In
[`atexit_thread.cc` from GCC 10.4](https://github.com/gcc-mirror/gcc/blob/7ff47281ce4f3699185b06a3430968eac2a5b0c6/libstdc%2B%2B-v3/libsupc%2B%2B/atexit_thread.cc),
`key_s::~key_s()` unconditionally calls `__gthread_key_delete`. In
[MinGW v9 `thread.c`](https://github.com/mingw-w64/mingw-w64/blob/v9.0.0/mingw-w64-libraries/winpthreads/src/thread.c),
`pthread_key_delete` acquires the write lock while `_pthread_cleanup_dest`
holds a read lock across arbitrary TLS destructor callbacks. Deleting the key
itself does not invoke those callbacks.

The same unconditional key deletion remains in
[current GCC source at 58f804554996](https://github.com/gcc-mirror/gcc/blob/58f804554996fe057e5ca4fc814449640a6e816c/libstdc%2B%2B-v3/libsupc%2B%2B/atexit_thread.cc).
An unqualified compiler upgrade is therefore not a source-backed fix.

The first production target should be **GCC libsupc++**, with a toolchain
backport or update delivered through **DuckDB extension-ci-tools/Rtools**.
Spatial provides the real integration workload and this controlled experiment.
This repository's source injection is an experimental delivery mechanism, not
a recommendation to permanently maintain a replacement C++ runtime in Spatial.

The existing MinGW history matters:

- [`a556c6943c442465dc9a051bc6d3a6d452df4a1d`](https://github.com/mingw-w64/mingw-w64/commit/a556c6943c442465dc9a051bc6d3a6d452df4a1d)
  broadly suppressed user destructors during final shutdown.
- [`dbe1fa06dd46c356c6020ad8d96aa14398fa8d2e`](https://github.com/mingw-w64/mingw-w64/commit/dbe1fa06dd46c356c6020ad8d96aa14398fa8d2e)
  reverted it. Its approach also removed regular DLL atexit/global cleanup.
- [`0fad182ff178044ee442b05e1913aa16cde8efcf`](https://github.com/mingw-w64/mingw-w64/commit/0fad182ff178044ee442b05e1913aa16cde8efcf)
  is a narrower accepted change for the win32 threading model, not the captured
  POSIX/winpthreads fallback.
- [`8e06daa36dfcea4bb491acf4b350658f40738f02`](https://github.com/mingw-w64/mingw-w64/commit/8e06daa36dfcea4bb491acf4b350658f40738f02)
  addresses a separate emutls destructor-order problem. Switching to MinGW's
  native CRT TLS implementation changes this ordering and needs separate work.

Neither an extra generic worker join nor forced single-threaded operation is
justified as the production fix. The inspected DuckDB scheduler already joins
its workers; MinGW's normal `pthread_join` waits for the native thread handle,
and its own worker cleanup precedes thread completion. That does not prove the
origin of the foreign-thread registry record retained in this dump.

## Candidate semantics

`prepare_runtime.py` retrieves the exact GCC source by immutable commit and
verifies its SHA-256. It produces an unmodified `baseline.cc`, a `guarded.cc`,
the small unified GCC patch, and both GCC license files.

The guarded source resolves `RtlDllShutdownInProgress` from the already-loaded
ntdll during key initialization and caches the function pointer. At key
destruction it skips only `__gthread_key_delete` if that query returns true.
It makes no loader lookup at destruction. If the API is unavailable, behavior
falls back to the original deletion. Non-Windows code is unchanged.

The existing `run()` functions, per-thread destructor lists and atexit
registration are untouched. Ordinary thread exits still run their registered
TLS destructors, and normal `FreeLibrary` unload still deletes the key.
During final process termination the OS will reclaim its
bookkeeping memory. This follows the distinction documented by Microsoft for
[DLL process termination versus normal unload](https://learn.microsoft.com/en-us/windows/win32/dlls/dllmain)
and the documented
[shutdown-state query](https://learn.microsoft.com/en-us/windows/win32/devnotes/rtldllshutdowninprogress).

This is a plausible correction to unsafe final key deletion. It is not yet
evidence that the real Spatial workload exits correctly with the patch.

## Matched Windows experiment

The dedicated workflow preserves these upstream pins:

| Component | Revision or value |
| --- | --- |
| Windows runner | `windows-2025-vs2026` |
| R / DBI / DuckDB R package | `4.6.2` / `1.3.0` / `2.0.0` |
| Compiler | Rtools 42, GNU 10.4.0, x64 POSIX/MinGW |
| DuckDB source / release metadata | `d54cbbe3df17e8e5ce68312f483b4155836112aa` / `v2.0.0` |
| Extension CI tools | `969bc76276acec1c0a3922ff500d2ec3a1a3b037` |
| vcpkg | `cd61e1e26a038e82d6550a3ebbe0fbbfe7da78e3` |

The current Spatial workflow's comment calling that DuckDB commit an RC is
stale: the v2.0.0 tag resolves to that same commit. Explicit release metadata
prevents inadvertently building a binary incompatible with the R host.

All three arms use the same Windows runner, compiler, dependencies and build
tree:

| Arm | C++ TLS implementation |
| --- | --- |
| `upstream` | The original compiler archive member; no source injected |
| `rebuilt-baseline` | The exact original GCC source, recompiled into the DLL |
| `guarded` | The same source with only the proposed guard |

Link maps and defined symbols must prove that exactly one
`__cxa_thread_atexit` was selected and that the archive member is absent from
both injected arms. Compiler, runtime archives, source, DLL and executable
hashes are retained. Installed compiler archives are never modified.

The native runtime fixture tests all three arms. Sixteen load/unload cycles
with eight joined workers per cycle must produce exactly 128 TLS destructors,
16 global destructors and 16 user atexit callbacks, and the DLL must actually
unload. A separate main-thread exit check verifies its TLS/global/atexit
callbacks. An artificial detached worker is deliberately stopped inside a TLS
destructor, leaving a key-table reader for `ExitProcess` to abandon. Both
baselines must hang only after the recorded exit request; the guarded DLL must
exit and preserve global/atexit cleanup. This synthetic mechanism fixture does
not identify the original vanished thread or claim to reproduce its lifecycle.

For the real workload each arm runs **30 fresh default-thread R processes and
five fresh single-thread controls**, plus a separate spatial smoke process.
Every process retains all three original GeoJSON/Shapefile iterations and
the original connection/driver/GC cleanup. The original Python supervisor is
imported byte-for-byte. Test adaptations are limited to:

- Exact runtime version checks and recorded default/control thread counts.
- Unsigned local-extension loading into a fresh process-specific repository.
  The local binary is installed once, then reused by all three iterations.
- Disabling automatic fallback installation/loading in those test databases.
- Loaded path, copied-binary hash and engine/platform provenance checks.
- Separate additional GEOS, PROJ and parallel geometry smoke operations.

The raw reproduction has no added smoke SQL. Adaptation diffs and per-iteration
load records are retained. Default mode must actually have at least two
threads; controls must have exactly one. Verified installed copies are removed
only after the supervised process has exited or been terminated, preventing
duplicate 100+ MB DLLs from exhausting runner disk. The exact arm DLL is kept.

Spatial's current MinGW config omits normal SQL test registration. The new
supervisor explicitly runs 33 unchanged upstream GDAL/GEOS/PROJ files, plus a
generated provenance fixture. It verifies the actual native mapped DLL,
complete test events, nonzero assertion counts, no skips/failures and normal
process exit. The original test configuration and files stay unchanged.

## Interpretation and launch

`assess_experiment.py` fails closed. It requires both real-workload baselines
to reproduce, every guarded default/control/smoke process to pass, all native
cleanup contracts and SQL tests to pass, and compiler/runtime/binary provenance
to agree. Missing results are incomplete. No matched baseline reproduction is
inconclusive. A captured or expected baseline hang remains a failed software
exit, even when it supports the experiment. The output never declares approval
to submit upstream.

The workflow starts on a push to the exact feature branch above and can later
be manually dispatched. The narrow push trigger avoids needing to merge a new
dispatch-only workflow into the fork's default branch. Default repetition
counts also apply to push events. No upstream repository mutation is needed.

After the fork exists, push the prepared branch, inspect the first native
fixture results, then the complete comparison artifacts. Any compiler,
dependency, SQL-version or runner failure must be resolved as an explicit
infrastructure/test problem; it must not be waived as a fix. If the comparison
supports the guard, repeat on independent Windows runners and run supported
platform checks before preparing a draft PR against the fork. Upstream
submission and any merge require the user's approval.

Local checks completed during preparation: exact source/hash and generated
patch verification; Python compilation; fourteen adverse/positive gate tests;
actual R parsing of both adapted programs; independent code review; YAML
parsing; action-pin verification; and native Linux compilation of original and
patched sources with identical executable `.text` bytes. These checks do not
validate Windows compilation, PowerShell execution, Spatial's full build,
macOS/Linux Spatial operations, or the shutdown fix.
