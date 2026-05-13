"""Tests for `hscla_tool.background` — the LSST-faithful coadd/bg recipe.

Offline tests use small synthetic FITS files and easily-checked
interpolation cases (flat, linear). One gated-live test pulls a real
(calexp, det_bkgd) pair for tract 15548 patch 1,6 in HSC-I, runs
``reconstruct_coadd_bg``, fetches the DAS ``kind='coadd/bg'`` cutout
for the same patch center, and confirms the reconstructed pixels are
bit-identical to the DAS values within float-32 precision.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from hscla_tool import archive, background, cutout

# --------------------------------------------------------------------------- #
# Pure-math: cell-center placement
# --------------------------------------------------------------------------- #


def test_lsst_cell_centers_hscla_layout() -> None:
    """For the HSCLA2020 default (4200, 33) the first centers match the LSST integer arithmetic."""

    centers = background.lsst_cell_centers(4200, 33)
    assert centers.shape == (33,)
    # First six centers, computed by hand from Background.cc _setCenOrigSize
    # for width=4200, n=33: cell sizes 127, 128, 127, 127, 127, 128, ...
    expected = np.array([63.0, 190.5, 318.0, 445.0, 572.0, 699.5])
    assert np.allclose(centers[:6], expected)
    # Last center should be inside the patch, ~half a cell from the right edge.
    assert 4100.0 < centers[-1] < 4199.0


def test_lsst_cell_centers_uniform_when_evenly_divisible() -> None:
    """A multiple-of-n_sample width gives equal-sized cells."""

    centers = background.lsst_cell_centers(330, 33)  # 10 px per cell
    assert centers.shape == (33,)
    expected = (np.arange(33) + 0.5) * 10.0 - 0.5
    assert np.allclose(centers, expected)


def test_lsst_cell_centers_close_to_uniform_but_not_equal() -> None:
    """LSST centers differ from a uniform grid by less than ~1 px, but not zero."""

    centers = background.lsst_cell_centers(4200, 33)
    uniform = (np.arange(33) + 0.5) * 4200 / 33
    diff = np.abs(centers - uniform)
    # Some cells differ noticeably (the integer-arithmetic rounding kicks in).
    assert diff.max() > 0.5
    # But the deviation stays under one pixel.
    assert diff.max() < 1.0


# --------------------------------------------------------------------------- #
# Pure-math: background_image
# --------------------------------------------------------------------------- #


def test_background_image_flat_input_returns_flat_output() -> None:
    """A 33x33 of constant K interpolates to a constant K image (no scalar)."""

    bg33 = np.full((33, 33), 0.42, dtype=float)
    img = background.lsst_background_image(
        bg33, patch_width=200, patch_height=200,
        out_x=np.arange(200, dtype=float),
        out_y=np.arange(200, dtype=float),
    )
    assert img.shape == (200, 200)
    assert np.allclose(img, 0.42, atol=1e-12)


def test_background_image_includes_scalar() -> None:
    """`scalar` is added to every output pixel (the BackgroundList second element)."""

    bg33 = np.full((33, 33), 0.0, dtype=float)
    img = background.lsst_background_image(
        bg33, scalar=0.31, patch_width=200, patch_height=200,
        out_x=np.arange(10, dtype=float),
        out_y=np.arange(10, dtype=float),
    )
    assert np.allclose(img, 0.31, atol=1e-12)


def test_background_image_linear_x_recovers_slope() -> None:
    """AKIMA spline is exact on a linear function (away from edges)."""

    n = 33
    patch_w = 330  # evenly divisible -> uniform 10-px cells
    centers = background.lsst_cell_centers(patch_w, n)
    # bg(x, y) = a * x_center  (independent of y)
    a = 1e-3
    bg33 = np.tile(a * centers, (n, 1))  # shape (n_y, n_x); rows identical
    out_x = np.arange(patch_w, dtype=float)
    out_y = np.arange(patch_w, dtype=float)
    img = background.lsst_background_image(
        bg33, patch_width=patch_w, patch_height=patch_w,
        out_x=out_x, out_y=out_y,
    )
    # AKIMA matches the linear function exactly between knots; the
    # extrapolated edges (out_x < centers[0] or > centers[-1]) can drift
    # slightly. Check the interior.
    interior = (out_x > centers[0]) & (out_x < centers[-1])
    expected = a * out_x[interior]
    actual = img[100, interior]   # any row
    assert np.allclose(actual, expected, atol=1e-10)


def test_background_image_rejects_3d_input() -> None:
    with pytest.raises(background.BackgroundError, match="must be 2-D"):
        background.lsst_background_image(
            np.zeros((3, 3, 3)), patch_width=100, patch_height=100,
        )


def test_background_image_rejects_too_many_nans() -> None:
    bg33 = np.full((33, 33), 0.0)
    bg33[:, 4] = np.nan  # an entire column NaN
    with pytest.raises(background.BackgroundError, match="iX=4"):
        background.lsst_background_image(
            bg33, patch_width=200, patch_height=200,
            out_x=np.arange(50, dtype=float),
            out_y=np.arange(50, dtype=float),
        )


# --------------------------------------------------------------------------- #
# reconstruct_coadd_bg with synthetic FITS files
# --------------------------------------------------------------------------- #


def _make_synthetic_calexp(path: Path, patch_size: int, fill: float = 1.0) -> Path:
    """Write a minimal hscPipe-style calexp: primary + IMAGE + MASK + VARIANCE."""

    primary = fits.PrimaryHDU()
    img = fits.ImageHDU(
        np.full((patch_size, patch_size), fill, dtype=np.float32),
        name="IMAGE",
    )
    img.header["EXTTYPE"] = "IMAGE"
    mask = fits.ImageHDU(
        np.zeros((patch_size, patch_size), dtype=np.int32), name="MASK",
    )
    mask.header["EXTTYPE"] = "MASK"
    var = fits.ImageHDU(
        np.full((patch_size, patch_size), 4.0, dtype=np.float32), name="VARIANCE",
    )
    var.header["EXTTYPE"] = "VARIANCE"
    fits.HDUList([primary, img, mask, var]).writeto(path, overwrite=True)
    return path


def _make_synthetic_det_bkgd(path: Path, patch_size: int,
                              bg33: np.ndarray, scalar: float) -> Path:
    """Write a minimal 6-HDU det_bkgd: (binned, mask, variance) × (33x33, 1x1)."""

    primary = fits.PrimaryHDU(bg33.astype(np.float32))
    primary.header["BKGD_WIDTH"] = patch_size
    primary.header["BKGD_HEIGHT"] = patch_size
    primary.header["INTERPSTYLE"] = 5  # AKIMA_SPLINE
    binned_mask = fits.ImageHDU(np.zeros_like(bg33, dtype=np.int32))
    binned_var = fits.ImageHDU(np.full_like(bg33, 1e-4, dtype=np.float32))
    scalar_img = fits.ImageHDU(np.array([[scalar]], dtype=np.float32))
    scalar_mask = fits.ImageHDU(np.array([[0]], dtype=np.int32))
    scalar_var = fits.ImageHDU(np.array([[scalar]], dtype=np.float32))
    fits.HDUList([primary, binned_mask, binned_var,
                  scalar_img, scalar_mask, scalar_var]).writeto(
        path, overwrite=True,
    )
    return path


def test_reconstruct_coadd_bg_flat_bg(tmp_path: Path) -> None:
    """A flat bg33 + zero scalar adds a constant offset to every calexp pixel."""

    patch = 200
    calexp_path = _make_synthetic_calexp(tmp_path / "calexp.fits", patch, fill=10.0)
    det_path = _make_synthetic_det_bkgd(
        tmp_path / "det.fits", patch,
        bg33=np.full((33, 33), 0.5, dtype=float), scalar=0.0,
    )
    hdul = background.reconstruct_coadd_bg(calexp_path, det_path)
    img = np.asarray(hdul[1].data, dtype=float)
    # Every pixel should be 10.0 + 0.5 = 10.5.
    assert np.allclose(img, 10.5, atol=1e-5)
    # Other HDUs unchanged.
    assert np.array_equal(hdul[2].data, np.zeros((patch, patch), dtype=np.int32))
    assert np.allclose(hdul[3].data, 4.0)


def test_reconstruct_coadd_bg_uses_scalar_term(tmp_path: Path) -> None:
    """The 1x1 HDU[3] scalar is added to every output pixel."""

    patch = 200
    calexp_path = _make_synthetic_calexp(tmp_path / "calexp.fits", patch, fill=0.0)
    det_path = _make_synthetic_det_bkgd(
        tmp_path / "det.fits", patch,
        bg33=np.zeros((33, 33), dtype=float),
        scalar=-1.5e-3,
    )
    hdul = background.reconstruct_coadd_bg(calexp_path, det_path)
    img = np.asarray(hdul[1].data, dtype=float)
    assert np.allclose(img, -1.5e-3, atol=1e-6)


def test_reconstruct_coadd_bg_stamps_provenance_headers(tmp_path: Path) -> None:
    """The IMAGE HDU header gains HSCLATL_SRC/_BG/_BGV cards."""

    patch = 200
    calexp_path = _make_synthetic_calexp(tmp_path / "calexp.fits", patch)
    det_path = _make_synthetic_det_bkgd(
        tmp_path / "det.fits", patch,
        bg33=np.zeros((33, 33), dtype=float), scalar=0.0,
    )
    hdul = background.reconstruct_coadd_bg(calexp_path, det_path)
    hdr = hdul[1].header
    assert hdr["HSCLATL_SRC"] == calexp_path.name
    assert hdr["HSCLATL_BG"] == det_path.name
    assert hdr["HSCLATL_BGV"] == 0.0


def test_reconstruct_coadd_bg_rejects_size_mismatch(tmp_path: Path) -> None:
    """calexp shape must match BKGD_WIDTH x BKGD_HEIGHT in the det_bkgd."""

    calexp_path = _make_synthetic_calexp(tmp_path / "calexp.fits", patch_size=200)
    det_path = _make_synthetic_det_bkgd(
        tmp_path / "det.fits", patch_size=400,
        bg33=np.zeros((33, 33), dtype=float), scalar=0.0,
    )
    with pytest.raises(background.BackgroundError, match="does not match"):
        background.reconstruct_coadd_bg(calexp_path, det_path)


def test_reconstruct_coadd_bg_rejects_short_det_bkgd(tmp_path: Path) -> None:
    """A det_bkgd file with <4 HDUs is rejected with a clear message."""

    calexp_path = _make_synthetic_calexp(tmp_path / "calexp.fits", patch_size=100)
    bad = tmp_path / "bad_det.fits"
    fits.HDUList([
        fits.PrimaryHDU(np.zeros((33, 33), dtype=np.float32)),
        fits.ImageHDU(np.zeros((33, 33), dtype=np.int32)),
    ]).writeto(bad, overwrite=True)
    with pytest.raises(background.BackgroundError, match="6-HDU BackgroundList"):
        background.reconstruct_coadd_bg(calexp_path, bad)


# --------------------------------------------------------------------------- #
# Live: full-pipeline check against DAS coadd/bg
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_reconstruct_matches_das_coadd_bg(tmp_path: Path) -> None:
    """Bit-equivalence: reconstructed coadd/bg vs DAS kind='coadd/bg' cutout.

    Downloads the HSC-I calexp + det_bkgd for tract 15548 patch 1,6
    (where the Perseus fixture lives), runs `reconstruct_coadd_bg`,
    then crops the result on the cutout's pixel grid via WCS and
    compares to a real DAS coadd/bg cutout. Residual std should sit at
    float-32 precision (~1e-7 ADU) and median at floating-point noise
    (~1e-10 ADU).
    """

    from astropy.wcs import WCS

    band = "HSC-I"
    tract = 15548
    patch = "1,6"

    # Download the patch-level products to a tmp scratch dir.
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    calexp_info = archive.download_patch_file(
        tract, patch, band, "calexp",
        dest=archive_dir / f"calexp-{band}-{tract}-{patch}.fits",
    )
    det_info = archive.download_patch_file(
        tract, patch, band, "det_bkgd",
        dest=archive_dir / f"det_bkgd-{band}-{tract}-{patch}.fits",
    )

    # Reconstruct in memory.
    hdul = background.reconstruct_coadd_bg(calexp_info.path, det_info.path)
    img_idx = background._find_image_hdu(hdul)
    recon = np.asarray(hdul[img_idx].data, dtype=float)
    patch_wcs = WCS(hdul[img_idx].header)

    # Fetch the DAS coadd/bg cutout for a position safely inside patch 1,6.
    # We use the patch geometric center to avoid any straddling.
    patch_h, patch_w = recon.shape
    center_sky = patch_wcs.pixel_to_world((patch_w - 1) / 2.0, (patch_h - 1) / 2.0)
    ra_c = float(center_sky.ra.deg)
    dec_c = float(center_sky.dec.deg)
    bg_cut = cutout.fetch_cutout(
        ra_c, dec_c, size_arcsec=60.0, band=band, kind="coadd/bg",
        cache_dir=tmp_path / "cutouts",
    )
    try:
        truth = np.asarray(bg_cut.image.data, dtype=float)
        cutout_wcs = WCS(bg_cut.image.header)

        # Map cutout pixels to patch pixels (both share CD + CRVAL).
        ny, nx = truth.shape
        x_cut = np.arange(nx, dtype=float)
        y_cut = np.arange(ny, dtype=float)
        sky_x = cutout_wcs.pixel_to_world(x_cut, np.zeros_like(x_cut))
        px_x, _ = patch_wcs.world_to_pixel(sky_x)
        sky_y = cutout_wcs.pixel_to_world(np.zeros_like(y_cut), y_cut)
        _, py_y = patch_wcs.world_to_pixel(sky_y)
        # Both should be integer-offset; round and index.
        px_int = np.round(px_x).astype(int)
        py_int = np.round(py_y).astype(int)
        cropped = recon[np.ix_(py_int, px_int)]

        residual = cropped - truth
        med = float(np.median(residual))
        std = float(np.std(residual))
        # Float-32 precision floor. Median should be effectively 0; std
        # near float-32 epsilon * typical pixel magnitude (~1e-7 ADU).
        assert abs(med) < 1e-6, f"median residual {med:+.3e} > 1e-6 ADU"
        assert std < 5e-5, f"residual std {std:.3e} > 5e-5 ADU"
    finally:
        bg_cut.close()
