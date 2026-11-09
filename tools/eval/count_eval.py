#!/usr/bin/env python3
"""
tools/eval/count_eval.py — checkpoint 2 acceptance tool

Runs the production counting pipeline (same code as the vision service) over a recorded video and
reports the count, speed and "suspect" events, optionally against a known ground-truth count.

Examples (run from the repo root):
  # full run, compare with the known count, write an annotated video and a CSV of count events
  python tools/eval/count_eval.py --video data/videos/250_coconuts.mp4 --expected 250 \
      --out data/eval/250_annotated.mp4 --events-csv data/eval/250_events.csv --save-dets data/eval/250_dets.npz

  # re-tune tracking/counting settings in seconds by replaying the saved detections (no model run)
  python tools/eval/count_eval.py --video data/videos/250_coconuts.mp4 --expected 250 --load-dets data/eval/250_dets.npz

  # simulate the board running at 15 fps on a 30 fps recording
  python tools/eval/count_eval.py --video data/videos/250_coconuts.mp4 --expected 250 --stride 2

Exit code: 0 = pass (or no --expected), 1 = count outside tolerance, 2 = error.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services" / "vision"))

from vision.config import ConfigError, load_config, load_profile  # noqa: E402
from vision.detectors.factory import create_detector  # noqa: E402
from vision.pipeline import CountingPipeline  # noqa: E402
from vision.sources.video_file import VideoFileSource  # noqa: E402
from vision.types import Detections, Frame  # noqa: E402


# ─── detection cache (lets you tune tracking/counting without re-running the model) ───
def save_dets(path: Path, rows, frame_size, stride, model_file):
    path.parent.mkdir(parents=True, exist_ok=True)
    lens = np.array([len(d) for _, _, d in rows], dtype=np.int64)
    np.savez_compressed(
        path,
        frame_ids=np.array([r[0] for r in rows], dtype=np.int64),
        timestamps=np.array([r[1] for r in rows], dtype=np.float64),
        lens=lens,
        boxes=np.concatenate([d.boxes for _, _, d in rows]) if rows else np.zeros((0, 4), np.float32),
        scores=np.concatenate([d.scores for _, _, d in rows]) if rows else np.zeros((0,), np.float32),
        class_ids=np.concatenate([d.class_ids for _, _, d in rows]) if rows else np.zeros((0,), np.int32),
        frame_size=np.array(frame_size), stride=np.array(stride), model_file=np.array(str(model_file)),
    )


def load_dets(path: Path):
    z = np.load(path, allow_pickle=False)
    offs = np.concatenate([[0], np.cumsum(z["lens"])])
    rows = []
    for i, (fid, ts) in enumerate(zip(z["frame_ids"], z["timestamps"])):
        a, b = offs[i], offs[i + 1]
        rows.append((int(fid), float(ts), Detections(z["boxes"][a:b], z["scores"][a:b], z["class_ids"][a:b])))
    return rows, tuple(int(v) for v in z["frame_size"]), int(z["stride"]), str(z["model_file"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline count evaluation on a recorded video")
    ap.add_argument("--config", help="site.yaml (default: config/site.yaml or $COUNTER_SITE_CONFIG)")
    ap.add_argument("--profile", help="override the product profile YAML")
    ap.add_argument("--video", help="override the video file")
    ap.add_argument("--expected", type=int, help="ground-truth count for pass/fail")
    ap.add_argument("--tolerance", type=int, default=0, help="allowed |count - expected| (default 0)")
    ap.add_argument("--stride", type=int, default=1, help="use every Nth frame (simulates a lower fps)")
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--out", help="write an annotated video (mp4)")
    ap.add_argument("--out-width", type=int, default=960, help="width of the annotated video")
    ap.add_argument("--events-csv", help="write one row per count event")
    ap.add_argument("--save-dets", help="save raw detections (.npz) for fast replays")
    ap.add_argument("--load-dets", help="replay saved detections instead of running the model")
    ap.add_argument("--show", action="store_true", help="show a live window (needs opencv-python with GUI)")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
        profile = load_profile(args.profile) if args.profile else cfg.profile
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2
    video = Path(args.video) if args.video else cfg.site.frame_source.video_file.path

    need_images = bool(args.out or args.show)
    cached = None
    if args.load_dets:
        cached, frame_size, cached_stride, cached_model = load_dets(Path(args.load_dets))
        if cached_stride != args.stride:
            print(f"ERROR: detections were saved with --stride {cached_stride}, not {args.stride}", file=sys.stderr)
            return 2
        detector = None
        print(f"Replaying {len(cached)} cached frames (model: {Path(cached_model).name})")
    else:
        detector = create_detector(cfg if not args.profile else cfg.model_copy(update={"profile": profile}))
        print(f"Detector: {type(detector).__name__} on {detector.device}")

    pipe = CountingPipeline(profile, detector)
    src = None
    if cached is None or need_images:
        src = VideoFileSource(video, realtime=False, stride=args.stride)
        src.open()
        frame_size = (int(src._cap.get(3)), int(src._cap.get(4)))
        print(f"Video: {video.name}  {frame_size[0]}x{frame_size[1]} @ {src._fps:.1f} fps, "
              f"{src.frame_count} frames, stride {args.stride} -> effective {src.nominal_fps:.1f} fps")

    writer = None
    if args.out:
        import cv2
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        out_h = int(frame_size[1] * args.out_width / frame_size[0]) // 2 * 2
        writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"),
                                 (src.nominal_fps if src else 15.0), (args.out_width, out_h))
    if args.show or args.out:
        import cv2
        from vision.draw import annotate

    dummy = np.zeros((frame_size[1], frame_size[0], 3), np.uint8)
    rows_for_cache, events, infer_ms, pipe_ms = [], [], [], []
    n = 0
    t_start = time.perf_counter()
    try:
        while True:
            if args.max_frames and n >= args.max_frames:
                break
            if cached is not None:
                if n >= len(cached):
                    break
                fid, ts, dets = cached[n]
                if src is not None:
                    fr = src.read()
                    if fr is None:
                        break
                    frame = Frame(fr.image, fid, ts)
                else:
                    frame = Frame(dummy, fid, ts)
                result = pipe.process(frame, detections=dets)
            else:
                frame = src.read()
                if frame is None:
                    break
                result = pipe.process(frame)
                infer_ms.append(sum(v for k, v in result.timings_ms.items() if k != "track_count"))
                if args.save_dets:
                    rows_for_cache.append((frame.frame_id, frame.timestamp, pipe.last_raw_detections))
            pipe_ms.append(result.timings_ms.get("track_count", 0.0))
            for ev in result.new_counts:
                events.append((ev.frame_id, round(ev.timestamp, 3), ev.track_id, result.total))
            n += 1

            if writer is not None or args.show:
                img = annotate(result, profile.counting, set(pipe.counter.stats.counted_ids),
                               extra_text=f"frame {frame.frame_id}  t={frame.timestamp:6.2f}s")
                if writer is not None:
                    writer.write(cv2.resize(img, (args.out_width, int(frame_size[1] * args.out_width / frame_size[0]) // 2 * 2)))
                if args.show:
                    cv2.imshow("count_eval", img)
                    if cv2.waitKey(1) == 27:
                        break
            if n % 200 == 0:
                print(f"  frame {n}: count {result.total}", flush=True)
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        if src:
            src.close()
        if writer:
            writer.release()

    wall = time.perf_counter() - t_start
    if args.save_dets and rows_for_cache:
        save_dets(Path(args.save_dets), rows_for_cache, frame_size, args.stride, profile.model.file)
        print(f"Saved detections -> {args.save_dets}")
    if args.events_csv:
        Path(args.events_csv).parent.mkdir(parents=True, exist_ok=True)
        with open(args.events_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["frame_id", "video_time_s", "track_id", "running_total"])
            w.writerows(events)

    st = pipe.counter.stats
    print("\n──────── RESULT ────────")
    print(f"frames processed : {n}  ({n / wall:.1f} frames/s wall clock on this machine)")
    if infer_ms:
        print(f"detector         : mean {np.mean(infer_ms):.1f} ms, p95 {np.percentile(infer_ms, 95):.1f} ms per frame")
    print(f"tracking+counting: mean {np.mean(pipe_ms):.2f} ms per frame")
    print(f"COUNT            : {st.total}")
    print(f"suspects         : {st.suspects}  {st.as_dict()}")
    if args.expected is not None:
        err = st.total - args.expected
        ok = abs(err) <= args.tolerance
        print(f"expected         : {args.expected}  error {err:+d} ({100 * err / max(1, args.expected):+.2f} %)"
              f"  -> {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
