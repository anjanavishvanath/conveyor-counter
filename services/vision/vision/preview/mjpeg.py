"""
services/vision/vision/preview/mjpeg.py

A small MJPEG server inside the vision service, because only this process has the frames.

What it is for
  * commissioning: see the counting line, the dead band, the tracks and which box was counted;
  * count disputes: show a supervisor what the system actually saw;
  * a live tile in the kiosk UI.

What it is not: a recording system, and not on the counting path. Frames are encoded at
`preview.fps` only (default 5), the newest frame always wins, and a slow or stalled HTTP
client can never hold up the camera loop - it just misses frames.
"""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import cv2
import numpy as np

from vision.config import PreviewCfg

log = logging.getLogger("preview")

BOUNDARY = "frameboundary"

_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Conveyor counter preview</title>
<style>html,body{margin:0;background:#111;color:#eee;font:14px system-ui,sans-serif}
img{display:block;max-width:100%;height:auto;margin:0 auto}
p{padding:8px 12px;margin:0;opacity:.7}</style></head>
<body><img src="/preview.mjpg" alt="preview"><p>green = counted, orange = tracked, grey = raw detection, red band = counting line</p></body></html>
"""


class _Latest:
    """Holds the most recent encoded JPEG and wakes up waiting clients."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._jpeg: Optional[bytes] = None
        self._seq = 0

    def put(self, jpeg: bytes) -> None:
        with self._cv:
            self._jpeg = jpeg
            self._seq += 1
            self._cv.notify_all()

    def get(self) -> Optional[bytes]:
        with self._cv:
            return self._jpeg

    def wait_next(self, last_seq: int, timeout: float = 5.0):
        with self._cv:
            if self._seq == last_seq:
                self._cv.wait(timeout)
            return self._jpeg, self._seq


class PreviewServer:
    def __init__(self, cfg: PreviewCfg):
        self.cfg = cfg
        self._latest = _Latest()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._min_interval = 1.0 / cfg.fps
        self._last_publish = 0.0

    # ── called from the vision loop ──
    def should_publish(self, now: float) -> bool:
        """Cheap rate gate: ask before doing the work of annotating a frame."""
        return self.cfg.enabled and (now - self._last_publish) >= self._min_interval

    def publish(self, image_bgr: np.ndarray, now: float) -> None:
        if not self.cfg.enabled:
            return
        self._last_publish = now
        try:
            h, w = image_bgr.shape[:2]
            if w > self.cfg.max_width:
                scale = self.cfg.max_width / w
                image_bgr = cv2.resize(image_bgr, (self.cfg.max_width, max(1, int(h * scale))),
                                       interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", image_bgr,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality])
            if ok:
                self._latest.put(buf.tobytes())
        except Exception as e:  # noqa: BLE001 - preview problems must never stop counting
            log.debug("preview encode failed: %s", e)

    # ── lifecycle ──
    def start(self) -> None:
        if not self.cfg.enabled:
            log.info("preview disabled")
            return
        latest = self._latest

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # keep the service log readable
                pass

            def _no(self, code=404):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path.startswith("/preview.mjpg"):
                    self._stream()
                elif self.path.startswith("/snapshot.jpg"):
                    self._snapshot()
                elif self.path in ("/", "/index.html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(_PAGE)))
                    self.end_headers()
                    self.wfile.write(_PAGE)
                else:
                    self._no()

            def _snapshot(self):
                jpeg = latest.get()
                if jpeg is None:
                    return self._no(503)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpeg)

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seq = -1
                try:
                    while True:
                        jpeg, seq = latest.wait_next(seq, timeout=5.0)
                        if jpeg is None:
                            continue
                        self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass  # client closed the tab

        self._httpd = ThreadingHTTPServer((self.cfg.host, self.cfg.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="preview", daemon=True)
        self._thread.start()
        log.info("preview on http://%s:%d/ (%.0f fps, q%d)", self.cfg.host, self.cfg.port,
                 self.cfg.fps, self.cfg.jpeg_quality)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
