"""Edge configuration: camera source, zones, room map and cloud settings."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config.json")
ENV_FILE = Path(".icamfind.env")


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE pairs written by scripts/deploy.py (existing environment wins)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


@dataclass
class Zone:
    name: str
    polygon: list[list[float]]  # image coordinates at processing resolution


@dataclass
class Config:
    # Camera: an integer webcam index, an RTSP/HTTP URL (CCTV) or a video file path.
    source: Any = "auto"
    device_id: str = "home-cam-1"
    process_width: int = 640
    target_fps: float = 12.0

    zones: list[Zone] = field(default_factory=list)
    door_zone: str | None = None
    # Four image points on the floor/table plane and their room-map coordinates (0..1).
    map_image_points: list[list[float]] | None = None
    map_world_points: list[list[float]] | None = None

    # Labels that trigger the "leaving without it" alert.
    essentials: list[str] = field(default_factory=lambda: ["keys", "wallet", "cell phone", "phone", "glasses"])

    # Perception tuning.
    stable_seconds: float = 2.0
    min_object_area: float = 0.0012  # fraction of frame area
    max_object_area: float = 0.25
    long_diff_threshold: int = 28
    person_conf: float = 0.45
    person_interval: float = 0.4  # seconds between person detections
    lighting_change_fraction: float = 0.35

    # Privacy: blur people in every image that leaves the device.
    blur_people: bool = True

    # Cloud.
    api_url: str | None = None
    api_token: str | None = None
    data_dir: str = "data"
    model_path: str = "models/object_detection_yolox_2022nov.onnx"
    speak: bool = True

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        path = Path(path or DEFAULT_CONFIG_PATH)
        raw: dict[str, Any] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        zones = [Zone(**z) for z in raw.pop("zones", [])]
        known = {f for f in cls.__dataclass_fields__}
        cfg = cls(**{k: v for k, v in raw.items() if k in known})
        cfg.zones = zones
        # Environment overrides keep secrets out of config files.
        load_env_file()
        cfg.api_url = os.environ.get("ICAMFIND_API_URL", cfg.api_url)
        cfg.api_token = os.environ.get("ICAMFIND_TOKEN", cfg.api_token)
        if isinstance(cfg.source, str) and cfg.source.isdigit():
            cfg.source = int(cfg.source)
        return cfg

    def save(self, path: str | os.PathLike | None = None) -> None:
        path = Path(path or DEFAULT_CONFIG_PATH)
        data = asdict(self)
        data.pop("api_token", None)  # never persist the secret
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
