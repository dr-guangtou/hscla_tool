"""Decode the bitmask plane that ships inside HSCLA cutouts.

The mask HDU of a HSCLA coadd cutout is a single `int32` image where
each pixel is a bit-OR of plane flags. The bit-to-name mapping is
written into the HDU header as a set of cards named ``MP_<NAME>``,
e.g. ``MP_BAD = 0``, ``MP_SAT = 1``, ``MP_INTRP = 2``. This module is
the tiny piece of code that turns that header plus the integer image
into a dict of named boolean arrays.

Use it like::

    from astropy.io import fits
    from hscla_tool import mask

    hdul = fits.open("...cutout.fits")
    planes = mask.decode(hdul[2])
    bad_pixels = planes["BAD"]
    bright_neighbors = planes["BRIGHT_OBJECT"]
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from astropy.io import fits

# Vendored fallback in case a mask HDU is somehow missing its `MP_*`
# header cards. Mirrors what we see in HSCLA2020 cutouts and matches the
# bit assignments documented for hscPipe v8 (same pipeline as HSC PDR3).
DEFAULT_PLANES: dict[str, int] = {
    "BAD": 0,
    "SAT": 1,
    "INTRP": 2,
    "CR": 3,
    "EDGE": 4,
    "DETECTED": 5,
    "DETECTED_NEGATIVE": 6,
    "SUSPECT": 7,
    "NO_DATA": 8,
    "BRIGHT_OBJECT": 9,
    "CROSSTALK": 10,
    "NOT_DEBLENDED": 11,
    "REJECTED": 13,
    "CLIPPED": 14,
    "SENSOR_EDGE": 15,
    "INEXACT_PSF": 16,
}

_PREFIX = "MP_"


class MaskPlaneError(RuntimeError):
    """Raised when a mask HDU can't be decoded (missing data, unknown plane, etc.)."""


def parse_mask_planes(header: fits.Header) -> dict[str, int]:
    """Return ``{plane_name: bit_index}`` parsed from ``MP_*`` cards.

    If the header has no ``MP_*`` cards, returns a copy of
    `DEFAULT_PLANES` — useful for older / hand-edited HDUs.
    """

    planes = {
        card.keyword[len(_PREFIX):]: int(card.value)
        for card in header.cards
        if card.keyword.startswith(_PREFIX)
    }
    return planes or dict(DEFAULT_PLANES)


def decode(
    mask_hdu: fits.ImageHDU | fits.PrimaryHDU,
    *,
    planes: Iterable[str] | None = None,
) -> dict[str, np.ndarray]:
    """Decode a mask HDU into a dict of named boolean arrays.

    Parameters
    ----------
    mask_hdu : ImageHDU or PrimaryHDU
        The HDU containing the integer mask plane. Its header is also
        used to read the bit-to-name map.
    planes : iterable of str, optional
        Subset of plane names to return (e.g. ``("BAD", "SAT", "CR")``).
        If omitted, every plane the header lists is returned.

    Returns
    -------
    dict[str, np.ndarray]
        Mapping from plane name to a boolean array the same shape as
        the mask image. Each True pixel is set in that plane's bit.
    """

    if mask_hdu.data is None:
        raise MaskPlaneError("mask HDU has no data")
    mapping = parse_mask_planes(mask_hdu.header)
    requested = list(planes) if planes is not None else list(mapping)
    missing = [name for name in requested if name not in mapping]
    if missing:
        raise MaskPlaneError(
            f"requested plane(s) {missing} not found in HDU header; "
            f"available: {sorted(mapping)}"
        )
    arr = np.asarray(mask_hdu.data, dtype=np.int64)
    return {name: (arr & (1 << mapping[name])).astype(bool) for name in requested}
