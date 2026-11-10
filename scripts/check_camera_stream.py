#!/usr/bin/env python3
"""Exercise UI → service → real OpenCV/FFmpeg capture using a local MJPEG stream."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from face_locker.config import load_settings
from face_locker.ipc import Client
from face_locker.qt import QtWidgets as W
from face_locker.ui import MainWindow, CameraSourceDialog


def main():
    import cv2
    import numpy as np
    image = np.full((480, 640, 3), (35, 60, 40), dtype=np.uint8)
    cv2.putText(image, "LOCAL CAMERA STREAM TEST", (65, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (200, 240, 180), 2)
    jpeg = cv2.imencode(".jpg", image)[1].tobytes()
    stop_stream = threading.Event()

    class Stream(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while not stop_stream.is_set():
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                                     str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                    self.wfile.flush()
                    stop_stream.wait(.07)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stream)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    address = f"http://127.0.0.1:{server.server_port}/video"
    app = W.QApplication([])
    with tempfile.TemporaryDirectory(prefix="fl-camera-") as state:
        command = [sys.executable, str(ROOT / "app.py"), "--demo", "--service", "--state-dir", state]
        process = subprocess.Popen(command)
        client = Client(Path(state) / "service.sock", timeout=.8)
        window = None

        def wait_for(predicate, timeout=10):
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                app.processEvents()
                try:
                    if predicate():
                        return
                except (OSError, RuntimeError):
                    pass
                time.sleep(.02)
            raise AssertionError("Camera integration condition timed out")

        try:
            wait_for(lambda: client.call("status")["camera_ready"])
            window = MainWindow(load_settings(demo=True, state_dir=state), windowed=True)
            wait_for(lambda: window.connected)
            dialog = CameraSourceDialog("demo", True, window)
            dialog.kind.setCurrentIndex(dialog.kind.findData("network"))
            dialog.address.setText(address)
            dialog.validate_source()
            assert dialog.source == address
            client.call("configure_camera", source=dialog.source)
            wait_for(lambda: client.call("status")["camera_ready"] and not client.call("status")["simulated_camera"])
            window.navigate(1)
            wait_for(lambda: window.preview_button.isEnabled())
            window.preview_button.click()
            wait_for(lambda: window.camera_view.original is not None)
            snapshot = client.call("status")
            assert snapshot["demo"] and snapshot["mode"] == "demo"
            assert snapshot["controller"]["demo"] and snapshot["controller"]["telemetry_fresh"]
            assert snapshot["state"] == "previewing"
            assert not window.scenario.isVisible()
            output = ROOT / "artifacts"
            output.mkdir(exist_ok=True)
            window.grab().save(str(output / "camera-stream.png"))
            window.resize(800, 480)
            app.processEvents()
            window.grab().save(str(output / "camera-stream-800x480.png"))
            client.call("standby")
            window.close()
            window = None
            process.terminate()
            process.wait(timeout=12)
            # Source selected in the UI survives service restarts.
            process = subprocess.Popen(command)
            wait_for(lambda: client.call("status")["camera_ready"] and not client.call("status")["simulated_camera"])
            client.call("preview")
            wait_for(lambda: client.call("status", with_frame=True)["frame"] is not None)
            # Disconnect during capture: fault is bounded and serial remains responsive.
            stop_stream.set()
            wait_for(lambda: client.call("status")["state"] == "fault", timeout=8)
            assert client.call("status")["controller"]["telemetry_fresh"]
            client.call("standby")
            client.call("configure_camera", source="demo")
            wait_for(lambda: client.call("status")["camera_ready"] and client.call("status")["simulated_camera"])
            # Repeated failed opens must return diagnostics without aborting the service/UI.
            for _ in range(3):
                client.call("standby")
                client.call("configure_camera", source="/dev/video999999")
                wait_for(lambda: client.call("status")["camera_ready"])
                client.call("preview")
                wait_for(lambda: client.call("status")["state"] == "fault")
                assert "/dev/video999999 was not found" in client.call("status")["message"]
                assert client.call("status")["controller"]["telemetry_fresh"]
            print("PASS: source UI, real FFmpeg stream capture, preview, concurrent serial, persistence, disconnect recovery, and return to simulated camera")
        finally:
            if window:
                window.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=12)
            stop_stream.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
