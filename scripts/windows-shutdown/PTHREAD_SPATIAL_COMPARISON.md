# Matched Windows Spatial runtime comparison

This is a fork-only experiment, not a production toolchain distribution or an
upstream submission. The normal build leaves both runtime override options empty.
The comparison must establish which change resolves the observed R shutdown
failure before a production delivery method is selected.

## Question and fixed factors

The original dump establishes a final-process wait in GCC's fallback C++ TLS-key
destructor, with an outstanding winpthreads key-table reader and only the shutdown
thread remaining. It does not establish which earlier thread left that reader.

Separately, the pinned GCC/MinGW sources free emulated TLS storage before invoking
the registered C++ destructor callbacks. MinGW already accepted the bounded
reverse-key-order correction in commit
[`8e06daa36dfcea4bb491acf4b350658f40738f02`](https://github.com/mingw-w64/mingw-w64/commit/8e06daa36dfcea4bb491acf4b350658f40738f02).
The native ordering probe tests that storage contract directly without accessing
freed storage. The actual Spatial comparison tests whether correcting it also
resolves the R shutdown phenotype.

| Spatial arm | Private winpthreads archive | GCC TLS registration implementation |
| --- | --- | --- |
| `original` | Unmodified installed archive, copied privately | Unmodified installed libstdc++ archive |
| `rebuilt-control` | Original MinGW source rebuilt | Original GCC source rebuilt |
| `order-only` | Accepted reverse-order change | Original GCC source rebuilt |
| `guard-only` | Original MinGW source rebuilt | Narrow final-shutdown key-deletion guard |
| `combined` | Accepted reverse-order change | Narrow final-shutdown key-deletion guard |
| `order-only-archive` | Accepted reverse-order change | Unmodified installed libstdc++ archive |

The four rebuilt arms form a two-factor comparison. The two archive-GCC arms test
whether the ordering correction is sufficient without replacing GCC's TLS
registration implementation. Rebuilding the original source is a control; it is
not assumed to reproduce the vendor's object bytes or every compiler flag.

## Evidence collected

The workflow first prepares private copies of both pthread archives and verifies
the actual ABI layout, source hashes, archive members and immutable installed
inputs. It then executes all three native runtime families, including ordinary
DLL unload, foreign Windows threads, final-process cleanup and deliberately
abandoned-reader cases. Actual linker maps and unique API definitions must prove
which private archive supplied each DLL.

Each Spatial arm reuses the pinned standalone R reproducer and supervisor for
30 default-thread exits, five single-thread controls and one smoke process. That
is 216 fresh R processes across six arms. Each arm also runs the same 33 upstream
SQL test files plus binary-provenance assertions. The known empty upstream file
remains selected and hash checked; the total requires at least 130 passing SQL
statements, with no failures or skips.

The same build tree and SQL runner are reused. Each arm retains the exact Spatial
DLL, linker map, symbol definitions, CMake cache, compilation commands, input
hashes and individual test results. The supervisor continues collecting after a
baseline hang; the hang remains a failed software exit. Successful exits require
no forced termination and successful removal of the installed extension copy.

## Interpretation

`assess_pthread_spatial.py` verifies the retained raw records and input identities.
Both original and rebuilt-control must reproduce at least one default-thread R
quit hang after a successful workload. Otherwise this comparison is inconclusive.

An ordering-only result requires every R and SQL outcome to pass in both
`order-only` and `order-only-archive`, together with the corrected native storage
contract and ordinary cleanup controls. A combination result requires those
checks for `combined`. Guard-only clean exits cannot establish that the storage
lifetime defect is repaired.

Old-order native lifetime failures and deliberately induced native hangs remain
explicit negative outcomes even when the diagnostic expectations are met. The
abandoned-reader fixture intentionally holds a reader independently of normal
key ordering; an ordering correction is not expected to make that artificial
reader disappear.

A finite passing sample does not prove freedom from all shutdown races. No
claim about the original reader's owner or an earlier exception follows from
these comparisons alone. No new R hang is assigned the original native stack
without a new capture. `ready_for_upstream_submission` remains false.

If ordering alone succeeds, the likely production route is a coherent backport
of the accepted MinGW change into the runtime/toolchain selected by DuckDB's
extension build infrastructure. Private member replacement and source injection
remain experimental isolation mechanisms. Any required GCC guard would need a
separate libsupc++ review. The broad MinGW destructor-suppression change that was
reverted for breaking normal DLL cleanup is not used here.
