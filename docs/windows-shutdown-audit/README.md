# Independent six-arm Windows evidence audit

This directory preserves the reviewed audit driver, its unchanged reference
helper and the independently verified early native report for
[run 42398571027](https://github.com/e-kotov/duckdb-spatial/actions/runs/42398571027).
The experiment source is exactly `bf3256b770c8a5d7924a2eb97a0ce085ec258071`.
These files do not alter a build, runtime, workload or CI gate.

Read the [complete handoff and invocation](windows-pthread-spatial-audit-handoff-42398571027.txt)
before using the driver. Every input location can be passed explicitly, so the
audit can run from another checkout or workspace. The shared helper and early
report must retain the exact hashes in [the manifest](handoff-manifest.json).
The original native and final artifact ZIPs remain separate inputs; they are
not committed to the repository.

The final audit has not been executed at this checkpoint because the actual
Spatial comparison is still running. Its SQL checker has been replayed against
three earlier actual Windows SQL records; the complete early native audit is
finished. A successful audit establishes evidence consistency, not that every
software process passed or that a generally safe production fix exists.

The [evidence ledger](../windows-shutdown-experiment-status.md) includes the
independent key-reuse counterexample. That later experiment must inform the
production recommendation, but it must not be silently added to this run's
historical matrix or used to rewrite its original verdict.
