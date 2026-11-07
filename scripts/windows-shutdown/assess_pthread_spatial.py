#!/usr/bin/env python3
"""Assess the six-arm pthread/GCC comparison without rewriting failed exits.

This is a live Windows-run gate: it rechecks installed compiler inputs as well
as retained private archives. Offline artifact review must deliberately remap
those native paths instead of treating missing installed files as verified.
Neither a diagnostic success nor this gate authorizes upstream submission.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys

from assess_experiment import assess_native, assess_r, assess_sql, read_json
from prepare_pthread_archives import (
    ARCHIVE_NAMES, MINGW_COMMIT, PATCHED_SHA256, PINNED_INSTALLED, REVERSE_COMMIT,
    SOURCES as PTHREAD_SOURCES, reverse_source, validate_layout,
)
from prepare_runtime import SOURCES, guard_source
from run_runtime import assess as assess_cleanup_case
from run_spatial import adapt_reproducer
from run_sql_tests import event_passed, read_events, verified_empty_tests
from run_tls_storage_order import assess as assess_order_case
from verify_pthread_build import inspect_link, inspect_preparation


ARMS = {
    "original": ("original", "archive"),
    "rebuilt-control": ("rebuilt-baseline", "baseline"),
    "order-only": ("reverse-order", "baseline"),
    "guard-only": ("rebuilt-baseline", "guarded"),
    "combined": ("reverse-order", "guarded"),
    "order-only-archive": ("reverse-order", "archive"),
}
PTHREAD_ARMS = ("original", "rebuilt-baseline", "reverse-order")
GCC_ARMS = ("archive", "baseline", "guarded")
CORE_COMMIT = "d54cbbe3df17e8e5ce68312f483b4155836112aa"
CI_TOOLS_COMMIT = "969bc76276acec1c0a3922ff500d2ec3a1a3b037"
VCPKG_COMMIT = "cd61e1e26a038e82d6550a3ebbe0fbbfe7da78e3"
RAW_COMMAND_FIELDS = (
    "command", "pid", "elapsed_seconds", "exit_request_observed_seconds",
    "timeout_stage", "timed_out", "terminated_by_supervisor", "returncode",
    "returncode_after_termination", "stdout", "stderr", "stdout_path", "stderr_path",
)
SCRIPT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = SCRIPT_ROOT.parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def norm(path: str | Path) -> str:
    return re.sub("/+", "/", str(path).replace("\\", "/")).casefold()


def records_match_paths(actual: dict, expected: dict) -> bool:
    return set(actual) == set(expected) and all(
        norm(actual[name]["path"]) == norm(expected[name]["path"])
        and actual[name]["sha256"] == expected[name]["sha256"] for name in expected)


def strict_exit_fields(row: dict) -> bool:
    """A fabricated success label cannot excuse a kill or a retained DLL."""
    if row.get("reason") == "success":
        return (type(row.get("exit_code")) is int and row["exit_code"] == 0
                and "kill_detail" in row and row["kill_detail"] is None
                and row.get("verified_installed_copy_removed") is True
                and row.get("software_passed") is True)
    if row.get("reason") == "quit-hang":
        return (type(row.get("exit_code")) is int and row["exit_code"] != 0
                and isinstance(row.get("kill_detail"), str) and bool(row["kill_detail"].strip())
                and row.get("software_passed") is False and row.get("quit_grace_seconds") == 30)
    return False


def assess_r_records(summary: dict, extension_sha: str) -> dict:
    """Allow a measured negative outcome, while retaining the original gates."""
    observed = assess_r(summary, extension_sha, guarded=False)
    rows = summary.get("results", []) + [summary.get("smoke", {})]
    checks = {
        "original_observation_gates": observed["valid"],
        "exact_requested_matrix": summary.get("requested") == {
            "default_attempts": 30, "control_attempts": 5},
        "normal_exit_and_hang_fields": len(rows) == 36 and all(strict_exit_fields(row) for row in rows),
        "declared_pass_matches_individual_outcomes": summary.get("all_passed") is
            all(row.get("reason") == "success" and row.get("software_passed") is True for row in rows),
    }
    valid = all(checks.values())
    return {
        "valid": valid, "checks": checks, "original_gates": observed,
        "all_software_passed": valid and assess_r(summary, extension_sha, guarded=True)["valid"],
        "default_quit_hangs": observed["default_exit_hangs"],
        "smoke_reason": summary.get("smoke", {}).get("reason"),
        "counts": {mode: dict(Counter(row.get("reason") for row in summary.get("results", [])
                                    if row.get("thread_mode") == mode))
                   for mode in ("default", "control")},
    }


def verify_r_files(folder: Path, summary: dict, runtime: Path, extension: Path) -> None:
    """Bind every outcome to its raw supervisor, checkpoints and exact DLL."""
    original = (runtime / "windows-native-geometry.R").read_text(encoding="utf-8")
    programs = {smoke: adapt_reproducer(original, smoke=smoke).encode("utf-8") for smoke in (False, True)}
    expected_sha, expected_md5 = digest(extension), digest(extension, "md5")
    rows = summary["results"] + [summary["smoke"]]
    cases = []
    for row in rows:
        mode, attempt, kind = row["thread_mode"], row["attempt"], row["case_kind"]
        require(mode in ("default", "control") and kind in ("raw", "smoke")
                and type(attempt) is int and attempt > 0, "Invalid R case identity")
        case = folder / f"{kind}-{mode}-{attempt:03d}"
        cases.append(case.name)
        require(norm(row["directory"]) == norm(case.resolve()), "R case directory disagrees with its arm")
        require(read_json(case / "result.json") == row, "R summary differs from individual outcome")
        program = case / "reproducer.R"
        require(program.read_bytes() == programs[kind == "smoke"]
                and digest(program) == row["adapted_reproducer_sha256"], "Executed R program changed")
        label = "raw--all--base--" + ("t1" if mode == "control" else "t0")
        require(row["label"] == label, "Unexpected original-supervisor label")
        diagnostics = case / "diagnostics"
        raw = read_json(diagnostics / f"{label}-result.json")
        require(all(row.get(key) == value for key, value in raw.items()), "Original supervisor result changed")
        lines = (diagnostics / f"{label}-checkpoints.log").read_text(encoding="utf-8").splitlines()
        messages = [re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+", "", line).strip()
                    for line in lines]
        require(bool(messages) and re.fullmatch(r"PROCESS START: " + re.escape(label) + r" PID=\d+", messages[0]),
                "Missing original R process-start checkpoint")
        expected = [messages[0], "TESTS ENTER"]
        for iteration, recorded in enumerate(row["loaded_extensions"], 1):
            with (diagnostics / f"loaded-extension-{iteration}.csv").open(newline="", encoding="utf-8-sig") as stream:
                observations = list(csv.DictReader(stream))
            require(len(observations) == 1 and all(recorded.get(key) == value for key, value in observations[0].items()),
                    "Raw loaded-extension CSV differs from the recorded observation")
            require(recorded["sha256"] == expected_sha and recorded["source_md5"] == expected_md5
                    and norm(recorded["source_path"]) == norm(extension.resolve())
                    and norm(recorded["installed_from"]) == norm(extension.resolve())
                    and norm(recorded["install_path"]).startswith(norm(case.resolve()) + "/extensions/")
                    and recorded["loaded"] == recorded["installed"] == "TRUE"
                    and recorded["install_mode"] == "CUSTOM_PATH", "R loaded the wrong or unverified Spatial DLL")
            expected.extend((f"ITERATION ENTER: {iteration}", f"LOCAL SPATIAL LOADED: {recorded['install_path']}"))
            if kind == "smoke":
                expected.append("SPATIAL SMOKE RETURNED")
            for operation in ("geojson", "shapefile", "shapefile-mismatch"):
                expected.extend((f"COPY ENTER: {operation}", f"COPY LEAVE: {operation}"))
            expected.extend((f"ITERATION RETURN: {iteration}", f"OWNED CONNECTION CLEANUP ENTER: {iteration}",
                             f"OWNED CONNECTION CLEANUP LEAVE: {iteration}", "rapi_disconnect ENTER",
                             "rapi_disconnect LEAVE", "rapi_shutdown ENTER", "rapi_shutdown LEAVE"))
        expected.extend(("TESTS RETURNED", "GC ENTER", "GC LEAVE", "R EXIT REQUEST: status=0"))
        require(messages == expected, "R workload, cleanup or quit checkpoint sequence incomplete")
        stdout = (diagnostics / f"{label}-stdout.log").read_text(encoding="utf-8")
        require(all(value in stdout for value in ("R version 4.6.2", "DBI_1.3.0", "duckdb_2.0.0")),
                "R host package versions differ from the reproduced environment")
        if row["reason"] == "quit-hang":
            times = [dt.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S.%f") for line in lines]
            require(row["elapsed_seconds"] >= (times[-1] - times[0]).total_seconds() + 29.5,
                    "Shutdown hang did not retain the full post-quit grace period")
    require(len(cases) == 36 and len(set(cases)) == 36
            and {path.parent.name for path in folder.glob("*/result.json")} == set(cases),
            "Missing, duplicate or unexpected individual R result")


def assess_sql_records(summary: dict, extension_sha: str) -> dict:
    observed = assess_sql(summary, extension_sha, guarded=False)
    reason, code = summary.get("reason"), summary.get("exit_code")
    exit_valid = type(code) is int and (
        (reason == "passed" and code == 0 and summary.get("all_passed") is True
         and summary.get("timeout_processes") == []) or
        (reason == "timeout_after_test_completion" and code != 0 and summary.get("all_passed") is False))
    valid = observed["valid"] and exit_valid
    return {"valid": valid, "original_gates": observed, "exit_fields_valid": exit_valid,
            "reason": reason, "all_software_passed": valid and assess_sql(summary, extension_sha, guarded=True)["valid"]}


def verify_sql_files(folder: Path, summary: dict) -> None:
    selected = sorted((SOURCE_ROOT / "test/sql/gdal").glob("*.test"))
    selected += sorted((SOURCE_ROOT / "test/sql/geos").glob("*.test"))
    selected += [SOURCE_ROOT / "test/sql/proj.test"]
    require(len(selected) == 33, "Unchanged SQL test selection must contain all 33 upstream files")
    known_empty = verified_empty_tests(selected, SOURCE_ROOT)
    expected = [folder / "candidate-binary-provenance.test", *selected]
    paths = (folder / "sql-inputs.txt").read_text(encoding="utf-8").splitlines()
    require([norm(path) for path in paths] == [norm(path.resolve()) for path in expected],
            "SQL input list changed or omitted an original test")
    events, errors = read_events(folder / "unittest.log")
    begins = [event for event in events if event.get("event") == "begin"]
    ends = [event for event in events if event.get("event") == "end"]
    require(not errors and len(events) == 68 and len(begins) == len(ends) == 34
            and {norm(event["name"]) for event in begins} == {norm(path) for path in paths}
            and {norm(event["name"]) for event in ends} == {norm(path) for path in paths},
            "SQL terminal event coverage is incomplete or duplicated")
    require(ends == summary["tests"] and all(event_passed(event, SOURCE_ROOT, known_empty) for event in ends)
            and all(type(event.get(key)) is int for event in ends for key in ("passes", "fails", "skip-mode"))
            and sum(event["passes"] for event in ends) == summary["statement_passes"] >= 130
            and sum(event["fails"] for event in ends) == summary["statement_failures"] == 0,
            "Raw SQL statements failed, skipped, changed or lost assertion coverage")
    source_records = [{"path": str(path.relative_to(SOURCE_ROOT)), "sha256": digest(path)} for path in selected]
    require(summary["source_files"] == source_records and summary["verified_empty_test_files"] == list(known_empty.values())
            and summary["upstream_test_files"] == 33 and summary["expected_test_files"] == 34,
            "SQL source or exact known-empty-file provenance changed")
    require({norm(path) for path in summary["observed_spatial_modules"]} == {norm(summary["repository_extension"])},
            "SQL process did not map only the recorded candidate repository DLL")
    require(digest(Path(summary["runner"])) == summary["runner_sha256"], "SQL native runner changed")


def gcc_link_proof(map_text: str, symbols: str, gcc_arm: str, *, native: bool) -> dict:
    definitions = re.findall(r"^\s*[0-9a-fA-F]+\s+(\S)\s+__cxa_thread_atexit\s*$", symbols, re.MULTILINE)
    archived = re.search(r"libstdc\+\+\.a\([^)]*atexit_thread\.(?:o|obj)\)", map_text) is not None
    selected = gcc_arm == "archive" or (f"{gcc_arm}-runtime.o" if native else f"{gcc_arm}.cc.obj") in map_text
    return {"passed": gcc_arm in GCC_ARMS and definitions in (["T"], ["t"])
            and archived == (gcc_arm == "archive") and selected,
            "cxa_definitions": len(definitions), "cxa_definition_types": definitions,
            "archive_fallback_present": archived, "selected_source_present": selected}


def raw_command_result(row: dict, folder: Path, stem: str) -> dict:
    # Both per-case assess helpers append **result. Only raw command fields may
    # enter reassessment, or stored derived booleans could override new checks.
    raw = {name: row[name] for name in RAW_COMMAND_FIELDS}
    for channel in ("stdout", "stderr"):
        path = folder / f"{stem}.{channel}.log"
        require(norm(row[f"{channel}_path"]) == norm(path.resolve())
                and path.read_bytes() == row[channel].encode("utf-8"), "Native raw output record differs")
    require(type(raw["timed_out"]) is bool and type(raw["terminated_by_supervisor"]) is bool,
            "Native termination fields are not booleans")
    return raw


def verify_preflight_identity(folder: Path, summary: dict, runtime: Path, preparation: dict,
                              pthread_arm: str, *, ordering: bool) -> None:
    variant = preparation["variants"][pthread_arm]
    selected = {name: {"path": record["path"], "sha256": record["sha256"]}
                for name, record in variant["archives"].items()}
    require(summary["compiler_binary"] == preparation["compiler"], "Preflight compiler differs from prepared compiler")
    require(isinstance(summary.get("platform"), str) and summary["platform"].startswith("Windows"),
            "Preflight did not record a native Windows platform")
    commands = summary["commands" if ordering else "compiler_commands"]
    require(bool(commands) and all(type(row["returncode"]) is int and row["returncode"] == 0
                                  and row["timed_out"] is False for row in commands), "A compiler identity command failed")
    for option, expected in (("-dumpfullversion", "10.4.0"), ("-dumpmachine", None)):
        values = [row["stdout"].strip() for row in commands if row["command"][-1] == option]
        require(len(values) == 1 and (values[0] == expected if expected else values[0].startswith("x86_64-w64-mingw32")),
                "Preflight compiler version or target is unproved")
    require(bool(summary["builds"]) and all(type(row["returncode"]) is int and row["returncode"] == 0
                                          and row["timed_out"] is False for row in summary["builds"]),
            "A native preflight build failed")
    require(norm(summary["selected_pthread_library_dir"]) == norm(variant["library_dir"]),
            "Preflight selected a different pthread family")
    if ordering:
        archives = summary["runtime_archives"]
        require(records_match_paths({name: archives[name] for name in ARCHIVE_NAMES}, selected)
                and archives == summary["runtime_archives_after_execution"]
                and summary["selected_archives_unchanged_after_linking"] is True
                and summary["selected_archives_unchanged_after_execution"] is True,
                "Ordering preflight archive identity changed")
        for name in ("libstdc++.a", "libgcc.a"):
            require(archives[name]["sha256"] == PINNED_INSTALLED[name]
                    and digest(Path(archives[name]["path"])) == PINNED_INSTALLED[name], "Wrong ordering GCC archive")
        require(set(summary["compiler_default_runtime_archives"]) == {"libstdc++.a", "libgcc.a", *ARCHIVE_NAMES},
                "Ordering preflight omitted an installed runtime identity")
        for name, record in summary["compiler_default_runtime_archives"].items():
            require(name in PINNED_INSTALLED and record["sha256"] == PINNED_INSTALLED[name], "Default runtime archive changed")
        fixture = SCRIPT_ROOT / "tls_storage_order"
        source_files = [fixture / name for name in ("probe.cpp", "probe.h", "host.c")]
        source_files += [SCRIPT_ROOT / name for name in ("run_tls_storage_order.py", "run_runtime.py", "prepare_runtime.py", "pthread_provenance.py")]
        prefix, host = "storage-order", "storage-order-host.exe"
        require(summary["compiler_emutls_access_proven"] is True, "Probe did not access actual compiler emutls")
    else:
        require(records_match_paths(summary["thread_runtime_archives"], selected)
                and summary["thread_runtime_archives"] == summary["thread_runtime_archives_after_linking"]
                == summary["thread_runtime_archives_after"]
                and summary["selected_pthread_archives_unchanged_after_linking"] is True
                and summary["selected_pthread_archives_unchanged_after_execution"] is True,
                "Cleanup preflight archive identity changed")
        require(summary["runtime_archive"]["sha256"] == PINNED_INSTALLED["libstdc++.a"]
                and digest(Path(summary["runtime_archive"]["path"])) == PINNED_INSTALLED["libstdc++.a"],
                "Wrong cleanup GCC archive")
        fixture = SCRIPT_ROOT / "runtime_tests"
        source_files = [fixture / name for name in ("fixture.cpp", "fixture.h", "host.c")]
        source_files += [SCRIPT_ROOT / name for name in ("run_runtime.py", "pthread_provenance.py")]
        prefix, host = "runtime", "runtime-host.exe"
    source_files += [runtime / "baseline.cc", runtime / "guarded.cc"]
    expected_sources = {norm(path.resolve()): digest(path) for path in source_files}
    require({norm(path): value for path, value in summary["source_sha256"].items()} == expected_sources,
            "Preflight source snapshot differs from current verified sources")
    expected_binaries = {norm((folder / name).resolve()): digest(folder / name)
                         for name in (host, *(f"{prefix}-{gcc}.dll" for gcc in GCC_ARMS))}
    require({norm(path): value for path, value in summary["binary_sha256"].items()} == expected_binaries,
            "Preflight retained binary differs or is missing")
    for gcc in GCC_ARMS:
        mapping = (folder / f"{prefix}-{gcc}.map").read_text(encoding="utf-8-sig")
        symbols = (folder / f"symbols-{gcc}.stdout.log").read_text(encoding="utf-8-sig")
        require(gcc_link_proof(mapping, symbols, gcc, native=True)["passed"], "Preflight linked the wrong GCC fallback")
        require(inspect_link(mapping, symbols, variant["archives"])["passed"], "Preflight linked the wrong pthread provider")
        require(summary["link_proofs"][gcc]["passed"] is True, "Preflight link check was not successful")
        if ordering:
            names = ("__cxa_thread_atexit", "__emutls_get_address", "emutls_destroy", "__wrap_free",
                     "pthread_key_create", "pthread_key_delete", "_pthread_cleanup_dest")
            counts = {name: len(re.findall(r"^\s*[0-9a-fA-F]+\s+[Tt]\s+" + re.escape(name) + r"\s*$",
                                           symbols, re.MULTILINE)) for name in names}
            disassembly = (folder / f"emutls-destroy-{gcc}.stdout.log").read_text(encoding="utf-8-sig")
            branches = len(re.findall(r"\b(?:callq?|jmpq?)\s+[^\n]*<__wrap_free>", disassembly))
            require(all(value == 1 for value in counts.values()) and branches >= 1
                    and summary["link_proofs"][gcc]["symbol_counts"] == counts
                    and summary["link_proofs"][gcc]["emutls_destroy_branches_to_wrapped_free"] == branches,
                    "Real emutls release interception is unproved")


def assess_cleanup_records(summary: dict, folder: Path) -> dict:
    require(summary["workload"] == {"cycles": 16, "workers": 8, "timeout": 10.0}, "Native cleanup workload changed")
    base = assess_native(summary)
    require(base["valid"], "Original native cleanup matrix gates failed")
    rows = []
    for row in summary["results"]:
        gcc, mode = row["variant"], row["mode"]
        stem = f"{mode}-{gcc}"
        markers = (folder / f"{stem}.markers.log").read_text(encoding="utf-8").splitlines()
        if mode in ("unload", "foreign-unload"):
            cycle = ["CPP_TLS_DTOR"] * 8
            if mode == "foreign-unload":
                cycle += ["FOREIGN_WORKERS_JOINED"]
            expected_markers = cycle + ["ATEXIT_CALLBACK", "GLOBAL_DTOR"]
            expected_markers = expected_markers * 16 + ["FOREIGN_UNLOAD_OK" if mode == "foreign-unload" else "UNLOAD_OK"]
        elif mode == "main-exit":
            expected_markers = ["PROCESS_EXIT_REQUEST", "CPP_TLS_DTOR", "ATEXIT_CALLBACK", "GLOBAL_DTOR"]
        else:
            expected_markers = ["BLOCKED_DTOR_ENTER", "PROCESS_EXIT_REQUEST"]
            if gcc == "guarded":
                expected_markers += ["ATEXIT_CALLBACK", "GLOBAL_DTOR"]
        require(markers == expected_markers, "Native cleanup events were missing, unexpected or out of order")
        raw = raw_command_result(row, folder, stem)
        expected_command = [str((folder / "runtime-host.exe").resolve()), str((folder / f"runtime-{gcc}.dll").resolve()),
                            mode, str((folder / f"{stem}.markers.log").resolve()), "16", "8"]
        require([norm(value) for value in raw["command"]] == [norm(value) for value in expected_command],
                "Native cleanup case ran a different host, DLL or workload")
        actual = assess_cleanup_case(raw, variant=gcc, mode=mode, markers=markers, cycles=16, workers=8)
        for key in ("checks", "marker_counts", "classification", "expected_hang", "experiment_expectation_met", "software_exit_passed"):
            require(actual[key] == row[key], f"Stored native cleanup {key} disagrees with raw evidence")
        require(actual["experiment_expectation_met"], "Native cleanup contract failed")
        if actual["expected_hang"]:
            require(raw["terminated_by_supervisor"] is True and raw["returncode"] is None
                    and raw["elapsed_seconds"] - raw["exit_request_observed_seconds"] >= 9.95,
                    "Deliberate native hang was not observed for the full exit grace")
        else:
            require(raw["terminated_by_supervisor"] is False and type(raw["returncode"]) is int
                    and raw["returncode"] == 0, "Native cleanup success was forced or nonzero")
        rows.append({"gcc_arm": gcc, "mode": mode, "software_exit_passed": actual["software_exit_passed"]})
    return {"valid": True, "original_gates": base, "cases": rows,
            "normal_exits": sum(row["software_exit_passed"] for row in rows),
            "deliberate_failed_exits": sum(not row["software_exit_passed"] for row in rows)}


def assess_order_records(summary: dict, folder: Path, pthread_arm: str) -> dict:
    expected_order = "callback-first" if pthread_arm == "reverse-order" else "storage-first"
    expected_cases = {(gcc, mode) for gcc in GCC_ARMS for mode in ("pthread", "foreign")}
    pairs = [(row["variant"], row["mode"]) for row in summary["results"]]
    require(len(pairs) == 6 and set(pairs) == expected_cases, "Incomplete or duplicate storage-order matrix")
    require(summary["expected_order"] == expected_order and summary["diagnostic_expectation_met"] is True
            and summary["storage_lifetime_contract_passed"] is (pthread_arm == "reverse-order"),
            "Storage order expectations or lifetime distinction changed")
    rows = []
    for row in summary["results"]:
        gcc, mode = row["variant"], row["mode"]
        raw = raw_command_result(row, folder, f"{mode}-{gcc}")
        expected_command = [str((folder / "storage-order-host.exe").resolve()),
                            str((folder / f"storage-order-{gcc}.dll").resolve()), mode]
        require([norm(value) for value in raw["command"]] == [norm(value) for value in expected_command],
                "Ordering case ran a different host, DLL or worker origin")
        actual = assess_order_case(raw, variant=gcc, mode=mode, expected_order=expected_order)
        for key in ("checks", "observation", "classification", "observed_order", "diagnostic_expectation_met", "storage_lifetime_contract_passed"):
            require(actual[key] == row[key], f"Stored ordering {key} disagrees with raw evidence")
        require(actual["diagnostic_expectation_met"] and type(raw["returncode"]) is int and raw["returncode"] == 0,
                "Real storage release/callback order did not meet the diagnostic expectation")
        rows.append({"gcc_arm": gcc, "mode": mode, "storage_lifetime_contract_passed": actual["storage_lifetime_contract_passed"]})
    return {"valid": True, "expected_order": expected_order, "cases": rows,
            "storage_lifetime_contract_passed": all(row["storage_lifetime_contract_passed"] for row in rows)}


def inspect_inputs(root: Path) -> tuple[dict, dict]:
    runtime = root / "runtime"
    manifest = read_json(runtime / "inputs.json")
    for name, (_, _, _, expected) in SOURCES.items():
        require(manifest["sources"][name]["sha256"] == digest(runtime / name) == expected,
                f"Immutable diagnostic/GCC source changed: {name}")
    baseline = (runtime / "gcc10-atexit_thread.cc").read_bytes()
    require((runtime / "baseline.cc").read_bytes() == baseline
            and (runtime / "guarded.cc").read_bytes() == guard_source(baseline.decode()).encode(),
            "GCC source reconstruction or guard changed")
    for name in ("baseline.cc", "guarded.cc"):
        require(manifest["generated"][name]["sha256"] == digest(runtime / name), "Generated GCC source hash differs")
    preparation = inspect_preparation(root / "pthread")
    prepared = read_json(root / "pthread/manifest.json")
    require(prepared["sources"]["commit"] == MINGW_COMMIT and prepared["sources"]["reverse_commit"] == REVERSE_COMMIT,
            "Unrecognized MinGW baseline or accepted patch")
    for name, expected in PTHREAD_SOURCES.items():
        require(prepared["sources"]["files"][name]["sha256"] == digest(root / "pthread/inputs" / name) == expected,
                f"Immutable MinGW source changed: {name}")
    require((root / "pthread/inputs/reverse-thread.c").read_bytes() ==
            reverse_source((root / "pthread/inputs/src/thread.c").read_bytes())
            and digest(root / "pthread/inputs/reverse-thread.c") == PATCHED_SHA256, "Pthread patch differs from the accepted two changes")
    validate_layout(prepared["layout"]["observed"])
    require(preparation["compiler"]["sha256"] == PINNED_INSTALLED["g++.exe"]
            and prepared["tools"]["g++"]["numeric_version"] == "10.4.0", "Wrong prepared compiler")
    for name in ("pthread-preparation-before.json", "pthread-preparation-after.json"):
        recorded = read_json(root / name)
        require(all(recorded.get(key) == value for key, value in preparation.items()),
                f"Installed/private inputs changed or final audit is incomplete: {name}")
    provenance = read_json(root / "build-provenance.json")
    require(provenance["compiler_sha256"] == preparation["compiler"]["sha256"]
            and norm(provenance["compiler_path"]) == norm(preparation["compiler"]["path"])
            and provenance["compiler_version"] == "10.4.0" and provenance["platform"] == "windows_amd64_mingw"
            and provenance["duckdb_commit"] == CORE_COMMIT and provenance["duckdb_version"] == "v2.0.0"
            and provenance["ci_tools_commit"] == CI_TOOLS_COMMIT and provenance["vcpkg_commit"] == VCPKG_COMMIT,
            "Spatial build source/toolchain identity differs from the comparison")
    archives = {record["name"]: record for record in provenance["archives"]}
    require(set(archives) == {"libstdc++.a", "libgcc.a", *ARCHIVE_NAMES}
            and all(record["sha256"] == PINNED_INSTALLED[name] == digest(Path(record["path"]))
                    for name, record in archives.items()), "Spatial installed runtime identity changed")
    return preparation, provenance


def inspect_spatial_link(folder: Path, arm: str, preparation: dict, provenance: dict) -> dict:
    pthread_arm, gcc_arm = ARMS[arm]
    record = read_json(folder / "arm-result.json")
    binary = folder / "spatial.duckdb_extension"
    actual_sha = digest(binary)
    require(record["arm"] == arm and record["pthread_arm"] == pthread_arm and record["gcc_arm"] == gcc_arm
            and record["build_completed"] is True and record["extension_sha256"] == actual_sha,
            "Spatial build identity or factorial arm assignment changed")
    runtime_source = None if gcc_arm == "archive" else folder.parent / "runtime" / f"{gcc_arm}.cc"
    require(record["runtime_source_sha256"] == (None if runtime_source is None else digest(runtime_source))
            and (record["runtime_source"] == "" if runtime_source is None
                 else norm(record["runtime_source"]) == norm(runtime_source.resolve())), "Wrong compiled GCC runtime source")
    with binary.open("rb") as stream:
        require(stream.read(2) == b"MZ", "Expected an actual Windows PE extension")
        stream.seek(-512, 2)
        footer_data = stream.read(256)
    footer = [footer_data[index:index + 32].rstrip(b"\0").decode("ascii") for index in range(0, 256, 32)][::-1]
    require(footer[:5] == ["4", "windows_amd64_mingw", "v2.0.0", provenance["source_commit"][:10], "CPP"],
            "Extension platform/version/source footer differs")
    mapping_path, symbols_path = folder / "spatial-link.map", folder / "defined-symbols.txt"
    mapping, symbols = mapping_path.read_text(encoding="utf-8-sig"), symbols_path.read_text(encoding="utf-8-sig")
    variant = preparation["variants"][pthread_arm]
    expected_archives = {name: {"path": info["path"], "sha256": info["sha256"]} for name, info in variant["archives"].items()}
    link = inspect_link(mapping, symbols, variant["archives"])
    gcc = gcc_link_proof(mapping, symbols, gcc_arm, native=False)
    require(link["passed"] and gcc["passed"], "Actual Spatial link did not select the requested runtimes")
    for name in ("pthread-link-proof-before-tests.json", "pthread-link-proof.json"):
        proof = read_json(folder / name)
        require(proof["passed"] is True and proof["pthread_arm"] == pthread_arm
                and proof["extension_sha256"] == actual_sha and proof["prep_manifest_sha256"] == preparation["prep_manifest_sha256"]
                and norm(proof["selected_library_dir"]) == norm(variant["library_dir"])
                and records_match_paths(proof["archives"], expected_archives)
                and proof["link_map_sha256"] == digest(mapping_path) and proof["symbols_sha256"] == digest(symbols_path)
                and proof["link_proof"] == link, "Before/after Spatial link proof differs from actual retained inputs")
    cache = (folder / "CMakeCache.txt").read_text(encoding="utf-8-sig")
    selected_pthread = re.search(r"^SPATIAL_MINGW_PTHREAD_LIBRARY_DIR:PATH=(.*)$", cache, re.MULTILINE)
    selected_gcc = re.search(r"^SPATIAL_MINGW_TLS_ATEXIT_SOURCE:FILEPATH=(.*)$", cache, re.MULTILINE)
    require(selected_pthread is not None and norm(selected_pthread[1].strip()) == norm(variant["library_dir"]),
            "CMake did not select the requested private pthread directory")
    require(selected_gcc is not None and
            (not selected_gcc[1].strip() if runtime_source is None else norm(selected_gcc[1].strip()) == norm(runtime_source.resolve())),
            "CMake GCC source selection differs from arm identity")
    commands = read_json(folder / "compile_commands.json")
    runtime_commands = [command for command in commands if norm(command["file"]).endswith(("/runtime/baseline.cc", "/runtime/guarded.cc"))]
    require(not runtime_commands if runtime_source is None else
            len(runtime_commands) == 1 and norm(runtime_commands[0]["file"]) == norm(runtime_source.resolve()),
            "Compiled GCC source command differs from expected arm")
    return {"valid": True, "arm_result": record, "extension_sha256": actual_sha,
            "pthread_arm": pthread_arm, "gcc_arm": gcc_arm, "pthread_link": link, "gcc_link": gcc}


def decide(result: dict) -> dict:
    """A guard-only success cannot satisfy either supported-fix predicate."""
    all_valid = (not result["errors"] and result.get("preparation_valid") is True
                 and set(result["preflights"]) == set(PTHREAD_ARMS)
                 and all(value.get("valid") is True for value in result["preflights"].values())
                 and set(result["arms"]) == set(ARMS)
                 and all(value.get("observation_valid") is True for value in result["arms"].values()))
    baselines = all(result["arms"].get(name, {}).get("r", {}).get("default_quit_hangs", 0) > 0
                    for name in ("original", "rebuilt-control"))
    supported = all_valid and baselines
    ordering = supported and all(result["arms"][name]["all_software_passed"] for name in ("order-only", "order-only-archive"))
    combined = supported and result["arms"]["combined"]["all_software_passed"]
    result.update(complete_valid_observation=all_valid, baselines_both_reproduced=baselines,
                  supported_ordering_only=ordering, supported_combination=combined,
                  supported_windows_fix_comparison=ordering or combined, ready_for_upstream_submission=False,
                  guard_only_is_a_complete_fix=False)
    result["verdict"] = (
        "failed-validation" if not all_valid else
        "inconclusive-no-matched-baseline-reproduction" if not baselines else
        "supported-six-arm-comparison" if ordering or combined else "no-supported-candidate")
    return result


def assess(root: Path) -> dict:
    root = root.resolve()
    result = {"verdict": "incomplete", "supported_windows_fix_comparison": False,
              "supported_ordering_only": False, "supported_combination": False,
              "ready_for_upstream_submission": False, "guard_only_is_a_complete_fix": False,
              "preparation_valid": False, "arms": {}, "preflights": {}, "errors": []}
    try:
        preparation, provenance = inspect_inputs(root)
        result.update(preparation_valid=True, prep_manifest_sha256=preparation["prep_manifest_sha256"], build_provenance=provenance)
        require({path.parent.name for path in root.glob("*/arm-result.json")} == set(ARMS), "Six exact Spatial arms are required")
    except Exception as error:
        result["errors"].append(f"inputs: {type(error).__name__}: {error}")
        return decide(result)
    for pthread_arm in PTHREAD_ARMS:
        try:
            native_dir, order_dir = root / "native" / pthread_arm, root / "ordering" / pthread_arm
            native, ordering = read_json(native_dir / "summary.json"), read_json(order_dir / "summary.json")
            verify_preflight_identity(native_dir, native, root / "runtime", preparation, pthread_arm, ordering=False)
            verify_preflight_identity(order_dir, ordering, root / "runtime", preparation, pthread_arm, ordering=True)
            cleanup_result = assess_cleanup_records(native, native_dir)
            ordering_result = assess_order_records(ordering, order_dir, pthread_arm)
            result["preflights"][pthread_arm] = {"valid": True, "cleanup": cleanup_result, "ordering": ordering_result}
        except Exception as error:
            result["preflights"][pthread_arm] = {"valid": False, "error": f"{type(error).__name__}: {error}"}
            result["errors"].append(f"preflight/{pthread_arm}: {type(error).__name__}: {error}")
    runner_hashes = set()
    for name in ARMS:
        try:
            folder = root / name
            linked = inspect_spatial_link(folder, name, preparation, provenance)
            r, sql = read_json(folder / "r/summary.json"), read_json(folder / "sql/summary.json")
            r_result, sql_result = assess_r_records(r, linked["extension_sha256"]), assess_sql_records(sql, linked["extension_sha256"])
            verify_r_files(folder / "r", r, root / "runtime", folder / "spatial.duckdb_extension")
            verify_sql_files(folder / "sql", sql)
            runner_hashes.add(sql["runner_sha256"])
            arm_record = linked["arm_result"]
            require(type(arm_record["r_exit_code"]) is int and arm_record["r_exit_code"] == (0 if r["all_passed"] else 1)
                    and type(arm_record["sql_exit_code"]) is int and arm_record["sql_exit_code"] == (0 if sql["all_passed"] else 1),
                    "Test-driver exits do not match the retained individual outcomes")
            result["arms"][name] = {**linked, "r": r_result, "sql": sql_result,
                                    "observation_valid": r_result["valid"] and sql_result["valid"],
                                    "all_software_passed": r_result["all_software_passed"] and sql_result["all_software_passed"],
                                    "storage_contract_passed_in_native_probe": ARMS[name][0] == "reverse-order"}
        except Exception as error:
            result["arms"][name] = {"observation_valid": False, "all_software_passed": False,
                                    "error": f"{type(error).__name__}: {error}"}
            result["errors"].append(f"arm/{name}: {type(error).__name__}: {error}")
    if len(runner_hashes) != 1:
        result["errors"].append("The six Spatial arms did not share one verified SQL runner")
    result["note"] = (
        "Baseline and negative-candidate hangs remain failed software exits. Old-order native lifetime failures "
        "and deliberate native hangs remain explicit failed contracts. An ordering-only claim needs both GCC "
        "source and untouched-archive order-only arms; guard-only exits cannot prove storage safety. "
        "Supported comparison means evidence for independent review, never permission to submit or merge.")
    return decide(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    outcome = assess(args.root)
    args.root.mkdir(parents=True, exist_ok=True)
    (args.root / "assessment.json").write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["supported_windows_fix_comparison"] else 1)
