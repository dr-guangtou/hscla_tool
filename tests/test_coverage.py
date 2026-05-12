"""Tests for `hscla_tool.coverage`.

Offline tests use a tiny in-memory stub for `HscLaClient.preview_sql`
so we never hit the network. One live test, gated by
``HSCLA_LIVE_TESTS=1``, exercises both fixture coordinates against the
real archive.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any

import pytest

from hscla_tool import coverage, db


_BETWEEN_RE = re.compile(r"BETWEEN\s+([-\d.eE+]+)\s+AND\s+([-\d.eE+]+)")


def _between_bounds(sql_text: str) -> list[tuple[float, float]]:
    """Pull every (lo, hi) BETWEEN pair out of a SQL string, in order."""

    return [(float(lo), float(hi)) for lo, hi in _BETWEEN_RE.findall(sql_text)]


# --------------------------------------------------------------------------- #
# Stubbed client
# --------------------------------------------------------------------------- #


class _StubClient:
    """Minimal stand-in for HscLaClient.

    Returns one of two canned responses depending on which table the
    SQL touches. Records every SQL string so tests can assert on it.
    """

    def __init__(self, mosaic_rows: list[list[Any]], frame_rows: list[list[Any]]) -> None:
        self._mosaic_rows = mosaic_rows
        self._frame_rows = frame_rows
        self.queries: list[str] = []

    def preview_sql(self, sql: str, **_: Any) -> dict[str, Any]:
        self.queries.append(sql)
        if "FROM la2020.mosaic" in sql:
            return {
                "count": len(self._mosaic_rows),
                "fields": ["band", "tract", "patch", "patch_s", "skymap_id",
                           "ra2000", "dec2000", "seeing"],
                "rows": self._mosaic_rows,
            }
        if "FROM la2020.frame" in sql and "COUNT(*)" in sql:
            return {
                "count": len(self._frame_rows),
                "fields": ["band", "n_frames", "n_visits"],
                "rows": self._frame_rows,
            }
        raise AssertionError(f"unexpected SQL: {sql}")


# --------------------------------------------------------------------------- #
# region_coverage
# --------------------------------------------------------------------------- #


def test_region_coverage_parses_patches_and_filters() -> None:
    stub = _StubClient(
        mosaic_rows=[
            ["HSC-G", "9618", "23", "5,4", "12345", "49.27", "41.24", "0.69"],
            ["HSC-G", "9618", "24", "5,5", "12346", "49.28", "41.25", "0.71"],
            ["HSC-I", "9618", "23", "5,4", "12347", "49.27", "41.24", "0.64"],
            ["HSC-R", "9618", "23", "5,4", "12348", "49.27", "41.24", "0.53"],
        ],
        frame_rows=[],
    )
    result = coverage.region_coverage(49.27, 41.24, size_deg=0.03, client=stub)
    assert result.covered
    assert result.filters == ("HSC-G", "HSC-I", "HSC-R")
    assert len(result.patches) == 4
    assert all(isinstance(p.tract, int) and isinstance(p.skymap_id, int) for p in result.patches)
    assert math.isclose(result.mean_seeing_per_band["HSC-G"], (0.69 + 0.71) / 2)
    assert math.isclose(result.mean_seeing_per_band["HSC-I"], 0.64)


def test_region_coverage_empty_for_uncovered_region() -> None:
    stub = _StubClient(mosaic_rows=[], frame_rows=[])
    result = coverage.region_coverage(198.13, 29.56, size_deg=0.03, client=stub)
    assert result.covered is False
    assert result.filters == ()
    assert result.patches == ()
    assert result.mean_seeing_per_band == {}


def test_region_coverage_sql_margin_includes_patch_half() -> None:
    stub = _StubClient(mosaic_rows=[], frame_rows=[])
    coverage.region_coverage(49.27, 41.24, size_deg=0.04, client=stub)
    (ra_lo, ra_hi), (dec_lo, dec_hi) = _between_bounds(stub.queries[0])
    margin = coverage.PATCH_HALF_DEG + 0.04 / 2.0
    assert math.isclose(ra_lo, 49.27 - margin)
    assert math.isclose(ra_hi, 49.27 + margin)
    assert math.isclose(dec_lo, 41.24 - margin)
    assert math.isclose(dec_hi, 41.24 + margin)


def test_region_coverage_point_query_uses_patch_half_only() -> None:
    stub = _StubClient(mosaic_rows=[], frame_rows=[])
    coverage.region_coverage(49.27, 41.24, client=stub)
    (ra_lo, ra_hi), (dec_lo, dec_hi) = _between_bounds(stub.queries[0])
    margin = coverage.PATCH_HALF_DEG
    assert math.isclose(ra_lo, 49.27 - margin)
    assert math.isclose(ra_hi, 49.27 + margin)
    assert math.isclose(dec_lo, 41.24 - margin)
    assert math.isclose(dec_hi, 41.24 + margin)


def test_region_coverage_handles_null_seeing() -> None:
    stub = _StubClient(
        mosaic_rows=[
            ["HSC-G", "9618", "23", "5,4", "12345", "49.27", "41.24", None],
            ["HSC-G", "9618", "24", "5,5", "12346", "49.28", "41.25", "0.71"],
        ],
        frame_rows=[],
    )
    result = coverage.region_coverage(49.27, 41.24, client=stub)
    # The null is dropped; the average is over the one finite value.
    assert math.isclose(result.mean_seeing_per_band["HSC-G"], 0.71)


# --------------------------------------------------------------------------- #
# frame_coverage
# --------------------------------------------------------------------------- #


def test_frame_coverage_aggregates_by_band() -> None:
    stub = _StubClient(
        mosaic_rows=[],
        frame_rows=[
            ["HSC-G", "391", "102"],
            ["HSC-I", "36", "10"],
            ["HSC-R", "74", "20"],
        ],
    )
    result = coverage.frame_coverage(49.27, 41.24, size_deg=0.03, client=stub)
    assert result.covered
    assert result.filters == ("HSC-G", "HSC-I", "HSC-R")
    assert result.band_summary["HSC-G"].n_frames == 391
    assert result.band_summary["HSC-G"].n_visits == 102
    assert result.frames is None


def test_frame_coverage_empty_for_uncovered_region() -> None:
    stub = _StubClient(mosaic_rows=[], frame_rows=[])
    result = coverage.frame_coverage(198.13, 29.56, client=stub)
    assert result.covered is False
    assert result.band_summary == {}
    assert result.frames is None


def test_frame_coverage_sql_margin_uses_frame_half() -> None:
    stub = _StubClient(mosaic_rows=[], frame_rows=[])
    coverage.frame_coverage(49.27, 41.24, size_deg=0.0, client=stub)
    (ra_lo, ra_hi), (dec_lo, dec_hi) = _between_bounds(stub.queries[0])
    margin = coverage.FRAME_HALF_DEG
    assert math.isclose(ra_lo, 49.27 - margin)
    assert math.isclose(ra_hi, 49.27 + margin)
    assert math.isclose(dec_lo, 41.24 - margin)
    assert math.isclose(dec_hi, 41.24 + margin)


# --------------------------------------------------------------------------- #
# Live tests (opt-in)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_region_coverage_perseus_has_multiple_bands() -> None:
    fixture = db.get_fixture("covered_lsbg")
    result = coverage.region_coverage(
        fixture["ra_deg"], fixture["dec_deg"], size_deg=fixture["box_size_deg"]
    )
    assert result.covered
    assert "HSC-G" in result.filters
    assert "HSC-I" in result.filters
    assert len(result.patches) >= 4
    # Seeing on these patches should be sub-arcsec.
    for band, seeing in result.mean_seeing_per_band.items():
        assert 0.1 < seeing < 2.0, f"band {band} seeing looks wrong: {seeing}"


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_region_coverage_uncovered_is_empty() -> None:
    fixture = db.get_fixture("uncovered_blank")
    result = coverage.region_coverage(
        fixture["ra_deg"], fixture["dec_deg"], size_deg=0.03
    )
    assert result.covered is False
    assert result.filters == ()


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_frame_coverage_perseus_has_visits() -> None:
    fixture = db.get_fixture("covered_lsbg")
    result = coverage.frame_coverage(
        fixture["ra_deg"], fixture["dec_deg"], size_deg=fixture["box_size_deg"]
    )
    assert result.covered
    # Perseus has plenty of HSC-G data.
    assert "HSC-G" in result.band_summary
    assert result.band_summary["HSC-G"].n_visits >= 10
