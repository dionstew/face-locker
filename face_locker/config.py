from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
import os


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    state_dir: str = "~/.local/share/face-locker"
    serial_port: str = "/dev/serial0"
    baudrate: int = 115200
    lock_id: int = 1
    unlock_ms: int = 3000
    ack_timeout: float = 2.0
    telemetry_timeout: float = 6.0
    scan_timeout: float = 15.0
    enrollment_timeout: float = 45.0
    result_seconds: float = 5.0
    match_threshold: float = 0.50
    match_margin: float = 0.08
    consistent_frames: int = 3
    enrollment_samples: int = 5
    min_face_pixels: int = 90
    min_sharpness: float = 60.0
    detector_model: str = "models/face_detection_yunet_2023mar.onnx"
    recognizer_model: str = "models/face_recognition_sface_2021dec.onnx"
    pad_model: str = ""
    pad_size: int = 128
    pad_real_index: int = 1
    pad_logits: bool = True
    pad_threshold: float = 0.95
    unlock_enabled: bool = False
    backlight_path: str = ""
    timezone: str = "Asia/Jakarta"
    demo: bool = False
    _base: Path = field(default=ROOT, repr=False)

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser().resolve()

    @property
    def socket_path(self) -> Path:
        return self.state_path / "service.sock"

    def model_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self._base / path

    def prepare(self):
        self.state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_path, 0o700)
        if len(str(self.socket_path).encode()) > 100:
            raise ValueError("state_dir is too long for a Unix socket")

    def validate(self):
        for name in ("ack_timeout", "telemetry_timeout", "scan_timeout",
                     "enrollment_timeout", "result_seconds"):
            if not 0.1 <= float(getattr(self, name)) <= 3600:
                raise ValueError(f"Invalid {name}")
        for name in ("match_threshold", "match_margin", "pad_threshold"):
            if not 0 <= float(getattr(self, name)) <= 1:
                raise ValueError(f"Invalid {name}")
        if not 1 <= self.unlock_ms <= 10000:
            raise ValueError("unlock_ms must be between 1 and 10000")
        if not 2 <= self.consistent_frames <= 20 or not 3 <= self.enrollment_samples <= 20:
            raise ValueError("Invalid recognition sample count")
        if self.baudrate <= 0 or self.lock_id < 1 or self.pad_real_index not in (0, 1):
            raise ValueError("Invalid hardware or PAD configuration")
        from zoneinfo import ZoneInfo
        ZoneInfo(self.timezone)


def load_settings(path=None, *, demo=False, state_dir=None) -> Settings:
    settings = Settings()
    config_path = Path(path).resolve() if path else ROOT / "config/settings.yaml"
    if config_path.exists():
        import yaml
        values = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(values, dict):
            raise ValueError("Configuration must be a mapping")
        valid = {f.name for f in fields(settings) if not f.name.startswith("_")}
        for key, value in values.items():
            if key not in valid or key == "demo":
                raise ValueError(f"Unknown configuration key: {key}")
            expected = type(getattr(settings, key))
            if expected is bool and type(value) is not bool:
                raise ValueError(f"{key} must be true or false")
            if expected is int and type(value) is not int:
                raise ValueError(f"{key} must be an integer")
            if expected is str and not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            if expected is float and (type(value) not in (int, float)):
                raise ValueError(f"{key} must be a number")
            setattr(settings, key, value)
        settings._base = config_path.parent.parent
    elif path:
        raise FileNotFoundError(config_path)
    settings.demo = demo
    if demo:
        settings.state_dir = "~/.local/share/face-locker-demo"
    if state_dir:
        settings.state_dir = state_dir
    settings.validate()
    return settings
