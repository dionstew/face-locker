Run `python3 scripts/download_models.py` to download the two OpenCV Zoo models.
Weights are not bundled with this repository. The script verifies Git LFS SHA-256
object IDs before installation and records the source revision in manifest.json.
See the upstream per-model LICENSE files before redistributing the weights.

A liveness/PAD model is separate and must be supplied and validated by the installer.
See docs/recognition.md for its exact input/output contract.
