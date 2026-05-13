"""Tests for `hscla_tool.cutout`.

Offline tests build a tiny multi-extension FITS and pack it into a TAR
the way the HSCLA server does, then exercise the client with a fake
`requests.Session`. One live test, gated by `HSCLA_LIVE_TESTS=1`,
fetches a real cutout from the Perseus fixture and verifies it has
the expected mask planes; another checks that the uncovered fixture
raises `NoCoverageError`.
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

from hscla_tool import config, cutout, db

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
# Synthetic cutout payloads
# --------------------------------------------------------------------------- #


def _make_cutout_fits(*, with_mask: bool = True, with_variance: bool = True) -> bytes:
    """Build a synthetic multi-extension cutout FITS in the HSCLA layout."""

    primary = fits.PrimaryHDU()
    hdus = [primary]
    image = fits.ImageHDU(np.ones((10, 12), dtype=np.float32), name="image")
    hdus.append(image)
    if with_mask:
        mask_data = np.zeros((10, 12), dtype=np.int32)
        mask_data[0, 0] = 1 << 0       # BAD
        mask_data[1, 1] = 1 << 1       # SAT
        mask_hdu = fits.ImageHDU(mask_data, name="mask")
        mask_hdu.header["MP_BAD"] = 0
        mask_hdu.header["MP_SAT"] = 1
        mask_hdu.header["MP_INTRP"] = 2
        hdus.append(mask_hdu)
    if with_variance:
        variance = fits.ImageHDU(np.full((10, 12), 4.0, dtype=np.float32), name="variance")
        hdus.append(variance)
    buf = io.BytesIO()
    fits.HDUList(hdus).writeto(buf)
    return buf.getvalue()


def _wrap_in_tar(fits_bytes: bytes, name: str = "arch-x/0-cutout-HSC-I-9999-la2020.fits") -> bytes:
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
# Helpers
# --------------------------------------------------------------------------- #


def test_cache_key_is_deterministic() -> None:
    a = cutout._cache_key(1.0, 2.0, 30.0, "HSC-I", "coadd", "any", True, True, "la2020")
    b = cutout._cache_key(1.0, 2.0, 30.0, "HSC-I", "coadd", "any", True, True, "la2020")
    assert a == b
    assert len(a) == 16


def test_cache_key_changes_with_inputs() -> None:
    base = cutout._cache_key(1.0, 2.0, 30.0, "HSC-I", "coadd", "any", True, True, "la2020")
    assert cutout._cache_key(1.0, 2.0, 30.0, "HSC-R", "coadd", "any", True, True, "la2020") != base
    assert cutout._cache_key(1.0, 2.0, 60.0, "HSC-I", "coadd", "any", True, True, "la2020") != base
    assert cutout._cache_key(1.0, 2.0, 30.0, "HSC-I", "coadd", "any", False, True, "la2020") != base


def test_build_multipart_body_includes_required_columns() -> None:
    body, boundary = cutout._build_multipart_body(
        rerun="la2020", kind="coadd", band="HSC-I", tract="any",
        ra=49.27, dec=41.24, half_deg=0.015,
        with_image=True, with_mask=True, with_variance=True,
        multipart_field="list",
    )
    text = body.decode("utf-8")
    assert "#? rerun type filter tract ra dec sw sh image mask variance" in text
    assert "la2020 coadd HSC-I any " in text
    assert "deg " in text and "true true true" in text
    assert 'name="list"' in text
    assert boundary in text


def test_extract_one_fits_returns_none_on_empty_tar() -> None:
    assert cutout._extract_one_fits(_empty_tar()) is None


def test_extract_one_fits_returns_fits_bytes() -> None:
    f = _make_cutout_fits()
    raw = cutout._extract_one_fits(_wrap_in_tar(f))
    assert raw is not None and raw.startswith(b"SIMPLE")


def test_split_hdul_maps_image_mask_variance() -> None:
    hdul = fits.open(io.BytesIO(_make_cutout_fits()))
    image, mask_hdu, variance = cutout._split_hdul(hdul, expect_mask=True, expect_variance=True)
    assert image is not None and image.data.dtype.kind == "f"
    assert mask_hdu is not None and mask_hdu.data.dtype.kind == "i"
    assert variance is not None and variance.data.dtype.kind == "f"


# --------------------------------------------------------------------------- #
# Client behavior
# --------------------------------------------------------------------------- #


def test_fetch_cutout_writes_cache_and_returns_cutout(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    fits_bytes = _make_cutout_fits()
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar(fits_bytes)))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    result = client.fetch_cutout(
        49.27, 41.24, size_arcsec=108.0, band="HSC-I",
        cache_dir=tmp_path / "cutouts",
    )
    assert result.fits_path.is_file()
    assert result.image is not None
    assert result.mask_hdu is not None
    assert result.variance is not None
    # The basic-auth header was sent.
    sent = session.posts[0]
    assert sent["headers"]["Authorization"].startswith("Basic ")
    # The multipart body carries our coordinate (scientific-notation deg).
    assert b"HSC-I" in sent["data"]
    assert b"4.927" in sent["data"]   # RA rendered as 4.927...e+01deg
    assert b"e+01deg" in sent["data"]


def test_fetch_cutout_serves_from_cache_without_posting(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    fits_bytes = _make_cutout_fits()
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar(fits_bytes)))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    client.fetch_cutout(49.27, 41.24, size_arcsec=108.0, band="HSC-I",
                       cache_dir=tmp_path / "cutouts").close()
    assert len(session.posts) == 1
    # Second call: same args -> should hit cache.
    client.fetch_cutout(49.27, 41.24, size_arcsec=108.0, band="HSC-I",
                       cache_dir=tmp_path / "cutouts").close()
    assert len(session.posts) == 1  # no extra POST


def test_fetch_cutout_raises_no_coverage_on_empty_tar(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeResponse(200, _empty_tar()))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    with pytest.raises(cutout.NoCoverageError):
        client.fetch_cutout(198.13, 29.56, size_arcsec=108.0, band="HSC-I",
                           cache_dir=tmp_path / "cutouts")


def test_fetch_cutout_propagates_http_error(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    session = _FakeSession(_FakeResponse(401, b"unauthorized", reason="Unauthorized"))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    with pytest.raises(cutout.CutoutError, match="401"):
        client.fetch_cutout(49.27, 41.24, size_arcsec=108.0, band="HSC-I",
                           cache_dir=tmp_path / "cutouts")


def test_cutout_mask_planes_helper(
    fake_creds: config.Credentials, tmp_path: Path
) -> None:
    fits_bytes = _make_cutout_fits()
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar(fits_bytes)))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    result = client.fetch_cutout(49.27, 41.24, size_arcsec=108.0, band="HSC-I",
                                 cache_dir=tmp_path / "cutouts")
    planes = result.mask_planes()
    assert planes["BAD"][0, 0]
    assert planes["SAT"][1, 1]
    assert not planes["BAD"][1, 1]


# --------------------------------------------------------------------------- #
# Live tests
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_fetch_cutout_perseus(tmp_path: Path) -> None:
    fixture = db.get_fixture("covered_lsbg")
    size_arcsec = fixture["box_size_deg"] * 3600.0
    result = cutout.fetch_cutout(
        fixture["ra_deg"], fixture["dec_deg"],
        size_arcsec=size_arcsec, band="HSC-I",
        cache_dir=tmp_path / "cutouts",
    )
    try:
        assert result.image is not None
        assert result.mask_hdu is not None
        assert result.variance is not None
        planes = result.mask_planes()
        # HSCLA2020 ships these plane names in every cutout.
        assert {"BAD", "SAT", "INTRP", "CR", "EDGE", "DETECTED"} <= set(planes)
        wcs = result.wcs()
        assert wcs.has_celestial
    finally:
        result.close()


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_fetch_cutout_uncovered_raises(tmp_path: Path) -> None:
    fixture = db.get_fixture("uncovered_blank")
    with pytest.raises(cutout.NoCoverageError):
        cutout.fetch_cutout(
            fixture["ra_deg"], fixture["dec_deg"],
            size_arcsec=108.0, band="HSC-I",
            cache_dir=tmp_path / "cutouts",
        )
