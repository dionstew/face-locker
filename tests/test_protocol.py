import unittest

from face_locker.protocol import LineBuffer, decode, encode, validate_controller


class ProtocolTests(unittest.TestCase):
    def test_partial_and_multiple_frames(self):
        parser = LineBuffer()
        self.assertEqual(parser.feed(b'{"v":'), [])
        self.assertEqual([decode(x) for x in parser.feed(b'1}\n{"v":2}\n')], [{"v": 1}, {"v": 2}])

    def test_oversized_frame_drops_tail(self):
        parser = LineBuffer(20)
        self.assertEqual(parser.feed(b"x" * 100 + b'{"v":1}\n'), [])
        self.assertEqual([decode(x) for x in parser.feed(b'{"v":2}\n')], [{"v": 2}])

    def test_reject_wrong_controller_and_malformed_values(self):
        valid = {"v": 1, "type": "status", "lock_id": 1, "actuator": "unknown", "door": "unknown", "battery_voltage": 12.4}
        self.assertEqual(validate_controller(valid, 1), valid)
        for changes in ({"lock_id": 2}, {"battery_voltage": float("nan")}, {"battery_voltage": True},
                        {"actuator": "open"}, {"type": "unlock"}, {"v": 2}):
            with self.assertRaises(ValueError):
                validate_controller({**valid, **changes}, 1)
        with self.assertRaises(ValueError):
            validate_controller({"v": 1, "type": "ack", "lock_id": 1, "request_id": "x", "accepted": "true"}, 1)

    def test_encoder_rejects_nonfinite_numbers(self):
        with self.assertRaises(ValueError):
            encode({"voltage": float("nan")})
