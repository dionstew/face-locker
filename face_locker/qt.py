"""Prefer PySide6; allow distro PyQt5 (also used by Picamera2) on the Pi."""
import ctypes
import os
from pathlib import Path
import sys

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal
except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets
    Signal = QtCore.pyqtSignal

Qt = QtCore.Qt


def check_runtime():
    """Check xcb's shared libraries without creating QApplication (which can abort)."""
    if not sys.platform.startswith("linux"):
        return
    platform = os.environ.get("QT_QPA_PLATFORM", "").split(":", 1)[0]
    # Let Qt select Wayland when it is available. Explicit non-xcb backends,
    # including offscreen in the test suite, do not need xcb libraries.
    if platform and platform != "xcb":
        return
    if not platform and os.environ.get("WAYLAND_DISPLAY"):
        return
    info = QtCore.QLibraryInfo
    path = info.path(info.PluginsPath) if hasattr(info, "path") else info.location(info.PluginsPath)
    plugin = Path(path) / "platforms" / "libqxcb.so"
    if not plugin.is_file():
        return  # Qt's own diagnostic handles custom plugin search paths.
    try:
        ctypes.CDLL(str(plugin))
    except OSError as exc:
        packages = {
            "libxcb-cursor.so": "libxcb-cursor0",
            "libxkbcommon-x11.so": "libxkbcommon-x11-0",
            "libxcb-icccm.so": "libxcb-icccm4",
            "libxcb-image.so": "libxcb-image0",
            "libxcb-keysyms.so": "libxcb-keysyms1",
            "libxcb-render-util.so": "libxcb-render-util0",
            "libxcb-xinerama.so": "libxcb-xinerama0",
            "libxcb-xkb.so": "libxcb-xkb1",
        }
        package = next((value for key, value in packages.items() if key in str(exc)), None)
        fix = (f"On Raspberry Pi OS/Debian/Ubuntu, run: sudo apt install {package}. "
               if package else "Install the missing system library reported above. ")
        raise RuntimeError(
            f"Qt's xcb display plugin cannot load: {exc}. {fix}"
            "Then retry from a terminal in your graphical desktop session. "
            "For detailed plugin diagnostics use QT_DEBUG_PLUGINS=1."
        ) from exc
