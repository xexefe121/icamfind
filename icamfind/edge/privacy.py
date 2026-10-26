"""Privacy filter: people never leave the device unblurred."""

from __future__ import annotations

import cv2 as cv
import numpy as np


def blur_boxes(image: np.ndarray, boxes: list[tuple[int, int, int, int]], offset: tuple[int, int] = (0, 0)) -> np.ndarray:
    """Heavily pixelate the given boxes (frame coordinates) inside `image` (which starts at `offset`)."""
    out = image.copy()
    ox, oy = offset
    h, w = out.shape[:2]
    for x, y, bw, bh in boxes:
        pad_x, pad_y = int(bw * 0.1), int(bh * 0.1)
        x0, y0 = max(0, x - pad_x - ox), max(0, y - pad_y - oy)
        x1, y1 = min(w, x + bw + pad_x - ox), min(h, y + bh + pad_y - oy)
        if x1 <= x0 or y1 <= y0:
            continue
        region = out[y0:y1, x0:x1]
        small = cv.resize(region, (max(1, (x1 - x0) // 16), max(1, (y1 - y0) // 16)), interpolation=cv.INTER_LINEAR)
        out[y0:y1, x0:x1] = cv.resize(small, (x1 - x0, y1 - y0), interpolation=cv.INTER_NEAREST)
    return out


def encode_jpeg(image: np.ndarray, quality: int = 85, max_side: int = 640) -> bytes:
    h, w = image.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        image = cv.resize(image, (int(w * scale), int(h * scale)), interpolation=cv.INTER_AREA)
    ok, buf = cv.imencode(".jpg", image, [cv.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def crop_with_margin(frame: np.ndarray, box: tuple[int, int, int, int], margin: float = 0.35,
                     min_side: int = 48) -> tuple[np.ndarray, tuple[int, int]]:
    """Crop a box with context margin; returns the crop and its top-left offset."""
    x, y, w, h = box
    m = int(max(w, h) * margin)
    side_pad = max(0, (min_side - max(w, h)) // 2)
    x0 = max(0, x - m - side_pad)
    y0 = max(0, y - m - side_pad)
    x1 = min(frame.shape[1], x + w + m + side_pad)
    y1 = min(frame.shape[0], y + h + m + side_pad)
    return frame[y0:y1, x0:x1].copy(), (x0, y0)
