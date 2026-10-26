"""YOLOX object detector running on the OpenCV 5 DNN module (OpenCV Model Zoo weights)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import cv2 as cv
import numpy as np

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)

# Furniture and large fixtures are scene, not "things you misplace".
SCENE_CLASSES = {"person", "chair", "couch", "bed", "dining table", "toilet", "tv", "sink",
                 "refrigerator", "oven", "microwave", "car", "bus", "train", "truck", "bench"}


@dataclass
class Detection:
    label: str
    confidence: float
    box: tuple[int, int, int, int]  # x, y, w, h in source image pixels

    @property
    def is_person(self) -> bool:
        return self.label == "person"


class YoloxDetector:
    """Thin wrapper around cv.dnn for the 640x640 YOLOX-S ONNX model from OpenCV Zoo."""

    def __init__(self, model_path: str | Path, conf_threshold: float = 0.35, nms_threshold: float = 0.45):
        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"Detector model not found at {model_path}. Run: python scripts/get_models.py"
            )
        self.net = cv.dnn.readNet(str(model_path))
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold
        self.size = 640
        self._lock = threading.Lock()
        self._grids, self._strides = self._make_grids()

    def _make_grids(self) -> tuple[np.ndarray, np.ndarray]:
        grids, strides = [], []
        for stride in (8, 16, 32):
            n = self.size // stride
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grid = np.stack((xv, yv), 2).reshape(-1, 2)
            grids.append(grid)
            strides.append(np.full((grid.shape[0], 1), stride))
        return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)

    def detect(self, image: np.ndarray, conf_threshold: float | None = None) -> list[Detection]:
        conf_threshold = self.conf_threshold if conf_threshold is None else conf_threshold
        h, w = image.shape[:2]
        ratio = min(self.size / h, self.size / w)
        padded = np.full((self.size, self.size, 3), 114, np.float32)
        resized = cv.resize(image, (int(w * ratio), int(h * ratio)), interpolation=cv.INTER_LINEAR)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        blob = np.transpose(padded, (2, 0, 1))[np.newaxis]

        with self._lock:
            self.net.setInput(blob)
            out = self.net.forward(self.net.getUnconnectedOutLayersNames())[0][0].copy()

        out[:, :2] = (out[:, :2] + self._grids) * self._strides
        out[:, 2:4] = np.exp(out[:, 2:4]) * self._strides
        scores = out[:, 4:5] * out[:, 5:]
        class_ids = scores.argmax(1)
        confidences = scores.max(1)
        keep = confidences >= conf_threshold
        if not np.any(keep):
            return []
        out, class_ids, confidences = out[keep], class_ids[keep], confidences[keep]

        boxes = []
        for cx, cy, bw, bh in out[:, :4] / ratio:
            boxes.append([int(cx - bw / 2), int(cy - bh / 2), int(bw), int(bh)])
        indices = cv.dnn.NMSBoxesBatched(boxes, confidences.tolist(), class_ids.tolist(),
                                         conf_threshold, self.nms_threshold)
        detections = []
        for i in np.array(indices).reshape(-1):
            x, y, bw, bh = boxes[i]
            x, y = max(0, x), max(0, y)
            bw, bh = min(bw, w - x), min(bh, h - y)
            if bw > 1 and bh > 1:
                detections.append(Detection(COCO_CLASSES[int(class_ids[i])], float(confidences[i]), (x, y, bw, bh)))
        return detections

    def label_region(self, frame: np.ndarray, box: tuple[int, int, int, int],
                     frame_detections: list[Detection] | None = None) -> tuple[str, float]:
        """Best COCO label for a region: full-frame detections first, then a zoomed crop."""
        best = ("object", 0.0)
        for det in frame_detections if frame_detections is not None else self.detect(frame, 0.25):
            if det.label in SCENE_CLASSES:
                continue
            iou = box_iou(det.box, box)
            if iou > 0.3 and det.confidence > best[1]:
                best = (det.label, det.confidence)
        if best[1] > 0:
            return best

        # Small objects: detect on a crop with context, upscaled by the letterbox.
        x, y, w, h = box
        pad = int(max(w, h) * 0.6) + 8
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(frame.shape[1], x + w + pad), min(frame.shape[0], y + h + pad)
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return best
        for det in self.detect(crop, 0.25):
            if det.label in SCENE_CLASSES:
                continue
            dx, dy, dw, dh = det.box
            iou = box_iou((dx + x0, dy + y0, dw, dh), box)
            if iou > 0.25 and det.confidence > best[1]:
                best = (det.label, det.confidence)
        return best


def box_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0
