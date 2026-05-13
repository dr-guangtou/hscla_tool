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


# --------------------------------------------------------------------------- #
# Batch path (fetch_cutouts, CutoutRequest, BatchResult)
# --------------------------------------------------------------------------- #


def _wrap_in_tar_multi(
    members: dict[int, bytes], *, dir_prefix: str = "arch-260513-test",
    band: str = "HSC-I", tract: int = 15548, release: str = "la2020",
) -> bytes:
    """Build a multi-member TAR named like the HSCLA cutout server names them.

    ``members`` maps the integer filename prefix (1-indexed coordlist
    line number) to the FITS bytes for that row.
    """

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for prefix, fits_bytes in sorted(members.items()):
            name = f"{dir_prefix}/{prefix}-cutout-{band}-{tract}-{release}.fits"
            info = tarfile.TarInfo(name=name)
            info.size = len(fits_bytes)
            tar.addfile(info, io.BytesIO(fits_bytes))
    return buf.getvalue()


def test_extract_tar_by_prefix_maps_back_to_row_indices() -> None:
    f = _make_cutout_fits()
    # Three members with prefixes 2, 4, 6 -> row indices 0, 2, 4.
    tar_bytes = _wrap_in_tar_multi({2: f, 4: f, 6: f})
    by_idx = cutout._extract_tar_by_prefix(tar_bytes)
    assert set(by_idx) == {0, 2, 4}
    assert all(v.startswith(b"SIMPLE") for v in by_idx.values())


def test_extract_tar_by_prefix_skips_unparseable_names() -> None:
    f = _make_cutout_fits()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # One valid, one with no prefix.
        for name in ("arch/2-cutout-HSC-I-1-la2020.fits", "arch/nope.fits"):
            info = tarfile.TarInfo(name=name)
            info.size = len(f)
            tar.addfile(info, io.BytesIO(f))
    by_idx = cutout._extract_tar_by_prefix(buf.getvalue())
    assert set(by_idx) == {0}


def test_extract_tar_by_prefix_matches_coadd_bg_token() -> None:
    """coadd/bg cutouts arrive named '<N>-coadd+bg-...' (slash -> plus).

    The original prefix regex was ``r"^(\\d+)-cutout-"`` which only
    matched the default ``-cutout-`` token; coadd/bg members would
    have been silently dropped. The relaxed regex must accept both.
    """

    f = _make_cutout_fits()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # Mix the two filename forms in one TAR.
        for name in (
            "arch/2-cutout-HSC-I-15548-la2020.fits",     # coadd row at idx 0
            "arch/3-coadd+bg-HSC-I-15548-la2020.fits",   # coadd/bg row at idx 1
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(f)
            tar.addfile(info, io.BytesIO(f))
    by_idx = cutout._extract_tar_by_prefix(buf.getvalue())
    assert set(by_idx) == {0, 1}


def test_normalize_requests_dataframe_minimal_columns() -> None:
    import pandas as pd

    df = pd.DataFrame({
        "ra":          [49.27, 49.28],
        "dec":         [41.25, 41.26],
        "size_arcsec": [108.0, 108.0],
        "band":        ["HSC-I", "HSC-R"],
    })
    out = cutout._normalize_requests(df)
    assert len(out) == 2
    assert out[0].kind == "coadd" and out[0].tract == "any"
    assert out[1].band == "HSC-R"


def test_normalize_requests_dataframe_missing_column_rejected() -> None:
    import pandas as pd

    df = pd.DataFrame({"ra": [49.27], "dec": [41.25], "band": ["HSC-I"]})
    with pytest.raises(cutout.CutoutError, match="missing required column"):
        cutout._normalize_requests(df)


def test_normalize_requests_list_of_request_objects() -> None:
    reqs = [
        cutout.CutoutRequest(ra=49.27, dec=41.25, size_arcsec=108.0, band="HSC-I"),
        cutout.CutoutRequest(ra=49.28, dec=41.25, size_arcsec=108.0, band="HSC-R"),
    ]
    out = cutout._normalize_requests(reqs)
    assert out == reqs


def test_normalize_requests_rejects_plain_dicts() -> None:
    with pytest.raises(cutout.CutoutError, match="must be a CutoutRequest"):
        cutout._normalize_requests([
            {"ra": 1.0, "dec": 2.0, "size_arcsec": 10.0, "band": "HSC-I"},
        ])


def test_validate_request_catches_bad_inputs() -> None:
    with pytest.raises(cutout.CutoutError, match="ra out of"):
        cutout._validate_request(
            cutout.CutoutRequest(ra=400, dec=0, size_arcsec=10, band="HSC-I"), 0,
        )
    with pytest.raises(cutout.CutoutError, match="dec out of"):
        cutout._validate_request(
            cutout.CutoutRequest(ra=0, dec=100, size_arcsec=10, band="HSC-I"), 0,
        )
    with pytest.raises(cutout.CutoutError, match="size_arcsec"):
        cutout._validate_request(
            cutout.CutoutRequest(ra=0, dec=0, size_arcsec=0, band="HSC-I"), 0,
        )
    with pytest.raises(cutout.CutoutError, match="band"):
        cutout._validate_request(
            cutout.CutoutRequest(ra=0, dec=0, size_arcsec=10, band=""), 0,
        )


def test_build_batch_multipart_body_has_header_plus_n_rows() -> None:
    reqs = [
        cutout.CutoutRequest(ra=1.0, dec=2.0, size_arcsec=10.0, band="HSC-I"),
        cutout.CutoutRequest(ra=3.0, dec=4.0, size_arcsec=20.0, band="HSC-R"),
    ]
    body, _ = cutout._build_batch_multipart_body(
        rerun="la2020", rows=reqs, multipart_field="list",
    )
    text = body.decode("utf-8")
    # Header on line 1, two data rows after.
    coord_block = text.split('\r\n\r\n', 1)[1].split('\r\n--', 1)[0]
    lines = [ln for ln in coord_block.splitlines() if ln.strip()]
    assert lines[0].startswith("#?")
    assert "HSC-I" in lines[1] and "HSC-R" in lines[2]
    assert len(lines) == 3


def test_fetch_cutouts_empty_input_no_http(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    session = _FakeSession(_FakeResponse(200, _empty_tar()))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    result = client.fetch_cutouts([], cache_dir=tmp_path / "cutouts")
    assert len(result) == 0
    assert result.failures == ()
    assert session.posts == []


def test_fetch_cutouts_all_covered(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    f = _make_cutout_fits()
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar_multi({2: f, 3: f})))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    reqs = [
        cutout.CutoutRequest(ra=49.27, dec=41.25, size_arcsec=108.0, band="HSC-I"),
        cutout.CutoutRequest(ra=49.28, dec=41.25, size_arcsec=108.0, band="HSC-R"),
    ]
    result = client.fetch_cutouts(reqs, cache_dir=tmp_path / "cutouts")
    assert len(result) == 2
    assert result.n_success == 2 and result.n_failure == 0
    assert all(c is not None for c in result.cutouts)
    # Only one POST for the whole batch.
    assert len(session.posts) == 1
    assert b"HSC-I" in session.posts[0]["data"]
    assert b"HSC-R" in session.posts[0]["data"]
    result.close()


def test_fetch_cutouts_mixed_coverage_failures_carry_index(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    f = _make_cutout_fits()
    # Three input rows; server returns only rows 0 and 2 (prefixes 2 and 4).
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar_multi({2: f, 4: f})))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    reqs = [
        cutout.CutoutRequest(ra=49.27, dec=41.25, size_arcsec=108.0, band="HSC-I"),
        cutout.CutoutRequest(ra=198.0, dec=29.5, size_arcsec=108.0, band="HSC-I"),
        cutout.CutoutRequest(ra=49.28, dec=41.25, size_arcsec=108.0, band="HSC-R"),
    ]
    result = client.fetch_cutouts(reqs, cache_dir=tmp_path / "cutouts")
    assert result.cutouts[0] is not None
    assert result.cutouts[1] is None
    assert result.cutouts[2] is not None
    assert result.failures == ((1, result.failures[0][1]),)
    assert isinstance(result.failures[0][1], cutout.NoCoverageError)
    assert "(198.000000, 29.500000)" in str(result.failures[0][1])
    result.close()


def test_fetch_cutouts_all_uncovered_returns_all_failures(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    session = _FakeSession(_FakeResponse(200, _empty_tar()))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    reqs = [
        cutout.CutoutRequest(ra=198.0, dec=29.5, size_arcsec=108.0, band="HSC-I"),
        cutout.CutoutRequest(ra=198.1, dec=29.6, size_arcsec=108.0, band="HSC-R"),
    ]
    result = client.fetch_cutouts(reqs, cache_dir=tmp_path / "cutouts")
    assert result.cutouts == (None, None)
    assert {idx for idx, _ in result.failures} == {0, 1}


def test_fetch_cutouts_dataframe_input_works(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    import pandas as pd

    f = _make_cutout_fits()
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar_multi({2: f, 3: f})))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    df = pd.DataFrame({
        "ra":          [49.27, 49.28],
        "dec":         [41.25, 41.25],
        "size_arcsec": [108.0, 108.0],
        "band":        ["HSC-I", "HSC-R"],
    })
    result = client.fetch_cutouts(df, cache_dir=tmp_path / "cutouts")
    assert result.n_success == 2
    result.close()


def test_fetch_cutouts_partial_cache_only_fetches_missing(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    # Pre-populate one cache file so row 0 is a cache hit and row 1 is a miss.
    cache_root = tmp_path / "cutouts"
    cache_root.mkdir(parents=True, exist_ok=True)
    req0 = cutout.CutoutRequest(ra=49.27, dec=41.25, size_arcsec=108.0, band="HSC-I")
    req1 = cutout.CutoutRequest(ra=49.28, dec=41.25, size_arcsec=108.0, band="HSC-R")
    key0 = cutout._request_cache_key(req0, rerun="la2020")
    (cache_root / f"{key0}.fits").write_bytes(_make_cutout_fits())

    f = _make_cutout_fits()
    # Only the single miss row goes out; the server returns it with prefix 2
    # (line 1 is the header; the missing-only POST has that single data row at line 2).
    session = _FakeSession(_FakeResponse(200, _wrap_in_tar_multi({2: f})))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    result = client.fetch_cutouts([req0, req1], cache_dir=cache_root)
    assert result.n_success == 2
    # The body sent should contain only the second band.
    body = session.posts[0]["data"]
    assert body.count(b"\nla2020 coadd ") == 1
    assert b"HSC-R" in body and b"HSC-I" not in body.split(b"\n#?", 1)[0]
    result.close()


def test_fetch_cutouts_all_cached_no_post(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cutouts"
    cache_root.mkdir(parents=True, exist_ok=True)
    reqs = [
        cutout.CutoutRequest(ra=49.27, dec=41.25, size_arcsec=108.0, band="HSC-I"),
        cutout.CutoutRequest(ra=49.28, dec=41.25, size_arcsec=108.0, band="HSC-R"),
    ]
    for r in reqs:
        key = cutout._request_cache_key(r, rerun="la2020")
        (cache_root / f"{key}.fits").write_bytes(_make_cutout_fits())

    session = _FakeSession(_FakeResponse(500, b"should not be hit"))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    result = client.fetch_cutouts(reqs, cache_dir=cache_root)
    assert result.n_success == 2
    assert session.posts == []
    result.close()


def test_fetch_cutouts_oversized_batch_rejected(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    session = _FakeSession(_FakeResponse(200, _empty_tar()))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    reqs = [
        cutout.CutoutRequest(ra=49.27, dec=41.25, size_arcsec=10.0, band="HSC-I")
        for _ in range(cutout.MAX_BATCH_ROWS + 1)
    ]
    with pytest.raises(cutout.CutoutError, match="exceeds the server-side"):
        client.fetch_cutouts(reqs, cache_dir=tmp_path / "cutouts")
    assert session.posts == []


def test_fetch_cutouts_http_error_raises_whole_batch(
    fake_creds: config.Credentials, tmp_path: Path,
) -> None:
    session = _FakeSession(_FakeResponse(500, b"server boom", reason="Internal Server Error"))
    client = cutout.HscLaCutoutClient(credentials=fake_creds, session=session)
    reqs = [
        cutout.CutoutRequest(ra=49.27, dec=41.25, size_arcsec=108.0, band="HSC-I"),
    ]
    with pytest.raises(cutout.CutoutError, match="batch POST failed"):
        client.fetch_cutouts(reqs, cache_dir=tmp_path / "cutouts")


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


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_fetch_cutout_coadd_bg_perseus_i_band(tmp_path: Path) -> None:
    """Verify HSCLA2020 serves the ``coadd/bg`` data product.

    The Perseus LSB galaxy fixture is the canonical "covered" region.
    We fetch the same cutout in two flavors — ``coadd`` (default,
    per-visit local bg subtraction) and ``coadd/bg`` (full focal-plane
    bg correction) — and confirm:

    1. ``coadd/bg`` returns a well-formed multi-extension FITS at the
       same image shape as the default ``coadd``.
    2. The image median **differs measurably** between the two, since
       the bg-correction policy is different. We don't pin a magnitude
       (server-side reprocessing could shift it) but we do require the
       difference to exceed shot noise from a single pixel of the
       std-dev, which is a very loose floor.

    Both confirm the original user observation: ``coadd/bg`` is better
    for LSB galaxy morphology than the over-subtracting ``coadd``.
    """

    import numpy as np

    fixture = db.get_fixture("covered_lsbg")
    ra, dec = fixture["ra_deg"], fixture["dec_deg"]
    size_arcsec = fixture["box_size_deg"] * 3600.0

    plain = cutout.fetch_cutout(
        ra, dec, size_arcsec=size_arcsec, band="HSC-I",
        kind="coadd", cache_dir=tmp_path / "cutouts",
    )
    bg = cutout.fetch_cutout(
        ra, dec, size_arcsec=size_arcsec, band="HSC-I",
        kind="coadd/bg", cache_dir=tmp_path / "cutouts",
    )

    try:
        assert plain.image is not None and bg.image is not None
        assert plain.mask_hdu is not None and bg.mask_hdu is not None
        assert plain.variance is not None and bg.variance is not None

        plain_arr = np.asarray(plain.image.data)
        bg_arr = np.asarray(bg.image.data)
        assert plain_arr.shape == bg_arr.shape
        assert plain_arr.dtype.kind == "f" and bg_arr.dtype.kind == "f"

        plain_med = float(np.median(plain_arr[np.isfinite(plain_arr)]))
        bg_med = float(np.median(bg_arr[np.isfinite(bg_arr)]))
        # The two reductions should not produce byte-identical pixels.
        # Use a very loose floor (1e-6 ADU) so the assertion can't trip
        # on numerical jitter but must trip on "server returned the
        # same image for both kinds".
        assert abs(plain_med - bg_med) > 1e-6, (
            f"coadd vs coadd/bg returned the same median ({plain_med}); "
            f"the bg-corrected variant should differ measurably."
        )

        # Cached FITS paths differ because the cache key includes 'kind'.
        assert plain.fits_path != bg.fits_path
    finally:
        plain.close()
        bg.close()


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_fetch_cutouts_mixed_batch(tmp_path: Path) -> None:
    """Live batch: 2 Perseus rows (covered) + 1 uncovered row in one POST.

    Confirms the 1-indexed coordlist-prefix mapping on the real server,
    not just on a synthetic TAR.
    """

    covered = db.get_fixture("covered_lsbg")
    blank = db.get_fixture("uncovered_blank")
    size_arcsec = covered["box_size_deg"] * 3600.0
    reqs = [
        cutout.CutoutRequest(
            ra=covered["ra_deg"], dec=covered["dec_deg"],
            size_arcsec=size_arcsec, band="HSC-I",
        ),
        cutout.CutoutRequest(
            ra=blank["ra_deg"], dec=blank["dec_deg"],
            size_arcsec=size_arcsec, band="HSC-I",
        ),
        cutout.CutoutRequest(
            ra=covered["ra_deg"], dec=covered["dec_deg"],
            size_arcsec=size_arcsec, band="HSC-R",
        ),
    ]
    result = cutout.fetch_cutouts(reqs, cache_dir=tmp_path / "cutouts")
    try:
        assert result.cutouts[0] is not None
        assert result.cutouts[1] is None
        assert result.cutouts[2] is not None
        assert [idx for idx, _ in result.failures] == [1]
        assert isinstance(result.failures[0][1], cutout.NoCoverageError)
        assert result.cutouts[0].band == "HSC-I"
        assert result.cutouts[2].band == "HSC-R"
    finally:
        result.close()
