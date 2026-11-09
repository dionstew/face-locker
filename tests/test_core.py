import tempfile
import time
import unittest

import numpy as np

from face_locker.config import Settings
from face_locker.core import ApplicationService
from face_locker.vision import DemoVision, Observation, best_match


class SerialStub:
    def __init__(self, available=True):
        self.available = available
        self.sent = []

    def snapshot(self):
        return {"pending": self.sent[-1] if self.sent else None, "telemetry_fresh": self.available}

    def unlock(self, request_id):
        if not self.available:
            raise RuntimeError("Controller unavailable")
        self.sent.append(request_id)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(state_dir=self.directory.name, demo=True)
        self.settings.prepare()
        self.core = ApplicationService(self.settings)
        self.core.serial = SerialStub()
        self.core.vision = DemoVision(self.settings)
        self.core.vision_error = ""
        self.core.model_version = self.core.vision.model_version
        self.embedding = np.zeros(128, dtype=np.float32)
        self.embedding[0] = 1
        self.user_id = self.core.db.add_user("Test Person", [self.embedding.tolist()], self.core.model_version)
        self.core._refresh_templates()

    def tearDown(self):
        self.core.db.close()
        self.directory.cleanup()

    def scan(self):
        self.core.dispatch({"command": "scan"})
        self.core.session["started"] -= 2

    def observe(self, embedding=None, live=True, count=3):
        for _ in range(count):
            if self.core.session:
                self.core._process_observation(Observation(self.embedding if embedding is None else embedding, live))

    def test_known_face_sends_one_request_and_records_ack_separately(self):
        self.scan()
        self.observe()
        self.assertEqual(self.core.state, "unlocking")
        self.assertEqual(len(self.core.serial.sent), 1)
        request_id = self.core.serial.sent[0]
        self.core._hardware_event("command", {"request_id": request_id, "result": "accepted"})
        self.assertEqual(self.core.state, "unlocking")
        self.core._hardware_event("command", {"request_id": request_id, "result": "completed"})
        self.assertEqual(self.core.state, "success")
        self.assertEqual(len(self.core.serial.sent), 1)

    def test_observation_mode_never_unlocks(self):
        self.settings.demo = False
        self.scan()
        self.observe()
        self.assertEqual(self.core.state, "observed")
        self.assertEqual(self.core.serial.sent, [])

    def test_missing_liveness_never_unlocks_even_if_enabled(self):
        self.settings.demo = False
        self.settings.unlock_enabled = True
        self.core.vision.pad = None
        self.scan()
        self.observe(live=False)
        self.assertEqual(self.core.state, "observed")
        self.assertEqual(self.core.serial.sent, [])

    def test_unknown_face_and_spoof_are_denied(self):
        unknown = np.roll(self.embedding, 1)
        self.scan()
        self.observe(unknown, count=5)
        self.assertEqual(self.core.state, "denied")
        self.scan()
        self.observe(live=False)
        self.assertEqual(self.core.state, "denied")
        self.assertEqual(self.core.serial.sent, [])

    def test_interrupted_face_sequence_does_not_authorize(self):
        self.scan()
        self.observe(count=2)
        self.core._process_observation(Observation())
        self.observe(count=2)
        self.assertEqual(self.core.state, "scanning")
        self.assertEqual(self.core.serial.sent, [])

    def test_cancel_discards_session(self):
        self.scan()
        attempt_id = self.core.session["id"]
        self.core.dispatch({"command": "standby"})
        self.assertIsNone(self.core.session)
        row = self.core.db.connection.execute("SELECT decision FROM access_attempts WHERE id=?", (attempt_id,)).fetchone()
        self.assertEqual(row[0], "cancelled")

    def test_controller_failure_does_not_report_success(self):
        self.core.serial.available = False
        self.scan()
        self.observe()
        self.assertEqual(self.core.state, "fault")
        self.assertEqual(self.core.serial.sent, [])

    def test_disabled_user_is_excluded(self):
        self.core.db.set_active(self.user_id, False)
        self.core._refresh_templates()
        with self.assertRaises(RuntimeError):
            self.scan()

    def test_enrollment_requires_admin_and_same_identity(self):
        with self.assertRaises(PermissionError):
            self.core.dispatch({"command": "enroll", "name": "New Person"})
        token = self.core.dispatch({"command": "login", "pin": "123456"})["token"]
        self.core.dispatch({"command": "enroll", "name": "New Person", "token": token})
        new_face = np.roll(self.embedding, 1)
        self.core._process_observation(Observation(new_face, True))
        self.core.session["last_sample"] = 0
        self.core._process_observation(Observation(self.embedding, True))
        self.assertEqual(len(self.core.session["samples"]), 1)
        for _ in range(4):
            self.core.session["last_sample"] = 0
            self.core._process_observation(Observation(new_face, True))
        self.assertEqual(self.core.state, "success")
        self.assertEqual(len(self.core.db.users()), 2)

    def test_pin_lockout_and_token_expiry(self):
        for _ in range(5):
            with self.assertRaises(PermissionError):
                self.core.dispatch({"command": "login", "pin": "000000"})
        with self.assertRaisesRegex(PermissionError, "Too many"):
            self.core.dispatch({"command": "login", "pin": "123456"})
        self.core.pin_blocked_until = 0
        token = self.core.dispatch({"command": "login", "pin": "123456"})["token"]
        self.core.admin_sessions[token] = time.monotonic() - 1
        with self.assertRaises(PermissionError):
            self.core.dispatch({"command": "users", "token": token})

    def test_ambiguous_match_is_rejected_per_identity(self):
        templates = [("a", "A", self.embedding), ("a", "A", self.embedding), ("b", "B", self.embedding)]
        self.assertIsNone(best_match(self.embedding, templates, .5, .08))
