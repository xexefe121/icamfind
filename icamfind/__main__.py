"""iCamFind command line.

  python -m icamfind run          start the camera memory (opens a preview window)
  python -m icamfind calibrate    draw zones, the door, and the room-map points
  python -m icamfind record       record a clip for evaluation
  python -m icamfind annotate     label ground-truth events in a clip
  python -m icamfind eval         score a clip against its ground truth
  python -m icamfind benchmark    generate the synthetic benchmark and score it
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


def _cfg(path: str):
    from .edge.config import Config

    return Config.load(path)


def cmd_run(args) -> None:
    from .edge.app import EdgeApp

    cfg = _cfg(args.config)
    if args.source is not None:
        cfg.source = int(args.source) if str(args.source).isdigit() else args.source
    EdgeApp(cfg, show=not args.headless).run(max_seconds=args.max_seconds)


def cmd_calibrate(args) -> None:
    from .edge.calibrate import calibrate

    cfg = _cfg(args.config)
    if args.source is not None:
        cfg.source = int(args.source) if str(args.source).isdigit() else args.source
    calibrate(cfg, args.config)


def cmd_record(args) -> None:
    import cv2 as cv

    from .edge.app import open_source, resize_for_processing

    cfg = _cfg(args.config)
    cap = open_source(cfg.source)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    end = time.time() + args.seconds
    while time.time() < end:
        ok, frame = cap.read()
        if not ok:
            break
        frame = resize_for_processing(frame, cfg.process_width)
        if writer is None:
            writer = cv.VideoWriter(str(out), cv.VideoWriter_fourcc(*"mp4v"), args.fps, frame.shape[1::-1])
        writer.write(frame)
        cv.imshow("recording (q to stop)", frame)
        if cv.waitKey(max(1, int(1000 / args.fps))) & 0xFF == ord("q"):
            break
    cap.release()
    if writer:
        writer.release()
    cv.destroyAllWindows()
    print(f"saved {out}")


def cmd_annotate(args) -> None:
    from .eval.annotate import annotate

    video = Path(args.video)
    annotate(video, _cfg(args.config), Path(args.out or video.with_suffix(".truth.json")))


def cmd_eval(args) -> None:
    from .eval.evaluate import evaluate

    cfg = _cfg(args.config)
    result = evaluate(Path(args.video), Path(args.truth), cfg, use_dnn=not args.no_dnn)
    print(json.dumps({k: v for k, v in result.items() if k != "predictions"}, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")


def cmd_benchmark(args) -> None:
    from .eval.evaluate import evaluate
    from .eval.report import write_report
    from .eval.synth import make_clip

    out = Path(args.out)
    results = []
    for seed in args.seeds:
        video, truth = make_clip(out, seed)
        res = evaluate(video, truth, _cfg(args.config) if Path(args.config).exists() else None,
                       use_dnn=not args.no_dnn)
        print(f"seed {seed}: P={res['precision']} R={res['recall']} F1={res['f1']} "
              f"zone={res['zone_accuracy']} latency={res['mean_latency_s']}s id={res['identity_consistency']}")
        results.append(res)
    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    write_report(results, Path(args.report))
    print(f"wrote {out / 'results.json'} and {args.report}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="icamfind", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run")
    s.add_argument("--config", default="config.json")
    s.add_argument("--source", help="webcam index, RTSP URL or video file (overrides config)")
    s.add_argument("--headless", action="store_true")
    s.add_argument("--max-seconds", type=float, help="stop after this many seconds (testing)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("calibrate")
    s.add_argument("--config", default="config.json")
    s.add_argument("--source")
    s.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("record")
    s.add_argument("--config", default="config.json")
    s.add_argument("--out", default="clips/clip.mp4")
    s.add_argument("--seconds", type=float, default=180)
    s.add_argument("--fps", type=float, default=15)
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("annotate")
    s.add_argument("--video", required=True)
    s.add_argument("--config", default="config.json")
    s.add_argument("--out")
    s.set_defaults(func=cmd_annotate)

    s = sub.add_parser("eval")
    s.add_argument("--video", required=True)
    s.add_argument("--truth", required=True)
    s.add_argument("--config", default="config.json")
    s.add_argument("--no-dnn", action="store_true", help="skip the person detector (synthetic clips)")
    s.add_argument("--out")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("benchmark")
    s.add_argument("--config", default="config.json")
    s.add_argument("--out", default="bench")
    s.add_argument("--report", default="docs/EVALUATION.md")
    s.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    s.add_argument("--no-dnn", action="store_true")
    s.set_defaults(func=cmd_benchmark)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
