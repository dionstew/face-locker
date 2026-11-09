#!/usr/bin/env python3
"""Verify service templates with local paths; never installs or starts services."""
from pathlib import Path
import getpass
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory(prefix="fl-units-") as folder:
        units = []
        for name in ("face-locker.service", "face-locker-ui.service"):
            content = (ROOT / "deployment" / name).read_text()
            content = content.replace("/home/pi/face-locker/.venv/bin/python", sys.executable)
            content = content.replace("%h/face-locker/.venv/bin/python", sys.executable)
            content = content.replace("/home/pi/face-locker", str(ROOT))
            content = content.replace("%h/face-locker", str(ROOT))
            content = content.replace("User=pi\n", f"User={getpass.getuser()}\n")
            destination = Path(folder) / name
            destination.write_text(content)
            units.append(str(destination))
        subprocess.run(["systemd-analyze", "verify", "--man=no", *units], check=True)


if __name__ == "__main__":
    main()
