"""Render benchmark results as Markdown for the technical report."""

from __future__ import annotations

import platform
import time
from pathlib import Path

import cv2 as cv


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def write_report(results: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tp = sum(r["true_positives"] for r in results)
    truth = sum(r["truth_events"] for r in results)
    pred = sum(r["predicted_events"] for r in results)
    precision = tp / pred if pred else 1.0
    recall = tp / truth if truth else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    lines = [
        "# Evaluation results (synthetic benchmark)",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M')} with OpenCV {cv.__version__} on "
        f"{platform.system()} {platform.machine()} / Python {platform.python_version()}.",
        "Reproduce: `python -m icamfind benchmark --no-dnn` (deterministic seeds).",
        "",
        "| Clip | Truth events | Predicted | Precision | Recall | F1 | Zone acc. | Mean latency (s) | Identity kept | Rebaselines | ms/frame (median) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        perf = r.get("performance", {})
        lines.append(
            f"| {r['video']} | {r['truth_events']} | {r['predicted_events']} | {r['precision']} | {r['recall']} | "
            f"{r['f1']} | {r['zone_accuracy']} | {r['mean_latency_s']} | {r['identity_consistency']} | "
            f"{perf.get('rebaselines')} | {perf.get('median_ms_per_frame')} |"
        )
    lines += [
        f"| **All** | **{truth}** | **{pred}** | **{precision:.3f}** | **{recall:.3f}** | **{f1:.3f}** | "
        f"**{_mean([r['zone_accuracy'] for r in results])}** | **{_mean([r['mean_latency_s'] for r in results])}** | | | |",
        "",
        "## Recall by event type",
        "",
        "| Type | Truth | Detected | Recall |",
        "|---|---|---|---|",
    ]
    for kind in ("placed", "removed", "moved"):
        n = sum(r["by_type"][kind]["truth"] for r in results)
        d = sum(r["by_type"][kind]["detected"] for r in results)
        lines.append(f"| {kind} | {n} | {d} | {round(d / n, 3) if n else '-'} |")

    lines += ["", "## Failure cases", ""]
    any_fail = False
    for r in results:
        for m in r["missed"]:
            any_fail = True
            lines.append(f"- **Missed** in {r['video']}: {m['type']} `{m['object']}` @ {m['zone']} (t={m['t']} s)")
        for fp in r["false_positives"]:
            any_fail = True
            lines.append(f"- **False positive** in {r['video']}: {fp['type']} `{fp['label']}` @ {fp['zone']} "
                         f"(t={round(fp['ts'], 1)} s)")
    if not any_fail:
        lines.append("- None on this benchmark (see the real-footage limitations in the report).")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
