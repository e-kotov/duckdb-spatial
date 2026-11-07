# Independent six-arm raw-evidence review — 7 November 2026

Scope: **fork-only** `e-kotov/duckdb-spatial`. [Run 42398571027](https://github.com/e-kotov/duckdb-spatial/actions/runs/42398571027), exact workflow head `bf3256b770c8a5d7924a2eb97a0ce085ec258071`. The experiment branch differs from draft [PR #1](https://github.com/e-kotov/duckdb-spatial/pull/1), whose unchanged code head is `e2355ef8f061546f2fecf55b8a64f4297ab71555`.

## Immutable artifact identities

| Archive | GitHub artifact ID | Bytes | SHA-256 | ZIP files |
| --- | ---: | ---: | --- | ---: |
| Final Spatial/R/SQL evidence | 13589715624 | 344410864 | `86d023f647790307f88483e20c75827e85cf06cd447c2f387f5f7655da4fdbf3` | 3319 |
| Early native checkpoint | 13584960803 | 9196621 | `e497a0cd7f061401e217408642cd94206085f166b97fdf988f9d839a58592851` | 807 |

The final artifact API confirms the exact run and head. Local verification checked sizes, SHA-256, all ZIP CRCs, member uniqueness, safe extraction (no escaping paths or symbolic-link entries) and hashes of every extracted file. All 807 early native files are identical to their final-archive copies. The early independent native audit therefore remains applicable to its **54 native cases** (18 ordering cases, 36 cleanup cases with six intentional failed exits). Its original assessment is not rewritten.

## Per-process actual results

| Arm | Pthread / GCC implementation | Normal default R / 30 | Failed default quit hangs | Single-thread / 5 | Smoke | Normal DLL removals |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| original | Original pthread / GCC archive | 10 | **20** | 5 | **Hang** | 15 |
| rebuilt-control | Rebuilt original pthread / GCC baseline | 15 | **15** | 5 | Normal | 21 |
| order-only | Reverse-order pthread / GCC baseline | **30** | 0 | 5 | Normal | 36 |
| guard-only | Rebuilt pthread / GCC guard | **30** | 0 | 5 | Normal | 36 |
| combined | Reverse-order pthread / GCC guard | **30** | 0 | 5 | Normal | 36 |
| order-only-archive | Reverse-order pthread / GCC archive | **30** | 0 | 5 | Normal | 36 |

Exactly **216 R processes** independently inspected: 180 default (four threads), 30 single-thread controls, six smokes. **180 unforced exits, 36 failed software exits**, the latter 35 baseline defaults and one original smoke. Every failed exit completed the workload and entered zero-status quit but timed out; the supervisor forcibly terminated it. In all 36, installed-copy removal reported `AccessDenied`, and a post-kill descendant census is absent, so complete forced teardown is not asserted. The four candidate arms supplied **144/144 unforced normal exits** and removed all their installed DLL copies.

Each of the six SQL suites returned status zero, with 34 exact source/provenance cases, 68 begin/end events, 130 passing statements, 201 Catch assertions, zero failed/skipped, and the exact known empty `geos/equals.test`. Their unchanged 33 upstream source files were independently compared against commit `d332807f4ffdcc4da0092fe760ef48266f548941`. The six actual retained Spatial DLL hashes, loaded-extension observations, link maps and strong symbol providers, source variants, 32 source snapshots, 195 included headers, six exact pthread archives, all 78 archive members and 203 recorded installed-input before/after matches were checked. All R original supervisor outcomes, checkpoint sequences, thread counts, SQL command/source maps, data/readback and cleanup markers were checked per process, not inferred from the workflow's green status.

## Gate and interpretive limits

The read-only independent raw audit returned **no evidence-integrity errors**, and the archived historical verdict is `supported-six-arm-comparison`: both baselines reproduce, `supported_ordering_only=true`, `supported_combination=true`, `ready_for_upstream_submission=false`, `complete_runtime_fix_established=false`. Passing the evidence audit is **not** equivalent to all software exits passing.

The published hash-pinned Python final audit driver was **not executed** here because the run was inspected from a reprovisioned environment lacking its original runnable Git checkout and companion inputs as local files. Instead a separately authored raw-evidence checker verified the retained individual records plus GitHub-fetched unchanged sources. This is an important scope distinction, not a claim that the pinned driver ran. The original ZIP bytes remain unchanged. The final artifact is preserved in GitHub Actions, and an independent local audit report/checker was retained with the original review work.

The eight unretained installed Windows compiler/tool binaries, the installed `libstdc++.a` / `libgcc.a`, and `unittest.exe` are represented by recorded Windows live hashes and cannot be independently rehashed offline from this archive. Original R wait stacks were **not** recaptured. Forced descendant teardown cannot be certified. Native runtime fixture process outcomes cannot be merged with R process counts. Historical failed CI runs retain their original failures.

**Production decision:** ordering-only and guard-only each independently suppress the R failure in this finite matched experiment. That does **not** establish a cleanup-safe runtime fix. A separate, independently audited [native key-reuse run 42400027588](https://github.com/e-kotov/duckdb-spatial/actions/runs/42400027588) proves this numeric-key reversal can run callbacks after storage release under valid key reuse (6/6 reverse-order reuse cases). A combined intervention inherits that hazard. Conversely, guard-only leaves the independently reproduced TLS lifetime defect untouched. The original lost reader is unidentified and there are no fresh R native waits. Continue research on coordinated destructor/storage lifetime or a compatible toolchain; no upstream PR, merge, release, deployment or PR readiness change.

This file is independent review documentation on the experiment branch, **not** a new software experiment and **not** a revision of the run's pinned source or historical assessment.
