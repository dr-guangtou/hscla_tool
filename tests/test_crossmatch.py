"""Tests for `hscla_tool.crossmatch`.

Offline tests stub out `HscLaClient.run_sql` so they never touch the
network. One live test, gated by `HSCLA_LIVE_TESTS=1`, runs a real
3-row crossmatch against the Perseus fixture and the uncovered
fixture.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import pytest

from hscla_tool import crossmatch, db


# --------------------------------------------------------------------------- #
# Stub client
# --------------------------------------------------------------------------- #


class _StubClient:
    def __init__(self, response: pd.DataFrame | None = None) -> None:
        self._response = response if response is not None else pd.DataFrame()
        self.queries: list[str] = []

    def run_sql(self, sql_text: str, **_: Any) -> pd.DataFrame:
        self.queries.append(sql_text)
        return self._response


# --------------------------------------------------------------------------- #
# SQL builder
# --------------------------------------------------------------------------- #


def test_build_match_sql_contains_required_clauses() -> None:
    df = pd.DataFrame({"ra": [49.27, 49.28], "dec": [41.24, 41.25]})
    sql_text = crossmatch._build_match_sql(
        table=df, ra_col="ra", dec_col="dec", id_col=None,
        radius_arcsec=1.5, release="la2020",
        extra_columns=(), nearest_only=False,
    )
    assert "WITH matches AS" in sql_text
    assert "UNION ALL" in sql_text
    assert "la2020.forced" in sql_text
    assert "coneSearch(forced.coord," in sql_text
    # Great-circle distance in arcseconds, no earth_distance / earth domain.
    assert "earth_distance" not in sql_text
    assert "acos(GREATEST(-1.0, LEAST(1.0," in sql_text
    # Cone radius appears with the user-specified value (in arcsec).
    assert ", 1.5)" in sql_text
    assert "isprimary" in sql_text
    # The bbox pre-filter should appear with literal float bounds per row.
    assert "forced.ra BETWEEN " in sql_text
    assert "forced.dec BETWEEN " in sql_text
    assert "ORDER BY match_input_id, match_distance" in sql_text


def test_build_match_sql_propagates_id_column() -> None:
    df = pd.DataFrame({"name": ["a", "b"], "ra": [1.0, 2.0], "dec": [3.0, 4.0]})
    sql_text = crossmatch._build_match_sql(
        table=df, ra_col="ra", dec_col="dec", id_col="name",
        radius_arcsec=1.0, release="la2020",
        extra_columns=(), nearest_only=False,
    )
    assert "'a'::text" in sql_text
    assert "'b'::text" in sql_text


def test_build_match_sql_synthesizes_id_when_omitted() -> None:
    df = pd.DataFrame({"ra": [10.0, 20.0], "dec": [-5.0, -10.0]})
    sql_text = crossmatch._build_match_sql(
        table=df, ra_col="ra", dec_col="dec", id_col=None,
        radius_arcsec=1.0, release="la2020",
        extra_columns=(), nearest_only=False,
    )
    assert "'row_0'::text" in sql_text
    assert "'row_1'::text" in sql_text


def test_build_match_sql_with_extra_columns_appears_in_select() -> None:
    df = pd.DataFrame({"ra": [49.27], "dec": [41.24]})
    sql_text = crossmatch._build_match_sql(
        table=df, ra_col="ra", dec_col="dec", id_col=None,
        radius_arcsec=1.0, release="la2020",
        extra_columns=("i_cmodel_mag", "i_cmodel_magerr"),
        nearest_only=False,
    )
    assert "forced.i_cmodel_mag AS i_cmodel_mag" in sql_text
    assert "forced.i_cmodel_magerr AS i_cmodel_magerr" in sql_text


def test_build_match_sql_emits_one_branch_per_input_row() -> None:
    df = pd.DataFrame({"ra": [10.0, 20.0, 30.0], "dec": [-5.0, 0.0, 60.0]})
    sql_text = crossmatch._build_match_sql(
        table=df, ra_col="ra", dec_col="dec", id_col=None,
        radius_arcsec=1.0, release="la2020",
        extra_columns=(), nearest_only=False,
    )
    # Three union-all branches.
    assert sql_text.count("UNION ALL") == 2
    # Each branch carries the literal RA/Dec in its coneSearch + bbox.
    assert "coneSearch(forced.coord, 10.0, -5.0," in sql_text
    assert "coneSearch(forced.coord, 30.0, 60.0," in sql_text


def test_build_match_sql_nearest_only_emits_rank_filter() -> None:
    df = pd.DataFrame({"ra": [49.27], "dec": [41.24]})
    sql_text = crossmatch._build_match_sql(
        table=df, ra_col="ra", dec_col="dec", id_col=None,
        radius_arcsec=1.0, release="la2020",
        extra_columns=(), nearest_only=True,
    )
    assert "ROW_NUMBER() OVER" in sql_text
    assert "_rn = 1" in sql_text


def test_build_match_sql_quotes_embedded_apostrophes() -> None:
    df = pd.DataFrame({"name": ["O'Brien"], "ra": [10.0], "dec": [20.0]})
    sql_text = crossmatch._build_match_sql(
        table=df, ra_col="ra", dec_col="dec", id_col="name",
        radius_arcsec=1.0, release="la2020",
        extra_columns=(), nearest_only=False,
    )
    assert "'O''Brien'::text" in sql_text


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_match_rejects_non_dataframe() -> None:
    with pytest.raises(crossmatch.CrossmatchError, match="DataFrame"):
        crossmatch.match([(49.27, 41.24)])  # type: ignore[arg-type]


def test_match_rejects_missing_columns() -> None:
    df = pd.DataFrame({"alpha": [1.0], "delta": [2.0]})
    with pytest.raises(crossmatch.CrossmatchError, match="missing required column"):
        crossmatch.match(df)


def test_match_rejects_bad_extra_columns() -> None:
    df = pd.DataFrame({"ra": [49.27], "dec": [41.24]})
    with pytest.raises(crossmatch.CrossmatchError, match="not a valid SQL identifier"):
        crossmatch.match(df, extra_columns=("i_cmodel_mag; DROP TABLE x;",),
                         client=_StubClient())


def test_match_rejects_out_of_range_coords() -> None:
    df = pd.DataFrame({"ra": [400.0], "dec": [0.0]})
    with pytest.raises(crossmatch.CrossmatchError, match="ra/dec must be in degrees"):
        crossmatch.match(df, client=_StubClient())


def test_match_rejects_unsupported_release() -> None:
    df = pd.DataFrame({"ra": [49.27], "dec": [41.24]})
    with pytest.raises(crossmatch.CrossmatchError, match="release"):
        crossmatch.match(df, release="pdr3", client=_StubClient())


def test_match_empty_input_returns_empty_frame_without_query() -> None:
    stub = _StubClient()
    df = pd.DataFrame({"ra": [], "dec": []})
    out = crossmatch.match(df, client=stub)
    assert isinstance(out, pd.DataFrame)
    assert out.empty
    assert stub.queries == []   # never hit the server


def test_match_returns_run_sql_result() -> None:
    response = pd.DataFrame({
        "match_input_id": ["row_0"],
        "match_ra": [49.27],
        "match_dec": [41.24],
        "object_id": [123456789],
        "match_distance": [0.0001],
    })
    stub = _StubClient(response)
    df = pd.DataFrame({"ra": [49.27], "dec": [41.24]})
    out = crossmatch.match(df, client=stub, cache=False)
    assert len(stub.queries) == 1
    assert out.equals(response)


# --------------------------------------------------------------------------- #
# Live test
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("HSCLA_LIVE_TESTS") != "1",
    reason="set HSCLA_LIVE_TESTS=1 to hit the real HSCLA server",
)
def test_live_crossmatch_perseus_and_uncovered() -> None:
    perseus = db.get_fixture("covered_lsbg")
    blank = db.get_fixture("uncovered_blank")
    df = pd.DataFrame({
        "name": ["perseus_center", "perseus_offset", "elsewhere"],
        "ra":   [perseus["ra_deg"], perseus["ra_deg"] + 0.0001, blank["ra_deg"]],
        "dec":  [perseus["dec_deg"], perseus["dec_deg"] + 0.0001, blank["dec_deg"]],
    })
    out = crossmatch.match(df, id_col="name", radius_arcsec=2.0)
    assert isinstance(out, pd.DataFrame)
    ids_matched = set(out["match_input_id"]) if "match_input_id" in out.columns else set()
    # Perseus inputs should match at least one HSCLA object each.
    assert {"perseus_center"} <= ids_matched
    # The "elsewhere" input has no HSCLA coverage, so it must not appear.
    assert "elsewhere" not in ids_matched
