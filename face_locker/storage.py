from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS face_templates (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    embedding TEXT NOT NULL, model_version TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_attempts (
    id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    name TEXT, started_at TEXT NOT NULL, completed_at TEXT, decision TEXT NOT NULL,
    reason TEXT NOT NULL, score REAL
);
CREATE INDEX IF NOT EXISTS attempts_date ON access_attempts(started_at);
CREATE TABLE IF NOT EXISTS lock_commands (
    request_id TEXT PRIMARY KEY, attempt_id TEXT REFERENCES access_attempts(id),
    sent_at TEXT NOT NULL, ack_at TEXT, result TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS device_events (
    id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, source TEXT NOT NULL,
    event_type TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry_samples (
    id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, battery_voltage REAL,
    actuator TEXT NOT NULL, door TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        # Pending unlocks from a previous process are never replayed.
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE lock_commands SET result='unknown_after_restart', completed_at=? "
                "WHERE result IN ('pending','accepted')", (utc_now(),))
            self.connection.execute(
                "UPDATE access_attempts SET decision='interrupted', reason='Service restarted', "
                "completed_at=? WHERE completed_at IS NULL", (utc_now(),))

    def close(self):
        with self.lock:
            self.connection.close()

    def event(self, source, event_type, details=""):
        with self.lock, self.connection:
            self.connection.execute("INSERT INTO device_events(timestamp,source,event_type,details) VALUES(?,?,?,?)",
                                    (utc_now(), source, event_type, details))

    def add_user(self, name, embeddings, model_version):
        user_id = secrets.token_hex(8)
        with self.lock, self.connection:
            self.connection.execute("INSERT INTO users(id,name,created_at) VALUES(?,?,?)",
                                    (user_id, name, utc_now()))
            self.connection.executemany(
                "INSERT INTO face_templates(user_id,embedding,model_version,created_at) VALUES(?,?,?,?)",
                [(user_id, json.dumps(list(map(float, e))), model_version, utc_now()) for e in embeddings])
        return user_id

    def users(self):
        with self.lock:
            return [dict(r) for r in self.connection.execute(
                "SELECT u.*, COUNT(f.id) AS samples FROM users u LEFT JOIN face_templates f "
                "ON f.user_id=u.id GROUP BY u.id ORDER BY u.name")]

    def templates(self, model_version):
        with self.lock:
            return [(r["id"], r["name"], json.loads(r["embedding"])) for r in self.connection.execute(
                "SELECT u.id,u.name,f.embedding FROM users u JOIN face_templates f ON u.id=f.user_id "
                "WHERE u.active=1 AND f.model_version=?", (model_version,))]

    def active(self, user_id):
        with self.lock:
            row = self.connection.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
            return bool(row and row[0])

    def delete_user(self, user_id):
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM users WHERE id=?", (user_id,))

    def set_active(self, user_id, active):
        with self.lock, self.connection:
            self.connection.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))

    def begin_attempt(self, attempt_id):
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO access_attempts(id,started_at,decision,reason) VALUES(?,?,'scanning','Scanning')",
                (attempt_id, utc_now()))

    def finish_attempt(self, attempt_id, decision, reason, user_id=None, name=None, score=None):
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE access_attempts SET completed_at=?,decision=?,reason=?,user_id=?,name=?,score=? WHERE id=?",
                (utc_now(), decision, reason, user_id, name, score, attempt_id))

    def record_command(self, request_id, attempt_id):
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO lock_commands(request_id,attempt_id,sent_at,result) VALUES(?,?,?,'pending')",
                (request_id, attempt_id, utc_now()))

    def command_result(self, request_id, result):
        with self.lock, self.connection:
            if result == "accepted":
                self.connection.execute("UPDATE lock_commands SET result=?,ack_at=? WHERE request_id=?",
                                        (result, utc_now(), request_id))
            else:
                self.connection.execute("UPDATE lock_commands SET result=?,completed_at=? WHERE request_id=?",
                                        (result, utc_now(), request_id))

    def telemetry(self, values):
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO telemetry_samples(timestamp,battery_voltage,actuator,door) VALUES(?,?,?,?)",
                (utc_now(), values.get("battery_voltage"), values.get("actuator", "unknown"), values.get("door", "unknown")))
            self.connection.execute("DELETE FROM telemetry_samples WHERE id NOT IN "
                                    "(SELECT id FROM telemetry_samples ORDER BY id DESC LIMIT 10000)")

    def history(self, day, zone):
        selected = date.fromisoformat(day)
        tz = ZoneInfo(zone)
        start = datetime.combine(selected, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(selected + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        with self.lock:
            rows = self.connection.execute(
                "SELECT a.*,c.result AS controller_result FROM access_attempts a LEFT JOIN lock_commands c "
                "ON c.attempt_id=a.id WHERE a.started_at>=? AND a.started_at<? ORDER BY a.started_at DESC LIMIT 500",
                (start.isoformat(timespec="milliseconds"), end.isoformat(timespec="milliseconds")))
            return [dict(r) for r in rows]

    def today_counts(self, zone):
        tz = ZoneInfo(zone)
        day = datetime.now(tz).date()
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(decision IN ('authorized','observed','demo')),0) "
                "FROM access_attempts WHERE started_at>=? AND started_at<?",
                (start.isoformat(timespec="milliseconds"), end.isoformat(timespec="milliseconds"))).fetchone()
            return {"attempts": row[0], "authorized": row[1]}

    def events(self, day, zone):
        selected, tz = date.fromisoformat(day), ZoneInfo(zone)
        start = datetime.combine(selected, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(selected + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        with self.lock:
            return [dict(row) for row in self.connection.execute(
                "SELECT * FROM device_events WHERE timestamp>=? AND timestamp<? ORDER BY timestamp DESC LIMIT 500",
                (start.isoformat(timespec="milliseconds"), end.isoformat(timespec="milliseconds")))]

    def set_pin(self, pin):
        if not pin.isdigit() or not 6 <= len(pin) <= 12:
            raise ValueError("Use a PIN containing 6–12 digits")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 250000).hex()
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('admin_pin',?)",
                                    (json.dumps({"salt": salt, "digest": digest}),))

    def has_pin(self):
        with self.lock:
            return self.connection.execute("SELECT 1 FROM settings WHERE key='admin_pin'").fetchone() is not None

    def verify_pin(self, pin):
        with self.lock:
            row = self.connection.execute("SELECT value FROM settings WHERE key='admin_pin'").fetchone()
        if not row:
            return False
        record = json.loads(row[0])
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(record["salt"]), 250000).hex()
        return hmac.compare_digest(digest, record["digest"])
