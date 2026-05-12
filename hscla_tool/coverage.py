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
from typing import Any

from hscla_tool import sql as _sql

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
    release: str = DEFAULT_RELEASE,
    client: _sql.HscLaClient | None = None,
) -> RegionCoverage:
    """Which HSC bands have a coadd patch overlapping the requested region?

    Parameters
    ----------
    ra, dec : float
        Region center in degrees (ICRS).
    size_deg : float, optional
        Edge of a square box centered on (ra, dec), in degrees. The
        default of 0 makes this a single-point query.
    release : str, optional
        Short release key from `data/hscla_db.yaml`; only `la2020` is
        supported in v0.
    client : HscLaClient, optional
        Reuse an existing client (and its logged-in session) instead of
        creating a new one.
    """

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
    release: str = DEFAULT_RELEASE,
    detailed: bool = False,
    client: _sql.HscLaClient | None = None,
) -> FrameCoverage:
    """Which single-CCD frames overlap the requested region?

    By default this returns one row per band with the number of CCD
    frames and the number of unique visits — enough to decide whether
    a region has deep coverage in a given band. Set ``detailed=True``
    to also receive every overlapping frame as a list of dicts.
    """

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
