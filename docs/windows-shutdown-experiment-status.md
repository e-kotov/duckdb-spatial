# Windows shutdown experiment: evidence ledger

This ledger supplements [the experiment design](../scripts/windows-shutdown/README.md).
All experimental branches, Actions runs and any review PR belong exclusively to
`e-kotov/duckdb-spatial`. No upstream PR or merge is authorized.

## Current assessment, 7 November 2026

The final-shutdown key-deletion guard is a **reproduced improvement in the
original R workload**, not a complete runtime-safety fix. Three matched Windows
runs produced 90 normal default-thread exits in 90 guarded attempts, plus all
15 single-thread controls and three separate smoke processes. Both baselines
continued to hang. A separate native lifetime probe establishes that the guard
does not fix the old emulated-TLS storage/destructor ordering defect.

The next actual-Spatial comparison varies that ordering change separately
from the guard. A further native probe has now confirmed that the two-line
ordering backport introduces an unsafe lifetime order under a valid pthread
key-reuse history. It is not a general production solution. Neither a green
diagnostic workflow nor the absence of hangs in a bounded sample establishes
universal cleanup safety.

The original vanished reader remains unidentified. New R hangs have the
original post-quit phenotype, but new native wait stacks were not captured.
The storage-lifetime observations are an independently demonstrated defect;
their causal role in the original shutdown hang is still a hypothesis.

The review remains [fork draft PR #1](https://github.com/e-kotov/duckdb-spatial/pull/1),
with experimental guard head `e2355ef8f061546f2fecf55b8a64f4297ab71555`.
The later ordering experiments use separate fork branches. Nothing has been
merged or submitted upstream, and duckspatial GeoParquet PR #5 is untouched.

## Three matched Windows comparisons

Every arm keeps the pinned R reproducer's three GeoJSON/Shapefile iterations,
readback/data checks, owned connection and driver cleanup, final garbage
collection, and zero-status quit request. Default-thread processes record four
threads; controls record one. Each arm runs 30 default attempts, five controls,
one separate smoke process and the same unchanged Spatial SQL inputs.

| Run and exact head | Original archive: default hangs / 30 | Recompiled original source: default hangs / 30 | Guard: default hangs / 30 | CI conclusion |
| --- | --- | --- | --- | --- |
| [42375674793](https://github.com/e-kotov/duckdb-spatial/actions/runs/42375674793), `dc61d2afc17b15996f2d8e5c1482308adf9c4c2a` | 9 | 9 | 0 | Failed: empty SQL-file accounting |
| [42377280484](https://github.com/e-kotov/duckdb-spatial/actions/runs/42377280484), `c47ef62faa71ee9739c4eac87ac6fead74875653` | 16 | 16 | 0 | Failed: same accounting rule |
| [42383927641](https://github.com/e-kotov/duckdb-spatial/actions/runs/42383927641), `e2355ef8f061546f2fecf55b8a64f4297ab71555` | 18 | 15 | 0 | Successful matched comparison under corrected gate |

All 45 single-thread controls exited normally. Eight of nine smoke processes
exited normally; **the rebuilt-baseline smoke in run 42377280484 also hung
after successful workload completion**. Across all 324 R processes, there were
240 normal exits and 84 baseline post-quit hangs. The default-attempt totals
are 43/90 hangs for the archive baseline, 40/90 for the rebuilt baseline, and
0/90 for the guard. All 108 guarded processes exited without supervisor
termination and allowed removal of their installed DLL copy.

Independent review re-read the individual supervisor records, workload
checkpoints, loaded-extension CSVs, retained binaries and link maps. The
guarded source and installed compiler/runtime hashes match across the three
runs. No runner-image or dependency-pin change was identified between the
second and third runs. Arms execute sequentially within each runner; these
counts are observations, not an independence assumption or a zero-risk bound.

All nine actual SQL processes exited zero with 130 passing statements and no
failed or skipped selected file. The second and third runs retain all 68
start/end events for 34 files and 201 Catch assertions per arm. The first two
workflow failures remain failures: their original supervisor incorrectly
rejected the existing empty `test/sql/geos/equals.test`. The third run executes
the narrow correction described below; it does not rewrite the old outcomes.
The correction changes no runtime source or original test file.

The third workflow's success means its matched experiment met its stated
criteria. It does **not** turn its 33 baseline hangs into passing software
exits. Its historical assessor returns `supported-windows-comparison` and
keeps `ready_for_upstream_submission` false.

### Forced-cleanup limitation

For all 84 baseline hangs, the retained cleanup record reports successful
termination of the launcher, a later actual-R taskkill response of no running
instance, and `AccessDenied` while removing the installed DLL. There is no
post-kill descendant census. These records neither establish complete forced
teardown nor prove that a descendant remained alive. They are retained as a
limitation, not silently counted as clean exits. This limitation does not apply
to the 108 unforced guarded exits and their successful DLL removal.

### Preserved complete Windows artifacts

| Run | Artifact ID | ZIP bytes / members | SHA-256 |
| --- | --- | --- | --- |
| 42375674793 | 13578411129 | 169,234,536 / 1,323 | `a224fdc2de182fe14800ef77062af2b29a40f3b70f078f9ba7831f4eab273ae2` |
| 42377280484 | 13581376257 | 169,244,260 / 1,332 | `f3576402c34e8ce647a8bdc4365639bc1359ffa63f48675a9e18a80648fed3f7` |
| 42383927641 | 13584680289 | 169,244,646 / 1,332 | `bbde3fd73bdab78c2fcffca2c50dbe1a6d264d2a74881626829a99c62cd6de6f` |

All ZIP CRCs were checked. Full archives and independent review records are
preserved separately from expiring Actions artifacts.

## First matched Windows comparison

[Run 42375674793](https://github.com/e-kotov/duckdb-spatial/actions/runs/42375674793)
at `dc61d2afc17b15996f2d8e5c1482308adf9c4c2a` completed all three builds and
their actual R and SQL processes. Default R connections used four threads;
single-thread controls used one. The original workload still performed all
three GeoJSON/Shapefile iterations, data checks, owned connection/driver
cleanup, garbage collection and the explicit zero-status quit request.

| Arm | Default-thread R outcomes | Single-thread controls | Separate spatial smoke | SQL process |
| --- | --- | --- | --- | --- |
| Original compiler archive | 21 normal exits, **9 quit hangs** / 30 | 5 / 5 pass | Pass | Exit 0; 130 statements pass |
| Recompiled original GCC source | 21 normal exits, **9 quit hangs** / 30 | 5 / 5 pass | Pass | Exit 0; 130 statements pass |
| Guarded GCC source | **30 normal exits, 0 quit hangs** / 30 | 5 / 5 pass | Pass | Exit 0; 130 statements pass |

Loaded-DLL hashes, the original diagnostic-input hashes, recorded thread
counts and source-selection link maps were retained. All baseline hangs
remain failed software exits. No new native dumps were captured in this
comparison: the new failures reproduce the original workload and post-quit
phenotype, but their individual native wait stacks were not remeasured.

Independent review checked all 108 individual R supervisor records,
checkpoints and loaded-extension CSV files, not only the aggregate summary.
It found no discrepancy in these counts, input identities or loaded-DLL hashes.
All 36 guarded processes also allowed their installed DLL copies to be removed
after exit. Removal reported `AccessDenied` only for the 18 forcibly terminated
baseline hangs; those cleanup failures are retained in the recorded results.

The workflow's final conclusion is still **failure**, with verdict
`failed-validation`. Its SQL supervisor mistakenly required a nonzero
assertion count from every selected file. Upstream's
`test/sql/geos/equals.test` is zero bytes (Git blob
`e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`, SHA-256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).
Each raw SQL log has all 34 expected terminal events: 33 original files,
including that empty file, and one provenance fixture. Every event has status
`ok`, no assertion failure and no skip; the native process exits with status
zero. The empty file is the only event with zero passing statements.

The correction keeps this file in the input list and records its empty-source
identity explicitly. It accepts zero assertions only for that exact empty
file, rejects any other zero-assertion or skipped test, and requires at least
130 passing statements overall. Re-evaluating the original raw events under
that rule is an **offline accounting correction**, not a new execution or a
change to the original CI conclusion. Run 42383927641 subsequently exercised
the corrected gate successfully. The candidate runtime source is unchanged.

The complete artifact (ID `13578411129`, 169,234,536 bytes, 1,323 ZIP members)
was preserved and all CRCs verified. Its SHA-256 is
`a224fdc2de182fe14800ef77062af2b29a40f3b70f078f9ba7831f4eab273ae2`.

## Independent native cleanup control

[Run 42382378295](https://github.com/e-kotov/duckdb-spatial/actions/runs/42382378295)
at `23e398f4d0edc3c5dfedde47f105cce3aa576149` satisfies all twelve fixture
expectations. Each of the three runtime variants passes ordinary joined-worker
unload, foreign-worker unload and main-thread exit. Both unload cases preserve
128 TLS destructor calls, 16 global destructors and 16 atexit callbacks per
variant. The foreign-worker markers show each group of eight TLS destructors
before the host's joined-thread checkpoint and subsequent DLL unloading.

The artificial abandoned-reader case hangs after the exit request in the
archive and rebuilt baseline, and exits with status zero in the guarded arm.
The guarded arm retains its global destructor and atexit callback. Thus the
experiment has **ten clean software exits and two expected diagnostic hangs**.
The hung processes are explicitly marked as supervisor-terminated failures.

Raw markers, counters, DLL symbols and link maps were independently reviewed.
The compiler, C++ archive and both pthread archive hashes match the first full
comparison, as do both generated source files. The guarded source SHA-256 is
`6d13fcfaaea6098c2bc6ba5c80771af87bd85c58d1a4598eae19201859cac3a2`.
The separate native manifest does not record `libgcc.a`, so parity of that
archive between these two runners has not been established by its manifest.

This fixture tests the mechanism and the stated cleanup contracts. It does not
attribute the original vanished reader, trace destructors by thread ID, or test
the separate historical emutls object-storage lifetime defect.

Its preserved artifact (ID `13577174661`, 1,646,945 bytes, 95 ZIP members) has
SHA-256 `0362c695ee0d513a45472d481896959fda136686a361e251492604e27edfeab8`.
All ZIP CRCs verify.

## Native storage-lifetime comparison

[Run 42397301216](https://github.com/e-kotov/duckdb-spatial/actions/runs/42397301216)
at `fc93e32aef2adf3604e5b856d937c984686aa289` completes 54 native diagnostic
expectations. It varies original, rebuilt-original and reverse-order pthread
archives against archive, rebuilt-original and guarded GCC cleanup code.
Both pthread-created workers and foreign `CreateThread` workers are covered.

| Pthread implementation | Six storage-lifetime observations | Twelve existing cleanup controls |
| --- | --- | --- |
| Original archive | Storage free completes before the C++ callback in all six | Ten clean exits; two intentional baseline hangs |
| Rebuilt original source | Same storage-before-callback order in all six | Ten clean exits; two intentional baseline hangs |
| Reverse numeric key order | C++ callback precedes storage free in all six | Ten clean exits; two intentional baseline hangs |

The observer brackets the actual allocator free entry and return, and records
the C++ callback through host-owned state that remains alive. It never
dereferences the already-freed TLS object. Records retain native-handle waits,
public-key callbacks, actual `FreeLibrary` unloading and global cleanup. Thus
this result is stronger than merely observing that a destructor ran, while
avoiding an observer-induced use-after-free.

All 18 actual DLL link maps select their intended private pthread member and
exactly one provider for each of five checked pthread APIs; the corresponding
GCC provider is also checked. Independent review rehashed six retained pthread
archives, 78 member records, 24 binaries, source/patch records and 195 included
headers. Only the intended thread member differs in the four modified archive
copies. Installed-input before/after manifests record unchanged toolchain
files; installed compiler binaries and non-retained GCC archives are not
claimed to have been independently rehashed from the offline artifact.

The artificial abandoned-reader fixture intentionally creates a stuck reader
independently of normal TLS destruction order. Its expected unguarded hangs
remain useful controls; they do not show whether correcting normal ordering
alone fixes the actual R workload. That is the purpose of the six-arm
integration experiment below.

The preserved artifact (ID `13583917702`, 9,167,978 bytes, 799 members) has
SHA-256 `64b8eee4ec2316715e6cd7adc1e09c79a455a9ce565234ec99defcb44e06e727`.

## Six-arm actual-Spatial experiment

[Run 42398571027](https://github.com/e-kotov/duckdb-spatial/actions/runs/42398571027)
uses exact head `bf3256b770c8a5d7924a2eb97a0ce085ec258071` on
`test/windows-pthread-spatial-20261107`. The
[design](../scripts/windows-shutdown/PTHREAD_SPATIAL_COMPARISON.md) preserves
the compiler, source/dependency pins, workload, controls and original SQL
tests while separating two candidate changes:

| Arm | Pthread archive | GCC cleanup code |
| --- | --- | --- |
| `original` | Original | Original archive member |
| `rebuilt-control` | Rebuilt original | Rebuilt original |
| `order-only` | Reverse numeric key order | Rebuilt original |
| `guard-only` | Rebuilt original | Final-shutdown guard |
| `combined` | Reverse numeric key order | Final-shutdown guard |
| `order-only-archive` | Reverse numeric key order | Original archive member |

The run passed its Windows self-tests and all 54 native preflight expectations
and entered the six actual Spatial builds and R/SQL comparisons. **Actual
Spatial outcomes remain pending.** The gate requires both original and rebuilt
baselines to reproduce, rather than accepting a non-reproducing control. For a
candidate to be supported, every R/control/smoke process in its required arm or
arms must exit normally without forced cleanup. Negative-candidate hangs
remain failed software outcomes. The exact loaded binary and every unchanged
SQL test are checked.

The previous setup attempt,
[42398051324](https://github.com/e-kotov/duckdb-spatial/actions/runs/42398051324)
at `0f8d1e300038afb4b3a0ee64d1d20013b806e81c`, failed before any native or R
case. A synthetic test compared the Windows short TEMP path against its
resolved long form. The correction resolves only that fixture's temporary
root. Production path/hash verification is unchanged. The failed artifact and
conclusion remain retained.

The installed Rtools marker now records `5355\n` from
`C:/rtools42/x86_64-w64-mingw32.static.posix/.version`, SHA-256
`e522abda0009bcc9a137764bb37b4227c6790768aaa7dd072217aeea7c2ba598`.
This identifies the matching vendor recipe revision; it is not a complete
reproducible-build attestation.

## Confirmed key-reuse counterexample to numeric reversal

MinGW commit
[`8e06daa36dfcea4bb491acf4b350658f40738f02`](https://github.com/mingw-w64/mingw-w64/commit/8e06daa36dfcea4bb491acf4b350658f40738f02)
changes numeric key traversal, not key creation history. In the verified v9
source, deleting a key makes that lower slot available for immediate reuse.
Source review therefore identifies a concrete counterexample to investigate:
reserve a lower key, initialize emulated TLS, delete the reserved key, then
register the C++ cleanup key in the reused slot. Ascending and descending
numeric traversal may now reverse which callback sees live storage.

[The separate native key-reuse run 42400027588](https://github.com/e-kotov/duckdb-spatial/actions/runs/42400027588)
at `cbb40a40479fdae3b077a5c71028ebedfd3cb374` on
`test/windows-tls-key-reuse-20261107` completes 36 native cases and passes two
independent raw/provenance reviews. The paired scenarios use the same DLLs and
differ only in whether reserved key 0 is deleted before or after GCC cleanup
registration. Actual observations identify emutls key 1 in both scenarios,
with GCC key 2 in the normal case and GCC key 0 in the reuse case.

| Pthread family | Normal allocation: callback before storage release | Reused low key: callback before storage release |
| --- | --- | --- |
| Original archive | 0 / 6 | 6 / 6 |
| Rebuilt original | 0 / 6 | 6 / 6 |
| Reverse-order backport | 6 / 6 | **0 / 6** |

Each column covers all three GCC implementations and both pthread-created
and foreign Windows workers. All 18 paired histories invert the observed
lifetime order. All 36 host processes exit zero without supervisor termination,
and all retain actual native-thread waits, public-key deletion, global cleanup
and verified DLL unload. Nevertheless, 18 observations fail the storage-lifetime
contract, including all six reverse-order reuse cases. No callback inspects
freed storage. A green diagnostic means that this counterexample was reproduced
and retained; it does not mean all storage-lifetime contracts passed.

The independent raw review derives order directly from each stdout event
sequence and actual key IDs without importing the experiment's assessor. The
separate provenance review checks six archives, all 78 member payloads, 195
included headers, 12 host/DLL binaries and all nine DLL maps, and reruns `nm`
and `objdump` on the retained PE binaries. The actual intended pthread members,
unique strong API definitions, GCC providers and emutls free interception all
agree. Recorded installed inputs remain unchanged; unavailable installed-tool
bytes are not represented as offline verification.

The raw artifact (ID `13584324753`, 5,398,119 bytes, 621 members) has SHA-256
`8e27f535c386402c7be4884102c31cc1c0d010a0630ca3cc37b193a1089e4d93`.
This is a reproduced counterexample for the exact tested v9 backport, not a
claim that every later MinGW release has been tested. It does not establish
that key reuse occurred in the original R process.

The six-arm R comparison remains valuable for discriminating the actual hang,
but even a positive ordering-only result would not make this backport a general
cleanup-safe production recommendation. The shutdown guard remains a separate,
supported final-key-deletion intervention; it changes neither storage release
nor thread callback order and cannot repair this lifetime defect.

The production delivery question remains in GCC/MinGW runtime coordination and
the toolchain selected by DuckDB extension build infrastructure. A full private
winpthreads rebuild can be made from the exact v9 source and Rtools revision
5355 recipe, but it has not been validated as a distribution candidate. The
current member-replacement experiment is a controlled discriminator, not a
finished runtime package.

There is also a broader, distinct migration option: MSYS2 enabled native TLS
with GCC 16 in May 2026 and rebuilt affected packages. Its
[announcement](https://www.msys2.org/news/#2026-05-11-native-thread-local-storage-tls-with-gcc-16)
documents that libgcc emutls clients remain supported but must be rebuilt to
use native TLS. Some older clients of removed libstdc++ emutls exports also
need rebuilding to load. It further documents the limitation on accessing
native TLS across DLL boundaries. The relevant
[GCC accessor change](https://gcc.gnu.org/git/?p=gcc.git;a=commit;h=7ad39bc909d1b425d0fa1803dc53cd741c10150f)
addresses libstdc++ TLS exports. This is not an evaluated Spatial/R-compatible
upgrade: cached static dependencies would also need scrutiny and rebuilding.

Callback-provider selection is a separate part of that migration. The
[GCC 16.2 configuration](https://github.com/gcc-mirror/gcc/blob/releases/gcc-16.2.0/libstdc%2B%2B-v3/configure.ac)
probes the CRT's `__cxa_thread_atexit` only in its native-configuration branch;
its [MinGW cross-configuration path](https://github.com/gcc-mirror/gcc/blob/releases/gcc-16.2.0/libstdc%2B%2B-v3/crossconfig.m4)
does not make that probe. When detected, GCC omits the fallback cleanup-key
implementation. The CRT forwarding entry point already exists in
[MinGW v9](https://github.com/mingw-w64/mingw-w64/blob/acc9b9d9eb63a13d8122cbac4882eb5f4ee2f679/mingw-w64-crt/crt/cxa_thread_atexit.c),
and GCC 10.4 already contains the corresponding provider-selection branch.
This is not a newly introduced GCC 16 callback API.

The [MSYS2 GCC recipe](https://github.com/msys2/MINGW-packages/blob/5c1ca99730b358ba0915fc8eba7a29e6e169c378/mingw-w64-gcc/PKGBUILD)
uses POSIX threading, enables native TLS and requests a native bootstrap.
Source review therefore suggests a different provider from the captured
cross-built Rtools fallback. The distributed package's generated configuration,
archive contents and selected provider have not been inspected. Its actual CRT
dependency version also is not attested by the contemporaneous source recipe.
A version number alone must not substitute for those binary/configuration
checks or an actual Spatial/R comparison.

## Captured binary and current source

The preserved original `spatial.duckdb_extension` has SHA-256
`3de3f298b4dcaa10f3e081d6ff1e411cf44d487d19317be28bd20303494a1415`.
Its extension footer contains these fields:

| Field | Value |
| --- | --- |
| ABI | `CPP` |
| Extension version | `a3dbcd1781` |
| DuckDB version | `v2.0.0` |
| Platform | `windows_amd64_mingw` |

The extension version resolves to Spatial commit
[`a3dbcd1781ec26c3a1be9bd622be05b376d6f47a`](https://github.com/duckdb/duckdb-spatial/commit/a3dbcd1781ec26c3a1be9bd622be05b376d6f47a).
This is a build metadata observation, not an attestation of every source file
or dependency: the extension build allows an explicit version override and the
footer does not attest to an unmodified working tree.

The current upstream base
[`d332807f4ffdcc4da0092fe760ef48266f548941`](https://github.com/duckdb/duckdb-spatial/commit/d332807f4ffdcc4da0092fe760ef48266f548941)
is 15 commits later. The
[comparison](https://github.com/duckdb/duckdb-spatial/compare/a3dbcd1781ec26c3a1be9bd622be05b376d6f47a...d332807f4ffdcc4da0092fe760ef48266f548941)
contains no top-level build, toolchain or dependency-pin changes. One module
CMake source list adds the new window-functions source file. Its GDAL refactor
adds read/write methods for GDAL 3.13 and later; the corresponding GDAL 3.12 method
bodies and the subsequent read/copy cleanup code are unchanged. The captured
DLL identifies GDAL 3.12.4. Other commits change spatial operators and function
registration, so this does not establish whole-binary equivalence or exclude
every timing difference.

No source change in that bounded comparison was identified as a thread-cleanup
fix. The current-build baselines remain necessary. If neither current baseline
reproduces, passing guarded runs are **inconclusive**. The next discriminating
control would be the preserved exact captured DLL under the same R host and
runner; only a confirmed difference would justify rebuilding the older Spatial
source as another matched comparison.

## Default-path platform control: metadata failure

[Run 42378502824](https://github.com/e-kotov/duckdb-spatial/actions/runs/42378502824)
uses source commit `606e6a9f6c8847cdd2d377efb0411765b2d6dfa1` and leaves the
experimental runtime source option unset. Its Linux build completed. The
ordinary test step was green but reported **2 passed, 162 skipped**, with all
162 skips attributed to `require spatial`. This is a build result, not a
successful Spatial test result.

The produced Linux artifact (ID `13577837980`) was downloaded and all ZIP CRCs
checked. Its SHA-256 is
`6d4f93019ddc1d4be0e3105819ff6d4d32bf6cf24192045430a3007b3cf292c2`.
The extension inside it has SHA-256
`d2e38a1fb2b2eda6aa052ee928ca792a4142932fba40ce9688b93552946f7e7f`;
the accompanying CLI has SHA-256
`3567c2f01612c2e23be9621ccf361954b2aa39795f1edf5fab71e946aa1b642e`.

Directly loading that unchanged extension with its accompanying CLI fails.
The reusable build was given the full DuckDB commit as its version string.
The extension's 32-byte version field stores only
`d54cbbe3df17e8e5ce68312f483b4155`, whereas the engine compares against
`vd54cbbe3df17e8e5ce68312f483b4155836112aa`. The loader rejects that mismatch;
the ordinary SQL runner then treats the unavailable extension as a missing
requirement. This error is separate from the Windows shutdown deadlock.

The corrected platform control must use release metadata `v2.0.0`, verify
that its source is still exactly
`d54cbbe3df17e8e5ce68312f483b4155836112aa`, and require Spatial to load before
accepting test success. It must not bypass metadata validation or rewrite
extension binaries. The Windows experiment already uses the correct release
metadata. Native Linux and macOS controls must also keep the experimental
runtime source option unset.

[Strict follow-up run 42382937642](https://github.com/e-kotov/duckdb-spatial/actions/runs/42382937642)
uses `e07a59560b98171872655092670bebf352b08387`. It applies the release
metadata checks, requires Spatial, rejects missing-requirement skips and
disables recovery retries. Subsequent artifact checks require all 164 original
test registrations, exact installed-binary identity, native loader evidence,
metadata validation and normal process exit. Both native suites subsequently
completed **164 passed, zero skipped**, with nine fast and two slow additional
stabilization rounds
and no recovery retries. The run's final failure was a later provenance JSON
parse error caused by a deprecation warning, not a suite failure.

[Provenance recheck 42394893158](https://github.com/e-kotov/duckdb-spatial/actions/runs/42394893158)
at `2e69d72f65b87572ef658f392ae567a18a93c1da` uses the exact retained Linux and
macOS binaries, corrects the deprecated singular setting name, and passes on
both native runners. It verifies release metadata, exact installed-DLL
identity, successful Spatial loading and `POINT (1 2)`, all 164 test
registrations, and normal process exit. It is a recheck of those binaries,
not another build or another full-suite execution.

| Platform | Retained Spatial SHA-256 |
| --- | --- |
| Linux | `2ba85e029d132b38e6d6f3131415de33d868c1c9e3513bdbf2a0b19f203b7f50` |
| macOS | `1aaa8c7c737cbe33c6cda75ea9fdaf2ad72bd8a2eda8374599af01869a49ddde` |

The ordinary fork PR workflow
[42383933182](https://github.com/e-kotov/duckdb-spatial/actions/runs/42383933182)
at `e2355ef8f061546f2fecf55b8a64f4297ab71555` also completes its existing
build jobs. The relassert/ASan/UBSan/LSan job runs all 164 tests with no skips.
The MSVC job reports two passed and 162 skipped and is retained as build-only
evidence. WebAssembly compiles successfully; a full native-equivalent Spatial
suite is not claimed. Fork deployment is skipped by the repository guard.

## Fork deployment boundary

The feature branch gates the existing deployment job on
`github.repository == 'duckdb/duckdb-spatial'`. Thus a fork review PR can run
the original build and test jobs without entering the release/signing job.
The upstream repository's behavior is unchanged by that condition. Dedicated
experiment workflows contain no external deployment job.
