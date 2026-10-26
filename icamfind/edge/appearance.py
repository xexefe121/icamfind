"""Appearance models used to re-identify moved objects and verify them on request.

Three complementary OpenCV signals are fused:
  * ORB keypoints + Hamming brute-force matching with Lowe's ratio test (texture),
  * HSV hue/saturation histogram correlation (colour, robust to small pose change),
  * normalised cross-correlation of grayscale patches (exact-position checks).
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2 as cv
import numpy as np

_ORB = cv.ORB_create(nfeatures=300, scaleFactor=1.2, nlevels=6, edgeThreshold=9, patchSize=15)
_MATCHER = cv.BFMatcher(cv.NORM_HAMMING)


@dataclass
class Signature:
    hist: np.ndarray
    descriptors: np.ndarray | None
    keypoints: list[tuple[float, float]]
    size: tuple[int, int]


def _upscale(patch: np.ndarray, min_side: int = 64) -> np.ndarray:
    h, w = patch.shape[:2]
    s = max(1.0, min_side / max(1, min(h, w)))
    return cv.resize(patch, None, fx=s, fy=s, interpolation=cv.INTER_CUBIC) if s > 1 else patch


def signature(patch: np.ndarray, mask: np.ndarray | None = None) -> Signature:
    patch = _upscale(patch)
    if mask is not None:
        mask = cv.resize(mask, (patch.shape[1], patch.shape[0]), interpolation=cv.INTER_NEAREST)
    hsv = cv.cvtColor(patch, cv.COLOR_BGR2HSV)
    hist = cv.calcHist([hsv], [0, 1], mask, [24, 16], [0, 180, 0, 256])
    cv.normalize(hist, hist, 1.0, 0.0, cv.NORM_L1)
    gray = cv.cvtColor(patch, cv.COLOR_BGR2GRAY)
    kps, desc = _ORB.detectAndCompute(gray, mask)
    return Signature(hist, desc, [kp.pt for kp in kps], (patch.shape[1], patch.shape[0]))


def orb_score(a: Signature, b: Signature) -> float:
    if a.descriptors is None or b.descriptors is None or len(a.descriptors) < 4 or len(b.descriptors) < 4:
        return 0.0
    pairs = _MATCHER.knnMatch(a.descriptors, b.descriptors, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    return min(1.0, len(good) / max(8.0, 0.25 * min(len(a.descriptors), len(b.descriptors))))


def hist_score(a: Signature, b: Signature) -> float:
    return max(0.0, float(cv.compareHist(a.hist, b.hist, cv.HISTCMP_CORREL)))


def similarity(a: Signature, b: Signature) -> float:
    """Fused identity similarity in [0, 1]."""
    h = hist_score(a, b)
    o = orb_score(a, b)
    ar = a.size[0] / max(1, a.size[1])
    br = b.size[0] / max(1, b.size[1])
    shape = min(ar, br) / max(ar, br)
    return 0.5 * h + 0.35 * o + 0.15 * shape


def ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-mean normalised cross-correlation between two same-size BGR patches."""
    if a.shape != b.shape or a.size == 0:
        return 0.0
    ga = cv.cvtColor(a, cv.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv.cvtColor(b, cv.COLOR_BGR2GRAY).astype(np.float32)
    ga -= ga.mean()
    gb -= gb.mean()
    denom = float(np.sqrt((ga * ga).sum() * (gb * gb).sum()))
    if denom < 1e-6:
        # Flat patches: compare mean intensity instead.
        return 1.0 - min(1.0, abs(float(a.mean()) - float(b.mean())) / 40.0)
    return float((ga * gb).sum() / denom)


def patch_match(current: np.ndarray, reference: np.ndarray) -> float:
    """How well the current patch matches a stored reference at the same location, in [0, 1]."""
    n = (ncc(current, reference) + 1) / 2
    diff = float(np.mean(cv.absdiff(current, reference))) / 255.0
    return 0.6 * n + 0.4 * (1 - min(1.0, diff * 4))


def locate(template: np.ndarray, frame: np.ndarray, template_sig: Signature | None = None,
           search_mask: np.ndarray | None = None) -> tuple[tuple[int, int, int, int] | None, float]:
    """Find a stored object appearance anywhere in the frame.

    Tries ORB feature matching with a RANSAC homography first (handles rotation and scale),
    then falls back to multi-scale normalised template matching.
    """
    best_box, best_score = None, 0.0
    sig = template_sig or signature(template)
    gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
    kps, desc = _ORB.detectAndCompute(gray, search_mask)
    if sig.descriptors is not None and desc is not None and len(sig.descriptors) >= 8 and len(desc) >= 8:
        pairs = _MATCHER.knnMatch(sig.descriptors, desc, k=2)
        good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
        if len(good) >= 8:
            src = np.float32([sig.keypoints[m.queryIdx] for m in good]).reshape(-1, 1, 2)
            dst = np.float32([kps[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            H, inliers = cv.findHomography(src, dst, cv.RANSAC, 5.0)
            if H is not None and inliers is not None and int(inliers.sum()) >= 6:
                tw, th = sig.size
                corners = np.float32([[0, 0], [tw, 0], [tw, th], [0, th]]).reshape(-1, 1, 2)
                x, y, w, h = cv.boundingRect(cv.perspectiveTransform(corners, H))
                if w > 4 and h > 4:
                    best_box = (max(0, x), max(0, y), w, h)
                    best_score = min(1.0, int(inliers.sum()) / 20.0)

    tgray = cv.cvtColor(template, cv.COLOR_BGR2GRAY)
    for scale in (0.8, 1.0, 1.25):
        t = cv.resize(tgray, None, fx=scale, fy=scale)
        if t.shape[0] >= gray.shape[0] or t.shape[1] >= gray.shape[1] or min(t.shape) < 6:
            continue
        res = cv.matchTemplate(gray, t, cv.TM_CCOEFF_NORMED)
        if search_mask is not None:
            valid = search_mask[: res.shape[0], : res.shape[1]] > 0
            res = np.where(valid, res, -1)
        _, score, _, loc = cv.minMaxLoc(res)
        score = float(score)
        if score > best_score and score > 0.6:
            best_box, best_score = (loc[0], loc[1], t.shape[1], t.shape[0]), score
    return best_box, best_score
