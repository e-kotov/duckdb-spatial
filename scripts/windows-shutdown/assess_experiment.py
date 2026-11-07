#!/usr/bin/env python3
"""Fail-closed assessment of a Windows experiment, distinct from test status.

Baseline hangs remain failed software exits. A supported comparison requires
both original and recompiled baselines to reproduce, the guarded build to pass
every workload/cleanup check, and all binary/source provenance to agree. A run
without baseline hangs is inconclusive, even if every process happened to exit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from prepare_runtime import SOURCES

ARMS = ("upstream", "rebuilt-baseline", "guarded")
MIN_DEFAULT_ATTEMPTS = 30
MIN_CONTROL_ATTEMPTS = 5


def read_json(path: Path) -> dict:
    # PowerShell 5/7 emit different BOM conventions; accept either deliberately.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def valid_r_row(row: dict, extension_sha: str, mode: str, allow_hang: bool) -> bool:
    outcomes = ("success", "quit-hang") if allow_hang else ("success",)
    records = row.get("loaded_extensions", [])
    try:
        correct_threads = all(int(record["threads"]) == 1 if mode == "control"
                              else int(record["threads"]) >= 2 for record in records)
    except (ValueError, KeyError, TypeError):
        correct_threads = False
    return bool(
        row.get("thread_mode") == mode and row.get("reason") in outcomes
        and row.get("tests_returned") is True and row.get("r_error") is False
        and row.get("requested_exit_status") == 0 and row.get("provenance_passed") is True
        and len(records) == 3 and correct_threads
        and all(record.get("sha256") == extension_sha
                and record.get("platform") == "windows_amd64_mingw"
                and record.get("engine_version") == "v2.0.0" for record in records)
        and (row.get("reason") != "success" or
             (row.get("exit_code") == 0 and row.get("software_passed") is True))
        and (row.get("reason") != "quit-hang" or row.get("software_passed") is False)
    )


def assess_r(summary: dict, extension_sha: str, guarded: bool) -> dict:
    requested = summary.get("requested", {})
    default_count = requested.get("default_attempts", 0)
    control_count = requested.get("control_attempts", 0)
    rows = summary.get("results", [])
    defaults = [row for row in rows if row.get("thread_mode") == "default"]
    controls = [row for row in rows if row.get("thread_mode") == "control"]
    counts_ok = (
        isinstance(default_count, int) and default_count >= MIN_DEFAULT_ATTEMPTS
        and isinstance(control_count, int) and control_count >= MIN_CONTROL_ATTEMPTS
        and len(defaults) == default_count and len(controls) == control_count
        and len(rows) == default_count + control_count
        and {row.get("attempt") for row in defaults} == set(range(1, default_count + 1))
        and {row.get("attempt") for row in controls} == set(range(1, control_count + 1))
    )
    checks = {
        "native_windows": summary.get("native_windows") is True,
        "exact_extension": summary.get("extension_sha256") == extension_sha,
        "original_supervisor": summary.get("supervisor_sha256") == SOURCES["windows-test-supervisor.py"][3],
        "original_reproducer": summary.get("original_reproducer_sha256") == SOURCES["windows-native-geometry.R"][3],
        "all_requested_fresh_processes_recorded": counts_ok,
        "default_processes_valid": bool(defaults) and all(
            valid_r_row(row, extension_sha, "default", allow_hang=not guarded)
            and row.get("case_kind") == "raw" for row in defaults),
        "single_thread_controls_passed": bool(controls) and all(
            valid_r_row(row, extension_sha, "control", allow_hang=False)
            and row.get("case_kind") == "raw" for row in controls),
        "spatial_operations_checked": valid_r_row(summary.get("smoke", {}), extension_sha,
                                                 "default", allow_hang=not guarded)
                                      and summary.get("smoke", {}).get("case_kind") == "smoke",
    }
    hang_count = sum(row.get("reason") == "quit-hang" for row in defaults)
    if guarded:
        checks["all_software_tests_passed"] = summary.get("all_passed") is True
    return {"checks": checks, "valid": all(checks.values()),
            "default_exit_hangs": hang_count,
            "default_attempts": default_count, "control_attempts": control_count}


def assess_sql(summary: dict, extension_sha: str, guarded: bool) -> dict:
    allowed = {"passed"} if guarded else {"passed", "timeout_after_test_completion"}
    checks = {
        "same_binary": summary.get("extension_sha256") == extension_sha
                       and summary.get("binary_unchanged") is True,
        "mapped_candidate_verified": summary.get("observed_exact_binary") is True
                                     and summary.get("module_observation_errors") == [],
        "tests_all_executed": summary.get("all_tests_reported") is True,
        "assertions_all_passed": summary.get("all_tests_ok") is True,
        "no_event_parse_errors": summary.get("event_errors") == [],
        "valid_process_outcome": summary.get("reason") in allowed,
        "no_skip_or_empty_suite": summary.get("upstream_test_files", 0) > 0
                                  and summary.get("statement_passes", 0) > 0,
    }
    if guarded:
        checks["clean_exit"] = summary.get("all_passed") is True and summary.get("exit_code") == 0
    return {"checks": checks, "valid": all(checks.values())}


def assess_native(summary: dict) -> dict:
    expected = {(variant, mode) for variant in ("archive", "baseline", "guarded")
                for mode in ("unload", "foreign-unload", "main-exit", "abandoned-reader")}
    rows = summary.get("results", [])
    pairs = [(row.get("variant"), row.get("mode")) for row in rows]
    proofs = summary.get("link_proofs", {})
    checks = {
        "complete_mechanism_and_cleanup_matrix": len(rows) == len(expected) and set(pairs) == expected,
        "fixture_expectations_met": bool(rows) and all(row.get("experiment_expectation_met") is True for row in rows),
        "all_three_link_proofs": set(proofs) == {"archive", "baseline", "guarded"}
                                 and all(proof.get("passed") is True for proof in proofs.values()),
        "declared_complete": summary.get("candidate_validated_for_runtime_fixture") is True,
    }
    for row in rows:
        if row.get("variant") == "guarded" or row.get("mode") != "abandoned-reader":
            checks[f"clean_exit_{row.get('variant')}_{row.get('mode')}"] = row.get("software_exit_passed") is True
        else:
            checks[f"hang_preserved_as_failure_{row.get('variant')}"] = (
                row.get("classification") == "expected-diagnostic-hang"
                and row.get("software_exit_passed") is False
                and row.get("terminated_by_supervisor") is True)
    return {"checks": checks, "valid": all(checks.values())}


def assess(root: Path) -> dict:
    result = {"verdict": "incomplete", "supported_windows_fix_comparison": False,
              "ready_for_upstream_submission": False, "arms": {}, "errors": []}
    try:
        manifest = read_json(root / "runtime" / "inputs.json")
        native = read_json(root / "native" / "summary.json")
        result["native"] = assess_native(native)
        provenance = read_json(root / "build-provenance.json")
        result["build_provenance"] = provenance
        archives = {entry.get("name"): entry.get("sha256") for entry in provenance.get("archives", [])}
        if not provenance.get("compiler_sha256") or provenance.get("compiler_sha256") != native.get("compiler_binary", {}).get("sha256"):
            result["errors"].append("Native fixture and Spatial used different/unverified compiler binaries")
        if not archives.get("libstdc++.a") or archives.get("libstdc++.a") != native.get("runtime_archive", {}).get("sha256"):
            result["errors"].append("Native fixture and Spatial used different/unverified C++ runtime archives")
        for library in ("libpthread.a", "libwinpthread.a"):
            if not archives.get(library) or archives.get(library) != native.get("thread_runtime_archives", {}).get(library, {}).get("sha256"):
                result["errors"].append(f"Native fixture and Spatial used different/unverified {library}")
        if provenance.get("compiler_version") != "10.4.0" or provenance.get("platform") != "windows_amd64_mingw":
            result["errors"].append("Wrong Windows compiler/platform provenance")
        if provenance.get("duckdb_commit") != "d54cbbe3df17e8e5ce68312f483b4155836112aa":
            result["errors"].append("Wrong DuckDB commit")
        if provenance.get("ci_tools_commit") != "969bc76276acec1c0a3922ff500d2ec3a1a3b037":
            result["errors"].append("Wrong extension build infrastructure commit")
        for name in ARMS:
            folder = root / name
            arm = read_json(folder / "arm-result.json")
            binary_hash = hashlib.sha256((folder / "spatial.duckdb_extension").read_bytes()).hexdigest()
            if arm.get("arm") != name or arm.get("build_completed") is not True or arm.get("extension_sha256") != binary_hash:
                result["errors"].append(f"Invalid build/binary identity: {name}")
            expected_source = None if name == "upstream" else manifest["generated"][
                "guarded.cc" if name == "guarded" else "baseline.cc"]["sha256"]
            if arm.get("runtime_source_sha256") != expected_source:
                result["errors"].append(f"Wrong selected runtime source: {name}")
            spatial = read_json(folder / "r" / "summary.json")
            sql = read_json(folder / "sql" / "summary.json")
            arm_result = {
                "extension_sha256": binary_hash,
                "r": assess_r(spatial, binary_hash, name == "guarded"),
                "sql": assess_sql(sql, binary_hash, name == "guarded"),
                "recorded_test_exit_codes": {"r": arm.get("r_exit_code"), "sql": arm.get("sql_exit_code")},
            }
            if name == "guarded" and (arm.get("r_exit_code") != 0 or arm.get("sql_exit_code") != 0):
                result["errors"].append("Guarded software test driver returned nonzero")
            result["arms"][name] = arm_result
        all_valid = result["native"]["valid"] and not result["errors"] and all(
            arm["r"]["valid"] and arm["sql"]["valid"] for arm in result["arms"].values())
        baselines_reproduced = all(result["arms"][name]["r"]["default_exit_hangs"] > 0
                                  for name in ("upstream", "rebuilt-baseline"))
        result["baselines_both_reproduced"] = baselines_reproduced
        if not all_valid:
            result["verdict"] = "failed-validation"
        elif not baselines_reproduced:
            result["verdict"] = "inconclusive-no-matched-baseline-reproduction"
        else:
            result["verdict"] = "supported-windows-comparison"
            result["supported_windows_fix_comparison"] = True
        result["note"] = (
            "A supported result is evidence for independent review, not permission to merge or submit upstream. "
            "Baseline shutdown hangs remain failed software exits. Broader supported-platform validation "
            "and investigation of the original abandoned reader remain separate work.")
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    outcome = assess(args.root)
    args.root.mkdir(parents=True, exist_ok=True)
    (args.root / "assessment.json").write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["supported_windows_fix_comparison"] else 1)
