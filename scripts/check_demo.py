#!/usr/bin/env python3
"""Headless end-to-end UI/service check; saves touchscreen screenshots."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from face_locker.config import load_settings
from face_locker.ipc import Client
from face_locker.qt import QtWidgets as W
from face_locker.ui import MainWindow


def main():
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fl-demo-") as state:
        process = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--demo", "--service", "--state-dir", state])
        app = W.QApplication([])
        window = None
        settings = load_settings(demo=True, state_dir=state)
        client = Client(settings.socket_path, timeout=.5)

        def wait_for(predicate, timeout=6):
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                app.processEvents()
                try:
                    if predicate():
                        return
                except (OSError, RuntimeError):
                    pass
                time.sleep(.02)
            raise AssertionError("End-to-end condition timed out")

        try:
            wait_for(lambda: client.call("status")["camera_ready"])
            window = MainWindow(settings, windowed=True)
            wait_for(lambda: window.connected and window.start_button.isEnabled())
            window.grab().save(str(output / "dashboard.png"))
            window.resize(800, 480)
            app.processEvents()
            print("Compact size:", window.width(), window.height(), "minimum:", window.minimumSizeHint().width(), window.minimumSizeHint().height())
            window.grab().save(str(output / "dashboard-800x480.png"))
            window.resize(1024, 600)
            window.start_button.click()
            wait_for(lambda: window.camera_view.original is not None)
            window.grab().save(str(output / "camera.png"))
            wait_for(lambda: client.call("status")["state"] == "success")
            window.navigate(2)
            wait_for(lambda: window.history_table.rowCount() == 1)
            window.grab().save(str(output / "calendar.png"))
            assert window.history_records[0]["controller_result"] == "completed"
            client.call("standby")
            client.call("demo_scenario", scenario="unknown")
            window.start_scan()
            wait_for(lambda: client.call("status")["state"] == "denied")
            token = client.call("login", pin="123456")["token"]
            window.on_response("login", {"token": token})
            window.navigate(3)
            wait_for(lambda: window.users_table.rowCount() == 1)
            window.grab().save(str(output / "administration.png"))
            window.sleep_display()
            app.processEvents()
            assert window.overlay.isVisible()
            window.wake_display()
            assert not window.overlay.isVisible()
            assert window.pages.currentIndex() == 0
            print("PASS: dashboard, camera, known/unknown recognition, serial completion, calendar, admin, display sleep/wake")
        finally:
            if window:
                window.close()
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
