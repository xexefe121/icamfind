"""Download the OpenCV Model Zoo YOLOX-S detector used by the edge pipeline and verify its checksum."""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

URL = "https://huggingface.co/opencv/object_detection_yolox/resolve/main/object_detection_yolox_2022nov.onnx"
DEST = Path(__file__).resolve().parents[1] / "models" / "object_detection_yolox_2022nov.onnx"
SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if not DEST.exists():
        print(f"downloading {URL}")
        urllib.request.urlretrieve(URL, DEST)
    digest = sha256(DEST)
    print(f"{DEST.name}  sha256={digest}  size={DEST.stat().st_size} bytes")
    if SHA256 and digest != SHA256:
        DEST.unlink()
        sys.exit("checksum mismatch; file removed")


if __name__ == "__main__":
    main()
