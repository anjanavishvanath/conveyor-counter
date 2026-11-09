"""
services/vision/tests/test_hik_mvs.py

The parts of the Hikrobot source that do not need the SDK or a camera: pixel conversion and
address formatting. The SDK path itself is exercised on the board by tools/board/camera_check.py.
"""
import numpy as np
import pytest

from vision.sources.base import SourceError
from vision.sources.hik_mvs import ip_to_str, to_bgr


def mosaic(pattern: str, rgb, h=6, w=8):
    """Build a raw Bayer buffer of a flat colour. pattern = first-row/second-row order, e.g. 'RGGB'."""
    val = {"R": rgb[0], "G": rgb[1], "B": rgb[2]}
    raw = np.zeros((h, w), np.uint8)
    raw[0::2, 0::2] = val[pattern[0]]
    raw[0::2, 1::2] = val[pattern[1]]
    raw[1::2, 0::2] = val[pattern[2]]
    raw[1::2, 1::2] = val[pattern[3]]
    return raw.ravel()


@pytest.mark.parametrize("fmt,pattern", [("BayerRG8", "RGGB"), ("BayerBG8", "BGGR"),
                                         ("BayerGR8", "GRBG"), ("BayerGB8", "GBRG")])
def test_bayer_colour_order(fmt, pattern):
    red = to_bgr(mosaic(pattern, (200, 0, 0)), 8, 6, fmt)
    blue = to_bgr(mosaic(pattern, (0, 0, 200)), 8, 6, fmt)
    assert red.shape == (6, 8, 3)
    b, g, r = red[3, 4]
    assert r > 150 and g < 50 and b < 50, f"{fmt}: red came out as BGR {red[3, 4]}"
    b, g, r = blue[3, 4]
    assert b > 150 and g < 50 and r < 50, f"{fmt}: blue came out as BGR {blue[3, 4]}"


def test_packed_and_mono():
    rgb = np.tile(np.array([10, 20, 30], np.uint8), 4 * 2)
    assert tuple(to_bgr(rgb, 4, 2, "RGB8Packed")[0, 0]) == (30, 20, 10)
    assert tuple(to_bgr(rgb, 4, 2, "BGR8Packed")[0, 0]) == (10, 20, 30)
    mono = np.full(8, 77, np.uint8)
    assert to_bgr(mono, 4, 2, "Mono8").shape == (2, 4, 3)
    with pytest.raises(SourceError):
        to_bgr(mono, 4, 2, "YUV422")


def test_ip_to_str():
    assert ip_to_str(0xC0A80040) == "192.168.0.64"
