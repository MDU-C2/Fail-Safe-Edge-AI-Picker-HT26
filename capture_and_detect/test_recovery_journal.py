"""Offline persistence tests; no gateway or robot is contacted."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_journal import RecoveryJournal


class RecoveryJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "recovery.json"
        self.legacy = Path(self.directory.name) / "cups.json"
        self.journal = RecoveryJournal(self.path, self.legacy)

    def write_legacy(self, cups):
        self.legacy.write_text(json.dumps({"schema_version": 2, "cups": cups}), encoding="utf-8")

    def test_absent_journal_and_inventory_do_not_require_recovery(self):
        self.assertFalse(self.journal.required())
        self.assertFalse(RecoveryJournal(self.path).required())
        self.assertFalse(self.path.exists())

    def test_begin_persists_across_restart_and_preserves_context(self):
        context = {"capture": "scan-1", "cup_id": 2, "action": "goto"}
        self.journal.begin(context)
        restarted = RecoveryJournal(self.path, self.legacy)
        self.assertTrue(restarted.required())
        self.assertEqual(json.loads(self.path.read_text()), {
            "schema_version": 1, "recovery_required": True, "context": context,
        })

    def test_clear_persists_and_removes_old_context(self):
        self.journal.begin({"cup_id": 2})
        self.journal.clear()
        self.assertFalse(RecoveryJournal(self.path).required())
        self.assertEqual(json.loads(self.path.read_text()), {
            "schema_version": 1, "recovery_required": False,
        })

    def test_malformed_journal_is_never_treated_as_clear(self):
        invalid = ["", "{", "null", "[]", "{}", '{"schema_version":2,"recovery_required":false}',
                   '{"schema_version":true,"recovery_required":false}',
                   '{"schema_version":1,"recovery_required":0}',
                   '{"schema_version":1,"recovery_required":"false"}',
                   '{"schema_version":1,"recovery_required":false,"context":[]}',
                   '{"schema_version":1,"recovery_required":false,"context":{"x":NaN}}',
                   '{"schema_version":1,"recovery_required":true,"recovery_required":false}']
        self.write_legacy([])
        for content in invalid:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.journal.required()

    def test_unreadable_journal_propagates_error_without_legacy_fallback(self):
        self.write_legacy([])
        with patch("recovery_journal._read_json", side_effect=PermissionError("permission denied")):
            with self.assertRaises(PermissionError):
                self.journal.required()

    def test_missing_journal_checks_old_unconfirmed_statuses(self):
        for status in ("movement_unconfirmed", "command_pending"):
            with self.subTest(status=status):
                self.path.unlink(missing_ok=True)
                self.write_legacy([{"status": "available"}, {"status": status}])
                self.assertTrue(self.journal.required())
        self.path.unlink()
        self.write_legacy([{"status": "placed"}, {"status": "at_hover"}])
        self.assertFalse(self.journal.required())

    def test_missing_journal_checks_incomplete_execution_states(self):
        for state in ("pending", "running", "interrupted"):
            with self.subTest(state=state):
                self.path.unlink(missing_ok=True)
                self.write_legacy([{"status": "needs_rescan", "execution": {"state": state}}])
                self.assertTrue(self.journal.required())
        self.path.unlink()
        self.write_legacy([{"status": "placed", "execution": {"state": "completed"}}])
        self.assertFalse(self.journal.required())

    def test_cleared_journal_takes_precedence_over_old_failed_scan(self):
        self.write_legacy([{"status": "movement_unconfirmed"}])
        self.journal.clear()
        self.assertFalse(self.journal.required())
        self.legacy.write_text("malformed stale inventory", encoding="utf-8")
        self.assertFalse(self.journal.required())

    def test_required_journal_remains_required_when_scan_file_is_replaced(self):
        self.journal.begin({"cup_id": 1})
        self.write_legacy([])
        self.assertTrue(self.journal.required())
        self.write_legacy([{"status": "available"}])
        self.assertTrue(self.journal.required())

    def test_legacy_bare_cup_list_is_supported(self):
        self.legacy.write_text('[{"status":"command_pending"}]', encoding="utf-8")
        self.assertTrue(self.journal.required())
        self.path.unlink()
        self.legacy.write_text("[]", encoding="utf-8")
        self.assertFalse(self.journal.required())

    def test_malformed_legacy_inventory_fails_closed(self):
        invalid = ["", "{", "{}", "null", '{"cups":null}', '{"cups":[1]}',
                   '{"cups":[{"status":false}]}', '{"cups":[{"execution":null}]}',
                   '{"cups":[{"execution":{}}]}', '{"cups":[{"execution":{"state":"unknown"}}]}']
        for content in invalid:
            with self.subTest(content=content):
                self.legacy.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.journal.required()

    def test_unreadable_legacy_inventory_propagates_error(self):
        with patch("recovery_journal._read_json", side_effect=[FileNotFoundError(), PermissionError()]):
            with self.assertRaises(PermissionError):
                self.journal.required()

    def test_failed_clear_leaves_previous_required_journal_intact(self):
        self.journal.begin({"cup_id": 1})
        before = self.path.read_bytes()
        with patch("cup_inventory.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.journal.clear()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.journal.required())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_failed_begin_preserves_previous_complete_state(self):
        self.journal.clear()
        before = self.path.read_bytes()
        with patch("cup_inventory.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.journal.begin({"cup_id": 1})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_invalid_begin_context_cannot_overwrite_existing_journal(self):
        self.journal.begin({"cup_id": 1})
        before = self.path.read_bytes()
        for invalid in (None, [], "cup 1", {"x": float("nan")}):
            with self.subTest(context=invalid):
                with self.assertRaises(ValueError):
                    self.journal.begin(invalid)
                self.assertEqual(self.path.read_bytes(), before)

    def test_context_cannot_override_journal_recovery_flag(self):
        self.journal.begin({"schema_version": 3, "recovery_required": False})
        self.assertTrue(self.journal.required())

    def test_legacy_lockout_is_migrated_before_scans_can_overwrite_evidence(self):
        self.write_legacy([{"status": "movement_unconfirmed"}])
        self.assertTrue(self.journal.required())
        self.assertTrue(self.path.exists())
        self.write_legacy([{"status": "available"}])
        restarted = RecoveryJournal(self.path, self.legacy)
        self.assertTrue(restarted.required())
        self.assertEqual(json.loads(self.path.read_text())["context"]["source"], "legacy_inventory")

    def test_failed_legacy_migration_propagates_and_preserves_evidence(self):
        self.write_legacy([{"status": "command_pending"}])
        legacy_before = self.legacy.read_bytes()
        with patch("cup_inventory.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.journal.required()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.legacy.read_bytes(), legacy_before)

    def test_existing_journal_is_only_read_by_required(self):
        self.journal.begin({"cup_id": 1})
        with patch("recovery_journal.atomic_json") as save:
            self.assertTrue(self.journal.required())
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
