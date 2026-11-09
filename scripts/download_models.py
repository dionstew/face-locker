#!/usr/bin/env python3
"""Download OpenCV Zoo weights, validating against the Git LFS pointer hashes."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import urllib.request

MODELS = {
    "face_detection_yunet_2023mar.onnx": "face_detection_yunet",
    "face_recognition_sface_2021dec.onnx": "face_recognition_sface",
}


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "face-locker-model-installer/1"})
    return urllib.request.urlopen(request, timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="Immutable OpenCV Zoo commit (default: resolve current main once)")
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent.parent / "models")
    args = parser.parse_args()
    revision = args.revision
    if revision is None:
        with fetch("https://api.github.com/repos/opencv/opencv_zoo/commits/main") as response:
            revision = json.load(response)["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full 40-character Git commit SHA")
    args.directory.mkdir(parents=True, exist_ok=True)
    manifest = {"repository": "https://github.com/opencv/opencv_zoo", "revision": revision, "models": {}}
    for name, folder in MODELS.items():
        relative = f"models/{folder}/{name}"
        with fetch(f"https://raw.githubusercontent.com/opencv/opencv_zoo/{revision}/{relative}") as response:
            pointer = response.read(2048).decode()
        sha_match = re.search(r"oid sha256:([0-9a-f]{64})", pointer)
        size_match = re.search(r"size (\d+)", pointer)
        if not sha_match or not size_match:
            raise RuntimeError("Upstream file is not a recognized Git LFS pointer")
        expected_sha, expected_size = sha_match[1], int(size_match[1])
        destination = args.directory / name
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected_sha:
            print(f"Verified existing {name}")
        else:
            temporary = destination.with_suffix(".download")
            digest, size = hashlib.sha256(), 0
            try:
                with fetch(f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{revision}/{relative}") as response, temporary.open("wb") as output:
                    while chunk := response.read(128 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != expected_size or digest.hexdigest() != expected_sha:
                    raise RuntimeError(f"Integrity check failed for {name}")
                temporary.replace(destination)
                print(f"Installed {name}")
            finally:
                temporary.unlink(missing_ok=True)
        manifest["models"][name] = {"sha256": expected_sha, "size": expected_size}
        with fetch(f"https://raw.githubusercontent.com/opencv/opencv_zoo/{revision}/models/{folder}/LICENSE") as response:
            (args.directory / f"{folder}.LICENSE").write_bytes(response.read())
    (args.directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
