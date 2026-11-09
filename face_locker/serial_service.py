from __future__ import annotations

import logging
import queue
import threading
import time

from .protocol import LineBuffer, decode, encode, validate_controller

log = logging.getLogger(__name__)


class SerialService:
    """Single serial owner, one in-flight unlock, no automatic unlock retries."""
    def __init__(self, settings, on_event=lambda *_: None, serial_factory=None):
        self.settings = settings
        self.on_event = on_event
        self.serial_factory = serial_factory
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._outgoing = queue.Queue(maxsize=8)
        self._transport_open = False
        self._last_rx = 0.0
        self._last_status = 0.0
        self._pending = None
        self._reconcile = False
        self._uncertain_until = 0.0
        self._values = {"battery_voltage": None, "actuator": "unknown", "door": "unknown"}
        self._error = "Waiting for controller"
        self.thread = threading.Thread(target=self._run, name="serial-service", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread.ident:
            self.thread.join(timeout=3)

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            connected = self._transport_open and now - self._last_rx < self.settings.telemetry_timeout
            fresh = connected and now - self._last_status < self.settings.telemetry_timeout
            return {
                "connected": connected, "telemetry_fresh": fresh,
                "battery_voltage": self._values["battery_voltage"] if fresh else None,
                "actuator": self._values["actuator"] if fresh else "unknown",
                "door": self._values["door"] if fresh else "unknown",
                "age_seconds": round(now - self._last_status, 1) if self._last_status else None,
                "pending": self._pending["request_id"] if self._pending else None,
                "error": "" if fresh else self._error or "Controller status is stale",
                "demo": self.settings.demo,
                "reconciliation_required": self._reconcile,
            }

    def unlock(self, request_id):
        with self._lock:
            if not self.snapshot()["telemetry_fresh"]:
                raise RuntimeError("Controller is unavailable or its status is stale")
            if self._pending:
                raise RuntimeError("A lock request is already in progress")
            if self._reconcile:
                raise RuntimeError("Previous command outcome unknown; waiting for a fresh secured controller status")
            if self._values["actuator"] == "released":
                raise RuntimeError("Controller reports the actuator is already released")
            command = {"v": 1, "type": "unlock", "request_id": request_id,
                       "lock_id": self.settings.lock_id, "duration_ms": self.settings.unlock_ms}
            self._pending = {"request_id": request_id, "sent": time.monotonic(), "accepted": False}
            try:
                self._outgoing.put_nowait(command)
            except queue.Full:
                self._pending = None
                raise RuntimeError("Serial queue is full")

    def _finish(self, result):
        with self._lock:
            pending, self._pending = self._pending, None
            if pending and result.startswith("unknown"):
                self._reconcile = True
                self._uncertain_until = pending["sent"] + self.settings.unlock_ms / 1000 + 2
        if pending:
            self.on_event("command", {"request_id": pending["request_id"], "result": result})

    def _receive(self, value):
        validate_controller(value, self.settings.lock_id)
        kind = value["type"]
        with self._lock:
            self._last_rx = time.monotonic()
            if kind == "status":
                self._values = {key: value.get(key) for key in self._values}
                self._last_status = self._last_rx
                self._error = ""
                if self._reconcile and self._last_rx >= self._uncertain_until and value["actuator"] == "secured":
                    self._reconcile = False
            pending = dict(self._pending) if self._pending else None
        if kind == "status":
            self.on_event("telemetry", dict(self._values))
        elif kind in ("ack", "result") and pending and value["request_id"] == pending["request_id"]:
            if kind == "ack":
                if value["accepted"]:
                    with self._lock:
                        if self._pending and self._pending["request_id"] == pending["request_id"]:
                            self._pending["accepted"] = True
                    self.on_event("command", {"request_id": pending["request_id"], "result": "accepted"})
                else:
                    self._finish("rejected")
            else:
                self._finish(value["result"])

    def _timeouts(self):
        with self._lock:
            pending = dict(self._pending) if self._pending else None
        if pending:
            deadline = (self.settings.unlock_ms / 1000 + self.settings.ack_timeout + 2
                        if pending["accepted"] else self.settings.ack_timeout)
            if time.monotonic() - pending["sent"] > deadline:
                self._finish("unknown_timeout")

    def _disconnected(self, error):
        with self._lock:
            self._transport_open = False
            self._last_rx = self._last_status = 0.0
            self._error = str(error)
        self._finish("unknown_disconnect")
        while True:
            try:
                self._outgoing.get_nowait()
            except queue.Empty:
                break

    def _run(self):
        if self.settings.demo:
            self._run_demo()
            return
        while not self._stop.is_set():
            port = None
            try:
                factory = self.serial_factory
                if factory is None:
                    from serial import Serial
                    factory = Serial
                port = factory(self.settings.serial_port, self.settings.baudrate,
                               timeout=0.05, write_timeout=0.5, exclusive=True)
                with self._lock:
                    self._transport_open = True
                self.on_event("device", {"event": "serial_open", "details": self.settings.serial_port})
                parser, last_query = LineBuffer(), 0.0
                while not self._stop.is_set():
                    now = time.monotonic()
                    if now - last_query >= 2:
                        self._write(port, {"v": 1, "type": "get_status", "lock_id": self.settings.lock_id})
                        last_query = now
                    try:
                        command = self._outgoing.get_nowait()
                    except queue.Empty:
                        command = None
                    if command:
                        self._write(port, command)
                    for line in parser.feed(port.read(512)):
                        try:
                            self._receive(decode(line))
                        except (ValueError, UnicodeError, TypeError):
                            log.warning("Ignoring malformed controller message")
                    self._timeouts()
            except Exception as exc:
                log.warning("Serial connection unavailable: %s", exc)
                self._disconnected(exc)
                self.on_event("device", {"event": "serial_disconnect", "details": str(exc)})
            finally:
                if port:
                    port.close()
            self._stop.wait(2)
        self._disconnected("Service stopped")

    @staticmethod
    def _write(port, value):
        data = encode(value)
        if port.write(data) != len(data):
            raise OSError("Incomplete serial write; command outcome is unknown")

    def _run_demo(self):
        with self._lock:
            self._transport_open = True
        last_status, release_until, command, sent = 0.0, 0.0, None, 0.0
        acked = False
        while not self._stop.wait(0.04):
            now = time.monotonic()
            if now - last_status > 0.5:
                self._receive({"v": 1, "type": "status", "lock_id": self.settings.lock_id,
                               "battery_voltage": 12.4, "actuator": "released" if now < release_until else "secured",
                               "door": "unknown"})
                last_status = now
            if command is None:
                try:
                    command = self._outgoing.get_nowait()
                    sent, acked = now, False
                except queue.Empty:
                    pass
            if command and now - sent > 0.15 and not acked:
                self._receive({"v": 1, "type": "ack", "lock_id": self.settings.lock_id,
                               "request_id": command["request_id"], "accepted": True})
                release_until, acked = now + self.settings.unlock_ms / 1000, True
            if command and now - sent > 0.7:
                self._receive({"v": 1, "type": "result", "lock_id": self.settings.lock_id,
                               "request_id": command["request_id"], "result": "completed"})
                command = None
            self._timeouts()
        self._disconnected("Service stopped")
