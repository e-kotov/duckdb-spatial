"""Adverse evidence for the six-arm gate; these are not Windows runtime tests."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assess_pthread_spatial as gate
from prepare_pthread_archives import PROVIDER_SYMBOLS
from test_assessment import r_summary as original_r_summary, sql_summary as original_sql_summary
from test_tls_storage_order import observation


SHA = "1" * 64


def r_summary(*, baseline=False):
    value = original_r_summary(SHA, baseline=baseline)
    for row in value["results"] + [value["smoke"]]:
        row.update(kill_detail="taskkill retained as failed exit" if row["reason"] == "quit-hang" else None,
                   verified_installed_copy_removed=row["reason"] == "success", quit_grace_seconds=30)
    return value


def sql_summary():
    value = original_sql_summary(SHA)
    value["timeout_processes"] = []
    return value


def complete_comparison():
    return {
        "errors": [], "preparation_valid": True,
        "preflights": {arm: {"valid": True} for arm in gate.PTHREAD_ARMS},
        "arms": {arm: {"observation_valid": True,
                       "r": {"default_quit_hangs": int(arm in ("original", "rebuilt-control"))},
                       "all_software_passed": arm not in ("original", "rebuilt-control")}
                 for arm in gate.ARMS},
    }


def raw_process(folder, stem, command, *, hang=False, stdout=""):
    row = {
        "command": command, "pid": 100, "elapsed_seconds": 12.1 if hang else 0.5,
        "exit_request_observed_seconds": 2.0 if hang else None,
        "timeout_stage": "after-exit-request" if hang else None,
        "timed_out": hang, "terminated_by_supervisor": hang,
        "returncode": None if hang else 0, "returncode_after_termination": 1 if hang else None,
        "stdout": stdout, "stderr": "",
    }
    for channel in ("stdout", "stderr"):
        path = folder / f"{stem}.{channel}.log"
        path.write_bytes(row[channel].encode())
        row[f"{channel}_path"] = str(path)
    return row


def cleanup_summary(folder):
    results = []
    for gcc in gate.GCC_ARMS:
        for mode in ("unload", "foreign-unload", "main-exit", "abandoned-reader"):
            hang = gcc != "guarded" and mode == "abandoned-reader"
            if mode in ("unload", "foreign-unload"):
                cycle = ["CPP_TLS_DTOR"] * 8
                if mode == "foreign-unload":
                    cycle += ["FOREIGN_WORKERS_JOINED"]
                markers = (cycle + ["ATEXIT_CALLBACK", "GLOBAL_DTOR"]) * 16
                markers += ["FOREIGN_UNLOAD_OK" if mode == "foreign-unload" else "UNLOAD_OK"]
            elif mode == "main-exit":
                markers = ["PROCESS_EXIT_REQUEST", "CPP_TLS_DTOR", "ATEXIT_CALLBACK", "GLOBAL_DTOR"]
            else:
                markers = ["BLOCKED_DTOR_ENTER", "PROCESS_EXIT_REQUEST"]
                if not hang:
                    markers += ["ATEXIT_CALLBACK", "GLOBAL_DTOR"]
            stem = f"{mode}-{gcc}"
            marker_path = folder / f"{stem}.markers.log"
            marker_path.write_text("\n".join(markers) + "\n")
            command = [str(folder / "runtime-host.exe"), str(folder / f"runtime-{gcc}.dll"),
                       mode, str(marker_path), "16", "8"]
            raw = raw_process(folder, stem, command, hang=hang)
            results.append(gate.assess_cleanup_case(raw, variant=gcc, mode=mode,
                                                   markers=markers, cycles=16, workers=8))
    return {"results": results, "workload": {"cycles": 16, "workers": 8, "timeout": 10.0},
            "link_proofs": {gcc: {"passed": True} for gcc in gate.GCC_ARMS},
            "candidate_validated_for_runtime_fixture": True}


def ordering_summary(folder, pthread_arm):
    order = "callback-first" if pthread_arm == "reverse-order" else "storage-first"
    results = []
    for gcc in gate.GCC_ARMS:
        for mode in ("pthread", "foreign"):
            value = observation(order)
            value["mode"] = mode
            command = [str(folder / "storage-order-host.exe"), str(folder / f"storage-order-{gcc}.dll"), mode]
            raw = raw_process(folder, f"{mode}-{gcc}", command, stdout=json.dumps(value) + "\r\n")
            results.append(gate.assess_order_case(raw, variant=gcc, mode=mode, expected_order=order))
    return {"results": results, "expected_order": order, "diagnostic_expectation_met": True,
            "storage_lifetime_contract_passed": pthread_arm == "reverse-order"}


class DecisionTests(unittest.TestCase):
    def test_complete_comparison_supports_both_candidates_without_upstream_permission(self):
        result = gate.decide(complete_comparison())
        self.assertTrue(result["supported_ordering_only"])
        self.assertTrue(result["supported_combination"])
        self.assertEqual(result["verdict"], "supported-six-arm-comparison")
        self.assertFalse(result["guard_only_is_a_complete_fix"])
        self.assertFalse(result["ready_for_upstream_submission"])

    def test_each_ordering_only_build_is_required(self):
        for arm in ("order-only", "order-only-archive"):
            with self.subTest(arm=arm):
                value = complete_comparison()
                value["arms"][arm]["all_software_passed"] = False
                value["arms"]["combined"]["all_software_passed"] = False
                result = gate.decide(value)
                self.assertTrue(result["complete_valid_observation"])
                self.assertFalse(result["supported_ordering_only"])
                self.assertFalse(result["supported_windows_fix_comparison"])
                self.assertEqual(result["verdict"], "no-supported-candidate")

    def test_combination_can_pass_when_ordering_only_has_negative_evidence(self):
        value = complete_comparison()
        value["arms"]["order-only-archive"]["all_software_passed"] = False
        result = gate.decide(value)
        self.assertFalse(result["supported_ordering_only"])
        self.assertTrue(result["supported_combination"])

    def test_guard_only_clean_exits_never_count_as_complete_fix(self):
        value = complete_comparison()
        for arm in ("order-only", "order-only-archive", "combined"):
            value["arms"][arm]["all_software_passed"] = False
        value["ready_for_upstream_submission"] = True
        result = gate.decide(value)
        self.assertTrue(result["arms"]["guard-only"]["all_software_passed"])
        self.assertFalse(result["supported_windows_fix_comparison"])
        self.assertFalse(result["ready_for_upstream_submission"])

    def test_both_actual_default_baseline_reproductions_are_required(self):
        for arm in ("original", "rebuilt-control"):
            value = complete_comparison()
            value["arms"][arm]["r"]["default_quit_hangs"] = 0
            result = gate.decide(value)
            self.assertFalse(result["supported_windows_fix_comparison"])
            self.assertEqual(result["verdict"], "inconclusive-no-matched-baseline-reproduction")

    def test_any_incomplete_or_untrusted_observation_fails_whole_gate(self):
        variants = []
        for arm in gate.ARMS:
            value = complete_comparison()
            del value["arms"][arm]
            variants.append(value)
            value = complete_comparison()
            value["arms"][arm]["observation_valid"] = False
            variants.append(value)
        for arm in gate.PTHREAD_ARMS:
            value = complete_comparison()
            value["preflights"][arm]["valid"] = False
            variants.append(value)
        value = complete_comparison()
        value["preparation_valid"] = False
        variants.append(value)
        value = complete_comparison()
        value["errors"] = ["installed archive changed"]
        variants.append(value)
        value = complete_comparison()
        value["arms"]["unplanned"] = deepcopy(value["arms"]["combined"])
        variants.append(value)
        for value in variants:
            self.assertEqual(gate.decide(value)["verdict"], "failed-validation")

    def test_missing_all_files_cannot_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = gate.assess(Path(directory))
        self.assertEqual(result["verdict"], "failed-validation")
        self.assertTrue(result["errors"])
        self.assertFalse(result["ready_for_upstream_submission"])


class SpatialOutcomeTests(unittest.TestCase):
    def test_complete_normal_exit_and_candidate_hang_are_distinct(self):
        passed = gate.assess_r_records(r_summary(), SHA)
        negative = gate.assess_r_records(r_summary(baseline=True), SHA)
        self.assertTrue(passed["valid"] and passed["all_software_passed"])
        self.assertTrue(negative["valid"])
        self.assertFalse(negative["all_software_passed"])
        self.assertEqual(negative["default_quit_hangs"], 1)

    def test_normal_exit_must_be_unforced_and_remove_installed_copy(self):
        for field, value in (("kill_detail", "forced termination"), ("verified_installed_copy_removed", False),
                             ("verified_installed_copy_removed", None), ("exit_code", False), ("exit_code", 1)):
            with self.subTest(field=field, value=value):
                data = r_summary()
                data["results"][0][field] = value
                self.assertFalse(gate.assess_r_records(data, SHA)["valid"])
        data = r_summary()
        del data["results"][0]["kill_detail"]
        self.assertFalse(gate.assess_r_records(data, SHA)["valid"])

    def test_hang_requires_successful_workload_and_real_failed_exit(self):
        for field, value in (("r_error", True), ("tests_returned", False), ("requested_exit_status", 1),
                             ("provenance_passed", False), ("exit_code", 0), ("kill_detail", None),
                             ("quit_grace_seconds", 1), ("software_passed", True)):
            with self.subTest(field=field, value=value):
                data = r_summary(baseline=True)
                data["results"][0][field] = value
                self.assertFalse(gate.assess_r_records(data, SHA)["valid"])

    def test_incomplete_matrix_or_single_thread_default_stays_invalid(self):
        for change in ("missing", "duplicate", "threads", "control-hang", "requested-count", "false-total"):
            data = r_summary()
            if change == "missing":
                data["results"].pop()
            elif change == "duplicate":
                data["results"][1]["attempt"] = 1
            elif change == "threads":
                data["results"][0]["loaded_extensions"][0]["threads"] = "1"
            elif change == "control-hang":
                data["results"][-1].update(reason="quit-hang", software_passed=False,
                                            exit_code=1, kill_detail="taskkill")
                data["all_passed"] = False
            elif change == "requested-count":
                data["requested"]["default_attempts"] = 31
            else:
                data["all_passed"] = False
            with self.subTest(change=change):
                self.assertFalse(gate.assess_r_records(data, SHA)["valid"])

    def test_smoke_quit_hang_retained_as_failed_software_exit(self):
        data = r_summary()
        data["smoke"].update(reason="quit-hang", software_passed=False, exit_code=1, kill_detail="taskkill")
        data["all_passed"] = False
        result = gate.assess_r_records(data, SHA)
        self.assertTrue(result["valid"])
        self.assertFalse(result["all_software_passed"])
        self.assertEqual(result["default_quit_hangs"], 0)

    def test_sql_pass_and_completed_test_timeout_are_distinct(self):
        data = sql_summary()
        self.assertTrue(gate.assess_sql_records(data, SHA)["all_software_passed"])
        data.update(reason="timeout_after_test_completion", all_passed=False, exit_code=1)
        result = gate.assess_sql_records(data, SHA)
        self.assertTrue(result["valid"])
        self.assertFalse(result["all_software_passed"])

    def test_sql_assertion_provenance_and_exit_failures_never_pass(self):
        for changes in ({"all_tests_ok": False}, {"statement_passes": 129}, {"observed_exact_binary": False},
                        {"module_observation_errors": ["denied"]}, {"exit_code": 1}, {"exit_code": False},
                        {"all_passed": False}, {"timeout_processes": [{"pid": 100}]},
                        {"reason": "timeout_before_test_completion", "all_passed": False, "exit_code": 1}):
            data = sql_summary()
            data.update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(gate.assess_sql_records(data, SHA)["valid"])


class NativeRawEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()

    def test_complete_cleanup_matrix_keeps_two_diagnostic_hangs_failed(self):
        result = gate.assess_cleanup_records(cleanup_summary(self.folder), self.folder)
        self.assertEqual((result["normal_exits"], result["deliberate_failed_exits"]), (10, 2))

    def test_missing_or_reordered_cleanup_cannot_reuse_stored_pass(self):
        for corruption in ("missing", "order"):
            data = cleanup_summary(self.folder)
            path = self.folder / "foreign-unload-guarded.markers.log"
            markers = path.read_text().splitlines()
            if corruption == "missing":
                markers.pop(0)
            else:
                markers[0], markers[8] = markers[8], markers[0]
            path.write_text("\n".join(markers) + "\n")
            with self.subTest(corruption=corruption), self.assertRaises(ValueError):
                gate.assess_cleanup_records(data, self.folder)

    def test_forced_exit_or_short_diagnostic_grace_cannot_reuse_stored_pass(self):
        for corruption in ("forced-normal", "short-grace", "wrong-dll", "missing-case"):
            data = cleanup_summary(self.folder)
            if corruption == "forced-normal":
                data["results"][0]["terminated_by_supervisor"] = True
            elif corruption == "short-grace":
                next(row for row in data["results"] if row["expected_hang"])["elapsed_seconds"] = 3.0
            elif corruption == "wrong-dll":
                data["results"][0]["command"][1] = str(self.folder / "wrong.dll")
            else:
                data["results"].pop()
            with self.subTest(corruption=corruption), self.assertRaises(ValueError):
                gate.assess_cleanup_records(data, self.folder)

    def test_storage_hazard_is_valid_diagnostic_but_not_valid_lifetime(self):
        for arm in gate.PTHREAD_ARMS:
            result = gate.assess_order_records(ordering_summary(self.folder, arm), self.folder, arm)
            self.assertTrue(result["valid"])
            self.assertIs(result["storage_lifetime_contract_passed"], arm == "reverse-order")
            self.assertEqual(len(result["cases"]), 6)

    def test_changed_raw_callback_cannot_reuse_stored_computed_pass(self):
        data = ordering_summary(self.folder, "reverse-order")
        row = data["results"][0]
        changed = json.loads(row["stdout"])
        changed["cleanup_callbacks"] = 0
        row["stdout"] = json.dumps(changed) + "\n"
        Path(row["stdout_path"]).write_bytes(row["stdout"].encode())
        self.assertTrue(row["diagnostic_expectation_met"])
        with self.assertRaises(ValueError):
            gate.assess_order_records(data, self.folder, "reverse-order")

    def test_order_requires_unforced_exit_exact_matrix_and_lifetime_flag(self):
        for corruption in ("kill", "exit", "duplicate", "missing", "fake-lifetime", "wrong-dll"):
            data = ordering_summary(self.folder, "original")
            if corruption == "kill":
                data["results"][0]["terminated_by_supervisor"] = True
            elif corruption == "exit":
                data["results"][0]["returncode"] = 90
            elif corruption == "duplicate":
                data["results"][1] = deepcopy(data["results"][0])
            elif corruption == "missing":
                data["results"].pop()
            elif corruption == "fake-lifetime":
                data["storage_lifetime_contract_passed"] = True
            else:
                data["results"][0]["command"][1] = str(self.folder / "wrong.dll")
            with self.subTest(corruption=corruption), self.assertRaises(ValueError):
                gate.assess_order_records(data, self.folder, "original")

    def test_changed_raw_log_is_rejected_even_if_summary_unchanged(self):
        data = ordering_summary(self.folder, "reverse-order")
        Path(data["results"][0]["stdout_path"]).write_bytes(b"changed\n")
        with self.assertRaises(ValueError):
            gate.assess_order_records(data, self.folder, "reverse-order")


class SpatialLinkIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "runtime").mkdir()
        for gcc in ("baseline", "guarded"):
            (self.root / "runtime" / f"{gcc}.cc").write_text(gcc + " synthetic source\n")
        self.provenance = {"source_commit": "2" * 40}
        self.preparation = {"prep_manifest_sha256": "3" * 64, "variants": {}}
        for arm in gate.PTHREAD_ARMS:
            directory = f"D:/private/{arm}/lib"
            self.preparation["variants"][arm] = {
                "library_dir": directory,
                "archives": {name: {"path": directory + "/" + name, "sha256": "4" * 64,
                                    "thread_provider": "libwinpthread_la-thread.o"}
                             for name in gate.ARCHIVE_NAMES},
            }

    def write_arm(self, arm):
        folder = self.root / arm
        folder.mkdir(exist_ok=True)
        pthread, gcc = gate.ARMS[arm]
        source = self.root / "runtime" / f"{gcc}.cc" if gcc != "archive" else None
        footer = ["4", "windows_amd64_mingw", "v2.0.0", self.provenance["source_commit"][:10], "CPP", "", "", ""]
        binary = b"MZ" + b"\0" * 510 + b"".join(value.encode().ljust(32, b"\0") for value in reversed(footer)) + b"\0" * 256
        (folder / "spatial.duckdb_extension").write_bytes(binary)
        record = {"arm": arm, "pthread_arm": pthread, "gcc_arm": gcc, "build_completed": True,
                  "extension_sha256": hashlib.sha256(binary).hexdigest(),
                  "runtime_source": str(source) if source else "",
                  "runtime_source_sha256": gate.digest(source) if source else None}
        (folder / "arm-result.json").write_text(json.dumps(record))
        variant = self.preparation["variants"][pthread]
        provider = variant["archives"]["libpthread.a"]["path"]
        mapping = f"{provider}(libwinpthread_la-thread.o)\r\nLOAD {provider}\r\n"
        mapping += "C:/rtools42/lib/libstdc++.a(atexit_thread.o)\r\n" if gcc == "archive" else f"CMakeFiles/spatial_loadable_extension.dir/{gcc}.cc.obj\r\n"
        symbols = "\r\n".join(f"0000000180001000 T {name}" for name in (*PROVIDER_SYMBOLS, "__cxa_thread_atexit")) + "\r\n"
        (folder / "spatial-link.map").write_bytes(mapping.encode())
        (folder / "defined-symbols.txt").write_bytes(symbols.encode())
        proof = {"passed": True, "pthread_arm": pthread, "extension_sha256": record["extension_sha256"],
                 "prep_manifest_sha256": self.preparation["prep_manifest_sha256"],
                 "selected_library_dir": variant["library_dir"],
                 "archives": {name: {"path": value["path"], "sha256": value["sha256"]}
                              for name, value in variant["archives"].items()},
                 "link_map_sha256": gate.digest(folder / "spatial-link.map"),
                 "symbols_sha256": gate.digest(folder / "defined-symbols.txt"),
                 "link_proof": gate.inspect_link(mapping, symbols, variant["archives"])}
        for name in ("pthread-link-proof-before-tests.json", "pthread-link-proof.json"):
            (folder / name).write_text(json.dumps(proof))
        (folder / "CMakeCache.txt").write_text(
            f"SPATIAL_MINGW_PTHREAD_LIBRARY_DIR:PATH={variant['library_dir']}\n"
            f"SPATIAL_MINGW_TLS_ATEXIT_SOURCE:FILEPATH={str(source) if source else ''}\n")
        (folder / "compile_commands.json").write_text(json.dumps([{"file": str(source)}] if source else []))
        return folder

    def test_all_six_fixed_factors_match_actual_map_and_footer(self):
        for arm in gate.ARMS:
            folder = self.write_arm(arm)
            with self.subTest(arm=arm):
                self.assertTrue(gate.inspect_spatial_link(folder, arm, self.preparation, self.provenance)["valid"])

    def test_changed_binary_map_symbols_proof_or_factor_are_rejected(self):
        for corruption in ("binary", "map", "weak-cxa", "proof", "gcc-factor", "pthread-factor", "source", "cache", "compile"):
            folder = self.write_arm("order-only")
            if corruption == "binary":
                with (folder / "spatial.duckdb_extension").open("ab") as stream:
                    stream.write(b"changed")
            elif corruption in ("map", "weak-cxa"):
                path = folder / ("spatial-link.map" if corruption == "map" else "defined-symbols.txt")
                with path.open("ab") as stream:
                    stream.write(b"\nC:/rtools42/lib/libpthread.a(other.o)\n" if corruption == "map"
                                 else b"\n0000000180002000 W __cxa_thread_atexit\n")
            elif corruption == "proof":
                (folder / "pthread-link-proof.json").unlink()
            elif corruption in ("gcc-factor", "pthread-factor", "source"):
                path = folder / "arm-result.json"
                record = json.loads(path.read_text())
                record[{"gcc-factor": "gcc_arm", "pthread-factor": "pthread_arm", "source": "runtime_source_sha256"}[corruption]] = "wrong"
                path.write_text(json.dumps(record))
            elif corruption == "cache":
                (folder / "CMakeCache.txt").write_text("SPATIAL_MINGW_PTHREAD_LIBRARY_DIR:PATH=C:/rtools42/lib\n")
            else:
                (folder / "compile_commands.json").write_text("[]")
            with self.subTest(corruption=corruption), self.assertRaises((ValueError, FileNotFoundError)):
                gate.inspect_spatial_link(folder, "order-only", self.preparation, self.provenance)

    def test_source_runtime_and_archive_fallback_cannot_be_mixed(self):
        symbols = "0000000180001000 T __cxa_thread_atexit\n"
        for gcc in gate.GCC_ARMS:
            mapping = "libstdc++.a(atexit_thread.o)" if gcc == "archive" else f"{gcc}.cc.obj"
            self.assertTrue(gate.gcc_link_proof(mapping, symbols, gcc, native=False)["passed"])
            self.assertFalse(gate.gcc_link_proof(mapping, symbols * 2, gcc, native=False)["passed"])
            self.assertFalse(gate.gcc_link_proof(mapping, symbols + "1 W __cxa_thread_atexit\n", gcc, native=False)["passed"])
            for other in set(gate.GCC_ARMS) - {gcc}:
                self.assertFalse(gate.gcc_link_proof(mapping, symbols, other, native=False)["passed"])


if __name__ == "__main__":
    unittest.main()
