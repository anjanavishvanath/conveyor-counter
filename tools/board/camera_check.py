#!/usr/bin/env python3
"""
tools/board/camera_check.py — first camera test on the board (checkpoint 8). Run ON THE BOARD.

Uses the same HikMvsSource and site config as the vision service, without the detector, the
broker or the API, so a camera/network problem is isolated from everything else.

    python tools/board/camera_check.py --list                       # cameras the SDK can see
    python tools/board/camera_check.py --seconds 20                 # open, grab, report fps / gaps
    python tools/board/camera_check.py --seconds 20 --snapshot data/cam.jpg

Pass criteria before running the vision service on the camera:
  * measured fps ~= hik_mvs.fps, lost frames 0, incomplete frames 0 over 60 s
  * the snapshot is sharp, correctly exposed, and colours are right (a red object looks red)

Close the MVS client first: the camera is opened exclusively.
Assumes: Ubuntu 24.04 aarch64, MVS 5.1.0 in /opt/MVS. Config: $COUNTER_SITE_CONFIG or --config.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "vision"))

from vision.config import ConfigError, load_config  # noqa: E402
from vision.sources.base import SourceError  # noqa: E402
from vision.sources.hik_mvs import HikMvsSource, list_cameras, load_sdk  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Hikrobot camera check")
    ap.add_argument("--config", default=None, help="site.yaml (default: $COUNTER_SITE_CONFIG or config/site.yaml)")
    ap.add_argument("--list", action="store_true", help="only list the cameras the SDK can see")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--snapshot", default=None, help="save the last frame as JPEG here")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}")
        return 2
    hc = cfg.site.frame_source.hik_mvs
    if hc is None:
        print(f"{cfg.site.path}: frame_source.hik_mvs is missing")
        return 2

    try:
        sdk = load_sdk(hc.sdk_python_path, hc.sdk_lib)
        if hasattr(sdk.MvCamera, "MV_CC_Initialize"):
            sdk.MvCamera.MV_CC_Initialize()
        cams = list_cameras(sdk)
    except SourceError as e:
        print(f"SDK ERROR: {e}")
        return 1
    print(f"{len(cams)} GigE camera(s):")
    for d in cams:
        print(f"  {d['model']:<20} serial {d['serial']:<12} ip {d['ip']:<15} name '{d['user_name']}'")
    if args.list:
        return 0 if cams else 1

    src = HikMvsSource(hc)
    try:
        src.open()
    except SourceError as e:
        print(f"OPEN FAILED: {e}")
        return 1

    n, gaps, lost, last_id, frame = 0, 0, 0, None, None
    read_ms = []
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds:
            t = time.perf_counter()
            frame = src.read(timeout_s=2.0)
            read_ms.append((time.perf_counter() - t) * 1000)
            if last_id is not None and frame.frame_id - last_id > 1:
                gaps += 1
                lost += frame.frame_id - last_id - 1
            last_id = frame.frame_id
            n += 1
            if n % 30 == 0:
                el = time.monotonic() - t0
                print(f"  {el:5.1f} s  frames {n}  {n / el:5.1f} fps  lost {lost}  incomplete {src.incomplete_frames}", end="\r")
    except SourceError as e:
        print(f"\nREAD FAILED after {n} frames: {e}")
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        src.close()

    el = time.monotonic() - t0
    print()
    print("──────── RESULT ────────")
    if frame is not None:
        h, w = frame.image.shape[:2]
        print(f"image            : {w}x{h} ({hc.pixel_format})")
    print(f"frames           : {n} in {el:.1f} s = {n / el:.1f} fps (configured {hc.fps})")
    print(f"lost frames      : {lost} in {gaps} gap(s)")
    print(f"incomplete frames: {src.incomplete_frames}")
    if read_ms:
        read_ms.sort()
        print(f"read+convert     : median {read_ms[len(read_ms) // 2]:.1f} ms (includes waiting for the next frame)")
    if args.snapshot and frame is not None:
        import cv2
        Path(args.snapshot).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(args.snapshot, frame.image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        print(f"snapshot         : {args.snapshot}")
    ok = n > 0 and lost == 0 and src.incomplete_frames == 0 and n / el >= 0.9 * hc.fps
    print("PASS" if ok else "CHECK: see the numbers above (fps low, lost or incomplete frames)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
