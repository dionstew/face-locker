import queue
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from face_locker.camera import (CameraError, OpenCVCamera, describe_source, normalize_source,
                                _capture_worker, _check_linux_device, _quiet_opencv)
from face_locker.config import Settings, load_settings
from face_locker.vision import PiVision


class CameraSourceTests(unittest.TestCase):
    def test_supported_sources(self):
        for source in ("pi", "demo", "0", "1", "/dev/video2", "/dev/camera0", "/dev/v4l/by-id/camera", "/dev/v4l/by-path/camera",
                       "http://127.0.0.1:8080/video", "https://camera.test/live", "rtsp://user:pass@camera.test:554/live", "rtsps://camera.test/live"):
            self.assertEqual(normalize_source(source), source)
        self.assertEqual(normalize_source(" 001 "), "1")

    def test_reject_unsupported_or_malformed_sources(self):
        for source in ("", "-1", "9999", "file:///etc/passwd", "/tmp/movie.avi", "rtsp://", "http://camera:bad/video",
                       "http://camera:0/video", "http://a\nb/video", "http://bad host/video", 0):
            with self.subTest(source=source), self.assertRaises(ValueError):
                normalize_source(source)

    def test_labels_do_not_disclose_url_secrets(self):
        label = describe_source("rtsp://username:password@camera.test:554/private/path?token=secret")
        self.assertEqual(label, "RTSP camera · camera.test")
        self.assertNotIn("password", label)
        self.assertNotIn("secret", label)

    def test_demo_can_select_real_camera_without_enabling_real_serial(self):
        settings = load_settings(demo=True, camera_source="0")
        self.assertTrue(settings.demo)
        self.assertEqual(settings.camera_source, "0")
        self.assertTrue(settings._camera_override)
        self.assertEqual(load_settings(demo=True).camera_source, "demo")
        with self.assertRaises(ValueError):
            load_settings(camera_source="demo")

    def test_old_buffered_frames_are_rejected(self):
        camera = OpenCVCamera(Settings(camera_source="0", camera_read_timeout=.1))
        camera.messages = queue.Queue()
        camera.process = Mock()
        old, new = np.zeros((10, 10, 3)), np.ones((10, 10, 3))
        camera.messages.put(("frame", time.monotonic() - 10, old))
        camera.messages.put(("frame", time.monotonic(), new))
        self.assertIs(camera._receive(.1), new)

    def test_no_frames_times_out(self):
        camera = OpenCVCamera(Settings(camera_source="0"))
        camera.messages, camera.process = queue.Queue(), Mock()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            camera._receive(.01)

    def test_reader_exit_reports_actionable_error(self):
        camera = OpenCVCamera(Settings(camera_source="0"))
        camera.messages, camera.process = queue.Queue(), Mock()
        camera.process.is_alive.return_value = False
        with self.assertRaisesRegex(RuntimeError, "reader stopped"):
            camera._receive(.2)

    def test_native_reader_crash_is_not_reported_as_permission_error(self):
        camera = OpenCVCamera(Settings(camera_source="0"))
        camera.messages, camera.process = queue.Queue(), Mock()
        camera.process.is_alive.return_value = False
        camera.process.exitcode = -6
        with self.assertRaisesRegex(RuntimeError, "native video library.*signal 6"):
            camera._receive(.2)

    @patch("cv2.VideoCapture")
    @patch("face_locker.camera._check_linux_device")
    @patch("face_locker.camera.sys.platform", "linux")
    def test_capture_uses_numeric_index_and_releases_device(self, check, factory):
        import cv2
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        capture = factory.return_value
        capture.read.side_effect = [(True, frame), (False, None)]
        messages, stop = Mock(), Mock()
        stop.is_set.return_value = False
        _capture_worker("1", 3, 2, messages, stop)
        factory.assert_called_once_with(1, cv2.CAP_V4L2)
        check.assert_called_once_with("1")
        capture.release.assert_called_once()

    def test_logging_setup_supports_missing_and_legacy_apis(self):
        _quiet_opencv(SimpleNamespace())
        legacy_setter = Mock()
        _quiet_opencv(SimpleNamespace(utils=SimpleNamespace(logging=SimpleNamespace(setLogLevel=legacy_setter))))
        legacy_setter.assert_called_once_with(0)
        setter = Mock(side_effect=RuntimeError("Unsupported logging API"))
        _quiet_opencv(SimpleNamespace(setLogLevel=setter))

    @patch("cv2.setLogLevel", None, create=True)
    @patch("cv2.VideoCapture")
    @patch("face_locker.camera._check_linux_device")
    @patch("face_locker.camera.sys.platform", "linux")
    def test_missing_set_log_level_does_not_stop_camera_capture(self, check, factory):
        capture = factory.return_value
        capture.read.side_effect = [(True, np.zeros((480, 640, 3), dtype=np.uint8)), (False, None)]
        messages, stop = Mock(), Mock()
        stop.is_set.return_value = False
        _capture_worker("0", 3, 2, messages, stop)
        self.assertEqual(messages.put_nowait.call_args_list[0].args[0][0], "frame")
        capture.release.assert_called_once()

    @patch("face_locker.camera.Path.exists", return_value=False)
    def test_missing_device_reports_real_linux_path(self, exists):
        with self.assertRaisesRegex(CameraError, "/dev/video1 was not found"):
            _check_linux_device("1")

    @patch("face_locker.camera.Path.exists", return_value=True)
    @patch("face_locker.camera.os.access", return_value=False)
    def test_permission_error_is_distinct_from_missing_device(self, access, exists):
        with self.assertRaisesRegex(CameraError, "No read/write permission for /dev/video0"):
            _check_linux_device("/dev/video0")

    @patch("cv2.VideoCapture")
    @patch("face_locker.camera._check_linux_device", side_effect=CameraError("No read/write permission for /dev/video0"))
    @patch("face_locker.camera.sys.platform", "linux")
    def test_device_error_reaches_parent_without_opening_capture(self, check, factory):
        messages, stop = Mock(), Mock()
        _capture_worker("0", 3, 2, messages, stop)
        self.assertIn("No read/write permission", messages.put_nowait.call_args.args[0][2])
        messages.join_thread.assert_called_once()
        factory.assert_not_called()

    @patch("cv2.VideoCapture")
    def test_network_open_has_timeouts_and_sanitizes_failures(self, factory):
        import cv2
        factory.side_effect = RuntimeError("password@camera.test")
        messages, stop = Mock(), Mock()
        _capture_worker("rtsp://user:password@camera.test/live", 3, 2, messages, stop)
        args = factory.call_args.args
        self.assertEqual(args[1], cv2.CAP_FFMPEG)
        self.assertEqual(args[2], [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000])
        self.assertNotIn("password", str(messages.put_nowait.call_args))

    def test_preview_does_not_require_recognition_weights(self):
        with tempfile.TemporaryDirectory() as folder:
            vision = PiVision(Settings(camera_source="0", detector_model=folder + "/missing.onnx"))
            self.assertIn("Model missing", vision.recognition_error)
            self.assertEqual(vision.model_version, "")
