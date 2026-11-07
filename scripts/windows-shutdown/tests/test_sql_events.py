"""Prevent false passes and the observed empty-upstream-file false failure."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_sql_tests import (EMPTY_SHA256, KNOWN_EMPTY_TEST, canonical, event_passed,
                           verified_empty_tests)


class SQLEventTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name).resolve()
        self.empty = self.source / KNOWN_EMPTY_TEST
        self.empty.parent.mkdir(parents=True)
        self.empty.write_bytes(b"")
        self.other = self.empty.with_name("real-assertion.test")
        self.other.write_text("query I\nSELECT 1\n----\n1\n", encoding="utf-8")
        self.allowed = verified_empty_tests([self.empty, self.other], self.source)

    def event(self, path=None, passes=0, **changes):
        result = {"event": "end", "name": str(path or self.empty), "status": "ok",
                  "passes": passes, "fails": 0, "skip-mode": 0}
        result.update(changes)
        return result

    def test_exact_empty_upstream_file_remains_an_executed_record(self):
        self.assertTrue(event_passed(self.event(), self.source, self.allowed))
        self.assertEqual(len(self.allowed), 1)
        self.assertEqual(self.allowed[canonical(self.empty, self.source)]["sha256"], EMPTY_SHA256)

    def test_other_test_requires_a_real_assertion(self):
        self.assertFalse(event_passed(self.event(self.other), self.source, self.allowed))
        self.assertTrue(event_passed(self.event(self.other, passes=1), self.source, self.allowed))

    def test_a_new_empty_file_is_rejected(self):
        self.other.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "Unexpected empty SQL test"):
            verified_empty_tests([self.empty, self.other], self.source)

    def test_formerly_empty_file_with_content_requires_an_assertion(self):
        self.empty.write_text("query I\nSELECT 1\n----\n1\n", encoding="utf-8")
        allowed = verified_empty_tests([self.empty], self.source)
        self.assertEqual(allowed, {})
        self.assertFalse(event_passed(self.event(), self.source, allowed))

    def test_empty_file_never_excuses_skip_failure_or_invalid_counter(self):
        for changes in ({"status": "fail"}, {"fails": 1}, {"skip-mode": 1},
                        {"partial-skip-reasons": ["spatial"]}, {"passes": -1},
                        {"passes": False}, {"passes": "0"}):
            with self.subTest(changes=changes):
                self.assertFalse(event_passed(self.event(**changes), self.source, self.allowed))


if __name__ == "__main__":
    unittest.main()
