"""Ablation study on the synthetic benchmark: what each part of the scene-memory pipeline contributes.

    python scripts/ablation.py            (expects bench/synthetic_*.mp4 from `python -m icamfind benchmark`)

Writes docs/ABLATION.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from icamfind.edge import scene as scene_mod  # noqa: E402
from icamfind.edge.config import Config  # noqa: E402
from icamfind.eval.evaluate import evaluate  # noqa: E402


def variant_full(cfg):
    return cfg


def variant_no_lighting_guard(cfg):
    cfg.lighting_change_fraction = 1.01
    return cfg


def variant_no_reid(cfg):
    scene_mod.SceneMemory.REID_THRESHOLD = 2.0
    return cfg


def variant_no_ghost_test(cfg):
    orig = scene_mod.SceneMemory._boundary_gradients
    scene_mod.SceneMemory._boundary_gradients = lambda self, cur, under, mask: (1.0, 0.0)
    variant_no_ghost_test.restore = lambda: setattr(scene_mod.SceneMemory, "_boundary_gradients", orig)
    return cfg


def variant_short_stability(cfg):
    cfg.stable_seconds = 0.4
    return cfg


VARIANTS = [
    ("Full pipeline", variant_full),
    ("No lighting guard (no re-baseline on global change)", variant_no_lighting_guard),
    ("No ghost test (every change treated as 'placed')", variant_no_ghost_test),
    ("No re-identification (moves become new objects)", variant_no_reid),
    ("Short stability window (0.4 s instead of 2 s)", variant_short_stability),
]


def main() -> None:
    clips = sorted((ROOT / "bench").glob("synthetic_*.mp4"))
    if not clips:
        sys.exit("run `python -m icamfind benchmark --no-dnn` first")
    rows = []
    for name, make in VARIANTS:
        scene_mod.SceneMemory.REID_THRESHOLD = 0.6
        tp = truth = pred = 0
        ids_ok = ids_n = 0
        lat = []
        for clip in clips:
            cfg = make(Config())
            res = evaluate(clip, clip.with_suffix("").with_suffix(".truth.json"), cfg, use_dnn=False)
            tp += res["true_positives"]
            truth += res["truth_events"]
            pred += res["predicted_events"]
            a, b = res["identity_consistency"].split("/")
            ids_ok, ids_n = ids_ok + int(a), ids_n + int(b)
            if res["mean_latency_s"] is not None:
                lat.append(res["mean_latency_s"])
        if hasattr(make, "restore"):
            make.restore()
        p = tp / pred if pred else 1.0
        r = tp / truth if truth else 1.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        rows.append((name, pred, round(p, 3), round(r, 3), round(f1, 3), f"{ids_ok}/{ids_n}",
                     round(sum(lat) / len(lat), 2) if lat else None))
        print(rows[-1], flush=True)

    lines = ["# Ablation study (synthetic benchmark, 5 seeds)", "",
             "Each row disables one part of the scene-memory pipeline. Event matching rules are the same as in "
             "`docs/EVALUATION.md` (type + time window; moved counts as placed).", "",
             "| Variant | Predicted events | Precision | Recall | F1 | Identity kept across moves | Mean latency (s) |",
             "|---|---|---|---|---|---|---|"]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |" for r in rows]
    (ROOT / "docs" / "ABLATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
