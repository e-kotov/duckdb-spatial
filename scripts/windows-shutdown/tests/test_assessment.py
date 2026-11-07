"""Adverse-outcome tests for gates that must never manufacture a green result."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assess_experiment import assess, assess_native, assess_r, assess_sql
from prepare_runtime import SOURCES, guard_source
from run_spatial import adapt_reproducer

SHA = "1" * 64


def row(mode="default", attempt=1, hang=False, kind="raw", sha=SHA):
    return {
        "thread_mode": mode, "attempt": attempt, "case_kind": kind,
        "reason": "quit-hang" if hang else "success", "tests_returned": True,
        "r_error": False, "requested_exit_status": 0, "exit_code": 1 if hang else 0,
        "provenance_passed": True, "software_passed": not hang,
        "loaded_extensions": [{"sha256": sha, "threads": "1" if mode == "control" else "4",
                               "platform": "windows_amd64_mingw", "engine_version": "v2.0.0"}
                              for _ in range(3)],
    }


def r_summary(sha=SHA, baseline=False):
    rows = [row(attempt=i, hang=baseline and i == 1, sha=sha) for i in range(1, 31)]
    rows += [row(mode="control", attempt=i, sha=sha) for i in range(1, 6)]
    return {
        "native_windows": True, "extension_sha256": sha,
        "supervisor_sha256": SOURCES["windows-test-supervisor.py"][3],
        "original_reproducer_sha256": SOURCES["windows-native-geometry.R"][3],
        "requested": {"default_attempts": 30, "control_attempts": 5},
        "results": rows, "smoke": row(kind="smoke", sha=sha), "all_passed": not baseline,
    }


def sql_summary(sha=SHA):
    return {
        "extension_sha256": sha, "binary_unchanged": True,
        "observed_exact_binary": True, "module_observation_errors": [],
        "all_tests_reported": True, "all_tests_ok": True, "event_errors": [],
        "reason": "passed", "upstream_test_files": 33, "statement_passes": 130,
        "all_passed": True, "exit_code": 0,
    }


def native_summary():
    rows = []
    for variant in ("archive", "baseline", "guarded"):
        for mode in ("unload", "foreign-unload", "main-exit", "abandoned-reader"):
            hang = variant != "guarded" and mode == "abandoned-reader"
            rows.append({
                "variant": variant, "mode": mode, "experiment_expectation_met": True,
                "classification": "expected-diagnostic-hang" if hang else "passed-clean-exit-and-contract",
                "software_exit_passed": not hang, "terminated_by_supervisor": hang,
            })
    return {"results": rows, "link_proofs": {name: {"passed": True} for name in
            ("archive", "baseline", "guarded")}, "candidate_validated_for_runtime_fixture": True,
            "compiler_binary": {"sha256": "compiler"}, "runtime_archive": {"sha256": "stdcxx"},
            "thread_runtime_archives": {name: {"sha256": name} for name in ("libpthread.a", "libwinpthread.a")}}


class AssessmentTests(unittest.TestCase):
    def test_complete_candidate_pass(self):
        self.assertTrue(assess_r(r_summary(), SHA, True)["valid"])

    def test_hang_is_baseline_evidence_and_candidate_failure(self):
        data = r_summary(baseline=True)
        self.assertTrue(assess_r(data, SHA, False)["valid"])
        self.assertFalse(assess_r(data, SHA, True)["valid"])

    def test_hang_after_error_is_never_evidence(self):
        data = r_summary(baseline=True)
        data["results"][0]["r_error"] = True
        self.assertFalse(assess_r(data, SHA, False)["valid"])

    def test_missing_or_duplicate_attempt_rejected(self):
        for change in ("missing", "duplicate"):
            with self.subTest(change=change):
                data = r_summary()
                if change == "missing":
                    data["results"].pop()
                else:
                    data["results"][1]["attempt"] = 1
                self.assertFalse(assess_r(data, SHA, True)["valid"])

    def test_wrong_binary_or_missing_load_record_rejected(self):
        for change in ("hash", "missing"):
            with self.subTest(change=change):
                data = r_summary()
                records = data["results"][0]["loaded_extensions"]
                if change == "hash":
                    records[0]["sha256"] = "wrong"
                else:
                    records.pop()
                self.assertFalse(assess_r(data, SHA, True)["valid"])

    def test_single_thread_default_cannot_count(self):
        data = r_summary()
        data["results"][0]["loaded_extensions"][0]["threads"] = "1"
        self.assertFalse(assess_r(data, SHA, True)["valid"])

    def test_control_hang_rejected_even_for_baseline(self):
        data = r_summary(baseline=True)
        data["results"][-1] = row(mode="control", attempt=5, hang=True)
        self.assertFalse(assess_r(data, SHA, False)["valid"])

    def test_sql_hang_cannot_bypass_loaded_module_provenance(self):
        for key, value in (("observed_exact_binary", False), ("module_observation_errors", ["denied"])):
            with self.subTest(key=key):
                data = sql_summary()
                data.update(reason="timeout_after_test_completion", all_passed=False, exit_code=1)
                data[key] = value
                self.assertFalse(assess_sql(data, SHA, False)["valid"])

    def test_sql_failures_and_skips_cannot_pass(self):
        data = sql_summary()
        data["all_tests_ok"] = False
        self.assertFalse(assess_sql(data, SHA, False)["valid"])

    def test_sql_empty_source_exception_cannot_reduce_assertion_coverage(self):
        for passes in (0, 1, 129):
            with self.subTest(passes=passes):
                data = sql_summary()
                data["statement_passes"] = passes
                self.assertFalse(assess_sql(data, SHA, True)["valid"])

    def test_native_hang_kept_separate_from_software_success(self):
        data = native_summary()
        self.assertTrue(assess_native(data)["valid"])
        next(row for row in data["results"] if row["classification"] == "expected-diagnostic-hang")["software_exit_passed"] = True
        self.assertFalse(assess_native(data)["valid"])

    def test_missing_native_cleanup_case_rejected(self):
        data = native_summary()
        data["results"].pop()
        self.assertFalse(assess_native(data)["valid"])

    def test_foreign_thread_cleanup_controls_required(self):
        data = native_summary()
        data["results"] = [row for row in data["results"] if row["mode"] != "foreign-unload"]
        self.assertFalse(assess_native(data)["valid"])

    def test_foreign_thread_cleanup_hang_is_never_expected(self):
        for variant in ("archive", "baseline", "guarded"):
            with self.subTest(variant=variant):
                data = native_summary()
                row = next(row for row in data["results"]
                           if row["mode"] == "foreign-unload" and row["variant"] == variant)
                row.update(classification="expected-diagnostic-hang",
                           software_exit_passed=False, terminated_by_supervisor=True)
                self.assertFalse(assess_native(data)["valid"])

    def test_no_windows_results_is_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            result = assess(Path(directory))
        self.assertEqual(result["verdict"], "incomplete")
        self.assertFalse(result["supported_windows_fix_comparison"])

    def test_source_adaptation_rejects_changed_input(self):
        with self.assertRaises(ValueError):
            adapt_reproducer("changed R program")
        with self.assertRaises(ValueError):
            guard_source("changed C++ runtime")

    def test_full_gate_requires_both_baseline_reproductions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def write(relative, data):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(data))
            write("runtime/inputs.json", {"generated": {
                "baseline.cc": {"sha256": "baseline"}, "guarded.cc": {"sha256": "guarded"}}})
            write("native/summary.json", native_summary())
            write("build-provenance.json", {
                "compiler_version": "10.4.0", "platform": "windows_amd64_mingw",
                "duckdb_commit": "d54cbbe3df17e8e5ce68312f483b4155836112aa",
                "ci_tools_commit": "969bc76276acec1c0a3922ff500d2ec3a1a3b037",
                "compiler_sha256": "compiler", "archives": [
                    {"name": "libstdc++.a", "sha256": "stdcxx"},
                    {"name": "libpthread.a", "sha256": "libpthread.a"},
                    {"name": "libwinpthread.a", "sha256": "libwinpthread.a"}]})
            for name in ("upstream", "rebuilt-baseline", "guarded"):
                binary = name.encode()
                sha = hashlib.sha256(binary).hexdigest()
                write(f"{name}/arm-result.json", {
                    "arm": name, "build_completed": True, "extension_sha256": sha,
                    "runtime_source_sha256": {"upstream": None, "rebuilt-baseline": "baseline", "guarded": "guarded"}[name],
                    "r_exit_code": 0 if name == "guarded" else 1, "sql_exit_code": 0})
                (root / name / "spatial.duckdb_extension").write_bytes(binary)
                write(f"{name}/r/summary.json", r_summary(sha, baseline=name != "guarded"))
                write(f"{name}/sql/summary.json", sql_summary(sha))
            supported = assess(root)
            self.assertEqual(supported["verdict"], "supported-windows-comparison")
            self.assertTrue(supported["supported_windows_fix_comparison"])
            self.assertFalse(supported["ready_for_upstream_submission"])
            sha = hashlib.sha256(b"rebuilt-baseline").hexdigest()
            write("rebuilt-baseline/r/summary.json", r_summary(sha, baseline=False))
            inconclusive = assess(root)
            self.assertEqual(inconclusive["verdict"], "inconclusive-no-matched-baseline-reproduction")
            self.assertFalse(inconclusive["supported_windows_fix_comparison"])


if __name__ == "__main__":
    unittest.main()
