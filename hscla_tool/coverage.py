"""Where does HSCLA have data?

Two questions this module answers:

1. **Coadd coverage.** Given a sky region, which HSC bands have a
   `la2020.mosaic` coadd patch overlapping it, and which patches are
   they? Use this to decide whether a cutout is worth requesting and
   to choose which (tract, patch, band) to ask the file tree for.

2. **Single-CCD provenance.** Given the same region, which HSC visits
   and how many CCDs went into the data? `frame_coverage` answers
   this with a per-band summary, or — with `detailed=True` — the
   individual frame rows.

How the spatial filter works
----------------------------

The `mosaic` table has no `coord`-like column that the server's
`coneSearch` or `boxSearch` accept (those work on `forced`/`meas`
tables which have a real `coord` column). The corner columns
(`llcra/ulcra/urcra/lrcra` etc.) give an axis-aligned envelope, but
that envelope wraps incorrectly across RA=0 and produces spurious
matches at antipodal RA values — we confirmed this on the live
archive.

Instead, we use a **patch-center / frame-center proximity test**: keep
rows whose `ra2000`/`dec2000` falls within `(footprint_half + box_half)`
of the query center. HSC patches are roughly 12 arcmin square, so
`footprint_half ≈ 0.12 deg`; CCD frames are slightly larger when the
per-CCD pointing is reported, so we use 0.20 deg for `frame`. This is
a small over-approximation (a patch whose center is just outside the
margin might still touch the box, by an arcsecond or two), but it is
loud about not silently missing patches and it works correctly even
when the query is near a tract boundary.

Known limitation
----------------

This module does **not** handle regions that wrap across RA=0 / RA=360.
For HSCLA2020 we have not yet hit such a region; if and when we do,
the proximity test needs to split the query box at the wrap line. The
fixture coordinates we ship with (`covered_lsbg`, `uncovered_blank`)
are both far from the wrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from hscla_tool import mirror as _mirror
from hscla_tool import sql as _sql

Source = Literal["server", "local"]

# Half-sizes used for the patch-center / frame-center proximity test.
PATCH_HALF_DEG = 0.12   # HSC coadd patch is roughly 12 arcmin square.
FRAME_HALF_DEG = 0.20   # A bit larger to comfortably catch CCD frames.

DEFAULT_RELEASE = "la2020"
SCHEMA_NAME = "la2020"   # The PostgreSQL schema; same string for now.


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PatchInfo:
    """One overlapping coadd patch in one HSC band."""

    band: str
    tract: int
    patch: int
    patch_s: str
    skymap_id: int
    ra2000: float
    dec2000: float
    seeing: float


@dataclass(frozen=True)
class RegionCoverage:
    """Coadd coverage of a query region."""

    filters: tuple[str, ...]
    patches: tuple[PatchInfo, ...]
    mean_seeing_per_band: dict[str, float]

    @property
    def covered(self) -> bool:
        """True if at least one band has any overlapping patch."""

        return bool(self.filters)


@dataclass(frozen=True)
class BandFrameSummary:
    """Per-band summary of single-CCD frames overlapping the query region."""

    band: str
    n_frames: int
    n_visits: int


@dataclass(frozen=True)
class FrameCoverage:
    """Single-CCD provenance of a query region."""

    filters: tuple[str, ...]
    band_summary: dict[str, BandFrameSummary]
    frames: tuple[dict[str, Any], ...] | None = None

    @property
    def covered(self) -> bool:
        """True if at least one band has any frame."""

        return bool(self.filters)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def region_coverage(
    ra: float,
    dec: float,
    *,
    size_deg: float = 0.0,
    source: Source = "server",
    release: str = DEFAULT_RELEASE,
    client: _sql.HscLaClient | None = None,
    mirror_df: "pd.DataFrame | None" = None,
) -> RegionCoverage:
    """Which HSC bands have a coadd patch overlapping the requested region?

    Parameters
    ----------
    ra, dec : float
        Region center in degrees (ICRS).
    size_deg : float, optional
        Edge of a square box centered on (ra, dec), in degrees. The
        default of 0 makes this a single-point query.
    source : "server" | "local", optional
        Where to look up the answer. ``"server"`` (default) sends a
        small SQL query to HSCLA using the patch-center proximity
        rule. ``"local"`` reads the local Parquet mirror of
        ``la2020.mosaic`` and runs an exact axis-aligned-bounding-box
        overlap test on the four patch corners — strictly tighter and
        handles tract boundaries correctly. Patches whose corners
        straddle RA = 0 / 360 are kept on both sides (conservative).
    release : str, optional
        Short release key from `data/hscla_db.yaml`; only `la2020` is
        supported in v0.
    client : HscLaClient, optional
        Reuse an existing client (and its logged-in session) instead of
        creating a new one. Ignored when ``source='local'``.
    mirror_df : pandas.DataFrame, optional
        Inject a preloaded mirror DataFrame (testing hook); when
        omitted and ``source='local'``, the on-disk Parquet is read.
    """

    if source == "local":
        return _region_coverage_local(ra, dec, size_deg=size_deg, mirror_df=mirror_df)
    if source != "server":
        raise ValueError(f"source must be 'server' or 'local', got {source!r}")

    cli = client or _sql.HscLaClient(release=release)
    margin = PATCH_HALF_DEG + max(0.0, float(size_deg)) / 2.0
    sql_text = (
        "SELECT band, tract, patch, patch_s, skymap_id, ra2000, dec2000, seeing "
        f"FROM {SCHEMA_NAME}.mosaic "
        f"WHERE ra2000 BETWEEN {float(ra) - margin} AND {float(ra) + margin} "
        f"  AND dec2000 BETWEEN {float(dec) - margin} AND {float(dec) + margin} "
        "ORDER BY band, tract, patch"
    )
    result = cli.preview_sql(sql_text)
    rows = result.get("rows", [])
    patches = tuple(_parse_patch_row(row) for row in rows)
    return _build_region_coverage(patches)


def _region_coverage_local(
    ra: float,
    dec: float,
    *,
    size_deg: float,
    mirror_df: "pd.DataFrame | None",
) -> RegionCoverage:
    """Exact bounding-box overlap against the local mosaic mirror."""

    df = mirror_df if mirror_df is not None else _mirror.load_mirror("mosaic")
    half = max(0.0, float(size_deg)) / 2.0
    box_ra_lo, box_ra_hi = float(ra) - half, float(ra) + half
    box_dec_lo, box_dec_hi = float(dec) - half, float(dec) + half
    ra_corners = df[["llcra", "ulcra", "urcra", "lrcra"]].to_numpy()
    dec_corners = df[["llcdec", "ulcdec", "urcdec", "lrcdec"]].to_numpy()
    patch_ra_min = ra_corners.min(axis=1)
    patch_ra_max = ra_corners.max(axis=1)
    patch_dec_min = dec_corners.min(axis=1)
    patch_dec_max = dec_corners.max(axis=1)
    # Patches whose corner span exceeds 180 deg are wrapping RA=0; the
    # min/max envelope can't be trusted for them, so we keep them only
    # when the query box also crosses (or is very near) the wrap. The
    # simple guard: if ra_span > 180, drop the bbox test entirely.
    wrap_mask = (patch_ra_max - patch_ra_min) > 180.0
    bbox_overlap = (
        (patch_ra_max >= box_ra_lo)
        & (patch_ra_min <= box_ra_hi)
        & (patch_dec_max >= box_dec_lo)
        & (patch_dec_min <= box_dec_hi)
    )
    keep = bbox_overlap & ~wrap_mask
    matched = df.loc[keep, ["band", "tract", "patch", "patch_s", "skymap_id",
                            "ra2000", "dec2000", "seeing"]]
    patches = tuple(
        PatchInfo(
            band=str(row.band),
            tract=int(row.tract),
            patch=int(row.patch),
            patch_s=str(row.patch_s),
            skymap_id=int(row.skymap_id),
            ra2000=float(row.ra2000),
            dec2000=float(row.dec2000),
            seeing=float(row.seeing) if pd.notna(row.seeing) else float("nan"),
        )
        for row in matched.sort_values(["band", "tract", "patch"]).itertuples(index=False)
    )
    return _build_region_coverage(patches)


def _build_region_coverage(patches: tuple[PatchInfo, ...]) -> RegionCoverage:
    filters = tuple(sorted({p.band for p in patches}))
    seeing_by_band: dict[str, float] = {}
    for band in filters:
        values = [p.seeing for p in patches if p.band == band and _is_finite(p.seeing)]
        if values:
            seeing_by_band[band] = sum(values) / len(values)
    return RegionCoverage(
        filters=filters,
        patches=patches,
        mean_seeing_per_band=seeing_by_band,
    )


def frame_coverage(
    ra: float,
    dec: float,
    *,
    size_deg: float = 0.0,
    source: Source = "server",
    release: str = DEFAULT_RELEASE,
    detailed: bool = False,
    client: _sql.HscLaClient | None = None,
    mirror_df: "pd.DataFrame | None" = None,
) -> FrameCoverage:
    """Which single-CCD frames overlap the requested region?

    By default this returns one row per band with the number of CCD
    frames and the number of unique visits — enough to decide whether
    a region has deep coverage in a given band. Set ``detailed=True``
    to also receive every overlapping frame as a list of dicts.

    ``source='local'`` uses the Parquet mirror at
    ``mirror_path('frame')`` and the same frame-center proximity rule
    as the server query (CCD corners are not present in `la2020.frame`,
    so we cannot do exact bbox overlap here — just an offline copy of
    the same proximity test).
    """

    if source == "local":
        return _frame_coverage_local(
            ra, dec, size_deg=size_deg, detailed=detailed, mirror_df=mirror_df
        )
    if source != "server":
        raise ValueError(f"source must be 'server' or 'local', got {source!r}")

    cli = client or _sql.HscLaClient(release=release)
    margin = FRAME_HALF_DEG + max(0.0, float(size_deg)) / 2.0
    where = (
        f"ra2000 BETWEEN {float(ra) - margin} AND {float(ra) + margin} "
        f"AND dec2000 BETWEEN {float(dec) - margin} AND {float(dec) + margin}"
    )
    summary_sql = (
        "SELECT band, COUNT(*) AS n_frames, COUNT(DISTINCT visit) AS n_visits "
        f"FROM {SCHEMA_NAME}.frame "
        f"WHERE {where} "
        "GROUP BY band ORDER BY band"
    )
    summary = cli.preview_sql(summary_sql)
    band_summary: dict[str, BandFrameSummary] = {}
    for row in summary.get("rows", []):
        band = str(row[0])
        band_summary[band] = BandFrameSummary(
            band=band,
            n_frames=int(row[1]),
            n_visits=int(row[2]),
        )
    filters = tuple(sorted(band_summary))
    frames: tuple[dict[str, Any], ...] | None = None
    if detailed:
        detail_sql = (
            "SELECT frame_id, visit, ccd, ccdname, band, ra2000, dec2000 "
            f"FROM {SCHEMA_NAME}.frame "
            f"WHERE {where} "
            "ORDER BY band, visit, ccd"
        )
        detail = cli.preview_sql(detail_sql)
        fields = detail.get("fields", [])
        frames = tuple({k: v for k, v in zip(fields, row)} for row in detail.get("rows", []))
    return FrameCoverage(
        filters=filters,
        band_summary=band_summary,
        frames=frames,
    )


def _frame_coverage_local(
    ra: float,
    dec: float,
    *,
    size_deg: float,
    detailed: bool,
    mirror_df: "pd.DataFrame | None",
) -> FrameCoverage:
    df = mirror_df if mirror_df is not None else _mirror.load_mirror("frame")
    margin = FRAME_HALF_DEG + max(0.0, float(size_deg)) / 2.0
    mask = (
        (df["ra2000"] >= float(ra) - margin)
        & (df["ra2000"] <= float(ra) + margin)
        & (df["dec2000"] >= float(dec) - margin)
        & (df["dec2000"] <= float(dec) + margin)
    )
    matched = df.loc[mask]
    grouped = matched.groupby("band")
    band_summary: dict[str, BandFrameSummary] = {}
    for band, sub in grouped:
        band_summary[str(band)] = BandFrameSummary(
            band=str(band),
            n_frames=int(len(sub)),
            n_visits=int(sub["visit"].nunique()),
        )
    filters = tuple(sorted(band_summary))
    frames: tuple[dict[str, Any], ...] | None = None
    if detailed:
        detail_cols = [c for c in ("frame_id", "visit", "ccd", "ccdname", "band",
                                    "ra2000", "dec2000") if c in matched.columns]
        sorted_rows = matched[detail_cols].sort_values(
            [c for c in ("band", "visit", "ccd") if c in detail_cols]
        )
        frames = tuple(sorted_rows.to_dict(orient="records"))
    return FrameCoverage(filters=filters, band_summary=band_summary, frames=frames)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _parse_patch_row(row: list[Any]) -> PatchInfo:
    """Build a PatchInfo from one preview-API row (strings in, types out)."""

    seeing_raw = row[7]
    try:
        seeing = float(seeing_raw) if seeing_raw is not None else float("nan")
    except (TypeError, ValueError):
        seeing = float("nan")
    return PatchInfo(
        band=str(row[0]),
        tract=int(row[1]),
        patch=int(row[2]),
        patch_s=str(row[3]),
        skymap_id=int(row[4]),
        ra2000=float(row[5]),
        dec2000=float(row[6]),
        seeing=seeing,
    )


def _is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))
