"""Camera sources; OpenCV capture is isolated so a stalled stream can be stopped."""
from __future__ import annotations

import multiprocessing
import hashlib
import os
from pathlib import Path
import queue
import re
import sys
import time
from urllib.parse import urlsplit


def normalize_source(value):
    if not isinstance(value, str):
        raise ValueError("Camera source must be text")
    source = value.strip()
    if not source or len(source) > 2048 or any(c.isspace() or ord(c) < 32 for c in source):
        raise ValueError("Enter a camera index, device path, or stream URL")
    if source in ("pi", "demo"):
        return source
    if re.fullmatch(r"[0-9]{1,3}", source):
        return str(int(source))
    if re.fullmatch(r"/dev/(?:video|camera)[0-9]+", source) or source.startswith(("/dev/v4l/by-id/", "/dev/v4l/by-path/")):
        return source
    try:
        parsed = urlsplit(source)
        if parsed.scheme in ("http", "https", "rtsp", "rtsps") and parsed.hostname and parsed.port != 0:
            return parsed.geturl()
    except ValueError:
        pass
    raise ValueError("Use pi, demo, a webcam index such as 0, /dev/video0, or an HTTP/RTSP stream URL")


def describe_source(source):
    """Public label deliberately omits URL credentials, path, and query tokens."""
    if source == "demo":
        return "Simulated camera"
    if source == "pi":
        return "Pi ribbon camera"
    if source.isdigit():
        return f"Webcam {source}"
    if source.startswith("/dev/"):
        return source
    parsed = urlsplit(source)
    return f"{parsed.scheme.upper()} camera · {parsed.hostname}"


def source_key(source):
    return hashlib.sha256(source.encode()).hexdigest()


class CameraError(RuntimeError):
    """App-generated camera errors that are safe to show without URL credentials."""


def _check_linux_device(source):
    device = Path(f"/dev/video{int(source)}") if source.isdigit() else Path(source)
    if not device.exists():
        raise CameraError(f"Camera device {device} was not found. Check /dev/video* and select an existing capture device.")
    if not os.access(device, os.R_OK | os.W_OK):
        raise CameraError(f"No read/write permission for {device}. Check its ownership, ACLs, and your video group membership.")


def _quiet_opencv(cv2):
    """Logging APIs vary across pip/distro builds; they are never required for capture."""
    setter = getattr(cv2, "setLogLevel", None)
    if not callable(setter):
        legacy = getattr(getattr(cv2, "utils", None), "logging", None)
        setter = getattr(legacy, "setLogLevel", None)
    if callable(setter):
        try:
            setter(0)
        except Exception:
            pass  # OPENCV_LOG_LEVEL still applies on builds supporting that setting.


class RibbonCamera:
    def __init__(self, settings):
        self.camera = None

    def start(self):
        if self.camera is not None:
            return
        try:
            from picamera2 import Picamera2
        except ImportError:
            raise RuntimeError("Pi ribbon camera needs Picamera2 on Raspberry Pi OS. Select a webcam or stream URL on a laptop.") from None
        camera = Picamera2()
        try:
            # Picamera2 RGB888 arrays have byte-order BGR, as expected by OpenCV.
            camera.configure(camera.create_video_configuration(
                main={"size": (640, 480), "format": "RGB888"},
                controls={"FrameRate": 15}, buffer_count=3))
            camera.start()
        except Exception:
            camera.close()
            raise
        self.camera = camera

    def read(self):
        return self.camera.capture_array("main")

    def stop(self):
        camera, self.camera = self.camera, None
        if camera:
            try:
                camera.stop()
            finally:
                camera.close()


def _publish(messages, value):
    try:
        messages.put_nowait(value)
    except queue.Full:
        try:
            messages.get_nowait()
        except queue.Empty:
            pass
        try:
            messages.put_nowait(value)
        except queue.Full:
            pass


def _capture_worker(source, open_timeout, read_timeout, messages, stop):
    # Some FFmpeg errors include full URLs. Keep credentials out of native logs.
    os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"
    os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
    capture = None
    failed = False
    try:
        import cv2
        _quiet_opencv(cv2)
        if source.startswith(("http://", "https://", "rtsp://", "rtsps://")):
            capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(open_timeout * 1000),
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(read_timeout * 1000)])
        else:
            device = int(source) if source.isdigit() else source
            if sys.platform.startswith("linux"):
                _check_linux_device(source)
                # Use the Linux camera API directly, avoiding unrelated auto-selected backends.
                capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
            else:
                capture = cv2.VideoCapture(device)
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            capture.set(cv2.CAP_PROP_FPS, 15)
        if not capture.isOpened():
            raise CameraError("Camera could not be opened. Check its index/address, permissions, and whether another app is using it.")
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        while not stop.is_set():
            ok, frame = capture.read()
            if not ok or frame is None or frame.size == 0:
                raise CameraError("Camera disconnected or stopped delivering frames")
            height, width = frame.shape[:2]
            if max(height, width) > 640:
                scale = 640 / max(height, width)
                frame = cv2.resize(frame, (round(width * scale), round(height * scale)))
            _publish(messages, ("frame", time.monotonic(), frame))
    except CameraError as exc:
        failed = True
        _publish(messages, ("error", 0, str(exc)))
    except Exception:
        failed = True
        # Never forward native exception strings: they can contain URL passwords.
        _publish(messages, ("error", 0, "Camera unavailable. Check the source, permissions, network, and stream format."))
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
        if failed:
            # Flush short startup diagnostics before the subprocess exits.
            messages.close()
            messages.join_thread()
        else:
            messages.cancel_join_thread()


class OpenCVCamera:
    def __init__(self, settings):
        self.source = normalize_source(settings.camera_source)
        self.open_timeout = settings.camera_open_timeout
        self.read_timeout = settings.camera_read_timeout
        self.process = None
        self.messages = None
        self.stop_event = None
        self.first_frame = None

    def start(self):
        if self.process is not None:
            return
        context = multiprocessing.get_context("spawn")
        self.messages = context.Queue(maxsize=1)
        self.stop_event = context.Event()
        self.process = context.Process(target=_capture_worker, args=(
            self.source, self.open_timeout, self.read_timeout, self.messages, self.stop_event), daemon=True)
        try:
            self.process.start()
            self.first_frame = self._receive(self.open_timeout + self.read_timeout + 2)
        except Exception:
            self.stop()
            raise

    def _receive(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Camera timed out. Check its source, connection, and permissions.")
            try:
                kind, captured_at, value = self.messages.get(timeout=min(remaining, .1))
            except queue.Empty:
                if self.process is not None and not self.process.is_alive():
                    code = self.process.exitcode
                    if isinstance(code, int) and code < 0:
                        raise RuntimeError(f"Camera reader crashed in a native video library (signal {-code}). Check the OpenCV installation and camera backend.") from None
                    raise RuntimeError("Camera reader stopped. Check the camera source and stream format.") from None
                continue
            if kind == "error":
                raise RuntimeError(value)
            if time.monotonic() - captured_at <= self.read_timeout:
                return value

    def read(self):
        if self.first_frame is not None:
            frame, self.first_frame = self.first_frame, None
            return frame
        return self._receive(self.read_timeout)

    def stop(self):
        process, self.process = self.process, None
        if process is not None:
            self.stop_event.set()
            if process.pid is not None:
                process.join(timeout=.4)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
            if not process.is_alive():
                process.close()
            self.messages.close()
            self.messages.cancel_join_thread()
        self.messages = self.stop_event = self.first_frame = None
