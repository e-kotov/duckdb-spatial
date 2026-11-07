# Windows shutdown experiment: evidence ledger

This ledger supplements [the experiment design](../scripts/windows-shutdown/README.md).
All experimental branches, Actions runs and any review PR belong exclusively to
`e-kotov/duckdb-spatial`. No upstream PR or merge is authorized.

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
