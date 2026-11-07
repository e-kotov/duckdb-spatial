#!/usr/bin/env python3
"""Audit the six-arm final artifact using its own sources and early checkpoint.

Requires the final artifact and API metadata, the preserved early native ZIP
and audit, and the earlier reusable raw-R audit. It does not execute Windows
binaries or pretend unavailable installed tools can be rehashed offline.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import zipfile

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
REPO = BASE / "duckdb-spatial"
ROOT = BASE / "evidence/windows-pthread-spatial-42398571027"
EARLY = BASE / "evidence/windows-pthread-native-42398571027"
COMMIT = "bf3256b770c8a5d7924a2eb97a0ce085ec258071"
UPSTREAM_COMMIT = "d332807f4ffdcc4da0092fe760ef48266f548941"
WINDOWS_ROOT = "D:/a/duckdb-spatial/duckdb-spatial/build/windows-pthread-spatial"
WINDOWS_REPO = "D:/a/duckdb-spatial/duckdb-spatial"
EARLY_SHA = "e497a0cd7f061401e217408642cd94206085f166b97fdf988f9d839a58592851"
EARLY_REPORT_SHA = "ad80b479bae46aa7b59738df758be73d5d5ce4f2c983b90ded8d4b400ccbfecc"
COMMON_SHA = "77e65c800854a1defda4c1603d0bd3a7cb322f036159baa240f03c9e81e548b4"
API_METADATA = ROOT.with_name(ROOT.name + "-api-metadata.json")
NATIVE_ARCHIVE = EARLY.with_suffix(".zip")
NATIVE_REPORT = BASE / "evidence/windows-pthread-native-independent-audit-42398571027.json"
COMMON_AUDIT = BASE / "evidence/audit_windows_corrected_42383927641.py"
PYTHON_DEPS = BASE / "windows-shutdown-test-deps"
OUTPUT = BASE / "evidence/windows-pthread-spatial-independent-audit-42398571027.json"
REPORT = {"run_id": 42398571027, "commit": COMMIT, "errors": [], "arms": {},
          "original_evidence_modified": False, "windows_executables_run_by_auditor": False,
          "ready_for_upstream_submission": False}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def norm(path):
    return re.sub("/+", "/", str(path).replace("\\", "/")).casefold()


def git_blob(commit, path):
    return subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=REPO)


def verify_integrity_and_checkpoint():
    metadata = read_json(API_METADATA)
    metadata = metadata.get("artifact", metadata)
    require(metadata["workflow_run"]["id"] == 42398571027
            and metadata["workflow_run"]["head_sha"] == COMMIT, "Wrong final artifact run/head")
    archive = ROOT.with_suffix(".zip")
    require(archive.stat().st_size == metadata["size_in_bytes"]
            and digest(archive) == metadata["digest"].removeprefix("sha256:"), "Final artifact digest differs from API metadata")
    with zipfile.ZipFile(archive) as bundle:
        require(bundle.testzip() is None, "Final ZIP CRC failed")
        names = [entry.filename for entry in bundle.infolist() if not entry.is_dir()]
        require(len(names) == len(set(names)), "Duplicate final ZIP members")
        for name in names:
            path = ROOT / name
            require(path.resolve().is_relative_to(ROOT.resolve()), "Unsafe final archive path")
            with bundle.open(name) as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == digest(path), f"Changed final payload: {name}")
        require({path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*") if path.is_file()} == set(names),
                "Final extraction omitted or added files")
    REPORT["artifact"] = {"id": metadata["id"], "bytes": archive.stat().st_size, "sha256": digest(archive),
                          "files": len(names), "all_extracted_payloads_rehashed": True}
    require(digest(NATIVE_REPORT) == EARLY_REPORT_SHA and digest(NATIVE_ARCHIVE) == EARLY_SHA,
            "Verified native checkpoint/reference changed")
    early_report = read_json(NATIVE_REPORT)
    require(early_report["audit_passed"] is True and early_report["errors"] == [], "Native checkpoint audit did not pass")
    with zipfile.ZipFile(NATIVE_ARCHIVE) as bundle:
        for entry in bundle.infolist():
            if entry.is_dir():
                continue
            with bundle.open(entry) as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == digest(ROOT / entry.filename),
                        f"Early native/source/preparation checkpoint changed: {entry.filename}")
    REPORT["native_checkpoint_reference"] = {
        "artifact_sha256": EARLY_SHA, "report_sha256": EARLY_REPORT_SHA,
        "all_807_checkpoint_files_identical_in_final": True,
        "totals": early_report["totals"], "rtools_version_marker": early_report["rtools_version_marker"],
        "unavailable_installed_bytes": early_report["preparation"]["unavailable_installed_bytes"],
        "predecessor_setup_failure": early_report["predecessor_setup_failure"]}
    return early_report


def verify_live_preparation_reports(early_report, gate):
    manifest = read_json(ROOT / "pthread/manifest.json")
    expected = {"passed": True, "prep_manifest_sha256": digest(ROOT / "pthread/manifest.json"),
                "preparation_directory": str(PureWindowsPath(WINDOWS_ROOT) / "pthread"),
                "variants": {}, "installed_inputs_checked": len(manifest["installed_inputs"]),
                "installed_inputs_unchanged": True,
                "compiler": {"path": manifest["tools"]["g++"]["path"],
                             "sha256": manifest["tools"]["g++"]["before_sha256"]}}
    for arm, variant in manifest["variants"].items():
        archives = {}
        for name, record in variant["archives"].items():
            actual = ROOT / "pthread" / arm / "lib" / name
            require(digest(actual) == record["sha256"], "Private archive changed after native preflight")
            archives[name] = {"path": str(PureWindowsPath(variant["library_dir"]) / name),
                              **{key: record[key] for key in ("sha256", "thread_provider", "thread_object_sha256")}}
        expected["variants"][arm] = {"library_dir": variant["library_dir"], "archives": archives}
    records = {}
    for name in ("pthread-preparation-before.json", "pthread-preparation-after.json"):
        record = read_json(ROOT / name)
        require(all(record.get(key) == value for key, value in expected.items()),
                f"Live preparation audit differs from retained inputs: {name}")
        records[name] = record
    provenance = read_json(ROOT / "build-provenance.json")
    require(provenance["source_commit"] == COMMIT and provenance["compiler_sha256"] == expected["compiler"]["sha256"]
            and norm(provenance["compiler_path"]) == norm(expected["compiler"]["path"])
            and provenance["compiler_version"] == "10.4.0" and provenance["platform"] == "windows_amd64_mingw"
            and provenance["duckdb_commit"] == gate.CORE_COMMIT and provenance["duckdb_version"] == "v2.0.0"
            and provenance["ci_tools_commit"] == gate.CI_TOOLS_COMMIT and provenance["vcpkg_commit"] == gate.VCPKG_COMMIT,
            "Spatial build identity differs from the prepared native experiment")
    archives = {row["name"]: row for row in provenance["archives"]}
    require(len(provenance["archives"]) == len(archives) == 4
            and set(archives) == {"libstdc++.a", "libgcc.a", "libpthread.a", "libwinpthread.a"}
            and all(row["sha256"] == gate.PINNED_INSTALLED[name] for name, row in archives.items()),
            "Recorded installed Spatial runtime identities differ")
    REPORT["preparation_live_reports"] = records
    REPORT["build_provenance"] = provenance
    REPORT["offline_current_installed_tool_rehash_claimed"] = False
    return expected, provenance


def verify_sql(arm, folder, summary, expected_sha, common, gate):
    require(norm(summary["runner"]) == norm(WINDOWS_REPO + "/build/release/test/unittest.exe")
            and norm(summary["extension"]) == norm(WINDOWS_ROOT + f"/{arm}/spatial.duckdb_extension")
            and norm(summary["repository_extension"]) == norm(WINDOWS_REPO + "/build/release/repository/v2.0.0/windows_amd64_mingw/spatial.duckdb_extension"),
            "Recorded SQL runner, archived DLL, or repository DLL path differs from this build")
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", COMMIT,
                                    "test/sql/gdal", "test/sql/geos", "test/sql/proj.test"], cwd=REPO, text=True).splitlines()
    selected = [name for name in paths if name.endswith(".test")]
    require(len(selected) == 33, "Expected unchanged 33-file SQL suite")
    source_paths = [row["path"].replace("\\", "/") for row in summary["source_files"]]
    require(source_paths == selected, "SQL source file list differs or is incomplete")
    source_modes = {}
    for row in summary["source_files"]:
        name = row["path"].replace("\\", "/")
        source = git_blob(COMMIT, name)
        require(source == git_blob(UPSTREAM_COMMIT, name), "Original upstream SQL source changed")
        mode = common.source_hash_mode(source, row["sha256"])
        require(mode is not None, "Recorded SQL source hash differs from exact checkout")
        source_modes[name] = mode
    inputs = (folder / "sql-inputs.txt").read_text().splitlines()
    expected_inputs = [WINDOWS_ROOT + f"/{arm}/sql/candidate-binary-provenance.test"]
    expected_inputs += [WINDOWS_REPO + "/" + name for name in selected]
    require([norm(path) for path in inputs] == [norm(path) for path in expected_inputs], "Actual SQL inputs omitted or changed tests")
    provenance = ("require spatial\n\nquery I\nSELECT count(*) FROM duckdb_extensions()\n"
                  "WHERE extension_name = 'spatial' AND loaded\n"
                  "AND coalesce(install_mode, '') <> 'STATICALLY_LINKED';\n----\n1\n\nsleep 1 second\n")
    require((folder / "candidate-binary-provenance.test").read_bytes() == provenance.encode(), "Executed DLL provenance assertion changed")
    commands = read_json(folder / "command.json")
    expected_command = [summary["runner"], "-f", WINDOWS_ROOT + f"/{arm}/sql/sql-inputs.txt", "--require", "spatial",
                        "--emit-test-events", "--test-dir", WINDOWS_REPO]
    require([norm(value) for value in commands] == [norm(value) for value in expected_command], "SQL runner command changed")
    events, errors = gate.read_events(folder / "unittest.log")
    require(not errors and len(events) == 68, "Incomplete or malformed raw SQL events")
    begins, ends, active = [], [], set()
    for event in events:
        name = norm(event["name"])
        if event["event"] == "begin":
            require(name not in active and name not in begins, "Duplicate SQL begin event")
            begins.append(name)
            active.add(name)
        elif event["event"] == "end":
            require(name in active, "SQL terminal event without prior begin")
            active.remove(name)
            ends.append(event)
        else:
            require(False, "Unknown SQL event")
    require(not active and len(begins) == len(ends) == 34
            and set(begins) == {norm(path) for path in inputs}
            and {norm(row["name"]) for row in ends} == set(begins) and ends == summary["tests"], "SQL event/summary coverage differs")
    require(all(row["status"] == "ok" and row["fails"] == row["skip-mode"] == 0
                and not row.get("partial-skip-reasons")
                and all(type(row[key]) is int for key in ("passes", "fails", "skip-mode"))
                and row["passes"] >= 0 for row in ends),
            "Actual SQL assertions failed or skipped")
    zero_files = [norm(row["name"]) for row in ends if row["passes"] == 0]
    require(zero_files == [norm(WINDOWS_REPO + "/test/sql/geos/equals.test")]
            and git_blob(COMMIT, "test/sql/geos/equals.test") == b"", "Unexpected empty SQL test")
    require(sum(row["passes"] for row in ends) == summary["statement_passes"] == 130
            and summary["statement_failures"] == 0 and summary["minimum_statement_passes"] == 130
            and summary["expected_test_files"] == 34 and summary["upstream_test_files"] == 33,
            "SQL assertion coverage changed")
    known_empty = [{"path": "test/sql/geos/equals.test", "bytes": 0,
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "upstream_git_blob": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"}]
    require(summary["verified_empty_test_files"] == known_empty, "Exact empty-file exception changed")
    terminal = re.findall(r"All tests passed \((\d+) assertions in (\d+) test cases\)", (folder / "unittest.log").read_text())
    require(terminal == [("201", "34")], "Actual Catch terminal summary differs")
    require(summary["extension_sha256"] == expected_sha and summary["binary_unchanged"] is True
            and summary["observed_exact_binary"] is True and summary["module_observation_errors"] == []
            and {norm(path) for path in summary["observed_spatial_modules"]} == {norm(summary["repository_extension"])},
            "Actual SQL DLL observation differs")
    assessed = gate.assess_sql_records(summary, expected_sha)
    require(assessed["valid"], "Actual SQL exit/assertion/provenance outcome is invalid")
    return {"assessment": assessed, "native_exit_code": summary["exit_code"], "timeout_processes": summary["timeout_processes"],
            "begin_end_events": len(events), "statement_passes": 130, "catch_assertions": 201,
            "statement_failures": 0, "skips": 0, "runner_sha256_recorded": summary["runner_sha256"],
            "source_checkout_modes": source_modes, "observed_modules_recorded": summary["observed_spatial_modules"]}


def main(argv=None):
    global ROOT, REPO, API_METADATA, NATIVE_ARCHIVE, NATIVE_REPORT, COMMON_AUDIT, PYTHON_DEPS, OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", "--artifact-root", dest="root", type=Path, default=ROOT,
                        help="Unmodified extracted final artifact; its original ZIP must be beside it with a .zip suffix")
    parser.add_argument("--api-metadata", type=Path,
                        help="Saved GitHub artifact metadata object (or {'artifact': object}); defaults beside --root")
    parser.add_argument("--native-archive", type=Path, default=NATIVE_ARCHIVE,
                        help="Exact separately retained early native ZIP")
    parser.add_argument("--native-report", type=Path, default=NATIVE_REPORT,
                        help="Hash-pinned independently verified early native audit JSON")
    parser.add_argument("--raw-r-audit", type=Path, default=COMMON_AUDIT,
                        help="Hash-pinned reusable corrected-run raw-R audit script")
    parser.add_argument("--repo", "--source-root", dest="repo", type=Path, default=REPO,
                        help="Git checkout containing this exact run commit and pinned upstream commit")
    parser.add_argument("--python-deps", type=Path, default=PYTHON_DEPS,
                        help="Optional additional Python package directory; psutil may instead be installed normally")
    parser.add_argument("--output", type=Path, default=OUTPUT,
                        help="New audit JSON outside the original extracted evidence")
    args = parser.parse_args(argv)
    ROOT, REPO = args.root.resolve(), args.repo.resolve()
    API_METADATA = (args.api_metadata or ROOT.with_name(ROOT.name + "-api-metadata.json")).resolve()
    NATIVE_ARCHIVE, NATIVE_REPORT = args.native_archive.resolve(), args.native_report.resolve()
    COMMON_AUDIT, PYTHON_DEPS, OUTPUT = args.raw_r_audit.resolve(), args.python_deps.resolve(), args.output.resolve()
    if OUTPUT.is_relative_to(ROOT) or OUTPUT in {ROOT.with_suffix(".zip"), API_METADATA, NATIVE_ARCHIVE, NATIVE_REPORT, COMMON_AUDIT}:
        parser.error("The report must not overwrite an original artifact, input, or extracted payload")
    REPORT["input_paths"] = {"final_extraction": str(ROOT), "final_zip": str(ROOT.with_suffix(".zip")),
                             "api_metadata": str(API_METADATA), "native_archive": str(NATIVE_ARCHIVE),
                             "native_report": str(NATIVE_REPORT), "raw_r_audit": str(COMMON_AUDIT), "git_repository": str(REPO)}
    try:
        early = verify_integrity_and_checkpoint()
        sys.path[:0] = [str(ROOT / "source/windows-shutdown"), str(PYTHON_DEPS)]
        import assess_pthread_spatial as gate
        common_path = COMMON_AUDIT
        require(digest(common_path) == COMMON_SHA, "Reusable raw-R audit changed")
        spec = importlib.util.spec_from_file_location("retained_r_audit", common_path)
        common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(common)
        common.REPORT, common.REPO = REPORT, REPO
        common.FULL_COMMIT, common.WINDOWS_ARTIFACT_ROOT = COMMIT, WINDOWS_ROOT
        original_norm = gate.norm
        def mapped_norm(path):
            value, prefix = original_norm(path), original_norm(WINDOWS_ROOT)
            return original_norm(ROOT) + value[len(prefix):] if value.startswith(prefix + "/") else value
        gate.norm = mapped_norm
        preparation, provenance = verify_live_preparation_reports(early, gate)
        require({path.parent.name for path in ROOT.glob("*/arm-result.json")} == set(gate.ARMS), "Missing or unexpected Spatial arm")
        decision = {"errors": [], "preparation_valid": True, "preflights": {}, "arms": {}}
        for arm, family in early["families"].items():
            decision["preflights"][arm] = {"valid": True, "cleanup": family["native"]["assessment"],
                                            "ordering": family["ordering"]["assessment"]}
        expected_r = {smoke: gate.adapt_reproducer((ROOT / "runtime/windows-native-geometry.R").read_text(), smoke=smoke).encode()
                      for smoke in (False, True)}
        for arm in gate.ARMS:
            try:
                folder = ROOT / arm
                linked = gate.inspect_spatial_link(folder, arm, preparation, provenance)
                extension = folder / "spatial.duckdb_extension"
                actual_nm = subprocess.run(["nm", "-C", "--defined-only", str(extension)], capture_output=True, text=True)
                mapping = (folder / "spatial-link.map").read_text()
                require(actual_nm.returncode == 0
                        and gate.inspect_link(mapping, actual_nm.stdout, preparation["variants"][linked["pthread_arm"]]["archives"])["passed"]
                        and gate.gcc_link_proof(mapping, actual_nm.stdout, linked["gcc_arm"], native=False)["passed"],
                        "Independent actual Spatial DLL symbols disagree with retained link proof")
                r = read_json(folder / "r/summary.json")
                r_assessed = gate.assess_r_records(r, linked["extension_sha256"])
                require(r_assessed["valid"], "R observations failed original and strict cleanup gates")
                gate.verify_r_files(folder / "r", r, ROOT / "runtime", extension)
                rows, source_hash, source_md5 = [], digest(extension), common.digest(extension, "md5")
                for row in r["results"] + [r["smoke"]]:
                    case = folder / "r" / f"{row['case_kind']}-{row['thread_mode']}-{row['attempt']:03d}"
                    require(read_json(case / "result.json") == row, "Summary differs from actual individual R result")
                    require((case / "reproducer.R").read_bytes() == expected_r[row["case_kind"] == "smoke"],
                            "Executed R program changed between cases")
                    rows.append(common.check_r_case(arm, case, source_hash, source_md5))
                require({path.parent.name for path in (folder / "r").glob("*/result.json")} == {row["case"] for row in rows}
                        and len(rows) == 36, "Missing or additional R processes")
                sql = read_json(folder / "sql/summary.json")
                sql_report = verify_sql(arm, folder / "sql", sql, source_hash, common, gate)
                record = linked["arm_result"]
                require(type(record["r_exit_code"]) is int and record["r_exit_code"] == (0 if r["all_passed"] else 1)
                        and type(record["sql_exit_code"]) is int and record["sql_exit_code"] == (0 if sql["all_passed"] else 1),
                        "Arm driver exits differ from actual software outcomes")
                decision["arms"][arm] = {**linked, "r": r_assessed, "sql": sql_report["assessment"],
                                           "observation_valid": r_assessed["valid"] and sql_report["assessment"]["valid"],
                                           "all_software_passed": r_assessed["all_software_passed"] and sql_report["assessment"]["all_software_passed"],
                                           "storage_contract_passed_in_native_probe": linked["pthread_arm"] == "reverse-order"}
                REPORT["arms"][arm] = {"factor_pair": gate.ARMS[arm], "extension_sha256": source_hash,
                                       "r": r_assessed, "r_cases": rows, "sql": sql_report,
                                       "actual_DLL_and_link_proofs_verified": True}
            except Exception as error:
                REPORT["errors"].append(f"{arm}: {type(error).__name__}: {error}")
                decision["arms"][arm] = {"observation_valid": False, "all_software_passed": False}
        hashes = {row["sql"]["runner_sha256_recorded"] for row in REPORT["arms"].values()}
        require(len(hashes) == 1, "SQL runner identity changed between arms")
        decision["errors"] = list(REPORT["errors"])
        assessed = gate.decide(decision)
        archived = read_json(ROOT / "assessment.json")
        compared = ("preparation_valid", "preflights", "arms", "complete_valid_observation", "baselines_both_reproduced",
                    "supported_ordering_only", "supported_combination", "supported_windows_fix_comparison",
                    "ready_for_upstream_submission", "guard_only_is_a_complete_fix", "verdict", "errors")
        require(all(assessed[key] == archived[key] for key in compared), "Retained raw gates/decision differ from archived live assessment")
        REPORT["historical_assessment"] = {key: archived[key] for key in compared if key not in ("preflights", "arms")}
        REPORT["historical_raw_gates_and_decision_match"] = True
        all_rows = [row for arm in REPORT["arms"].values() for row in arm["r_cases"]]
        REPORT["r_totals"] = {"processes": len(all_rows), "outcomes": dict(Counter(row["reason"] for row in all_rows)),
                              "normal_exits_with_installed_copy_removed": sum(row["reason"] == "success" and row["installed_copy_removed"] for row in all_rows),
                              "forced_hangs_with_removal_error": sum(row["reason"] == "quit-hang" and bool(row["installed_copy_removal_error"]) for row in all_rows),
                              "complete_forced_descendant_teardown_observed": False}
        REPORT["limits"] = [
            "This audit rehashes retained evidence and recomputes raw-case and decision gates; it does not rerun the live check against the unavailable Windows-installed tools.",
            "The raw native checkpoint is reused only after every one of its 807 retained files is byte-identical in the final artifact.",
            "The SQL unittest executable is represented by its recorded common hash and is not independently rehashed unless separately retained.",
            "R installed copies were removed on normal exits; their raw load records and parent hashes are checked against each retained source DLL.",
            "A completed quit workload and supervised timeout do not constitute a new native stack capture. Forced descendant teardown remains unconfirmed where no post-kill census exists.",
            "Guard-only exits do not establish TLS storage safety. The separate key-reuse probe is not part of this run's native or Spatial matrix.",
            "A successful finite comparison is evidence for independent review and does not authorize upstream submission or establish correctness for every supported lifecycle."]
    except Exception as error:
        REPORT["errors"].append(f"{type(error).__name__}: {error}")
    REPORT["audit_passed"] = not REPORT["errors"]
    REPORT["complete_runtime_fix_established"] = False
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(REPORT, indent=2) + "\n")
    print(json.dumps({"audit_passed": REPORT["audit_passed"], "errors": REPORT["errors"],
                      "historical_assessment": REPORT.get("historical_assessment"),
                      "r_totals": REPORT.get("r_totals"), "report": str(OUTPUT)}, indent=2))
    return 0 if REPORT["audit_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
