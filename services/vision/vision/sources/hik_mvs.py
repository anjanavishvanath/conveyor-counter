"""
services/vision/vision/sources/hik_mvs.py

Hikrobot GigE camera (MV-CS016-10GC) through the MVS SDK's Python binding (ctypes).
Runs on: the board CPU (capture + debayer). Assumes Ubuntu 24.04 aarch64 with
MVS-5.1.0_Linux_aarch64 installed by its setup.sh into /opt/MVS.

UNVERIFIED until the first run on the board (tools/board/camera_check.py):
  * the Python binding location (hik_mvs.sdk_python_path) and library (hik_mvs.sdk_lib).
    Find them with:  find /opt/MVS -name MvCameraControl_class.py ; find /opt/MVS -name libMvCameraControl.so
  * the colour order of BayerRG8 after debayering (check that a red object looks red in the snapshot).

Behaviour that matters for counting and the interlock
  * Fixed exposure and gain (auto off), so the image does not drift with the room light.
  * Trigger off, free-running at hik_mvs.fps, and "latest image only" grabbing: if processing is
    slower than the camera, old frames are dropped in the SDK instead of queueing up latency.
    Dropped frames show up as gaps in the camera's own frame number, which the controller counts
    (frames_lost) and turns into a FAULT above safety.max_frame_gap while a batch is active.
  * Incomplete frames (lost GigE packets) are discarded, never passed to the detector.
  * No frame within the timeout or any SDK error -> SourceError -> FAULT; the service then tries to
    reopen the camera every few seconds. Restarting the conveyor still needs an operator START.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from vision.sources.base import FrameSource, SourceError
from vision.types import Frame

log = logging.getLogger("hik_mvs")

# GenICam PFNC pixel format codes (as reported in MV_FRAME_OUT_INFO_EX.enPixelType)
PIXEL_TYPES = {
    0x01080001: "Mono8",
    0x01080008: "BayerGR8",
    0x01080009: "BayerRG8",
    0x0108000A: "BayerGB8",
    0x0108000B: "BayerBG8",
    0x02180014: "RGB8Packed",
    0x02180015: "BGR8Packed",
}

# OpenCV names a Bayer pattern after the 2x2 block starting at the SECOND row and column, so the
# sensor's first-row pattern maps to the "opposite" OpenCV code (RGGB sensor -> COLOR_BayerBG2BGR).
_BAYER_TO_BGR = {
    "BayerRG8": cv2.COLOR_BayerBG2BGR,
    "BayerBG8": cv2.COLOR_BayerRG2BGR,
    "BayerGR8": cv2.COLOR_BayerGB2BGR,
    "BayerGB8": cv2.COLOR_BayerGR2BGR,
}

MV_OK = 0
MV_E_NODATA = 0x80000007
MV_GIGE_DEVICE = 0x00000001
MV_ACCESS_EXCLUSIVE = 1
MV_GRAB_LATEST_IMAGES_ONLY = 1


def to_bgr(raw: np.ndarray, width: int, height: int, pixel_format: str) -> np.ndarray:
    """Convert one raw camera buffer to a BGR image (pure function, unit-tested on the PC)."""
    if pixel_format in _BAYER_TO_BGR:
        return cv2.cvtColor(raw[: width * height].reshape(height, width), _BAYER_TO_BGR[pixel_format])
    if pixel_format == "Mono8":
        return cv2.cvtColor(raw[: width * height].reshape(height, width), cv2.COLOR_GRAY2BGR)
    if pixel_format == "BGR8Packed":
        return raw[: width * height * 3].reshape(height, width, 3).copy()
    if pixel_format == "RGB8Packed":
        return cv2.cvtColor(raw[: width * height * 3].reshape(height, width, 3), cv2.COLOR_RGB2BGR)
    raise SourceError(f"unsupported pixel format {pixel_format}")


def ip_to_str(n: int) -> str:
    return ".".join(str((n >> s) & 0xFF) for s in (24, 16, 8, 0))


def _cstr(arr) -> str:
    return bytes(arr).split(b"\0", 1)[0].decode("ascii", "replace")


def load_sdk(python_path: Path, lib: Path):
    """Import the MVS Python binding. Preloading the .so by full path means the binding finds it
    even under systemd, where /etc/profile.d (LD_LIBRARY_PATH from the SDK installer) is not read."""
    if lib:
        # Some binding versions build the library path from this variable (set by the SDK
        # installer in /etc/profile.d, which systemd services do not read).
        os.environ.setdefault("MVCAM_COMMON_RUNENV", str(Path(lib).parent.parent))
    if lib and Path(lib).is_file():
        try:
            ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            raise SourceError(f"cannot load {lib}: {e}") from e
    if not Path(python_path).is_dir():
        raise SourceError(f"MVS Python binding not found in {python_path} "
                          f"(find /opt/MVS -name MvCameraControl_class.py) - set hik_mvs.sdk_python_path")
    if str(python_path) not in sys.path:
        sys.path.insert(0, str(python_path))
    try:
        import MvCameraControl_class as sdk  # type: ignore
    except Exception as e:  # noqa: BLE001 - OSError from LoadLibrary, ImportError, ...
        raise SourceError(f"cannot import the MVS Python binding from {python_path}: {e}") from e
    return sdk


def list_cameras(sdk) -> List[Dict[str, Any]]:
    """GigE cameras the SDK can see (also across subnets: discovery is broadcast)."""
    dev_list = sdk.MV_CC_DEVICE_INFO_LIST()
    ret = sdk.MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE, dev_list)
    if ret != MV_OK:
        raise SourceError(f"MV_CC_EnumDevices failed: 0x{ret:08x}")
    out = []
    for i in range(dev_list.nDeviceNum):
        info = ctypes.cast(dev_list.pDeviceInfo[i], ctypes.POINTER(sdk.MV_CC_DEVICE_INFO)).contents
        g = info.SpecialInfo.stGigEInfo
        out.append({
            "index": i,
            "info": info,
            "ip": ip_to_str(g.nCurrentIp),
            "model": _cstr(g.chModelName),
            "serial": _cstr(g.chSerialNumber),
            "user_name": _cstr(g.chUserDefinedName),
        })
    return out


class HikMvsSource(FrameSource):
    def __init__(self, cfg):
        self.cfg = cfg
        self._sdk = None
        self._cam = None
        self._initialized = False
        self._pixel_format = cfg.pixel_format
        self.incomplete_frames = 0
        self.device: Dict[str, Any] = {}

    @property
    def nominal_fps(self) -> float:
        return float(self.cfg.fps)

    # ───────────────────────── helpers ─────────────────────────
    def _check(self, ret: int, what: str) -> None:
        if ret != MV_OK:
            raise SourceError(f"{what} failed: 0x{ret & 0xFFFFFFFF:08x}")

    def _set(self, kind: str, name: str, value, required: bool = True) -> None:
        cam = self._cam
        if kind == "enum_str":
            ret = cam.MV_CC_SetEnumValueByString(name, value)
        elif kind == "enum":
            ret = cam.MV_CC_SetEnumValue(name, value)
        elif kind == "float":
            ret = cam.MV_CC_SetFloatValue(name, float(value))
        elif kind == "int":
            ret = cam.MV_CC_SetIntValue(name, int(value))
        elif kind == "bool":
            ret = cam.MV_CC_SetBoolValue(name, bool(value))
        else:
            raise ValueError(kind)
        if ret != MV_OK:
            msg = f"set {name}={value} failed: 0x{ret & 0xFFFFFFFF:08x}"
            if required:
                raise SourceError(msg)
            log.warning(msg)

    # ───────────────────────── lifecycle ─────────────────────────
    def open(self) -> None:
        c = self.cfg
        self._sdk = sdk = load_sdk(c.sdk_python_path, c.sdk_lib)
        if hasattr(sdk.MvCamera, "MV_CC_Initialize"):     # SDK 4.x+ needs it once per process
            ret = sdk.MvCamera.MV_CC_Initialize()
            if ret != MV_OK:                                 # e.g. already initialised by the caller
                log.debug("MV_CC_Initialize returned 0x%08x", ret & 0xFFFFFFFF)
            self._initialized = True

        cams = list_cameras(sdk)
        if not cams:
            raise SourceError("no GigE camera found (cable, PoE, NIC address, rp_filter?)")
        if c.ip:
            match = [d for d in cams if d["ip"] == c.ip]
        elif c.serial:
            match = [d for d in cams if d["serial"] == c.serial]
        else:
            match = cams if len(cams) == 1 else []
        if not match:
            found = ", ".join(f"{d['model']} {d['serial']} @ {d['ip']}" for d in cams)
            raise SourceError(f"configured camera (ip={c.ip}, serial={c.serial}) not found; found: {found}")
        self.device = {k: v for k, v in match[0].items() if k != "info"}

        cam = sdk.MvCamera()
        self._check(cam.MV_CC_CreateHandle(match[0]["info"]), "MV_CC_CreateHandle")
        self._cam = cam
        try:
            self._check(cam.MV_CC_OpenDevice(MV_ACCESS_EXCLUSIVE, 0),
                        f"open camera {self.device['ip']} (in use by the MVS client, or wrong subnet?)")
            size = c.packet_size or cam.MV_CC_GetOptimalPacketSize()
            if size > 0:
                self._set("int", "GevSCPSPacketSize", size, required=False)
            self._set("enum", "TriggerMode", 0)                                # free-running
            self._set("enum_str", "PixelFormat", c.pixel_format)
            self._set("enum", "ExposureAuto", 0, required=False)
            self._set("float", "ExposureTime", c.exposure_us)
            self._set("enum", "GainAuto", 0, required=False)
            self._set("float", "Gain", c.gain_db, required=False)
            self._set("bool", "AcquisitionFrameRateEnable", True, required=False)
            self._set("float", "AcquisitionFrameRate", c.fps, required=False)
            if hasattr(cam, "MV_CC_SetImageNodeNum"):
                cam.MV_CC_SetImageNodeNum(c.buffer_frames)
            if hasattr(cam, "MV_CC_SetGrabStrategy"):
                cam.MV_CC_SetGrabStrategy(MV_GRAB_LATEST_IMAGES_ONLY)
            self._check(cam.MV_CC_StartGrabbing(), "MV_CC_StartGrabbing")
        except Exception:
            self.close()
            raise
        log.info("camera %s %s @ %s open: %s, %.0f us, %.1f dB, %.1f fps, packet %s",
                 self.device["model"], self.device["serial"], self.device["ip"],
                 c.pixel_format, c.exposure_us, c.gain_db, c.fps, size)

    def read(self, timeout_s: float = 1.0) -> Optional[Frame]:
        if self._cam is None:
            raise SourceError("camera not open")
        sdk = self._sdk
        deadline = time.monotonic() + timeout_s
        while True:
            left_ms = max(1, int((deadline - time.monotonic()) * 1000))
            out = sdk.MV_FRAME_OUT()
            ctypes.memset(ctypes.byref(out), 0, ctypes.sizeof(out))
            ret = self._cam.MV_CC_GetImageBuffer(out, left_ms) & 0xFFFFFFFF   # ctypes returns a signed int
            if ret == MV_E_NODATA or (ret != MV_OK and time.monotonic() >= deadline):
                raise SourceError(f"no frame from camera within {timeout_s:.1f} s (0x{ret & 0xFFFFFFFF:08x})")
            if ret != MV_OK:
                raise SourceError(f"MV_CC_GetImageBuffer failed: 0x{ret & 0xFFFFFFFF:08x}")
            try:
                fi = out.stFrameInfo
                lost = int(getattr(fi, "nLostPacket", 0) or 0)
                if lost:
                    # Incomplete frame: never give a half image to the detector. Skipping it leaves
                    # a frame-number gap, which the controller counts as a lost frame.
                    self.incomplete_frames += 1
                    if self.incomplete_frames in (1, 10, 100) or self.incomplete_frames % 1000 == 0:
                        log.warning("dropped incomplete frame %d (%d lost packets; %d so far) - check "
                                    "packet size / jumbo frames / cabling", fi.nFrameNum, lost, self.incomplete_frames)
                    if time.monotonic() >= deadline:
                        raise SourceError("only incomplete frames from the camera")
                    continue
                w, h = int(fi.nWidth), int(fi.nHeight)
                fmt = PIXEL_TYPES.get(int(fi.enPixelType))
                if fmt is None:
                    raise SourceError(f"unexpected pixel type 0x{int(fi.enPixelType):08x}")
                n = int(fi.nFrameLen)
                buf = (ctypes.c_ubyte * n).from_address(ctypes.addressof(out.pBufAddr.contents))
                img = to_bgr(np.frombuffer(buf, dtype=np.uint8, count=n), w, h, fmt)  # copies
                return Frame(image=img, frame_id=int(fi.nFrameNum), timestamp=time.monotonic())
            finally:
                self._cam.MV_CC_FreeImageBuffer(out)

    def close(self) -> None:
        cam, self._cam = self._cam, None
        if cam is not None:
            for fn in ("MV_CC_StopGrabbing", "MV_CC_CloseDevice", "MV_CC_DestroyHandle"):
                try:
                    getattr(cam, fn)()
                except Exception:  # noqa: BLE001 - best effort while tearing down
                    pass
        if self._initialized and self._sdk is not None and hasattr(self._sdk.MvCamera, "MV_CC_Finalize"):
            try:
                self._sdk.MvCamera.MV_CC_Finalize()
            except Exception:  # noqa: BLE001
                pass
            self._initialized = False
