import tempfile
import unittest
from pathlib import Path

from face_locker.storage import Database


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_restart_marks_pending_requests_unknown_without_replay(self):
        self.db.begin_attempt("scan")
        self.db.record_command("request", "scan")
        self.db.command_result("request", "accepted")
        self.db.close()
        self.db = Database(self.path)
        row = self.db.connection.execute("SELECT result FROM lock_commands").fetchone()
        self.assertEqual(row[0], "unknown_after_restart")
        self.assertEqual(self.db.connection.execute("SELECT decision FROM access_attempts").fetchone()[0], "interrupted")

    def test_calendar_uses_local_midnight_boundaries(self):
        self.db.begin_attempt("a")
        self.db.connection.execute("UPDATE access_attempts SET started_at='2026-11-08T18:00:00.000+00:00'")
        self.db.connection.commit()
        self.assertEqual(len(self.db.history("2026-11-09", "Asia/Jakarta")), 1)
        self.assertEqual(len(self.db.history("2026-11-08", "Asia/Jakarta")), 0)

    def test_delete_removes_biometrics_but_preserves_attempt(self):
        user = self.db.add_user("Alice", [[1, 0]], "test")
        self.db.begin_attempt("a")
        self.db.finish_attempt("a", "observed", "Test", user, "Alice", .9)
        self.db.delete_user(user)
        self.assertEqual(self.db.templates("test"), [])
        row = self.db.connection.execute("SELECT name,user_id FROM access_attempts").fetchone()
        self.assertEqual(tuple(row), ("Alice", None))

    def test_pin_is_salted_and_never_stored_as_plaintext(self):
        self.db.set_pin("123456")
        self.assertTrue(self.db.verify_pin("123456"))
        self.assertFalse(self.db.verify_pin("111111"))
        self.assertNotIn("123456", self.db.connection.execute("SELECT value FROM settings").fetchone()[0])
