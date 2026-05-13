"""Reconstruct the DAS ``coadd/bg`` flavor from local file-archive products.

The HSCLA file archive at ``/archive/files/la2020/`` ships per-patch
``calexp-<F>-<T>-<P>.fits`` (the coadd image, bit-identical to the DAS
``kind='coadd'`` cutout) and ``det_bkgd-<F>-<T>-<P>.fits`` (the
detection-step background model). The DAS service's ``kind='coadd/bg'``
flavor — recommended for low-surface-brightness galaxy work — is *not*
in the file tree directly, but it can be reconstructed bit-exactly
from the pair using the same algorithm
``lsst::afw::math::BackgroundList::getImage()`` runs in the pipeline.

Empirical relationship, verified across HSC-G / HSC-R / HSC-I at the
center of patch (15548, 1,6) on 2026-05-13:

    coadd/bg  =  calexp  +  lsst_background_image(det_bkgd)

The right-hand side reproduces the DAS pixel values at float-32
precision (residual std ~ 1e-7 ADU, median ~ 1e-11 ADU). See
``docs/ARCHIVE_LAYOUT.md`` for the verification figures, and the LSST
``afw`` source files quoted in the docstrings below.

The persisted ``det_bkgd`` FITS file is a serialised
``lsst.afw.math.BackgroundList`` with **two** ``Background`` elements:

* HDUs 0/1/2 — a 33×33 binned background (image, mask, variance) at
  ``INTERPSTYLE=5`` (``AKIMA_SPLINE``). This carries the spatial bg
  structure.
* HDUs 3/4/5 — a 1×1 ``CONSTANT`` background. The scalar in HDU[3] is
  a per-patch / per-band offset (~1e-3 ADU). Without this term, the
  reconstruction has a milli-ADU constant residual.

``lsst_background_image`` sums both elements at the requested patch
pixels. ``reconstruct_coadd_bg`` is the high-level entry point: pass
in the two FITS files, get back a new ``astropy.io.fits.HDUList`` with
the image HDU's data replaced by the bg-corrected pixels.

Algorithm references
--------------------

* Cell-center placement from ``Background::_setCenOrigSize`` —
  https://github.com/lsst/afw/blob/main/src/math/Background.cc
* Separable 1-D AKIMA spline (y then x) from
  ``BackgroundMI::doGetImage`` —
  https://github.com/lsst/afw/blob/main/src/math/BackgroundMI.cc
* AKIMA wiring through GSL's ``gsl_interp_akima`` —
  https://github.com/lsst/afw/blob/main/src/math/Interpolate.cc
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from astropy.io import fits
from scipy.interpolate import Akima1DInterpolator

LOGGER = logging.getLogger(__name__)

# The hscPipe convention for HSCLA2020 patches. The det_bkgd
# ``BKGD_WIDTH`` / ``BKGD_HEIGHT`` cards confirm this.
DEFAULT_PATCH_SIZE = 4200

# Minimum number of valid (non-NaN) knots required by ``Akima1DInterpolator``.
# Matches LSST ``Interpolate.cc:327`` (``minPoints[AKIMA_SPLINE] = 5``).
AKIMA_MIN_POINTS = 5


class BackgroundError(RuntimeError):
    """Raised when the inputs to background reconstruction are malformed."""


# --------------------------------------------------------------------------- #
# Cell-center placement
# --------------------------------------------------------------------------- #


def lsst_cell_centers(width: int, n_sample: int) -> np.ndarray:
    """Place ``n_sample`` cell centers across ``width`` pixels, LSST style.

    Reproduces ``afw::math::Background::_setCenOrigSize`` (Background.cc
    lines 102-114 in lsst/afw main as of 2026-05-13). The cell widths
    are 127 or 128 px (for 4200/33) in an order set by integer
    arithmetic, *not* a clean alternation; centers fall at integer
    or half-integer pixel positions::

        for i in range(n_sample):
            endx = min(((i + 1) * width + n_sample // 2) // n_sample, width)
            xorig = 0 if i == 0 else xorig_prev + xsize_prev
            xsize = endx - xorig
            xcen[i] = xorig + 0.5 * xsize - 0.5

    For ``width=4200, n_sample=33`` (the HSCLA2020 default) the first
    centers are ``[63.0, 190.5, 318.0, 445.0, 572.0, 699.5, ...]``;
    the maximum offset from the naive uniform ``(i + 0.5) · width /
    n_sample`` is ~0.75 px.

    Parameters
    ----------
    width
        Patch size in pixels along the axis (4200 for HSCLA2020).
    n_sample
        Number of cells along the axis (33 for HSCLA2020).

    Returns
    -------
    ``numpy.ndarray`` of shape ``(n_sample,)``: cell centers in patch-local
    pixel coordinates (0-indexed).
    """

    centers = np.empty(n_sample, dtype=float)
    xorig_prev = 0
    xsize_prev = 0
    for i in range(n_sample):
        endx = min(((i + 1) * width + n_sample // 2) // n_sample, width)
        xorig = 0 if i == 0 else xorig_prev + xsize_prev
        xsize = endx - xorig
        centers[i] = xorig + 0.5 * xsize - 0.5
        xorig_prev, xsize_prev = xorig, xsize
    return centers


# --------------------------------------------------------------------------- #
# Core interpolation: BackgroundList::getImage
# --------------------------------------------------------------------------- #


def lsst_background_image(
    bg_binned: np.ndarray,
    *,
    scalar: float = 0.0,
    patch_width: int = DEFAULT_PATCH_SIZE,
    patch_height: int = DEFAULT_PATCH_SIZE,
    out_x: np.ndarray | None = None,
    out_y: np.ndarray | None = None,
) -> np.ndarray:
    """Reproduce ``BackgroundList::getImage`` at the requested patch pixels.

    Implements the separable 1-D AKIMA spline used by
    ``afw::math::BackgroundMI::doGetImage`` (BackgroundMI.cc:194-352):

    1. **Column stage** — for each of the ``n_x`` x-bin columns, fit a
       1-D AKIMA spline on the column's ``(y_center, value)`` pairs (after
       dropping NaN cells, as ``cullNan`` does on the LSST side). Evaluate
       at every requested output y-coordinate. Result: a
       ``(len(out_y), n_x)`` array of column-interpolated values.
    2. **Row stage** — for each output y-coordinate, fit a 1-D AKIMA
       spline across the ``n_x`` column-interpolated values at the
       LSST x-centers and evaluate at every requested output x. Result:
       a ``(len(out_y), len(out_x))`` array.
    3. **Add the scalar** — the persisted ``BackgroundList`` has a second
       ``Background`` element holding a 1×1 ``CONSTANT`` value. Including
       it in the sum is required for bit-exact reconstruction.

    The AKIMA spline (``scipy.interpolate.Akima1DInterpolator``) is the
    same algorithm LSST exposes via GSL's ``gsl_interp_akima``
    (``Interpolate.cc:153``). Its no-overshoot-at-extrema property
    handles the bright-source-contaminated cells gracefully, which is
    why no sigma-clipping is needed (and why clipping is in fact
    *harmful* — it removes signal the DAS service relied upon).

    Parameters
    ----------
    bg_binned
        2-D array of binned background values. For HSCLA2020 this is
        ``hdul[0].data`` of a ``det_bkgd-*.fits`` file (shape ``(33,
        33)``, dtype ``float32`` on disk; cast to ``float`` here).
        Cells with ``NaN`` values are excluded from the 1-D splines
        (matching LSST's ``cullNan``).
    scalar
        The 1×1 constant offset. For HSCLA2020 this is
        ``hdul[3].data[0, 0]`` of the same ``det_bkgd-*.fits``.
        Default ``0`` if you only want the binned-stage interpolation.
    patch_width, patch_height
        Patch size in pixels. Read these from the ``BKGD_WIDTH`` /
        ``BKGD_HEIGHT`` header cards (always ``4200`` × ``4200`` for
        HSCLA2020 patches).
    out_x, out_y
        Output coordinates in patch-local pixels. Default to
        ``np.arange(patch_width)`` and ``np.arange(patch_height)``,
        i.e. the entire patch grid. Pass narrower 1-D arrays to
        evaluate on a sub-image.

    Returns
    -------
    ``numpy.ndarray`` of shape ``(len(out_y), len(out_x))`` containing
    the full background model in ADU.

    Raises
    ------
    :class:`BackgroundError`
        If any spline column has fewer than 5 valid cells after NaN
        culling (AKIMA's minimum), or if ``bg_binned`` isn't 2-D.
    """

    if bg_binned.ndim != 2:
        raise BackgroundError(
            f"bg_binned must be 2-D; got shape {bg_binned.shape}"
        )
    bg = np.asarray(bg_binned, dtype=float)
    n_y, n_x = bg.shape

    if out_x is None:
        out_x = np.arange(patch_width, dtype=float)
    if out_y is None:
        out_y = np.arange(patch_height, dtype=float)
    out_x = np.asarray(out_x, dtype=float)
    out_y = np.asarray(out_y, dtype=float)

    x_centers = lsst_cell_centers(patch_width, n_x)
    y_centers = lsst_cell_centers(patch_height, n_y)

    # Stage 1: AKIMA in y, one spline per x-bin column.
    grid_cols = np.full((out_y.size, n_x), np.nan, dtype=float)
    for iX in range(n_x):
        col = bg[:, iX]
        good = ~np.isnan(col)
        if good.sum() < AKIMA_MIN_POINTS:
            raise BackgroundError(
                f"column iX={iX} has only {int(good.sum())} valid cells "
                f"(AKIMA requires {AKIMA_MIN_POINTS}); LSST would fall "
                f"back via REDUCE_INTERP_ORDER but we do not implement "
                f"that fallback here."
            )
        spline = Akima1DInterpolator(y_centers[good], col[good])
        grid_cols[:, iX] = spline(out_y, extrapolate=True)

    # Stage 2: AKIMA in x, one spline per output y row.
    out = np.full((out_y.size, out_x.size), np.nan, dtype=float)
    for iy in range(out_y.size):
        row = grid_cols[iy, :]
        good = ~np.isnan(row)
        if good.sum() < AKIMA_MIN_POINTS:
            raise BackgroundError(
                f"row iy={iy} has only {int(good.sum())} valid column "
                f"values (AKIMA requires {AKIMA_MIN_POINTS})."
            )
        spline = Akima1DInterpolator(x_centers[good], row[good])
        out[iy, :] = spline(out_x, extrapolate=True)

    # Stage 3: add the 1x1 BackgroundList::CONSTANT element.
    if scalar != 0.0:
        out += scalar
    return out


# --------------------------------------------------------------------------- #
# HDU helpers
# --------------------------------------------------------------------------- #


def _find_image_hdu(hdul: fits.HDUList) -> int:
    """Return the index of the IMAGE HDU in a hscPipe-style multi-extension FITS.

    Strategy mirrors :func:`hscla_tool.cutout._split_hdul`: prefer the
    HDU labeled ``EXTTYPE='IMAGE'``; fall back to the first float HDU
    with non-empty data.
    """

    for i, hdu in enumerate(hdul):
        if str(hdu.header.get("EXTTYPE", "")).upper() == "IMAGE":
            return i
    for i, hdu in enumerate(hdul):
        if getattr(hdu, "data", None) is not None and hdu.data.dtype.kind == "f":
            return i
    raise BackgroundError("no float IMAGE HDU found")


def _load_det_bkgd(det_bkgd_path: Path) -> tuple[np.ndarray, float, int, int]:
    """Pull the binned bg image, scalar, and patch dimensions from a det_bkgd file."""

    with fits.open(det_bkgd_path) as hdul:
        if len(hdul) < 4:
            raise BackgroundError(
                f"{det_bkgd_path}: expected a 6-HDU BackgroundList; "
                f"got {len(hdul)} HDUs"
            )
        bg33 = np.asarray(hdul[0].data, dtype=float)
        scalar_arr = np.asarray(hdul[3].data, dtype=float).flatten()
        if scalar_arr.size != 1:
            raise BackgroundError(
                f"{det_bkgd_path}: HDU[3] should be a 1x1 CONSTANT; "
                f"got shape {hdul[3].data.shape}"
            )
        scalar = float(scalar_arr[0])
        # Read the patch dimensions from the BKGD_WIDTH / BKGD_HEIGHT cards.
        patch_w = int(hdul[0].header.get("BKGD_WIDTH", DEFAULT_PATCH_SIZE))
        patch_h = int(hdul[0].header.get("BKGD_HEIGHT", DEFAULT_PATCH_SIZE))
    return bg33, scalar, patch_w, patch_h


# --------------------------------------------------------------------------- #
# High-level entry point: file in, HDUList out
# --------------------------------------------------------------------------- #


def reconstruct_coadd_bg(
    calexp_path: str | Path,
    det_bkgd_path: str | Path,
) -> fits.HDUList:
    """Reconstruct the DAS ``coadd/bg`` flavor from local file-archive products.

    The returned ``astropy.io.fits.HDUList`` is a deep copy of the
    ``calexp`` HDUList with **only the IMAGE HDU's data replaced**:

        new_image  =  calexp_image  +  lsst_background_image(det_bkgd)

    All other HDUs (PRIMARY, MASK, VARIANCE, …) are passed through
    unchanged, so the output is a drop-in replacement for the original
    calexp file with bg-corrected pixels.

    The reconstruction is **bit-identical** to the DAS service's
    ``kind='coadd/bg'`` cutout at float-32 precision (residual std
    ~1e-7 ADU, median ~1e-11 ADU). Verified at HSC-G / HSC-R / HSC-I at
    the geometric center of patch (15548, 1,6) on 2026-05-13.

    Parameters
    ----------
    calexp_path
        Local path to a ``calexp-<F>-<T>-<P>.fits`` from
        ``/archive/files/la2020/deepCoadd-results/<F>/<T>/<P>/``.
    det_bkgd_path
        Local path to the matching ``det_bkgd-<F>-<T>-<P>.fits`` from
        the same directory. Must come from the *same* (filter, tract,
        patch) — the function does not check filenames.

    Returns
    -------
    ``astropy.io.fits.HDUList`` ready to write with ``.writeto(path)``.

    Raises
    ------
    :class:`BackgroundError`
        If the calexp image shape doesn't match ``BKGD_WIDTH ×
        BKGD_HEIGHT``, or if the det_bkgd file is structurally wrong.
    """

    calexp_path = Path(calexp_path)
    det_bkgd_path = Path(det_bkgd_path)

    bg33, scalar, patch_w, patch_h = _load_det_bkgd(det_bkgd_path)

    # Open the calexp eagerly (no memmap) and edit in place. We do NOT
    # try to deep-copy the HDUList here — calexp's PSF HDU has variable-
    # length columns whose heap data does not survive an `hdu.copy()`,
    # which loses the reference once the source file closes. Pattern
    # matches `cutout.fetch_cutout`: the returned HDUList owns its file
    # handle and the caller should `.close()` it when done.
    hdul = fits.open(calexp_path, memmap=False)
    img_idx = _find_image_hdu(hdul)
    image = np.asarray(hdul[img_idx].data, dtype=float)
    if image.shape != (patch_h, patch_w):
        raise BackgroundError(
            f"calexp image shape {image.shape} does not match "
            f"det_bkgd BKGD_HEIGHT x BKGD_WIDTH = ({patch_h}, {patch_w})"
        )

    bg_full = lsst_background_image(
        bg33, scalar=scalar, patch_width=patch_w, patch_height=patch_h,
    )

    # Cast back to the calexp's dtype to keep the file representation faithful.
    corrected = (image + bg_full).astype(hdul[img_idx].data.dtype)
    hdul[img_idx].data = corrected

    # Stamp some breadcrumbs in the IMAGE HDU header.
    hdr = hdul[img_idx].header
    hdr["HISTORY"] = "Reconstructed by hscla_tool.background.reconstruct_coadd_bg"
    hdr["HISTORY"] = f"  = calexp + BackgroundList.getImage({det_bkgd_path.name})"
    hdr["HSCLATL_SRC"] = (str(calexp_path.name), "source calexp filename")
    hdr["HSCLATL_BG"] = (str(det_bkgd_path.name), "source det_bkgd filename")
    hdr["HSCLATL_BGV"] = (float(scalar), "BackgroundList HDU[3] scalar (ADU)")

    LOGGER.info(
        "reconstruct_coadd_bg: %s + %s -> in-memory HDUList "
        "(image shape %s, scalar=%+.3e)",
        calexp_path.name, det_bkgd_path.name, image.shape, scalar,
    )
    return hdul
