"""Score the browser engine (edge.js on OpenCV.js 5.0) against the synthetic benchmark, next to Python.

    python scripts/browser_parity.py --opencv-js path/to/opencv.js [--seeds 1 2 3 4 5]

Needs Node.js. Decodes each bench/synthetic_N.mp4 with scripts/dump_frames.py, replays the frames through
the DOM-free engine core (scripts/web_engine_bench.js), scores with the same rules as the Python
benchmark, and writes docs/BROWSER_PARITY.md.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from icamfind.eval.evaluate import score  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--opencv-js", required=True)
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    args = p.parse_args()
    python_results = {r["video"]: r for r in json.loads((ROOT / "bench" / "results.json").read_text())}
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for seed in args.seeds:
            video = ROOT / "bench" / f"synthetic_{seed}.mp4"
            truth_path = video.with_suffix("").with_suffix(".truth.json")
            frames = Path(tmp) / f"s{seed}.bin"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "dump_frames.py"), str(video), str(frames)], check=True)
            out = Path(tmp) / f"s{seed}.json"
            subprocess.run(["node", str(ROOT / "scripts" / "web_engine_bench.js"), args.opencv_js, str(truth_path),
                            str(out), str(frames)], check=True, capture_output=True)
            data = json.loads(out.read_text())
            truth = json.loads(truth_path.read_text())["events"]
            s = score(data["events"], truth)
            py = python_results.get(video.name, {})
            rows.append((video.name, s["precision"], s["recall"], s["f1"], s["identity_consistency"],
                         py.get("f1"), data.get("perf", {}).get("median_ms_per_frame")))
            print(rows[-1], flush=True)
    lines = ["# Browser engine parity (OpenCV.js 5.0.0 WebAssembly vs Python OpenCV 5)", "",
             "Same synthetic clips and scoring rules as `docs/EVALUATION.md`; the browser engine runs with",
             "`placesOnly` off so both see the full frame. Reproduce: `python scripts/browser_parity.py --opencv-js <opencv.js>`.", "",
             "| Clip | Browser precision | Browser recall | Browser F1 | Identity kept | Python F1 | Browser ms/frame (median, Node) |",
             "|---|---|---|---|---|---|---|"]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |" for r in rows]
    (ROOT / "docs" / "BROWSER_PARITY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
