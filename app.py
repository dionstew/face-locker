#!/usr/bin/env python3
"""Face Locker entry point. Hardware controller firmware is deliberately external."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from face_locker.config import load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline Raspberry Pi face access manager")
    parser.add_argument("--config", help="Settings YAML (default: config/settings.yaml)")
    parser.add_argument("--state-dir", help="Override private database/socket directory")
    parser.add_argument("--demo", action="store_true", help="Simulated camera and serial transport; never opens hardware")
    parser.add_argument("--service", action="store_true", help="Run only the background application/serial service")
    parser.add_argument("--standalone", action="store_true", help="Start a private child service if one is not running")
    parser.add_argument("--windowed", action="store_true", help="Windowed UI instead of fullscreen kiosk")
    parser.add_argument("--set-admin-pin", action="store_true", help="Set administrator PIN interactively; service must be stopped")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config, demo=args.demo, state_dir=args.state_dir)
    settings.prepare()
    if args.set_admin_pin:
        import fcntl
        from face_locker.storage import Database
        with (settings.state_path / "service.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("Stop the application service before changing its administrator PIN")
            pin = getpass.getpass("New administrator PIN (6–12 digits): ")
            if pin != getpass.getpass("Confirm PIN: "):
                parser.error("PINs do not match")
            os.umask(0o077)
            db = Database(settings.state_path / "locker.sqlite3")
            try:
                db.set_pin(pin)
            finally:
                db.close()
            print("Administrator PIN saved.")
        return 0
    if args.service:
        from face_locker.ipc import run_service
        run_service(settings)
        return 0
    # QApplication can abort the process on missing platform libraries, skipping
    # finally blocks. Diagnose common loader failures before spawning a service.
    from face_locker.qt import check_runtime
    check_runtime()
    child = None
    if args.demo or args.standalone:
        from face_locker.ipc import Client
        client = Client(settings.socket_path, timeout=0.5)
        try:
            status = client.call("status")
            if status["demo"] != settings.demo:
                raise RuntimeError("Existing service mode differs from the requested mode")
        except (OSError, TimeoutError):
            command = [sys.executable, str(Path(__file__).resolve()), "--service", "--state-dir", str(settings.state_path)]
            if args.demo:
                command.append("--demo")
            if args.config:
                command.extend(["--config", str(Path(args.config).resolve())])
            child = subprocess.Popen(command)
            for _ in range(100):
                if child.poll() is not None:
                    raise RuntimeError("Background service exited during startup")
                try:
                    client.call("status")
                    break
                except (OSError, TimeoutError):
                    time.sleep(0.05)
            else:
                child.terminate()
                child.wait(timeout=5)
                raise RuntimeError("Timed out starting application service")
    try:
        from face_locker.ui import run_ui
        return run_ui(settings, windowed=args.windowed)
    finally:
        if child:
            child.terminate()
            try:
                child.wait(timeout=7)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        print(f"Face Locker: {error}", file=sys.stderr)
        raise SystemExit(1)
