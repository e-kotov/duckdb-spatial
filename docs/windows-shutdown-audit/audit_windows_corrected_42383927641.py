#!/usr/bin/env python3
"""Read-only independent audit of corrected Windows run 42383927641.

Reuses the prior audit logic while retaining this run's own twelve-case fixture
and exact historical assessment. Reads the immutable archive and individual
records; never rewrites an original outcome or executes Windows binaries here.
An audit pass means internally consistent evidence, not all software passed.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import types
import zipfile

BASE = Path(__file__).resolve().parent.parent
REPO = BASE / "duckdb-spatial"
FULL = BASE / "evidence/windows-shutdown-experiment-42383927641"
FULL_COMMIT = "e2355ef8f061546f2fecf55b8a64f4297ab71555"
ARCHIVE_SHA256 = "bbde3fd73bdab78c2fcffca2c50dbe1a6d264d2a74881626829a99c62cd6de6f"
ARCHIVE_BYTES = 169244646
ARCHIVE_MEMBERS = 1332
PRIOR_REPORT_SHA256 = "73abce34b91730200b4e319427829a12da7f27d8d4ddba526eb2e98d135574fa"
UPSTREAM_COMMIT = "d332807f4ffdcc4da0092fe760ef48266f548941"
WINDOWS_ARTIFACT_ROOT = "D:/a/duckdb-spatial/duckdb-spatial/build/windows-shutdown"
REPORT = {"errors": [], "full_run": "42383927641", "artifact_id": "13584680289",
          "audit_kind": "read-only evidence integrity and outcome review",
          "original_artifacts_modified": False, "arms": {}, "native_runs": {}, "source_checkout_modes": {}}


def check(condition, message):
    if not condition:
        REPORT["errors"].append(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def norm(path):
    return re.sub("/+", "/", str(path).replace("\\", "/")).lower()


def git_blob(commit, path):
    return subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=REPO)


def source_hash_mode(data, expected_hash):
    # GitHub's Windows checkout converted repository text files to CRLF.
    # Accept only the precise original bytes or that deterministic conversion;
    # record the distinction rather than calling different bytes identical.
    if hashlib.sha256(data).hexdigest() == expected_hash:
        return "original-git-object-bytes"
    crlf = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if hashlib.sha256(crlf).hexdigest() == expected_hash:
        return "git-object-with-CRLF-checkout"
    return None


def read_footer(path):
    with path.open("rb") as stream:
        stream.seek(-512, 2)
        data = stream.read(256)
    return [data[offset:offset + 32].rstrip(b"\0").decode("ascii")
            for offset in range(0, 256, 32)][::-1]


def check_inputs(root, directory):
    folder = root / directory
    manifest = read_json(folder / "inputs.json")
    result = {}
    for section in ("sources", "generated"):
        for name, entry in manifest[section].items():
            actual = digest(folder / name)
            result[name] = actual
            check(actual == entry["sha256"], f"{root.name}: input hash {name}")
    check((folder / "baseline.cc").read_bytes() == (folder / "gcc10-atexit_thread.cc").read_bytes(),
          f"{root.name}: rebuilt baseline differs from original GCC source")
    return result


def check_r_case(arm, folder, expected_hash, expected_md5):
    record = read_json(folder / "result.json")
    diagnostics = folder / "diagnostics"
    raw_result = read_json(diagnostics / f"{record['label']}-result.json")
    tag = f"{arm}/{folder.name}"
    check(all(record.get(key) == value for key, value in raw_result.items()), f"{tag}: original supervisor mismatch")
    check(digest(folder / "reproducer.R") == record["adapted_reproducer_sha256"], f"{tag}: reproducer hash")
    lines = (diagnostics / f"{record['label']}-checkpoints.log").read_text().splitlines()
    messages = [re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+", "", line).strip() for line in lines]
    counts = collections.Counter(messages)
    for iteration in (1, 2, 3):
        for prefix in ("ITERATION ENTER: ", "ITERATION RETURN: ",
                       "OWNED CONNECTION CLEANUP ENTER: ", "OWNED CONNECTION CLEANUP LEAVE: "):
            check(counts[prefix + str(iteration)] == 1, f"{tag}: checkpoint {prefix}{iteration}")
    for operation in ("geojson", "shapefile", "shapefile-mismatch"):
        for prefix in ("COPY ENTER: ", "COPY LEAVE: "):
            check(counts[prefix + operation] == 3, f"{tag}: checkpoint {prefix}{operation}")
    for message in ("TESTS ENTER", "TESTS RETURNED", "GC ENTER", "GC LEAVE", "R EXIT REQUEST: status=0"):
        check(counts[message] == 1, f"{tag}: checkpoint {message}")
    for message in ("rapi_disconnect LEAVE", "rapi_shutdown LEAVE"):
        check(counts[message] == 3, f"{tag}: cleanup checkpoint {message}")
    check(messages[-1] == "R EXIT REQUEST: status=0", f"{tag}: final checkpoint")
    expected_messages = [messages[0], "TESTS ENTER"]
    for iteration in (1, 2, 3):
        expected_messages.append(f"ITERATION ENTER: {iteration}")
        loads = [message for message in messages if message.startswith("LOCAL SPATIAL LOADED:")]
        check(len(loads) == 3, f"{tag}: local load checkpoints")
        expected_messages.append(loads[iteration - 1])
        if folder.name.startswith("smoke-"):
            expected_messages.append("SPATIAL SMOKE RETURNED")
        for operation in ("geojson", "shapefile", "shapefile-mismatch"):
            expected_messages.extend((f"COPY ENTER: {operation}", f"COPY LEAVE: {operation}"))
        expected_messages.extend((f"ITERATION RETURN: {iteration}",
                                  f"OWNED CONNECTION CLEANUP ENTER: {iteration}",
                                  f"OWNED CONNECTION CLEANUP LEAVE: {iteration}",
                                  "rapi_disconnect ENTER", "rapi_disconnect LEAVE",
                                  "rapi_shutdown ENTER", "rapi_shutdown LEAVE"))
    expected_messages.extend(("TESTS RETURNED", "GC ENTER", "GC LEAVE", "R EXIT REQUEST: status=0"))
    check(messages == expected_messages, f"{tag}: complete chronological workload/cleanup sequence")
    check(not any(message.startswith("R ERROR") for message in messages), f"{tag}: R error checkpoint")
    check(record["tests_returned"] is True and record["r_error"] is False
          and record["requested_exit_status"] == 0, f"{tag}: workload did not return successfully")
    startup = [message for message in messages if message.startswith("PROCESS START:")]
    check(len(startup) == 1 and re.search(r"PID=\d+", startup[0]), f"{tag}: fresh process marker")
    stdout = (diagnostics / f"{record['label']}-stdout.log").read_text()
    stderr = (diagnostics / f"{record['label']}-stderr.log").read_text()
    non_checkpoint_stderr = [line for line in stderr.splitlines()
                             if not re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ ", line)]
    check(non_checkpoint_stderr == [f'Tracing function "{name}" in package "namespace:duckdb"'
                                   for name in ("rapi_prepare", "rapi_execute", "rapi_disconnect", "rapi_shutdown")],
          f"{tag}: unexpected non-checkpoint R stderr")
    check("R version 4.6.2" in stdout and "duckdb_2.0.0" in stdout and "DBI_1.3.0" in stdout,
          f"{tag}: runtime package versions")
    check(len(record["loaded_extensions"]) == 3, f"{tag}: three extension observations")
    thread_counts = []
    for iteration in (1, 2, 3):
        with (diagnostics / f"loaded-extension-{iteration}.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        check(len(rows) == 1, f"{tag}: CSV row count {iteration}")
        loaded = rows[0]
        recorded = record["loaded_extensions"][iteration - 1]
        check(all(recorded.get(key) == value for key, value in loaded.items()), f"{tag}: raw CSV mismatch {iteration}")
        check(recorded["sha256"] == expected_hash and loaded["source_md5"] == expected_md5,
              f"{tag}: selected extension hash {iteration}")
        check(loaded["extension_name"] == "spatial" and loaded["loaded"] == loaded["installed"] == "TRUE"
              and loaded["install_mode"] == "CUSTOM_PATH", f"{tag}: extension state {iteration}")
        check(norm(loaded["install_path"]).startswith(norm(record["directory"]) + "/extensions/"),
              f"{tag}: isolated installed path {iteration}")
        check(norm(loaded["source_path"]) == norm(WINDOWS_ARTIFACT_ROOT)
              + f"/{arm}/spatial.duckdb_extension", f"{tag}: source extension path {iteration}")
        check(norm(loaded["installed_from"]) == norm(loaded["source_path"]), f"{tag}: installation origin")
        check(loaded["engine_version"] == "v2.0.0" and loaded["platform"] == "windows_amd64_mingw"
              and loaded["extension_version"] == FULL_COMMIT[:10], f"{tag}: extension metadata {iteration}")
        threads = int(loaded["threads"])
        thread_counts.append(threads)
        check(threads == (1 if record["thread_mode"] == "control" else 4), f"{tag}: actual thread count")
    times = [dt.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S.%f") for line in lines]
    workload_seconds = (times[-1] - times[0]).total_seconds()
    if record["reason"] == "success":
        check(record["exit_code"] == 0 and record["kill_detail"] is None and record["software_passed"] is True,
              f"{tag}: success must be an unforced zero exit")
        check(record.get("verified_installed_copy_removed") is True, f"{tag}: installed copy remains after successful exit")
    elif record["reason"] == "quit-hang":
        check(record["exit_code"] != 0 and bool(record["kill_detail"]) and record["software_passed"] is False,
              f"{tag}: shutdown hang must remain software failure")
        check(record["quit_grace_seconds"] == 30 and record["elapsed_seconds"] >= workload_seconds + 29.5,
              f"{tag}: insufficient post-quit grace")
        pid = int(re.search(r"PID=(\d+)", startup[0]).group(1))
        detail = record["kill_detail"]
        # Retain teardown results as observations. A different cleanup outcome
        # is a material difference, not a reason to force this run to resemble
        # either prior run or to call a forced exit successful.
    else:
        check(False, f"{tag}: unexpected outcome {record['reason']}")
    return {"case": folder.name, "thread_mode": record["thread_mode"], "reason": record["reason"],
            "exit_code": record["exit_code"], "elapsed_seconds": record["elapsed_seconds"],
            "workload_seconds": round(workload_seconds, 3), "threads": thread_counts,
            "pid": int(re.search(r"PID=(\d+)", startup[0]).group(1)),
            "installed_copy_removed": record.get("verified_installed_copy_removed", False),
            "installed_copy_removal_error": record.get("installed_copy_removal_error"),
            "forced_termination_detail": record.get("kill_detail"),
            "same_prior_forced_teardown_pattern": (record["reason"] == "quit-hang"
                and record["kill_detail"].count("SUCCESS:") == 2 and record["kill_detail"].count("ERROR:") == 1
                and f"ERROR: The process with PID {int(re.search(r'PID=(\d+)', startup[0]).group(1))} " in record["kill_detail"]
                and "There is no running instance of the task." in record["kill_detail"]
                and "[WinError 5] Access is denied:" in record.get("installed_copy_removal_error", "")
                and record.get("verified_installed_copy_removed") is not True),
            "reproducer_sha256": record["adapted_reproducer_sha256"]}


def symbols_and_map(folder, variant, native):
    binary = folder / (f"runtime-{variant}.dll" if native else "spatial.duckdb_extension")
    map_path = folder / (f"runtime-{variant}.map" if native else "spatial-link.map")
    symbols = folder / (f"symbols-{variant}.stdout.log" if native else "defined-symbols.txt")
    map_text, symbol_text = map_path.read_text(), symbols.read_text()
    pattern = r"^\s*[0-9a-fA-F]+\s+[Tt]\s+__cxa_thread_atexit\s*$"
    count = len(re.findall(pattern, symbol_text, re.MULTILINE))
    archive = bool(re.search(r"libstdc\+\+\.a\([^)]*atexit_thread\.(?:o|obj)\)", map_text))
    original = variant in ("archive", "upstream")
    source_name = "guarded" if variant == "guarded" else "baseline"
    selected = original or ((f"{source_name}-runtime.o" if native else f"{source_name}.cc.obj") in map_text)
    check(count == 1 and archive == original and selected, f"{folder.name}/{variant}: link proof")
    actual_nm = None
    if shutil.which("nm"):
        result = subprocess.run(["nm", "-C", "--defined-only", str(binary)], capture_output=True, text=True)
        actual_nm = len(re.findall(pattern, result.stdout, re.MULTILINE))
        check(result.returncode == 0 and actual_nm == 1, f"{folder.name}/{variant}: independent binary nm")
    return {"binary_sha256": digest(binary), "cxa_definitions_recorded": count,
            "cxa_definitions_independent_nm": actual_nm, "archive_fallback_present": archive,
            "selected_source_object_present": selected, "map_sha256": digest(map_path)}


def check_native(root, expected_modes, commit):
    folder = root / "native"
    data = read_json(folder / "summary.json")
    check(data["workload"] == {"cycles": 16, "workers": 8, "timeout": 10.0}, f"{root.name}: native workload")
    pairs = [(row["variant"], row["mode"]) for row in data["results"]]
    expected = {(variant, mode) for variant in ("archive", "baseline", "guarded") for mode in expected_modes}
    check(len(pairs) == len(expected) and set(pairs) == expected, f"{root.name}: exact native matrix")
    result = {"commit": commit, "cases": [], "links": {}, "hashes": {},
              "compiler_binary": data["compiler_binary"], "runtime_archive": data["runtime_archive"],
              "thread_runtime_archives": data["thread_runtime_archives"]}
    for path, expected_sha in data["binary_sha256"].items():
        name = PureWindowsPath(path).name
        actual = digest(folder / name)
        check(actual == expected_sha, f"{root.name}: native binary {name}")
        result["hashes"][name] = actual
    for path, expected_sha in data["source_sha256"].items():
        name = PureWindowsPath(path).name
        if name in ("baseline.cc", "guarded.cc"):
            actual = digest(root / "runtime" / name)
            check(actual == expected_sha, f"{root.name}: runtime source {name}")
        else:
            relative = "scripts/windows-shutdown/" + ("runtime_tests/" if name in ("host.c", "fixture.cpp", "fixture.h") else "") + name
            mode = source_hash_mode(git_blob(commit, relative), expected_sha)
            REPORT["source_checkout_modes"][f"{root.name}/{relative}"] = mode
            check(mode is not None, f"{root.name}: committed native source {name}")
    for variant in ("archive", "baseline", "guarded"):
        result["links"][variant] = symbols_and_map(folder, variant, True)
    for row in data["results"]:
        stem = f"{row['mode']}-{row['variant']}"
        marker_sequence = (folder / f"{stem}.markers.log").read_text().splitlines()
        counts = collections.Counter(marker_sequence)
        check(all(counts[name] == value for name, value in row["marker_counts"].items()), f"{root.name}/{stem}: marker record")
        mode, variant = row["mode"], row["variant"]
        expected_counts = {}
        if mode in ("unload", "foreign-unload"):
            expected_counts = {"CPP_TLS_DTOR": 128, "GLOBAL_DTOR": 16, "ATEXIT_CALLBACK": 16,
                               "FOREIGN_UNLOAD_OK" if mode == "foreign-unload" else "UNLOAD_OK": 1}
            if mode == "foreign-unload":
                expected_counts["FOREIGN_WORKERS_JOINED"] = 16
        elif mode == "main-exit":
            expected_counts = {"CPP_TLS_DTOR": 1, "GLOBAL_DTOR": 1, "ATEXIT_CALLBACK": 1, "PROCESS_EXIT_REQUEST": 1}
        else:
            expected_counts = {"BLOCKED_DTOR_ENTER": 1, "PROCESS_EXIT_REQUEST": 1}
            if variant == "guarded":
                expected_counts.update(GLOBAL_DTOR=1, ATEXIT_CALLBACK=1)
        check(dict(counts) == expected_counts, f"{root.name}/{stem}: independent exact cleanup counts")
        if mode in ("unload", "foreign-unload"):
            cycle = ["CPP_TLS_DTOR"] * 8
            if mode == "foreign-unload":
                cycle.append("FOREIGN_WORKERS_JOINED")
            cycle.extend(("ATEXIT_CALLBACK", "GLOBAL_DTOR"))
            expected_sequence = cycle * 16 + ["FOREIGN_UNLOAD_OK" if mode == "foreign-unload" else "UNLOAD_OK"]
        elif mode == "main-exit":
            expected_sequence = ["PROCESS_EXIT_REQUEST", "CPP_TLS_DTOR", "ATEXIT_CALLBACK", "GLOBAL_DTOR"]
        else:
            expected_sequence = ["BLOCKED_DTOR_ENTER", "PROCESS_EXIT_REQUEST"]
            if variant == "guarded":
                expected_sequence.extend(("ATEXIT_CALLBACK", "GLOBAL_DTOR"))
        check(marker_sequence == expected_sequence, f"{root.name}/{stem}: exact per-cycle cleanup order")
        expected_hang = mode == "abandoned-reader" and variant != "guarded"
        if expected_hang:
            check(row["timed_out"] is True and row["terminated_by_supervisor"] is True
                  and row["timeout_stage"] == "after-exit-request" and row["returncode"] is None
                  and row["software_exit_passed"] is False
                  and row["elapsed_seconds"] - row["exit_request_observed_seconds"] >= 9.95,
                  f"{root.name}/{stem}: diagnostic hang semantics")
        else:
            check(row["timed_out"] is False and row["terminated_by_supervisor"] is False
                  and row["returncode"] == 0 and row["software_exit_passed"] is True,
                  f"{root.name}/{stem}: normal unforced exit")
        for channel in ("stdout", "stderr"):
            check((folder / f"{stem}.{channel}.log").read_text() == row[channel].replace("\r\n", "\n"),
                  f"{root.name}/{stem}: raw {channel} record")
        result["cases"].append({"variant": variant, "mode": mode, "counts": dict(counts),
                                "expected_hang": expected_hang, "software_exit_passed": row["software_exit_passed"],
                                "per_cycle_cleanup_order_verified": marker_sequence == expected_sequence,
                                "elapsed_seconds": row["elapsed_seconds"], "returncode": row["returncode"]})
    return result


def main():
    REPORT["input_hashes"] = {"full": check_inputs(FULL, "runtime")}
    # Check the actual immutable ZIP, then verify every extracted payload against
    # it. This also catches accidentally edited local evidence before review.
    archive = FULL.with_suffix(".zip")
    check(archive.stat().st_size == ARCHIVE_BYTES and digest(archive) == ARCHIVE_SHA256,
          "Corrected-run archive size or immutable SHA256 differs")
    with zipfile.ZipFile(archive) as bundle:
        names = [entry.filename for entry in bundle.infolist() if not entry.is_dir()]
        check(len(names) == ARCHIVE_MEMBERS and len(set(names)) == ARCHIVE_MEMBERS, "Unexpected archive member set")
        for entry in bundle.infolist():
            if entry.is_dir():
                continue
            target = (FULL / entry.filename).resolve()
            check(target.is_relative_to(FULL.resolve()), "Unsafe archive member path")
            with bundle.open(entry) as stream:
                archived_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            check(target.is_file() and digest(target) == archived_hash,
                  f"Extracted evidence differs from immutable ZIP: {entry.filename}")
    REPORT["archive"] = {"sha256": ARCHIVE_SHA256, "bytes": archive.stat().st_size,
                         "members": len(names), "every_extracted_payload_rehashed": True}
    # Load only the exact source snapshots used by the CI run. No latest-gate
    # substitution and no module main entry point executes here.
    for name in ("prepare_runtime", "run_spatial", "assess_experiment"):
        source = git_blob(FULL_COMMIT, f"scripts/windows-shutdown/{name}.py")
        module = types.ModuleType(name)
        module.__file__ = str(REPO / f"scripts/windows-shutdown/{name}.py")
        sys.modules[name] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    preparation = sys.modules["prepare_runtime"]
    for name, (_, _, _, expected_hash) in preparation.SOURCES.items():
        check(REPORT["input_hashes"]["full"][name] == expected_hash,
              f"Immutable source reference differs: {name}")
    original_gcc = (FULL / "runtime/gcc10-atexit_thread.cc").read_text()
    check((FULL / "runtime/guarded.cc").read_bytes() == preparation.guard_source(original_gcc).encode(),
          "Guarded source differs from the exact historical candidate generation")
    original_r = (FULL / "runtime/windows-native-geometry.R").read_text()
    expected_r = {smoke: sys.modules["run_spatial"].adapt_reproducer(original_r, smoke=smoke).encode()
                  for smoke in (False, True)}
    provenance = read_json(FULL / "build-provenance.json")
    REPORT["build_provenance"] = provenance
    check(provenance["source_commit"] == FULL_COMMIT and provenance["duckdb_version"] == "v2.0.0"
          and provenance["duckdb_commit"] == "d54cbbe3df17e8e5ce68312f483b4155836112aa"
          and provenance["ci_tools_commit"] == "969bc76276acec1c0a3922ff500d2ec3a1a3b037"
          and provenance["compiler_version"] == "10.4.0", "Unexpected full-run build identity")
    REPORT["upstream_equals_blob"] = subprocess.check_output(
        ["git", "ls-tree", UPSTREAM_COMMIT, "test/sql/geos/equals.test"], cwd=REPO, text=True).strip()
    check(git_blob(UPSTREAM_COMMIT, "test/sql/geos/equals.test") == b"", "Upstream equals.test is not empty")
    expected_sql_sources = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", UPSTREAM_COMMIT,
         "test/sql/gdal", "test/sql/geos", "test/sql/proj.test"], cwd=REPO, text=True).splitlines()
    expected_sql_sources = [name for name in expected_sql_sources if name.endswith(".test")]
    check(len(expected_sql_sources) == 33, "Expected exactly 33 unchanged upstream SQL source files")
    for arm in ("upstream", "rebuilt-baseline", "guarded"):
        folder = FULL / arm
        arm_result = read_json(folder / "arm-result.json")
        extension = folder / "spatial.duckdb_extension"
        actual_sha, actual_md5 = digest(extension), digest(extension, "md5")
        check(actual_sha == arm_result["extension_sha256"] and arm_result["arm"] == arm
              and arm_result["build_completed"] is True, f"{arm}: actual extension SHA256/build identity")
        footer = read_footer(extension)
        check(footer[:5] == ["4", "windows_amd64_mingw", "v2.0.0", FULL_COMMIT[:10], "CPP"], f"{arm}: PE metadata")
        r_summary = read_json(folder / "r/summary.json")
        check(r_summary["native_windows"] is True
              and r_summary["requested"] == {"default_attempts": 30, "control_attempts": 5}
              and r_summary["supervisor_sha256"] == REPORT["input_hashes"]["full"]["windows-test-supervisor.py"]
              and r_summary["original_reproducer_sha256"] == REPORT["input_hashes"]["full"]["windows-native-geometry.R"],
              f"{arm}: R platform, workload or original input identity")
        raw_folders = sorted((folder / "r").glob("raw-*-*"))
        check(len(raw_folders) == 35, f"{arm}: raw process count")
        rows = [check_r_case(arm, path, actual_sha, actual_md5) for path in raw_folders]
        for mode, count in (("default", 30), ("control", 5)):
            check([row["case"] for row in rows if row["thread_mode"] == mode]
                  == [f"raw-{mode}-{number:03d}" for number in range(1, count + 1)], f"{arm}: complete {mode} sequence")
        observed = {row["case"]: read_json(folder / "r" / row["case"] / "result.json") for row in rows}
        check(len(r_summary["results"]) == 35 and all(
            row == observed[PureWindowsPath(row["directory"]).name] for row in r_summary["results"]),
            f"{arm}: raw records versus R summary")
        smoke = check_r_case(arm, folder / "r/smoke-default-001", actual_sha, actual_md5)
        check(r_summary["smoke"] == read_json(folder / "r/smoke-default-001/result.json"), f"{arm}: smoke record")
        for case in raw_folders + [folder / "r/smoke-default-001"]:
            is_smoke = case.name.startswith("smoke-")
            check((case / "reproducer.R").read_bytes() == expected_r[is_smoke],
                  f"{arm}/{case.name}: executed R program differs from exact source adaptation")
        check(all(row["reason"] == "success" for row in rows if row["thread_mode"] == "control"),
              f"{arm}: single-thread control did not exit normally")
        r_all_passed = all(row["reason"] == "success" for row in rows + [smoke])
        check(r_summary["all_passed"] is r_all_passed
              and arm_result["r_exit_code"] == (0 if r_all_passed else 1),
              f"{arm}: R driver/aggregate outcome differs from all individual results")
        if arm == "guarded":
            check(all(row["reason"] == "success" for row in rows + [smoke]),
                  "Guarded arm has a failed software exit")
        sql_dir = folder / "sql"
        sql_summary = read_json(sql_dir / "summary.json")
        events = [json.loads(line.partition("[TEST_EVENT] ")[2]) for line in
                  (sql_dir / "unittest.log").read_text().splitlines() if "[TEST_EVENT] " in line]
        ends = [event for event in events if event.get("event") == "end"]
        starts = [event for event in events if event.get("event") == "begin"]
        expected_paths = (sql_dir / "sql-inputs.txt").read_text().splitlines()
        check(ends == sql_summary["tests"], f"{arm}: SQL raw events versus summary")
        check(len(events) == 68 and all(row["event"] in ("begin", "end") for row in events),
              f"{arm}: incomplete or unexpected SQL event stream")
        terminal = re.findall(r"All tests passed \((\d+) assertions in (\d+) test cases\)",
                              (sql_dir / "unittest.log").read_text())
        check(terminal == [("201", "34")], f"{arm}: actual test-runner terminal summary")
        check(len(starts) == len(ends) == len(expected_paths) == 34
              and {norm(row["name"]) for row in ends} == {norm(path) for path in expected_paths}
              and {norm(row["name"]) for row in starts} == {norm(path) for path in expected_paths},
              f"{arm}: exact SQL event coverage")
        check(all(row["status"] == "ok" and row["fails"] == row["skip-mode"] == 0
                  and not row.get("partial-skip-reasons")
                  and all(type(row[key]) is int for key in ("passes", "fails", "skip-mode")) for row in ends),
              f"{arm}: SQL failures or skips")
        zeros = [row["name"] for row in ends if row["passes"] == 0]
        check(len(zeros) == 1 and norm(zeros[0]).endswith("/test/sql/geos/equals.test"), f"{arm}: SQL empty template")
        check(sum(row["passes"] for row in ends) == 130 and sql_summary["exit_code"] == 0
              and not sql_summary["timeout_processes"], f"{arm}: actual SQL assertion/exit result")
        check(sql_summary["observed_exact_binary"] is True and sql_summary["binary_unchanged"] is True
              and sql_summary["extension_sha256"] == actual_sha
              and not sql_summary["module_observation_errors"] and not sql_summary["event_errors"],
              f"{arm}: recorded SQL binary observation")
        check(sql_summary["all_passed"] is True and sql_summary["reason"] == "passed"
              and sql_summary["all_tests_reported"] is True and sql_summary["all_tests_ok"] is True
              and sql_summary["statement_passes"] == 130 and sql_summary["statement_failures"] == 0
              and sql_summary["minimum_statement_passes"] == 130
              and sql_summary["expected_test_files"] == 34 and sql_summary["upstream_test_files"] == 33
              and arm_result["sql_exit_code"] == 0, f"{arm}: corrected SQL gate/driver mismatch or failure")
        check(sql_summary["verified_empty_test_files"] == [{
            "path": "test/sql/geos/equals.test", "bytes": 0,
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "upstream_git_blob": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"}],
            f"{arm}: corrected gate accepts more than the exact upstream empty template")
        check({norm(path) for path in sql_summary["observed_spatial_modules"]}
              == {norm(sql_summary["repository_extension"])}, f"{arm}: observed native module path differs")
        source_paths = [source["path"].replace("\\", "/") for source in sql_summary["source_files"]]
        check(len(source_paths) == 33 and set(source_paths) == set(expected_sql_sources),
              f"{arm}: selected SQL source manifest omitted or substituted original tests")
        expected_inputs = [WINDOWS_ARTIFACT_ROOT + f"/{arm}/sql/candidate-binary-provenance.test"]
        expected_inputs += ["D:/a/duckdb-spatial/duckdb-spatial/" + path for path in source_paths]
        check([norm(path) for path in expected_paths] == [norm(path) for path in expected_inputs],
              f"{arm}: actual SQL input list differs from retained selected sources")
        for source in sql_summary["source_files"]:
            path = source["path"].replace("\\", "/")
            before, during = git_blob(UPSTREAM_COMMIT, path), git_blob(FULL_COMMIT, path)
            mode = source_hash_mode(during, source["sha256"])
            REPORT["source_checkout_modes"][f"{arm}/{path}"] = mode
            check(before == during and mode is not None,
                  f"{arm}: unchanged upstream SQL source {path}")
        REPORT["arms"][arm] = {"sha256": actual_sha, "md5": actual_md5, "footer": footer[:5],
            "link": symbols_and_map(folder, arm, False), "r_cases": rows, "smoke": smoke,
            "counts": {mode: dict(collections.Counter(row["reason"] for row in rows if row["thread_mode"] == mode))
                       for mode in ("default", "control")},
            "sql": {"native_exit_code": sql_summary["exit_code"], "end_events": len(ends),
                    "statement_passes": sum(row["passes"] for row in ends), "catch_assertions": 201,
                    "statement_failures": sum(row["fails"] for row in ends),
                    "skip_modes": sum(row["skip-mode"] for row in ends), "zero_statement_files": zeros,
                    "original_gate_passed": sql_summary["all_passed"], "original_gate_reason": sql_summary["reason"],
                    "runner_sha256_recorded": sql_summary["runner_sha256"],
                    "observed_modules_recorded": sql_summary["observed_spatial_modules"]}}
        cache = (folder / "CMakeCache.txt").read_text()
        option = re.search(r"^SPATIAL_MINGW_TLS_ATEXIT_SOURCE:FILEPATH=(.*)$", cache, re.MULTILINE)
        commands = read_json(folder / "compile_commands.json")
        runtime_commands = [command for command in commands
                            if norm(command["file"]).endswith(("/runtime/baseline.cc", "/runtime/guarded.cc"))]
        if arm == "upstream":
            check(option is not None and option[1] == "" and not runtime_commands, f"{arm}: original runtime selection")
        else:
            name = "guarded" if arm == "guarded" else "baseline"
            check(option is not None and norm(option[1]).endswith(f"/runtime/{name}.cc")
                  and len(runtime_commands) == 1 and norm(runtime_commands[0]["file"]).endswith(f"/runtime/{name}.cc"),
                  f"{arm}: compiled runtime source selection")
        REPORT["arms"][arm]["runtime_source_cache_option"] = option[1] if option else None
        REPORT["arms"][arm]["runtime_compile_commands"] = runtime_commands
    all_r_cases = [row for arm in REPORT["arms"].values() for row in arm["r_cases"] + [arm["smoke"]]]
    check(len({row["reproducer_sha256"] for arm in REPORT["arms"].values() for row in arm["r_cases"]}) == 1,
          "The raw R workload differed between arms or attempts")
    check(len({arm["smoke"]["reproducer_sha256"] for arm in REPORT["arms"].values()}) == 1,
          "The smoke workload differed between arms")
    REPORT["r_cleanup"] = {
        "total_processes": len(all_r_cases),
        "normal_exits": sum(row["reason"] == "success" for row in all_r_cases),
        "normal_exits_with_installed_copy_removed": sum(row["reason"] == "success" and row["installed_copy_removed"] for row in all_r_cases),
        "forced_shutdown_hangs": sum(row["reason"] == "quit-hang" for row in all_r_cases),
        "forced_hangs_with_copy_removal_error": sum(row["reason"] == "quit-hang" and bool(row["installed_copy_removal_error"]) for row in all_r_cases),
        "forced_hangs_with_prior_teardown_pattern": sum(row["same_prior_forced_teardown_pattern"] for row in all_r_cases),
        "baseline_descendant_teardown_confirmed": False,
    }
    REPORT["native_runs"]["this_run_twelve_case"] = check_native(FULL, ("unload", "foreign-unload", "main-exit", "abandoned-reader"), FULL_COMMIT)
    archives = {entry["name"]: entry["sha256"] for entry in provenance["archives"]}
    for name, run in REPORT["native_runs"].items():
        check(run["compiler_binary"]["sha256"] == provenance["compiler_sha256"], f"{name}: compiler record equality")
        check(run["runtime_archive"]["sha256"] == archives["libstdc++.a"], f"{name}: libstdc++ record equality")
        for library in ("libpthread.a", "libwinpthread.a"):
            check(run["thread_runtime_archives"][library]["sha256"] == archives[library], f"{name}: {library} record equality")
    # Recompute this exact run's own assessment. It may support the old
    # three-arm exit comparison, but that does not supersede the independently
    # observed TLS storage hazard or establish a complete production fix.
    assessed = sys.modules["assess_experiment"].assess(FULL)
    original = read_json(FULL / "assessment.json")
    check(assessed == original, "Exact e2355ef8 twelve-case assessment did not reproduce archived assessment")
    REPORT["original_assessment_recomputed"] = {"matches_archived_json": assessed == original,
        "verdict": assessed["verdict"], "supported_windows_fix_comparison": assessed["supported_windows_fix_comparison"],
        "ready_for_upstream_submission": assessed["ready_for_upstream_submission"]}
    REPORT["limits"] = [
        "The original assessment is replayed from this run's exact committed source, without retroactively changing either prior run.",
        "This run has its own twelve-case native fixture; no earlier native artifact is substituted.",
        "Compiler and runtime archive identity is compared between recorded hashes; those archive files were not included for independent rehashing.",
        "Installed R DLL copies were intentionally removed; CSV/MD5 observations and parent SHA256 records are checked against the retained source DLL.",
        "SQL unittest.exe is represented by recorded identical hashes; it is not included in this artifact for independent rehashing.",
        "Windows repository source hashes reflect ordinary CRLF checkout conversion; every recorded hash matches the exact upstream git blob or its deterministic CRLF form.",
        "These R hangs have complete workload/cleanup/quit checkpoints, but new native R stacks were not captured.",
        "Forced baseline teardown outcomes are retained individually; without a post-kill process census complete descendant teardown is not established.",
        "A finite run without observed guarded shutdown hangs cannot establish a zero underlying failure probability.",
        "The guard-only candidate does not address the separately established compiler TLS storage lifetime hazard; this three-arm experiment cannot establish a complete fix.",
    ]
    # Compare with the already reviewed second run by its immutable audit hash.
    # This is a reference to the earlier review, not a fresh Windows execution.
    prior_path = BASE / "evidence/windows-repeat-independent-audit-42377280484.json"
    check(digest(prior_path) == PRIOR_REPORT_SHA256, "Prior independent report changed")
    prior = read_json(prior_path)
    check(prior["audit_passed"] is True and prior["errors"] == [], "Prior report was not a clean audit")
    check(prior["input_hashes"]["full"] == REPORT["input_hashes"]["full"], "Cross-run immutable inputs differ")
    REPORT["cross_run_identity_differences"] = {}
    for key in ("duckdb_commit", "duckdb_version", "ci_tools_commit", "vcpkg_commit",
                "compiler_sha256", "compiler_version", "platform", "runner_image_version", "archives"):
        if prior["build_provenance"][key] != provenance[key]:
            REPORT["cross_run_identity_differences"][key] = {
                "previous": prior["build_provenance"][key], "current": provenance[key]}
            if key != "runner_image_version":
                check(False, f"Pinned cross-run build identity differs: {key}")
    check(len({arm["sql"]["runner_sha256_recorded"] for arm in REPORT["arms"].values()}) == 1,
          "SQL runner recorded hash changed between arms")
    cumulative = {}
    for arm in REPORT["arms"]:
        previous = prior["arms"][arm]
        current = REPORT["arms"][arm]
        check({row["reproducer_sha256"] for row in previous["r_cases"]} ==
              {row["reproducer_sha256"] for row in current["r_cases"]}, f"{arm}: cross-run raw R adaptation changed")
        check(previous["smoke"]["reproducer_sha256"] == current["smoke"]["reproducer_sha256"],
              f"{arm}: cross-run smoke adaptation changed")
        previous_counts = prior["prior_run_reference"]["combined_counts_from_two_reviewed_runs"][arm]
        cumulative[arm] = {mode: dict(collections.Counter(previous_counts[mode])
                                     + collections.Counter(row["reason"] for row in current["r_cases"]
                                                           if row["thread_mode"] == mode))
                           for mode in ("default", "control")}
        cumulative[arm]["smoke"] = dict(collections.Counter(previous_counts["smoke"])
                                        + collections.Counter([current["smoke"]["reason"]]))
    REPORT["prior_run_reference"] = {"run": prior["full_run"], "report_sha256": PRIOR_REPORT_SHA256,
                                     "combined_counts_from_three_reviewed_runs": cumulative}
    REPORT["complete_runtime_fix_established"] = False
    REPORT["guarded_R_and_SQL_software_passed"] = (
        all(row["reason"] == "success" for row in REPORT["arms"]["guarded"]["r_cases"]
            + [REPORT["arms"]["guarded"]["smoke"]])
        and REPORT["arms"]["guarded"]["sql"]["original_gate_passed"] is True)
    REPORT["all_software_tests_passed"] = False
    REPORT["audit_passed"] = not REPORT["errors"]
    output = BASE / "evidence/windows-corrected-independent-audit-42383927641.json"
    output.write_text(json.dumps(REPORT, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"audit_passed": REPORT["audit_passed"], "errors": REPORT["errors"],
                      "counts": {name: arm["counts"] for name, arm in REPORT["arms"].items()},
                      "original_assessment": REPORT["original_assessment_recomputed"],
                      "report": str(output)}, indent=2))
    return 0 if REPORT["audit_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
