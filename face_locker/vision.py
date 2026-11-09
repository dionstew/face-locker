from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time


@dataclass
class Observation:
    embedding: object = None
    live: bool = False
    hint: str = "Position one face inside the frame"
    box: tuple | None = None


class PiVision:
    """Owned exclusively by the vision thread. Frames are BGR for OpenCV."""
    def __init__(self, settings):
        import cv2
        import numpy as np
        self.cv2, self.np, self.settings = cv2, np, settings
        if tuple(map(int, cv2.__version__.split(".")[:2])) < (4, 8):
            raise RuntimeError("OpenCV 4.8 or newer is required for the bundled YuNet model")
        detector = settings.model_path(settings.detector_model)
        recognizer = settings.model_path(settings.recognizer_model)
        for path in (detector, recognizer):
            if not path.is_file():
                raise RuntimeError(f"Model missing: {path.name}. Run scripts/download_models.py")
        self.detector = cv2.FaceDetectorYN.create(str(detector), "", (640, 480), 0.9, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer), "")
        self.model_version = "sface:" + hashlib.sha256(recognizer.read_bytes()).hexdigest()[:16]
        self.pad = None
        if settings.pad_model:
            self.pad = cv2.dnn.readNetFromONNX(str(settings.model_path(settings.pad_model)))
        self.camera = None

    def start(self):
        if self.camera is not None:
            return
        from picamera2 import Picamera2
        camera = Picamera2()
        try:
            # Picamera2 RGB888 yields byte-order BGR, matching OpenCV's array convention.
            camera.configure(camera.create_video_configuration(
                main={"size": (640, 480), "format": "RGB888"},
                controls={"FrameRate": 15}, buffer_count=3))
            camera.start()
        except Exception:
            camera.close()
            raise
        self.camera = camera
        self._warm_until = time.monotonic() + 1.0

    def stop(self):
        camera, self.camera = self.camera, None
        if camera:
            try:
                camera.stop()
            finally:
                camera.close()

    def capture(self):
        return self.camera.capture_array("main")

    def analyze(self, frame):
        cv2, np, cfg = self.cv2, self.np, self.settings
        if time.monotonic() < self._warm_until:
            return Observation(hint="Adjusting camera exposure…")
        self.detector.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = self.detector.detect(frame)
        if faces is None or len(faces) == 0:
            return Observation()
        if len(faces) != 1:
            return Observation(hint="One person at a time, please")
        face = faces[0]
        x, y, w, h = map(int, face[:4])
        box = (x, y, w, h)
        if min(w, h) < cfg.min_face_pixels:
            return Observation(hint="Move a little closer", box=box)
        if x < 0 or y < 0 or x + w >= frame.shape[1] or y + h >= frame.shape[0]:
            return Observation(hint="Keep your whole face in view", box=box)
        crop = frame[y:y+h, x:x+w]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if not 40 <= float(gray.mean()) <= 220:
            return Observation(hint="Improve the lighting on your face", box=box)
        if cv2.Laplacian(gray, cv2.CV_64F).var() < cfg.min_sharpness:
            return Observation(hint="Hold still while the camera focuses", box=box)
        aligned = self.recognizer.alignCrop(frame, face)
        embedding = self.recognizer.feature(aligned).flatten()
        norm = np.linalg.norm(embedding)
        if not np.isfinite(embedding).all() or norm < 1e-9:
            return Observation(hint="Unable to read this face", box=box)
        embedding = embedding / norm
        live = False
        if self.pad is not None:
            # Explicit adapter contract: single RGB image, NCHW, [0,1], two-class output.
            blob = cv2.dnn.blobFromImage(crop, 1 / 255, (cfg.pad_size, cfg.pad_size), swapRB=True)
            self.pad.setInput(blob)
            scores = self.pad.forward().flatten()
            if scores.size != 2 or not np.isfinite(scores).all():
                raise RuntimeError("PAD model must output two finite class scores")
            if cfg.pad_logits:
                scores = np.exp(scores - scores.max())
                scores /= scores.sum()
            if np.any(scores < 0) or np.any(scores > 1) or abs(float(scores.sum()) - 1) > 0.01:
                raise RuntimeError("PAD probabilities must sum to one")
            live = float(scores[cfg.pad_real_index]) >= cfg.pad_threshold
        hint = "Hold still…" if live else ("Liveness check not passed" if self.pad else "Recognition preview · liveness not configured")
        return Observation(embedding, live, hint, box)


class DemoVision:
    model_version = "demo-v1"
    pad = True

    def __init__(self, settings):
        self.scenario = "known"

    def start(self):
        pass

    def stop(self):
        pass

    def capture(self):
        import numpy as np
        import cv2
        frame = np.full((480, 640, 3), (35, 28, 20), dtype=np.uint8)
        for y in range(0, 480, 40):
            cv2.line(frame, (0, y), (640, y), (48, 40, 29), 1)
        for x in range(0, 640, 40):
            cv2.line(frame, (x, 0), (x, 480), (48, 40, 29), 1)
        cv2.ellipse(frame, (320, 218), (78, 100), 0, 0, 360, (115, 145, 152), 2)
        cv2.ellipse(frame, (320, 470), (150, 135), 0, 180, 360, (115, 145, 152), 2)
        cv2.putText(frame, "SIMULATED CAMERA", (208, 52), cv2.FONT_HERSHEY_SIMPLEX, .55, (169, 213, 191), 1)
        cv2.putText(frame, "No physical lock is connected", (170, 435), cv2.FONT_HERSHEY_SIMPLEX, .5, (169, 213, 191), 1)
        return frame

    def analyze(self, frame):
        import numpy as np
        if self.scenario == "no_face":
            return Observation(hint="Position one face inside the frame")
        embedding = np.zeros(128, dtype=np.float32)
        embedding[1 if self.scenario == "unknown" else 0] = 1
        return Observation(embedding, self.scenario != "spoof", "Hold still…", (240, 118, 160, 210))


def best_match(embedding, templates, threshold, margin):
    """Compare per-person maxima; reject ambiguous identities and unknown faces."""
    import numpy as np
    scores = {}
    for user_id, name, template in templates:
        vector = np.asarray(template, dtype=np.float32)
        if vector.shape != embedding.shape or not np.isfinite(vector).all():
            continue
        norm = np.linalg.norm(vector)
        if norm < 1e-9:
            continue
        score = float(np.dot(embedding, vector / norm))
        if user_id not in scores or score > scores[user_id][1]:
            scores[user_id] = (name, score)
    ranked = sorted(scores.items(), key=lambda item: item[1][1], reverse=True)
    if not ranked:
        return None
    user_id, (name, score) = ranked[0]
    second = ranked[1][1][1] if len(ranked) > 1 else -1.0
    if score < threshold or score - second < margin:
        return None
    return user_id, name, score
