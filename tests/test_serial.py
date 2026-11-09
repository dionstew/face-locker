"""Exercise pySerial through a real Linux pseudo-terminal, without a solenoid."""
import os
import select
import threading
import time
import unittest

from face_locker.config import Settings
from face_locker.protocol import LineBuffer, decode, encode
from face_locker.serial_service import SerialService


def wait_for(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Condition not reached before timeout")


class SerialTests(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = os.openpty()
        self.cfg = Settings(serial_port=os.ttyname(self.slave), ack_timeout=.2, telemetry_timeout=.3)
        self.events, self.received = [], []
        self.respond = True
        self.acknowledge = True
        self.complete = True
        self.end = threading.Event()
        self.peer = threading.Thread(target=self.external_peer, daemon=True)
        self.peer.start()
        self.service = SerialService(self.cfg, lambda kind, value: self.events.append((kind, value)))
        self.service.start()
        wait_for(lambda: self.service.snapshot()["telemetry_fresh"])

    def tearDown(self):
        self.service.stop()
        self.end.set()
        self.peer.join(timeout=1)
        os.close(self.master)
        os.close(self.slave)

    def external_peer(self):
        parser = LineBuffer()
        last_status = 0
        while not self.end.is_set():
            if self.respond and time.monotonic() - last_status > .1:
                os.write(self.master, encode({"v": 1, "type": "status", "lock_id": 1,
                                             "battery_voltage": 12.1, "actuator": "unknown", "door": "unknown"}))
                last_status = time.monotonic()
            if select.select([self.master], [], [], .02)[0]:
                for line in parser.feed(os.read(self.master, 4096)):
                    try:
                        value = decode(line)
                    except ValueError:
                        continue
                    self.received.append(value)
                    if value.get("type") == "unlock" and self.acknowledge:
                        os.write(self.master, encode({"v": 1, "type": "ack", "lock_id": 1,
                                                     "request_id": value["request_id"], "accepted": True}))
                        if self.complete:
                            os.write(self.master, encode({"v": 1, "type": "result", "lock_id": 1,
                                                         "request_id": value["request_id"], "result": "completed"}))

    def test_open_exchange_and_correlate_completion(self):
        self.service.unlock("one")
        wait_for(lambda: any(v.get("result") == "completed" for _, v in self.events))
        unlocks = [v for v in self.received if v.get("type") == "unlock"]
        self.assertEqual(len(unlocks), 1)
        self.assertEqual(unlocks[0]["duration_ms"], 3000)
        self.assertIsNone(self.service.snapshot()["pending"])

    def test_missing_ack_times_out_without_resend(self):
        self.acknowledge = False
        self.service.unlock("lost-ack")
        with self.assertRaises(RuntimeError):
            self.service.unlock("duplicate")
        wait_for(lambda: any(v.get("result") == "unknown_timeout" for _, v in self.events))
        time.sleep(.3)
        self.assertEqual(sum(v.get("type") == "unlock" for v in self.received), 1)
        self.assertTrue(self.service.snapshot()["reconciliation_required"])
        with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
            self.service.unlock("too-soon")
        self.service._uncertain_until = time.monotonic() - 1
        self.service._receive({"v": 1, "type": "status", "lock_id": 1, "battery_voltage": 12.1,
                               "actuator": "secured", "door": "unknown"})
        self.assertFalse(self.service.snapshot()["reconciliation_required"])

    def test_stale_telemetry_disables_commands_and_hides_voltage(self):
        self.respond = False
        wait_for(lambda: not self.service.snapshot()["telemetry_fresh"])
        self.assertIsNone(self.service.snapshot()["battery_voltage"])
        self.assertEqual(self.service.snapshot()["actuator"], "unknown")
        with self.assertRaises(RuntimeError):
            self.service.unlock("unsafe")

    def test_wrong_request_ack_cannot_complete_command(self):
        self.acknowledge = False
        self.service.unlock("correct")
        self.service._receive({"v": 1, "type": "ack", "lock_id": 1, "request_id": "wrong", "accepted": True})
        self.assertEqual(self.service.snapshot()["pending"], "correct")
        self.assertFalse(self.service._pending["accepted"])
