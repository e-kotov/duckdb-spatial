#!/usr/bin/env python3
"""Adversarial tests for the storage-order observation gate; no Windows claims."""

import json
import unittest

from run_tls_storage_order import assess, pthread_provider_paths


def observation(order="storage-first"):
    value = {
        "schema": 1, "mode": "foreign", "worker_id": 1200,
        "native_wait_confirmed": True, "actual_unload_confirmed": True,
        "worker_error": 0, "storage_touches": 2, "cleanup_registrations": 1,
        "keys_created": 2, "keys_deleted": 2, "low_key": 2, "high_key": 3,
        "object_address": "0x1008", "allocation_base": "0x1000", "observed_release_address": "0x1000",
        "sequence": 5, "release_calls": 1, "release_enter_event": 1,
        "release_leave_event": 2, "callbacks_at_release": 0,
        "cleanup_callbacks": 1, "cleanup_event": 3, "completed_releases_at_callback": 1,
        "low_key_callbacks": 1, "low_key_event": 4, "high_key_callbacks": 1,
        "high_key_event": 5, "dll_destructors": 1,
    }
    if order == "callback-first":
        value.update(release_enter_event=4, release_leave_event=5, callbacks_at_release=1,
                     completed_releases_at_callback=0, low_key_event=2, high_key_event=1)
    return value


def process(value=None):
    return {"timed_out": False, "terminated_by_supervisor": False, "returncode": 0,
            "stdout": json.dumps(observation() if value is None else value) + "\n", "stderr": ""}


def classify(result=None, expected="storage-first"):
    return assess(process() if result is None else result, variant="guarded", mode="foreign", expected_order=expected)


class StorageOrderGateTests(unittest.TestCase):
    def test_reproduction_does_not_claim_storage_contract(self):
        result = classify()
        self.assertTrue(result["diagnostic_expectation_met"])
        self.assertTrue(result["process_exit_passed"])
        self.assertFalse(result["storage_lifetime_contract_passed"])

    def test_reverse_order_establishes_narrow_contract(self):
        result = classify(process(observation("callback-first")), "callback-first")
        self.assertTrue(result["diagnostic_expectation_met"])
        self.assertTrue(result["storage_lifetime_contract_passed"])
        self.assertEqual(result["public_key_order"], "descending")

    def test_clean_exit_without_observation_fails(self):
        result = process()
        result["stdout"] = ""
        self.assertFalse(classify(result)["diagnostic_expectation_met"])

    def test_missing_or_duplicate_callbacks_fail(self):
        for name in ("release_calls", "cleanup_callbacks", "low_key_callbacks", "high_key_callbacks"):
            for count in (0, 2, True):
                with self.subTest(name=name, count=count):
                    value = observation()
                    value[name] = count
                    self.assertFalse(classify(process(value))["observation_valid"])

    def test_wrong_freed_allocation_fails(self):
        for field in ("observed_release_address", "object_address"):
            value = observation()
            value[field] = "0x2000"
            self.assertFalse(classify(process(value))["observation_valid"])

    def test_free_entry_without_return_fails(self):
        value = observation()
        value["release_leave_event"] = 0
        self.assertFalse(classify(process(value))["observation_valid"])

    def test_duplicate_event_or_impossible_release_bracket_fails(self):
        for changes in ({"release_leave_event": 1}, {"release_leave_event": 3, "cleanup_event": 2}):
            value = observation()
            value.update(changes)
            self.assertFalse(classify(process(value))["observation_valid"])

    def test_inconsistent_allocator_counter_fails(self):
        value = observation()
        value["completed_releases_at_callback"] = 0
        self.assertFalse(classify(process(value))["observation_valid"])

    def test_different_public_key_order_fails(self):
        value = observation()
        value.update(low_key_event=5, high_key_event=4)
        self.assertFalse(classify(process(value))["observation_valid"])

    def test_expected_lifetime_order_is_enforced(self):
        result = classify(expected="callback-first")
        self.assertTrue(result["observation_valid"])
        self.assertFalse(result["diagnostic_expectation_met"])
        self.assertFalse(result["storage_lifetime_contract_passed"])

    def test_timeout_or_kill_or_nonzero_exit_cannot_pass(self):
        for changes in ({"timed_out": True}, {"terminated_by_supervisor": True}, {"returncode": 90}):
            result = process()
            result.update(changes)
            self.assertFalse(classify(result)["diagnostic_expectation_met"])

    def test_native_wait_and_unload_required(self):
        for field in ("native_wait_confirmed", "actual_unload_confirmed"):
            value = observation()
            value[field] = False
            self.assertFalse(classify(process(value))["observation_valid"])

    def test_worker_mode_must_match(self):
        value = observation()
        value["mode"] = "pthread"
        self.assertFalse(classify(process(value))["observation_valid"])

    def test_multiple_output_records_fail(self):
        result = process()
        result["stdout"] *= 2
        self.assertFalse(classify(result)["observation_valid"])

    def test_archive_provider_normalization_and_outside_provider_visibility(self):
        text = ("D:/a/private/lib/libpthread.a(libwinpthread_la-thread.o)\n"
                "  .text 0x100 0x20 D:\\a\\private\\lib\\libpthread.a(libwinpthread_la-thread.o)\n"
                "LOAD D:/a/private/lib/libwinpthread.a\n"
                "LOAD C:/rtools42/bin/../lib/libpthread.a\n")
        self.assertEqual(pthread_provider_paths(text), [
            "c:\\rtools42\\lib\\libpthread.a", "d:\\a\\private\\lib\\libpthread.a",
            "d:\\a\\private\\lib\\libwinpthread.a",
        ])


if __name__ == "__main__":
    unittest.main()
