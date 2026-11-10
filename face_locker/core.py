from __future__ import annotations

import base64
import logging
import secrets
import threading
import time
from dataclasses import replace

from .camera import describe_source, normalize_source, source_key
from .serial_service import SerialService
from .storage import Database
from .vision import DemoVision, PiVision, Observation, best_match

log = logging.getLogger(__name__)


class ApplicationService:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings.state_path / "locker.sqlite3")
        saved_source = self.db.get_setting("camera_source")
        if saved_source and not settings._camera_override:
            source = normalize_source(saved_source)
            if source != "demo" or settings.demo:
                settings.camera_source = source
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.serial = SerialService(settings, self._hardware_event)
        self.vision = None
        self.vision_error = "Loading camera models…"
        self.model_version = ""
        self.templates = []
        self.state = "booting"
        self.message = "Starting services…"
        self.session = None
        self.frame = None
        self.frame_id = 0
        self.result_until = 0.0
        self.admin_sessions = {}
        self.pin_failures = 0
        self.pin_blocked_until = 0.0
        self.last_telemetry_saved = 0.0
        self.demo_scenario = "known"
        self.camera_revision = 0
        self.camera_changed = threading.Event()
        self.thread = threading.Thread(target=self._vision_loop, name="vision-worker", daemon=True)
        if settings.demo and not self.db.has_pin():
            self.db.set_pin("123456")
        self.db.event("service", "startup", "demo" if settings.demo else "hardware")

    def start(self):
        self.serial.start()
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.camera_changed.set()
        with self.lock:
            self._cancel("Service stopped")
        self.thread.join(timeout=5)
        self.serial.stop()
        # Do not close a database still in use by a hung hardware worker.
        if not self.thread.is_alive():
            self.db.close()

    def _refresh_templates(self):
        self.templates = self.db.templates(self.model_version)

    def _is_admin(self, token):
        return isinstance(token, str) and self.admin_sessions.get(token, 0) > time.monotonic()

    def _require_admin(self, token):
        if not self._is_admin(token):
            raise PermissionError("Administrator session expired. Sign in again.")

    def dispatch(self, request):
        command = request.get("command")
        with self.lock:
            if command == "status":
                return self.snapshot(bool(request.get("with_frame")))
            if command == "history":
                return self.db.history(str(request.get("day", "")), self.settings.timezone)
            if command == "events":
                return self.db.events(str(request.get("day", "")), self.settings.timezone)
            if command == "login":
                now = time.monotonic()
                if now < self.pin_blocked_until:
                    raise PermissionError("Too many attempts. Try again in 30 seconds.")
                pin = request.get("pin", "")
                if not isinstance(pin, str) or len(pin) > 12 or not self.db.verify_pin(pin):
                    self.pin_failures += 1
                    if self.pin_failures >= 5:
                        self.pin_blocked_until, self.pin_failures = now + 30, 0
                    raise PermissionError("Incorrect PIN or administrator PIN has not been configured")
                self.pin_failures = 0
                self.admin_sessions = {k: v for k, v in self.admin_sessions.items() if v > now}
                token = secrets.token_urlsafe(32)
                self.admin_sessions[token] = now + 900
                return {"token": token}
            if command == "logout":
                token = request.get("token")
                if self.session and self.session.get("admin_token") == token:
                    self._cancel("Enrollment cancelled")
                self.admin_sessions.pop(token, None)
                return {}
            if command == "scan":
                self._begin("scan")
                return {}
            if command == "preview":
                self._begin("preview")
                return {}
            if command in ("camera_settings", "configure_camera"):
                if not self.settings.demo:
                    self._require_admin(request.get("token"))
                if command == "camera_settings":
                    return {"source": self.settings.camera_source}
                if self.session or self.serial.snapshot()["pending"] or self.state == "unlocking":
                    raise RuntimeError("Enter standby before changing the camera source")
                source = normalize_source(request.get("source"))
                if source == "demo" and not self.settings.demo:
                    raise ValueError("The simulated camera is only available with --demo")
                self.db.set_setting("camera_source", source)
                self.settings.camera_source = source
                self.camera_revision += 1
                self.camera_changed.set()
                self.vision, self.frame = None, None
                self.vision_error = "Loading camera source…"
                self.state, self.message = "booting", "Loading camera source…"
                self.db.event("camera", "source_changed", describe_source(source))
                return {}
            if command == "standby":
                self._cancel("Standby · tap Start recognition when ready")
                return {}
            if command == "demo_scenario" and self.settings.demo:
                scenario = request.get("scenario")
                if scenario not in ("known", "unknown", "spoof", "no_face"):
                    raise ValueError("Unknown demo scenario")
                self.demo_scenario = scenario
                return {}
            if command in ("users", "enroll", "delete_user", "set_active"):
                token = request.get("token")
                self._require_admin(token)
                if command == "users":
                    return self.db.users()
                if command == "enroll":
                    name = request.get("name", "")
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
                        raise ValueError("Enter a name between 1 and 60 characters")
                    if any(ord(c) < 32 for c in name):
                        raise ValueError("Name contains unsupported characters")
                    self._begin("enroll", name=name.strip(), token=token)
                    return {}
                user_id = request.get("user_id")
                if not isinstance(user_id, str):
                    raise ValueError("Invalid user")
                if self.session:
                    raise RuntimeError("Finish the current session before changing users")
                if command == "delete_user":
                    self.db.delete_user(user_id)
                else:
                    if type(request.get("active")) is not bool:
                        raise ValueError("active must be boolean")
                    self.db.set_active(user_id, request["active"])
                self.db.event("admin", command, user_id)
                self._refresh_templates()
                return {}
            raise ValueError("Unknown command")

    def _begin(self, kind, name=None, token=None):
        if self.session or self.serial.snapshot()["pending"]:
            raise RuntimeError("An operation is already in progress")
        if not self.vision or self.vision_error:
            raise RuntimeError(self.vision_error or "Camera is not ready")
        if kind != "preview" and getattr(self.vision, "recognition_error", ""):
            raise RuntimeError(self.vision.recognition_error)
        if kind == "scan" and not self.templates:
            raise RuntimeError("No enrolled users. Open Administration to enroll someone.")
        session_id = secrets.token_hex(16)
        if kind == "scan":
            self.db.begin_attempt(session_id)
        self.session = {"id": session_id, "kind": kind, "started": time.monotonic(),
                        "candidate": None, "count": 0, "unknown": 0, "spoof": 0,
                        "samples": [], "last_sample": 0.0, "name": name, "admin_token": token}
        self.state = {"enroll": "enrolling", "scan": "scanning", "preview": "previewing"}[kind]
        self.message = "Opening camera…" if kind == "preview" else "Look at the camera and hold still"
        self.frame = None

    def _cancel(self, message):
        if self.session and self.session["kind"] == "scan":
            self.db.finish_attempt(self.session["id"], "cancelled", message)
        self.session = None
        self.frame = None
        self.state, self.message = "idle", message

    def _complete(self, state, message, decision=None, match=None):
        if self.session and self.session["kind"] == "scan":
            kwargs = dict(zip(("user_id", "name", "score"), match)) if match else {}
            self.db.finish_attempt(self.session["id"], decision or state, message, **kwargs)
        self.session = None
        self.state, self.message = state, message
        self.result_until = time.monotonic() + self.settings.result_seconds

    def snapshot(self, with_frame=False):
        controller = self.serial.snapshot()
        return {
            "state": self.state, "message": self.message, "demo": self.settings.demo,
            "camera_ready": bool(self.vision) and not self.vision_error,
            "camera_error": self.vision_error,
            "recognition_ready": bool(self.vision) and not self.vision_error and not getattr(self.vision, "recognition_error", ""),
            "recognition_error": getattr(self.vision, "recognition_error", ""),
            "camera_source_label": describe_source(self.settings.camera_source),
            "camera_source_key": source_key(self.settings.camera_source),
            "simulated_camera": self.settings.camera_source == "demo",
            "mode": "demo" if self.settings.demo else (
                "live" if self.settings.unlock_enabled and self.vision and self.vision.pad is not None else "observation"),
            "controller": controller, "users_count": len({t[0] for t in self.templates}),
            "today": self.db.today_counts(self.settings.timezone), "timezone": self.settings.timezone,
            "admin_configured": self.db.has_pin(), "demo_scenario": self.demo_scenario,
            "session": {"kind": self.session["kind"], "samples": len(self.session["samples"]),
                        "target": self.settings.enrollment_samples} if self.session else None,
            "frame_id": self.frame_id,
            "frame": base64.b64encode(self.frame).decode() if with_frame and self.frame else None,
        }

    def _hardware_event(self, kind, value):
        with self.lock:
            if kind == "telemetry":
                now = time.monotonic()
                if now - self.last_telemetry_saved > 60:
                    self.db.telemetry(value)
                    self.last_telemetry_saved = now
            elif kind == "device":
                self.db.event("controller", value["event"], value["details"])
            elif kind == "command":
                self.db.command_result(value["request_id"], value["result"])
                self.db.event("controller", value["result"], value["request_id"])
                if self.state == "unlocking":
                    result = value["result"]
                    if result == "accepted":
                        self.message = "Request accepted · waiting for controller result"
                    else:
                        good = result == "completed"
                        self.state = "success" if good else "fault"
                        prefix = "Demo · " if self.settings.demo else ""
                        self.message = prefix + ("Controller reports operation complete" if good else
                                                 "Lock response: " + result.replace("_", " ") + ". Check hardware status.")
                        self.result_until = time.monotonic() + self.settings.result_seconds

    def _vision_loop(self):
        vision = None
        revision = -1
        while not self.stop_event.is_set():
            with self.lock:
                current_revision = self.camera_revision
                camera_settings = replace(self.settings)
            if revision != current_revision:
                if vision:
                    try:
                        vision.stop()
                    except Exception:
                        log.exception("Previous camera close failed")
                vision = None
                revision = current_revision
                self.camera_changed.clear()
            if vision is None:
                try:
                    vision = DemoVision(camera_settings) if camera_settings.camera_source == "demo" else PiVision(camera_settings)
                    with self.lock:
                        if revision != self.camera_revision:
                            continue
                        self.vision, self.vision_error = vision, ""
                        self.model_version = vision.model_version
                        self._refresh_templates()
                        if isinstance(vision, DemoVision) and not self.db.has_templates(vision.model_version):
                            self.db.add_user("Alex Morgan", [[1.0] + [0.0] * 127] * 5, vision.model_version)
                            self._refresh_templates()
                        self.state, self.message = "idle", "Ready when you are"
                except Exception as exc:
                    with self.lock:
                        self.vision_error = str(exc)
                        self.state, self.message = "unavailable", str(exc)
                    vision = None
                    self.camera_changed.wait(5)
                    self.camera_changed.clear()
                    continue
            with self.lock:
                session_id = self.session["id"] if self.session else None
                session_kind = self.session["kind"] if self.session else None
                if self.state in ("success", "denied", "fault", "observed") and time.monotonic() > self.result_until:
                    self.state, self.message, self.frame = "idle", "Ready when you are", None
                scenario = self.demo_scenario
            if session_id is None:
                try:
                    vision.stop()
                except Exception:
                    log.exception("Camera close failed")
                self.stop_event.wait(0.1)
                continue
            try:
                if isinstance(vision, DemoVision):
                    vision.scenario = scenario
                vision.start()
                frame = vision.capture()
                observation = (Observation(hint="Camera preview · recognition is paused")
                               if session_kind == "preview" else vision.analyze(frame))
                import cv2
                if observation.box:
                    x, y, w, h = observation.box
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (170, 220, 100), 2)
                ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                with self.lock:
                    if self.session and self.session["id"] == session_id:
                        if ok:
                            self.frame, self.frame_id = jpeg.tobytes(), self.frame_id + 1
                        self._process_observation(observation)
            except Exception as exc:
                log.exception("Camera/recognition error")
                with self.lock:
                    if revision == self.camera_revision and self.session and self.session["id"] == session_id:
                        self.vision_error = str(exc)
                        self._complete("fault", "Camera error: " + str(exc), "error")
                        self.vision = None
                try:
                    vision.stop()
                except Exception:
                    log.exception("Camera close failed")
                vision = None
                self.stop_event.wait(2)
            self.stop_event.wait(0.15)
        if vision:
            try:
                vision.stop()
            except Exception:
                log.exception("Camera close failed")

    def _process_observation(self, observation):
        session, cfg = self.session, self.settings
        now = time.monotonic()
        limit = cfg.enrollment_timeout if session["kind"] in ("enroll", "preview") else cfg.scan_timeout
        if now - session["started"] > limit:
            self._complete("denied", "Session timed out · please try again", "timeout")
            return
        self.message = observation.hint
        if session["kind"] == "preview":
            return
        if observation.embedding is None:
            session["candidate"], session["count"], session["unknown"], session["spoof"] = None, 0, 0, 0
            return
        if session["kind"] == "enroll":
            if not self._is_admin(session["admin_token"]):
                self._cancel("Administrator session expired")
                return
            if self.vision.pad is not None and not observation.live:
                self.message = "Enrollment requires a successful liveness check"
                return
            if now - session["last_sample"] < 0.8:
                return
            import numpy as np
            if session["samples"] and float(np.dot(session["samples"][0], observation.embedding)) < cfg.match_threshold:
                self.message = "Keep the same person in view for all samples"
                return
            duplicate = best_match(observation.embedding, self.templates, cfg.match_threshold, cfg.match_margin)
            if duplicate:
                self._complete("denied", f"This face already belongs to {duplicate[1]}")
                return
            session["samples"].append(observation.embedding.tolist())
            session["last_sample"] = now
            count = len(session["samples"])
            self.message = f"Sample {count}/{cfg.enrollment_samples} · vary your angle slightly"
            if count >= cfg.enrollment_samples:
                user_id = self.db.add_user(session["name"], session["samples"], self.model_version)
                self.db.event("admin", "enrolled", user_id)
                self._refresh_templates()
                self._complete("success", f"{session['name']} enrolled successfully")
            return
        match = best_match(observation.embedding, self.templates, cfg.match_threshold, cfg.match_margin)
        if not match:
            session["candidate"], session["count"], session["spoof"] = None, 0, 0
            session["unknown"] += 1
            self.message = "Checking identity…"
            if session["unknown"] >= cfg.consistent_frames + 2:
                self._complete("denied", "Face not recognized", "unknown")
            return
        session["unknown"] = 0
        if self.vision.pad is not None and not observation.live:
            session["candidate"], session["count"] = None, 0
            session["spoof"] += 1
            self.message = "Liveness check not passed"
            if session["spoof"] >= cfg.consistent_frames:
                self._complete("denied", "Liveness check failed", "liveness_failed")
            return
        session["spoof"] = 0
        session["count"] = min(session["count"] + 1, cfg.consistent_frames) if session["candidate"] == match[0] else 1
        session["candidate"] = match[0]
        self.message = f"Verifying identity · {session['count']}/{cfg.consistent_frames}"
        if session["count"] < cfg.consistent_frames or now - session["started"] < 1.5:
            return
        if not self.db.active(match[0]):
            self._complete("denied", "Access is disabled for this user", "disabled")
            return
        if not cfg.demo and (not cfg.unlock_enabled or self.vision.pad is None):
            self._complete("observed", f"Recognized {match[1]} · observation mode, no unlock sent", "observed", match)
            return
        request_id = secrets.token_hex(16)
        self.db.record_command(request_id, session["id"])
        try:
            self.serial.unlock(request_id)
        except RuntimeError as exc:
            self.db.command_result(request_id, "not_sent")
            self._complete("fault", str(exc), "controller_unavailable", match)
            return
        self._complete("unlocking", f"Welcome, {match[1]} · requesting access…",
                       "demo" if cfg.demo else "authorized", match)
