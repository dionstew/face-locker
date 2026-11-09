from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import socket
import socketserver
import threading

from .core import ApplicationService
from .protocol import MAX_IPC_LINE, decode, encode


class Client:
    def __init__(self, socket_path, timeout=2):
        self.path, self.timeout = str(socket_path), timeout

    def call(self, command, **kwargs):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.path)
            connection.sendall(encode({"command": command, **kwargs}))
            with connection.makefile("rb") as reader:
                response = decode(reader.readline(MAX_IPC_LINE + 1), MAX_IPC_LINE)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Service request failed"))
        return response["data"]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        try:
            request = decode(self.rfile.readline(16385), 16384)
            data = self.server.application.dispatch(request)
            result = {"ok": True, "data": data}
        except (ValueError, TypeError, PermissionError, RuntimeError, KeyError) as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception:
            logging.exception("Service request failed")
            result = {"ok": False, "error": "Service request failed. Check service logs."}
        try:
            self.wfile.write(encode(result))
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 16


def run_service(settings):
    settings.prepare()
    os.umask(0o077)
    # Hold this descriptor for the entire service lifetime, including shutdown.
    with (settings.state_path / "service.lock").open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another face-locker service is already running")
        settings.socket_path.unlink(missing_ok=True)
        application = ApplicationService(settings)
        with Server(str(settings.socket_path), Handler) as server:
            server.application = application
            os.chmod(settings.socket_path, 0o600)
            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            server.timeout = 0.25
            application.start()
            try:
                while not stop.is_set():
                    server.handle_request()
            finally:
                application.stop()
                settings.socket_path.unlink(missing_ok=True)
