from pathlib import Path
import time
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from face_locker.config import ROOT, Settings
from face_locker.vision import PiVision


class VisionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.vision = PiVision.__new__(PiVision)
        self.vision.cv2 = cv2
        self.vision.np = np
        self.vision.settings = Settings()
        self.vision._warm_until = 0
        self.vision.detector = Mock()
        self.vision.detector.detect.return_value = (None, np.array([
            [100, 80, 120, 140, 130, 120, 180, 120, 155, 150, 135, 190, 175, 190, .99]], dtype=np.float32))
        self.vision.recognizer = Mock()
        self.vision.recognizer.feature.return_value = np.ones((1, 128), np.float32)
        self.vision.pad = Mock()
        self.frame = np.random.default_rng(42).integers(60, 200, (480, 640, 3), dtype=np.uint8)

    def test_live_and_attack_outputs(self):
        self.vision.pad.forward.return_value = np.array([[0, 10]], dtype=np.float32)
        observation = self.vision.analyze(self.frame)
        self.assertTrue(observation.live)
        self.assertAlmostEqual(float(np.linalg.norm(observation.embedding)), 1.0, places=5)
        self.vision.pad.forward.return_value = np.array([[10, 0]], dtype=np.float32)
        self.assertFalse(self.vision.analyze(self.frame).live)

    def test_bad_pad_output_faults_instead_of_authorizing(self):
        for output in (np.array([1, 2, 3]), np.array([np.nan, 1])):
            self.vision.pad.forward.return_value = output
            with self.assertRaises(RuntimeError):
                self.vision.analyze(self.frame)
        self.vision.settings.pad_logits = False
        self.vision.pad.forward.return_value = np.array([[.7, .8]])
        with self.assertRaises(RuntimeError):
            self.vision.analyze(self.frame)

    def test_missing_pad_is_never_treated_as_live(self):
        self.vision.pad = None
        self.assertFalse(self.vision.analyze(self.frame).live)

    def test_multiple_faces_or_poor_quality_produce_no_embedding(self):
        face = self.vision.detector.detect.return_value[1]
        self.vision.detector.detect.return_value = (None, np.vstack([face, face]))
        self.assertIsNone(self.vision.analyze(self.frame).embedding)
        self.vision.detector.detect.return_value = (None, face)
        self.assertIsNone(self.vision.analyze(np.zeros_like(self.frame)).embedding)


@unittest.skipUnless((ROOT / Settings().detector_model).exists() and
                     (ROOT / Settings().recognizer_model).exists(), "Download OpenCV models to check real inference")
class ModelInferenceTests(unittest.TestCase):
    def test_real_models_load_detect_blank_and_generate_embedding(self):
        vision = PiVision(Settings())
        vision._warm_until = 0
        observation = vision.analyze(np.zeros((480, 640, 3), dtype=np.uint8))
        self.assertIsNone(observation.embedding)
        # Exercise the actual SFace graph, without claiming this synthetic input is a face.
        features = vision.recognizer.feature(np.full((112, 112, 3), 127, dtype=np.uint8))
        self.assertEqual(features.shape, (1, 128))
        self.assertTrue(np.isfinite(features).all())
