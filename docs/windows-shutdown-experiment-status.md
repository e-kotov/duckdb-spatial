# Windows shutdown experiment: evidence ledger

This ledger supplements [the experiment design](../scripts/windows-shutdown/README.md).
All experimental branches, Actions runs and any review PR belong exclusively to
`e-kotov/duckdb-spatial`. No upstream PR or merge is authorized.

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
counts and source-selection link maps were retained. The two baseline hangs
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
change to the original CI conclusion. A fresh run will exercise the corrected
gate. The candidate runtime source itself is unchanged.

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
contains no CMake, toolchain or dependency-pin changes. Its GDAL refactor adds
read/write methods for GDAL 3.13 and later; the corresponding GDAL 3.12 method
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

## Fork deployment boundary

The feature branch gates the existing deployment job on
`github.repository == 'duckdb/duckdb-spatial'`. Thus a fork review PR can run
the original build and test jobs without entering the release/signing job.
The upstream repository's behavior is unchanged by that condition. Dedicated
experiment workflows contain no external deployment job.
