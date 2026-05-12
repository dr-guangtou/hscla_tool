"""Tests for `hscla_tool.mask`. Offline; uses a synthetic mask HDU."""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits

from hscla_tool import mask


def _make_mask_hdu(arr: np.ndarray, planes: dict[str, int] | None = None) -> fits.ImageHDU:
    hdu = fits.ImageHDU(arr.astype(np.int32))
    for name, bit in (planes or {}).items():
        hdu.header[f"MP_{name}"] = bit
    return hdu


def test_parse_mask_planes_reads_mp_cards() -> None:
    arr = np.zeros((3, 3), dtype=np.int32)
    hdu = _make_mask_hdu(arr, {"BAD": 0, "SAT": 1, "CR": 3})
    assert mask.parse_mask_planes(hdu.header) == {"BAD": 0, "SAT": 1, "CR": 3}


def test_parse_mask_planes_falls_back_to_defaults() -> None:
    arr = np.zeros((3, 3), dtype=np.int32)
    hdu = _make_mask_hdu(arr, planes=None)
    planes = mask.parse_mask_planes(hdu.header)
    # Default fallback always includes BAD=0, SAT=1, INTRP=2.
    assert planes["BAD"] == 0
    assert planes["SAT"] == 1
    assert planes["INTRP"] == 2


def test_decode_returns_named_boolean_arrays() -> None:
    arr = np.array(
        [[1 << 0, 1 << 1, 0],
         [(1 << 0) | (1 << 3), 0, 1 << 3]],
        dtype=np.int32,
    )
    hdu = _make_mask_hdu(arr, {"BAD": 0, "SAT": 1, "CR": 3})
    planes = mask.decode(hdu)
    assert planes["BAD"].tolist() == [[True, False, False], [True, False, False]]
    assert planes["SAT"].tolist() == [[False, True, False], [False, False, False]]
    assert planes["CR"].tolist() == [[False, False, False], [True, False, True]]


def test_decode_subset_of_planes() -> None:
    arr = np.array([[1 << 1, 1 << 3]], dtype=np.int32)
    hdu = _make_mask_hdu(arr, {"BAD": 0, "SAT": 1, "CR": 3, "EDGE": 4})
    planes = mask.decode(hdu, planes=("SAT", "CR"))
    assert set(planes) == {"SAT", "CR"}
    assert planes["SAT"][0, 0] and not planes["SAT"][0, 1]
    assert not planes["CR"][0, 0] and planes["CR"][0, 1]


def test_decode_unknown_plane_raises() -> None:
    arr = np.zeros((2, 2), dtype=np.int32)
    hdu = _make_mask_hdu(arr, {"BAD": 0})
    with pytest.raises(mask.MaskPlaneError, match="not found"):
        mask.decode(hdu, planes=("NOPE",))


def test_decode_no_data_raises() -> None:
    hdu = fits.ImageHDU()  # data=None
    hdu.header["MP_BAD"] = 0
    with pytest.raises(mask.MaskPlaneError, match="no data"):
        mask.decode(hdu)
