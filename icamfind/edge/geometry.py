"""Zones and the image-to-room-map homography."""

from __future__ import annotations

import cv2 as cv
import numpy as np

from .config import Config, Zone


class RoomGeometry:
    def __init__(self, cfg: Config):
        self.zones: list[Zone] = cfg.zones
        self._polys = [np.array(z.polygon, np.float32).reshape(-1, 1, 2) for z in self.zones]
        self.door_zone = cfg.door_zone
        self.homography = None
        if cfg.map_image_points and cfg.map_world_points and len(cfg.map_image_points) == 4:
            src = np.array(cfg.map_image_points, np.float32)
            dst = np.array(cfg.map_world_points, np.float32)
            self.homography = cv.getPerspectiveTransform(src, dst)

    def zone_at(self, point: tuple[float, float], exclude_door: bool = False) -> str:
        best, best_dist = "unzoned", -1e9
        for zone, poly in zip(self.zones, self._polys):
            if exclude_door and zone.name == self.door_zone:
                continue
            d = cv.pointPolygonTest(poly, (float(point[0]), float(point[1])), True)
            if d >= 0 and d > best_dist:
                best, best_dist = zone.name, d
        return best

    def zone_for_box(self, box: tuple[int, int, int, int]) -> str:
        x, y, w, h = box
        # Objects rest on surfaces: use the bottom-centre, fall back to the centre.
        zone = self.zone_at((x + w / 2, y + h * 0.9), exclude_door=True)
        if zone == "unzoned":
            zone = self.zone_at((x + w / 2, y + h / 2), exclude_door=True)
        return zone

    def in_door_zone(self, box: tuple[int, int, int, int]) -> bool:
        if not self.door_zone:
            return False
        x, y, w, h = box
        return self.zone_at((x + w / 2, y + h)) == self.door_zone

    def to_map(self, box: tuple[int, int, int, int]) -> list[float] | None:
        if self.homography is None:
            return None
        x, y, w, h = box
        pt = np.array([[[x + w / 2, y + h * 0.9]]], np.float32)
        mx, my = cv.perspectiveTransform(pt, self.homography)[0, 0]
        return [round(float(np.clip(mx, 0, 1)), 4), round(float(np.clip(my, 0, 1)), 4)]

    def draw(self, frame: np.ndarray) -> None:
        for zone, poly in zip(self.zones, self._polys):
            color = (0, 140, 255) if zone.name == self.door_zone else (255, 200, 0)
            cv.polylines(frame, [poly.astype(np.int32)], True, color, 1, cv.LINE_AA)
            x, y = poly.reshape(-1, 2).min(0).astype(int)
            cv.putText(frame, zone.name, (int(x) + 3, int(y) + 14), cv.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv.LINE_AA)
