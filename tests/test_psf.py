"""Tests for `hscla_tool.psf`.

Offline tests build a synthetic single-HDU PSF FITS, pack it into a
TAR, and run it through a fake `requests.Session`. Two live tests,
gated by `HSCLA_LIVE_TESTS=1`, hit the real PSF picker at the Perseus
and uncovered fixture coordinates.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits

from hscla_tool import config, cutout, db, psf

# --------------------------------------------------------------------------- #
# Fake HTTP transport
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes, reason: str = "OK") -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode("latin-1", errors="replace")
        self.reason = reason
        self.headers: dict[str, str] = {"content-type": "application/x-tar"}


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.posts: list[dict[str, Any]] = []

    def post(
        self, url: str, *, data: bytes, headers: dict[str, str], timeout: float,
    ) -> _FakeResponse:
        self.posts.append(
            {"url": url, "headers": dict(headers), "data": data, "timeout": timeout}
        )
        return self._response


# --------------------------------------------------------------------------- #
# Synthetic PSF payloads
# --------------------------------------------------------------------------- #


def _make_psf_fits(side: int = 41, peak_offset: tuple[int, int] = (0, 0)) -> bytes:
    """Build a normalized 2D Gaussian PSF FITS file."""

    y, x = np.indices((side, side))
    cy, cx = side // 2 + peak_offset[0], side // 2 + peak_offset[1]
    arr = np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * 3.0 ** 2))
    arr = (arr / arr.sum()).astype(np.float64)
    hdu = fits.PrimaryHDU(arr)
    hdu.header["CTYPE1A"] = "LINEAR"
    hdu.header["CTYPE2A"] = "LINEAR"
    hdu.header["CRPIX1A"] = 1.0
    hdu.header["CRPIX2A"] = 1.0
    hdu.header["CRVAL1A"] = float(-(side // 2))
    hdu.header["CRVAL2A"] = float(-(side // 2))
    buf = io.BytesIO()
    fits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def _wrap_in_tar(fits_bytes: bytes,
                 name: str = "1-psf-calexp-la2020-HSC-I-15548-2,6-49.27-41.25.fits") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(fits_bytes)
        tar.addfile(info, io.BytesIO(fits_bytes))
    return buf.getvalue()


def _empty_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as _:
        pass
    return buf.getvalue()


@pytest.fixture
def fake_creds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HSCLA_USR", "alice@example.com")
    monkeypatch.setenv("HSCLA_PWD", "hunter2")
    return config.load_credentials()


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_cache_key_is_deterministic_and_input_sensitive() -> None:
    a = psf._cache_key(1.0, 2.0, "HSC-I", "coadd", "auto", "auto", True, "la2020")
    b = psf._cache_key(1.0, 2.0, "HSC-I", "coadd", "auto", "auto", True, "la2020")
    assert a == b and len(a) == 16
    assert psf._cache_key(1.0, 2.0, "HSC-R", "coadd", "auto", "auto", True, "la2020") != a
    assert psf._cache_key(1.0, 2.0, "HSC-I", "coadd", "auto", "auto", False, "la2020") != a


def test_build_multipart_body_includes_required_columns() -> None:
    body, boundary = psf._build_multipart_body(
        rerun="la2020", kind="coadd", band="HSC-I", tract="auto", patch="auto",
        ra=49.27, dec=41.24, centered=True, multipart_field="list",
    )
    text = body.decode("utf-8")
    assert "#? rerun type filter tract patch ra dec centered" in text
    assert "la2020 coadd HSC-I auto auto " in text
    assert "deg " in text and "true" in text
    assert 'name="list"' in text
    assert boundary in text


def test_extract_one_fits_empty_tar_returns_none() -> None:
    assert psf._extract_one_fits(_empty_tar()) is None


def test_extract_one_fits_returns_bytes() -> None:
    raw = psf._extract_one_fits(_wrap_in_tar(_make_psf_fits()))
    assert raw is not None and raw.startswith(b"SIMPLE")


# --------------------------------------------------------------------------- #
# Client behavior
# --------------------------------------------------------------------------- #


def test_fetch_psf_writes_cache_and_returns_psf(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar(_make_psf_fits())))
    client = psf.HscLaPsfClient(credentials=fake_creds, session=session)
    result = client.fetch_psf(49.27, 41.24, band="HSC-I",
                               cache_dir=tmp_path / "psfs")
    try:
        assert result.fits_path.is_file()
        arr = result.array
        assert arr.shape == (41, 41)
        assert abs(arr.sum() - 1.0) < 1e-6
        assert arr.max() > 0.0
    finally:
        result.close()
    sent = session.posts[0]
    assert sent["headers"]["Authorization"].startswith("Basic ")
    assert sent["url"].endswith("?bulk=on")
    assert b"HSC-I" in sent["data"]


def test_fetch_psf_serves_from_cache_without_posting(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar(_make_psf_fits())))
    client = psf.HscLaPsfClient(credentials=fake_creds, session=session)
    client.fetch_psf(49.27, 41.24, band="HSC-I",
                    cache_dir=tmp_path / "psfs").close()
    assert len(session.posts) == 1
    client.fetch_psf(49.27, 41.24, band="HSC-I",
                    cache_dir=tmp_path / "psfs").close()
    assert len(session.posts) == 1  # cache hit, no extra POST


def test_fetch_psf_raises_no_coverage_on_empty_tar(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeResponse(200, _empty_tar()))
    client = psf.HscLaPsfClient(credentials=fake_creds, session=session)
    with pytest.raises(cutout.NoCoverageError):
        client.fetch_psf(198.13, 29.56, band="HSC-I",
                        cache_dir=tmp_path / "psfs")


def test_fetch_psf_propagates_http_error(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeResponse(401, b"unauthorized", reason="Unauthorized"))
    client = psf.HscLaPsfClient(credentials=fake_creds, session=session)
    with pytest.raises(psf.PsfError, match="401"):
        client.fetch_psf(49.27, 41.24, band="HSC-I",
                        cache_dir=tmp_path / "psfs")


def test_psf_wcs_returns_linear_pixel_frame(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar(_make_psf_fits())))
    client = psf.HscLaPsfClient(credentials=fake_creds, session=session)
    result = client.fetch_psf(49.27, 41.24, band="HSC-I",
                               cache_dir=tmp_path / "psfs")
    try:
        # The header has CTYPE*A='LINEAR', not standard CTYPE keywords,
        # so a default WCS read returns a non-celestial frame.
        w = result.wcs()
        assert not w.has_celestial
    finally:
        result.close()


# --------------------------------------------------------------------------- #
# Live tests
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_fetch_psf_perseus(tmp_path: Path) -> None:
    fixture = db.get_fixture("covered_lsbg")
    result = psf.fetch_psf(
        fixture["ra_deg"], fixture["dec_deg"], band="HSC-I",
        cache_dir=tmp_path / "psfs",
    )
    try:
        arr = result.array
        assert arr.ndim == 2
        assert arr.shape[0] >= 11 and arr.shape[1] >= 11
        assert np.isfinite(arr).all()
        # HSCLA delivers PSFs normalized to sum = 1.
        assert abs(arr.sum() - 1.0) < 1e-3
        # Sanity: the peak should be near the array center for centered=True.
        peak_y, peak_x = np.unravel_index(int(arr.argmax()), arr.shape)
        cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
        assert abs(peak_y - cy) <= 2
        assert abs(peak_x - cx) <= 2
    finally:
        result.close()


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_fetch_psf_uncovered_raises(tmp_path: Path) -> None:
    fixture = db.get_fixture("uncovered_blank")
    with pytest.raises(cutout.NoCoverageError):
        psf.fetch_psf(
            fixture["ra_deg"], fixture["dec_deg"], band="HSC-I",
            cache_dir=tmp_path / "psfs",
        )
