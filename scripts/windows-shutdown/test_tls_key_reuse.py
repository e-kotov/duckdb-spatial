from __future__ import annotations

import json
import unittest

from run_tls_key_reuse import ARMS, MODES, SCENARIOS, VARIANTS, assess, expectations


def observation(scenario: str, descending: bool) -> dict:
    # Explicit source-predicted sequences. The normal control's public key 0
    # straddles the runtime keys; key-reuse's public keys follow both runtimes.
    events = {
        ("normal", False): (2, 3, 4, 1, 5),
        ("normal", True): (3, 4, 2, 5, 1),
        ("key-reuse", False): (2, 3, 1, 4, 5),
        ("key-reuse", True): (3, 4, 5, 2, 1),
    }[scenario, descending]
    enter, leave, callback, low, high = events
    return {
        "schema": 1, "mode": "pthread", "scenario": scenario, "worker_id": 1600,
        "native_wait_confirmed": True, "actual_unload_confirmed": True,
        "worker_error": 0, "storage_touches": 2, "cleanup_registrations": 1,
        "keys_created": 2, "keys_deleted": 2, "sequence": 5, "release_calls": 1,
        "cleanup_callbacks": 1, "low_key_callbacks": 1, "high_key_callbacks": 1,
        "dll_destructors": 1, "reserved_key_created": 1, "reserved_key_deleted": 1,
        "key_create_calls": 5, "key_create_phase_mask": 62, "key_create_phase": 0,
        "setup_sequence": 4, "reserve_created_event": 1, "storage_ready_event": 2,
        "reserve_deleted_event": 3 if scenario == "key-reuse" else 4,
        "cleanup_registered_event": 4 if scenario == "key-reuse" else 3,
        "reserved_key": 0, "emutls_key": 1, "cleanup_key": 0 if scenario == "key-reuse" else 2,
        "low_key": 2 if scenario == "key-reuse" else 0, "high_key": 3,
        "object_address": "0x1008", "allocation_base": "0x1000", "observed_release_address": "0x1000",
        "release_enter_event": enter, "release_leave_event": leave, "cleanup_event": callback,
        "low_key_event": low, "high_key_event": high,
        "callbacks_at_release": int(callback < enter), "completed_releases_at_callback": int(leave < callback),
    }


def result(value: dict) -> dict:
    return {"stdout": json.dumps(value) + "\n", "stderr": "", "returncode": 0,
            "timed_out": False, "terminated_by_supervisor": False}


def evaluate(raw: dict, *, arm: str = "reverse-order", scenario: str = "key-reuse",
             variant: str = "archive", mode: str = "pthread") -> dict:
    lifetime, traversal = expectations(arm, scenario)
    return assess(raw, variant=variant, mode=mode, scenario=scenario,
                  expected_order=lifetime, expected_traversal=traversal)


class KeyReuseAssessmentTests(unittest.TestCase):
    def test_all_36_case_identities_keep_failed_lifetimes_separate_from_exits(self):
        for arm in ARMS:
            for scenario in SCENARIOS:
                for variant in VARIANTS:
                    for mode in MODES:
                        with self.subTest(arm=arm, scenario=scenario, variant=variant, mode=mode):
                            value = observation(scenario, arm == "reverse-order")
                            value["mode"] = mode
                            checked = evaluate(result(value), arm=arm, scenario=scenario, variant=variant, mode=mode)
                            self.assertTrue(checked["observation_valid"])
                            self.assertTrue(checked["diagnostic_expectation_met"])
                            self.assertTrue(checked["process_exit_passed"])
                            self.assertEqual(checked["storage_lifetime_contract_passed"],
                                             (arm == "reverse-order") != (scenario == "key-reuse"))

    def test_key_reuse_inverts_lifetime_while_public_traversal_stays_same(self):
        for arm in ARMS:
            normal = evaluate(result(observation("normal", arm == "reverse-order")), arm=arm, scenario="normal")
            reused = evaluate(result(observation("key-reuse", arm == "reverse-order")), arm=arm)
            self.assertNotEqual(normal["observed_order"], reused["observed_order"])
            self.assertEqual(normal["public_key_order"], reused["public_key_order"])

    def test_public_order_never_overwrites_measured_lifetime(self):
        value = observation("key-reuse", True)
        value["low_key_event"], value["high_key_event"] = value["high_key_event"], value["low_key_event"]
        checked = evaluate(result(value))
        self.assertTrue(checked["observation_valid"])
        self.assertEqual(checked["observed_order"], "storage-first")
        self.assertEqual(checked["public_key_order"], "ascending")
        self.assertFalse(checked["diagnostic_expectation_met"])
        self.assertFalse(checked["storage_lifetime_contract_passed"])

    def test_lifetime_order_never_inferred_from_expected_traversal(self):
        value = observation("key-reuse", True)
        value.update(release_enter_event=4, release_leave_event=5, cleanup_event=3,
                     callbacks_at_release=1, completed_releases_at_callback=0)
        checked = evaluate(result(value))
        self.assertTrue(checked["observation_valid"])
        self.assertEqual(checked["observed_order"], "callback-first")
        self.assertEqual(checked["public_key_order"], "descending")
        self.assertTrue(checked["storage_lifetime_contract_passed"])
        self.assertFalse(checked["diagnostic_expectation_met"])

    def test_real_runtime_key_capture_and_reservation_timing_are_required(self):
        invalid = {"reserved_key": 1, "emutls_key": 0, "cleanup_key": 2, "low_key": 0,
                   "high_key": 4, "key_create_calls": 4, "key_create_phase_mask": 30,
                   "key_create_phase": 3, "reserved_key_created": 0, "reserved_key_deleted": 0,
                   "reserve_created_event": 2, "storage_ready_event": 1,
                   "reserve_deleted_event": 4, "cleanup_registered_event": 3, "setup_sequence": 5}
        for name, wrong in invalid.items():
            with self.subTest(name=name):
                value = observation("key-reuse", True)
                value[name] = wrong
                self.assertFalse(evaluate(result(value))["observation_valid"])

    def test_missing_duplicate_or_boolean_counts_are_rejected(self):
        names = ("release_calls", "cleanup_callbacks", "low_key_callbacks", "high_key_callbacks",
                 "dll_destructors", "keys_deleted", "reserved_key_deleted", "key_create_calls")
        for name in names:
            for bad in (None, False, True, 0, 99):
                with self.subTest(name=name, bad=bad):
                    value = observation("key-reuse", True)
                    value[name] = bad
                    self.assertFalse(evaluate(result(value))["observation_valid"])

    def test_exact_storage_address_and_allocator_completion_are_required(self):
        changes = ({"observed_release_address": "0x2000"}, {"object_address": "0x1009"},
                   {"allocation_base": "0x0"}, {"allocation_base": 4096},
                   {"release_leave_event": 3}, {"cleanup_event": 4},
                   {"callbacks_at_release": 1}, {"completed_releases_at_callback": 0})
        for change in changes:
            with self.subTest(change=change):
                value = observation("key-reuse", True)
                value.update(change)
                self.assertFalse(evaluate(result(value))["observation_valid"])

    def test_native_wait_and_actual_unload_cannot_be_omitted(self):
        for name in ("native_wait_confirmed", "actual_unload_confirmed"):
            for value in (False, None, 1):
                changed = observation("key-reuse", True)
                changed[name] = value
                self.assertFalse(evaluate(result(changed))["observation_valid"])

    def test_bad_process_exit_is_not_success_even_with_fabricated_derived_fields(self):
        raw = result(observation("key-reuse", True))
        raw.update(returncode=90, diagnostic_expectation_met=True, observation_valid=True,
                   storage_lifetime_contract_passed=True, process_exit_passed=True)
        checked = evaluate(raw)
        for name in ("diagnostic_expectation_met", "observation_valid", "storage_lifetime_contract_passed", "process_exit_passed"):
            self.assertFalse(checked[name])

    def test_timeouts_forced_exits_stderr_and_non_integer_exit_codes_fail(self):
        changes = ({"timed_out": True}, {"timed_out": 0}, {"terminated_by_supervisor": True},
                   {"terminated_by_supervisor": 0}, {"returncode": False}, {"returncode": None},
                   {"returncode": -1}, {"stderr": "unexpected warning"})
        for change in changes:
            with self.subTest(change=change):
                raw = result(observation("key-reuse", True))
                raw.update(change)
                self.assertFalse(evaluate(raw)["observation_valid"])

    def test_wrong_scenario_worker_and_incomplete_json_fail(self):
        for name, value in (("scenario", "normal"), ("mode", "foreign"), ("worker_id", 0)):
            raw = observation("key-reuse", True)
            raw[name] = value
            self.assertFalse(evaluate(result(raw))["observation_valid"])
        for text in ("", "{}", "[]", "null", "unfinished", "{}\n{}"):
            raw = result(observation("key-reuse", True))
            raw["stdout"] = text
            self.assertFalse(evaluate(raw)["observation_valid"])

    def test_unknown_factors_are_not_accepted(self):
        raw = result(observation("key-reuse", True))
        self.assertFalse(evaluate(raw, variant="unknown")["observation_valid"])
        for arm, scenario in (("unknown", "normal"), ("original", "unknown")):
            with self.assertRaises(ValueError):
                expectations(arm, scenario)


if __name__ == "__main__":
    unittest.main()
