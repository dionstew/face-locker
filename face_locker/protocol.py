"""Bounded newline-delimited JSON protocol. This module contains no actuator code."""
import json
import math

MAX_SERIAL_LINE = 4096
MAX_IPC_LINE = 2_000_000


class LineBuffer:
    """Drop the entire oversized frame, including its tail through the newline."""
    def __init__(self, limit=MAX_SERIAL_LINE):
        self.limit = limit
        self.buffer = bytearray()
        self.discarding = False

    def feed(self, chunk):
        lines = []
        for byte in chunk:
            if self.discarding:
                if byte == 10:
                    self.discarding = False
                continue
            self.buffer.append(byte)
            if len(self.buffer) > self.limit:
                self.buffer.clear()
                self.discarding = byte != 10
            elif byte == 10:
                lines.append(bytes(self.buffer))
                self.buffer.clear()
        return lines


def encode(message):
    return (json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def decode(data, limit=MAX_SERIAL_LINE):
    if len(data) > limit or not data.endswith(b"\n"):
        raise ValueError("Incomplete or oversized message")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return value


def validate_controller(value, lock_id):
    if value.get("v") != 1 or type(value.get("lock_id")) is not int or value["lock_id"] != lock_id:
        raise ValueError("Unsupported version or lock ID")
    kind = value.get("type")
    if kind not in ("status", "ack", "result", "pong"):
        raise ValueError("Unsupported controller message")
    if kind in ("ack", "result"):
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
            raise ValueError("Invalid request ID")
    if kind == "ack" and type(value.get("accepted")) is not bool:
        raise ValueError("ack.accepted must be boolean")
    if kind == "result" and value.get("result") not in ("completed", "failed"):
        raise ValueError("Invalid result")
    if kind == "status":
        for key, choices in (("actuator", ("secured", "released", "unknown")),
                             ("door", ("open", "closed", "unknown"))):
            if value.get(key) not in choices:
                raise ValueError(f"Invalid {key}")
        voltage = value.get("battery_voltage")
        if voltage is not None and (type(voltage) not in (int, float) or not math.isfinite(voltage) or not 0 <= voltage <= 100):
            raise ValueError("Invalid battery voltage")
    return value
