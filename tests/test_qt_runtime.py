import os
import unittest
from unittest.mock import patch

from face_locker.qt import check_runtime


class QtRuntimeTests(unittest.TestCase):
    @patch("face_locker.qt.sys.platform", "linux")
    @patch.dict(os.environ, {"QT_QPA_PLATFORM": "xcb"})
    @patch("face_locker.qt.Path.is_file", return_value=True)
    @patch("face_locker.qt.ctypes.CDLL", side_effect=OSError(
        "libxcb-cursor.so.0: cannot open shared object file: No such file or directory"))
    def test_missing_cursor_library_reports_install_command(self, loader, exists):
        with self.assertRaisesRegex(RuntimeError, "sudo apt install libxcb-cursor0"):
            check_runtime()

    @patch("face_locker.qt.sys.platform", "linux")
    @patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"})
    @patch("face_locker.qt.ctypes.CDLL")
    def test_offscreen_does_not_require_xcb(self, loader):
        check_runtime()
        loader.assert_not_called()

    @patch("face_locker.qt.sys.platform", "linux")
    @patch.dict(os.environ, {"QT_QPA_PLATFORM": "", "WAYLAND_DISPLAY": "wayland-0"})
    @patch("face_locker.qt.ctypes.CDLL")
    def test_wayland_selection_does_not_require_xcb(self, loader):
        check_runtime()
        loader.assert_not_called()

    @patch("face_locker.qt.sys.platform", "linux")
    @patch.dict(os.environ, {"QT_QPA_PLATFORM": "xcb"})
    @patch("face_locker.qt.Path.is_file", return_value=True)
    @patch("face_locker.qt.ctypes.CDLL", return_value=object())
    def test_loadable_plugin_passes(self, loader, exists):
        check_runtime()
        loader.assert_called_once()
