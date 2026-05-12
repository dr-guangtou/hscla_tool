"""Crossmatch a small catalog against HSCLA.

Crossmatch in HSCLA is not a separate web service — it is a SQL
pattern over `la2020.forced`. This module emits a `UNION ALL` of
per-row `coneSearch` branches (with literal RA / Dec / radius values
so the postgres planner can use whatever spatial index `forced.coord`
has), runs the result through `hscla_tool.sql.run_sql`, and returns a
`pandas.DataFrame` of matches.

.. warning::
    **HSCLA's crossmatch service is currently very slow.** Live runs
    take **30–45 minutes on the server side** even for a three-row
    input. The infrastructure here is correct (the live test passes),
    but the practical experience on HSCLA2020 is poor enough that you
    should treat this module as a *placeholder* rather than something
    to use in interactive workflows. Two alternatives that are
    usually faster:

    - Crossmatch locally against the Parquet mirror of `la2020.forced`
      (not yet implemented in this repo — see `docs/todo.md`).
    - Pull cutouts and the relevant patch catalogs via
      :mod:`hscla_tool.archive` and crossmatch inside `pandas`.

    Re-evaluate when NAOJ improves the SQL service, or when a real
    use case shows up.

We model the SQL after the upstream NAOJ tool
`pdr2/hscSspCrossMatch/hscSspCrossMatch.py` (which is a SQL
*generator*, not an HTTP client) but with two changes forced by the
live server:

- Per-row `coneSearch(forced.coord, <lit RA>, <lit DEC>, R)` calls
  joined with `UNION ALL`, rather than `JOIN user_catalog ON
  coneSearch(...)` over a CTE. Without literal coordinates the
  planner did not use the spatial index at all.
- Match distance computed with a plain great-circle trig formula on
  `forced.ra` / `forced.dec` rather than via
  `earth_distance(coord, ll_to_earth(...))`. The latter trips a
  postgres `value for domain earth violates check constraint
  "on_surface"` against HSCLA's `coord` values.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Iterable

import pandas as pd

from hscla_tool import sql as _sql

LOGGER = logging.getLogger(__name__)

DEFAULT_RELEASE = "la2020"
DEFAULT_RADIUS_ARCSEC = 1.0
DEFAULT_RA_COL = "ra"
DEFAULT_DEC_COL = "dec"
# Server-side primary photometry summary table name used by the SQL template.
DEFAULT_TARGET_TABLE = "forced"

_SAFE_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CrossmatchError(RuntimeError):
    """Raised when crossmatch inputs are invalid or the SQL run fails."""


def match(
    table: pd.DataFrame,
    *,
    ra_col: str = DEFAULT_RA_COL,
    dec_col: str = DEFAULT_DEC_COL,
    id_col: "str | None" = None,
    radius_arcsec: float = DEFAULT_RADIUS_ARCSEC,
    release: str = DEFAULT_RELEASE,
    extra_columns: Iterable[str] = (),
    nearest_only: bool = False,
    client: "_sql.HscLaClient | None" = None,
    cache: bool = True,
) -> pd.DataFrame:
    """Crossmatch an input catalog against HSCLA `forced` photometry.

    Parameters
    ----------
    table : pandas.DataFrame
        Input catalog. Must contain `ra_col` and `dec_col` columns in
        degrees (ICRS). When `id_col` is given, that column is
        propagated to the result so each match carries the input row's
        identifier.
    ra_col, dec_col, id_col : str
        Column names in `table`. Defaults are `'ra'` / `'dec'`; if
        `id_col` is omitted, an integer `match_input_row` column is
        generated from the DataFrame index.
    radius_arcsec : float
        Match radius in arcseconds. The default (1.0″) is sub-pixel
        for HSC and keeps confused matches rare at HSC depths.
    release : str
        Short HSCLA release key. Only `'la2020'` is supported in v0.
    extra_columns : iterable of str
        Extra `la2020.forced` columns to include in the output
        (e.g., `('i_cmodel_mag', 'i_cmodel_magerr')`). Names are
        validated to be plain identifiers (`[A-Za-z_][A-Za-z0-9_]*`)
        to prevent SQL injection; pass them lowercase.
    nearest_only : bool
        When True, keep only the closest match per input row. The
        default is to return every HSCLA object inside the radius.
    client, cache : forwarded to `sql.run_sql`.

    Returns
    -------
    pandas.DataFrame
        One row per (input row, matched HSCLA object). Carries the
        input id/ra/dec, the matched `object_id`, the angular
        `match_distance` in **arcseconds**, and any `extra_columns`
        you requested. Empty DataFrame when no input row matches.
    """

    if not isinstance(table, pd.DataFrame):
        raise CrossmatchError("crossmatch.match() requires a pandas DataFrame")
    for col in (ra_col, dec_col):
        if col not in table.columns:
            raise CrossmatchError(f"input table is missing required column {col!r}")
    if id_col is not None and id_col not in table.columns:
        raise CrossmatchError(f"input table is missing id column {id_col!r}")
    for col in extra_columns:
        if not _SAFE_COLUMN_RE.match(col):
            raise CrossmatchError(
                f"extra_columns value {col!r} is not a valid SQL identifier"
            )

    if release != DEFAULT_RELEASE:
        raise CrossmatchError(
            f"crossmatch in v0 supports only release='{DEFAULT_RELEASE}'; got {release!r}"
        )
    if not (table[ra_col].between(0.0, 360.0).all() and table[dec_col].between(-90.0, 90.0).all()):
        raise CrossmatchError("ra/dec must be in degrees in [0, 360] / [-90, 90]")
    if len(table) == 0:
        return pd.DataFrame(columns=["match_input_id", "match_ra", "match_dec",
                                      "object_id", "match_distance", *extra_columns])

    sql_text = _build_match_sql(
        table=table,
        ra_col=ra_col,
        dec_col=dec_col,
        id_col=id_col,
        radius_arcsec=float(radius_arcsec),
        release=release,
        extra_columns=tuple(extra_columns),
        nearest_only=bool(nearest_only),
    )
    LOGGER.info(
        "submitting HSCLA crossmatch: %d input rows, radius=%g arcsec, release=%s",
        len(table), radius_arcsec, release,
    )
    cli = client or _sql.HscLaClient(release=release)
    return cli.run_sql(sql_text, release=release, cache=cache)


# --------------------------------------------------------------------------- #
# SQL builder
# --------------------------------------------------------------------------- #


def _build_match_sql(
    *,
    table: pd.DataFrame,
    ra_col: str,
    dec_col: str,
    id_col: "str | None",
    radius_arcsec: float,
    release: str,
    extra_columns: tuple[str, ...],
    nearest_only: bool,
) -> str:
    """Build the crossmatch SQL string.

    We emit one ``SELECT`` per input row joined with ``UNION ALL``
    rather than a single CTE/JOIN, so each `coneSearch` call sees
    literal RA / Dec / radius values and the postgres planner can use
    the GIST index on ``la2020.forced.coord`` directly. CTE-based
    queries that pass coordinates as column references kept the
    planner from using the spatial index and timed out at 45+ min on
    the live server for trivial 3-row inputs.

    The match distance is computed with a plain great-circle trig
    formula on ``forced.ra``/``forced.dec``; the upstream
    ``earth_distance(coord, ll_to_earth(...))`` route trips a
    ``on_surface`` domain check against HSCLA's ``forced.coord``.
    """

    radius_deg = float(radius_arcsec) / 3600.0
    branches: list[str] = []
    for idx, row in enumerate(table.itertuples(index=False, name=None)):
        record = dict(zip(table.columns, row, strict=True))
        ra = float(record[ra_col])
        dec = float(record[dec_col])
        if id_col is not None:
            raw_id = str(record[id_col])
        else:
            raw_id = f"row_{idx}"
        safe_id = raw_id.replace("'", "''")
        cos_dec = max(0.01, math.cos(math.radians(dec)))
        margin = radius_deg * 1.05
        dra = margin / cos_dec
        ddec = margin
        # Per-row great-circle distance in arcseconds, with literal user RA/Dec.
        dist_expr = (
            f"degrees(acos(GREATEST(-1.0, LEAST(1.0, "
            f"sin(radians(forced.dec)) * sin(radians({dec!r})) + "
            f"cos(radians(forced.dec)) * cos(radians({dec!r})) * "
            f"cos(radians(forced.ra - {ra!r}))"
            f")))) * 3600.0"
        )
        extras = (
            ", " + ", ".join(f"forced.{col} AS {col}" for col in extra_columns)
            if extra_columns else ""
        )
        branches.append(
            f"  SELECT '{safe_id}'::text AS match_input_id, "
            f"{ra!r}::float8 AS match_ra, {dec!r}::float8 AS match_dec, "
            f"forced.object_id, {dist_expr} AS match_distance"
            f"{extras}\n"
            f"  FROM {release}.{DEFAULT_TARGET_TABLE} AS forced\n"
            f"  WHERE forced.isprimary\n"
            # Cheap axis-aligned pre-filter so coneSearch's index lookup is
            # only run against rows near (RA, Dec).
            f"    AND forced.dec BETWEEN {dec - ddec:.10f} AND {dec + ddec:.10f}\n"
            f"    AND forced.ra BETWEEN {ra - dra:.10f} AND {ra + dra:.10f}\n"
            f"    AND coneSearch(forced.coord, {ra!r}, {dec!r}, {radius_arcsec:g})"
        )

    union_sql = "\nUNION ALL\n".join(branches)

    if nearest_only:
        return (
            "WITH matches AS (\n"
            f"{union_sql}\n"
            "),\n"
            "ranked AS (\n"
            "  SELECT *, ROW_NUMBER() OVER ("
            "PARTITION BY match_input_id ORDER BY match_distance"
            ") AS _rn FROM matches\n"
            ")\n"
            "SELECT match_input_id, match_ra, match_dec, object_id, match_distance"
            + (
                ", " + ", ".join(extra_columns) if extra_columns else ""
            )
            + " FROM ranked WHERE _rn = 1 "
            "ORDER BY match_input_id, match_distance"
        )
    return (
        "WITH matches AS (\n"
        f"{union_sql}\n"
        ")\n"
        "SELECT match_input_id, match_ra, match_dec, object_id, match_distance"
        + (
            ", " + ", ".join(extra_columns) if extra_columns else ""
        )
        + " FROM matches ORDER BY match_input_id, match_distance"
    )


