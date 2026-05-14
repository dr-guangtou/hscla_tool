"""Step 1 of the Perseus end-to-end example: HSCLA2020 coverage map.

Enumerate every coadd patch and tally per-band frame statistics within
a 2-degree-radius circle centered on NGC 1275 (the Perseus BCG), using
the local Parquet mirror of `la2020.mosaic` / `la2020.frame`.

Outputs (all under `example/perseus/`):
    figures/step1_coverage_combined.png
    figures/step1_coverage_per_band.png
    notes/step1_patches.csv
    notes/step1_frame_counts.csv
    notes/step1_coverage.md

Run from the repo root:

    uv run python example/perseus/step1_coverage.py
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Polygon

from hscla_tool import coverage as cov
from hscla_tool import mirror

NGC1275_RA = 49.9506670
NGC1275_DEC = 41.5117083
SEARCH_RADIUS_DEG = 2.0
SEARCH_BOX_DEG = 2.0 * SEARCH_RADIUS_DEG   # inscribed square edge in deg

HSC_BROADBANDS = ("HSC-G", "HSC-R", "HSC-I", "HSC-Z", "HSC-Y")
BAND_COLORS: dict[str, str] = {
    "HSC-G": "#1f77b4",
    "HSC-R": "#2ca02c",
    "HSC-I": "#d62728",
    "HSC-Z": "#9467bd",
    "HSC-Y": "#8c564b",
    "IB0945": "#bcbd22",
    "NB0387": "#17becf",
    "NB0400": "#7f7f7f",
    "NB0468": "#3a86ff",
    "NB0515": "#06d6a0",
    "NB0527": "#118ab2",
    "NB0656": "#ef476f",
    "NB0718": "#ffd166",
    "NB0816": "#e63946",
    "NB0921": "#a78bfa",
    "NB0926": "#f4a261",
    "NB0973": "#6a4c93",
}

EXAMPLE_DIR = Path(__file__).resolve().parent
FIG_DIR = EXAMPLE_DIR / "figures"
NOTES_DIR = EXAMPLE_DIR / "notes"


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #


def angular_separation_deg(
    ra: np.ndarray, dec: np.ndarray, ra0: float, dec0: float
) -> np.ndarray:
    """Great-circle distance in degrees (Vincenty form, stable at small angles)."""

    lam1 = np.radians(ra)
    lam2 = math.radians(ra0)
    phi1 = np.radians(dec)
    phi2 = math.radians(dec0)
    dlam = lam1 - lam2
    num = np.hypot(
        np.cos(phi1) * np.sin(dlam),
        np.cos(phi2) * np.sin(phi1) - np.sin(phi2) * np.cos(phi1) * np.cos(dlam),
    )
    den = np.sin(phi2) * np.sin(phi1) + np.cos(phi2) * np.cos(phi1) * np.cos(dlam)
    return np.degrees(np.arctan2(num, den))


def patch_touches_circle(
    df: pd.DataFrame, ra0: float, dec0: float, radius_deg: float
) -> np.ndarray:
    """True for rows whose patch center OR any corner is within `radius_deg`.

    "Center or any corner inside" is generous toward edge patches and
    correct for our purpose: we want the set of patches whose pixels
    overlap a 2-deg circle, and a corner-test catches patches whose
    centers are just outside.
    """

    centers = angular_separation_deg(
        df["ra2000"].to_numpy(), df["dec2000"].to_numpy(), ra0, dec0
    )
    touches = centers <= radius_deg
    for ra_col, dec_col in (("llcra", "llcdec"), ("ulcra", "ulcdec"),
                            ("urcra", "urcdec"), ("lrcra", "lrcdec")):
        d = angular_separation_deg(
            df[ra_col].to_numpy(), df[dec_col].to_numpy(), ra0, dec0
        )
        touches = touches | (d <= radius_deg)
    return touches


# --------------------------------------------------------------------------- #
# Coverage queries
# --------------------------------------------------------------------------- #


def collect_patches() -> pd.DataFrame:
    """Return all overlapping mosaic rows as a DataFrame with corners."""

    mosaic = mirror.load_mirror("mosaic")
    coverage = cov.region_coverage(
        NGC1275_RA,
        NGC1275_DEC,
        size_deg=SEARCH_BOX_DEG,
        source="local",
        mirror_df=mosaic,
    )
    if not coverage.covered:
        return mosaic.iloc[0:0].copy()
    keys = {(p.band, int(p.tract), int(p.patch)) for p in coverage.patches}
    sub = mosaic[
        mosaic[["band", "tract", "patch"]]
        .apply(lambda r: (r.band, int(r.tract), int(r.patch)) in keys, axis=1)
    ].copy()
    sub = sub.loc[patch_touches_circle(sub, NGC1275_RA, NGC1275_DEC, SEARCH_RADIUS_DEG)]
    sub["sep_center_deg"] = angular_separation_deg(
        sub["ra2000"].to_numpy(), sub["dec2000"].to_numpy(), NGC1275_RA, NGC1275_DEC
    )
    sub = sub.sort_values(["band", "tract", "patch"]).reset_index(drop=True)
    return sub


def collect_frame_counts() -> pd.DataFrame:
    """Per-band CCD frame and visit counts from the local frame mirror."""

    frame_cov = cov.frame_coverage(
        NGC1275_RA,
        NGC1275_DEC,
        size_deg=SEARCH_BOX_DEG,
        source="local",
    )
    rows = [
        {"band": b, "n_frames": s.n_frames, "n_visits": s.n_visits}
        for b, s in sorted(frame_cov.band_summary.items())
    ]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


PATCH_TABLE_COLS = [
    "band", "tract", "patch", "patch_s", "skymap_id",
    "ra2000", "dec2000",
    "llcra", "llcdec", "ulcra", "ulcdec",
    "urcra", "urcdec", "lrcra", "lrcdec",
    "seeing", "ellipticity", "ellipticity_pa", "zeropt",
    "sep_center_deg",
]


def write_tables(patches: pd.DataFrame, frame_counts: pd.DataFrame) -> tuple[Path, Path]:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    p_path = NOTES_DIR / "step1_patches.csv"
    f_path = NOTES_DIR / "step1_frame_counts.csv"
    cols = [c for c in PATCH_TABLE_COLS if c in patches.columns]
    patches[cols].to_csv(p_path, index=False)
    frame_counts.to_csv(f_path, index=False)
    return p_path, f_path


# --------------------------------------------------------------------------- #
# QA figures
# --------------------------------------------------------------------------- #


def _setup_sky_axis(ax: plt.Axes) -> None:
    """RA increases to the left; equal aspect at the target Dec."""

    cos_dec = math.cos(math.radians(NGC1275_DEC))
    margin = 1.15 * SEARCH_RADIUS_DEG
    ax.set_xlim(NGC1275_RA + margin / cos_dec, NGC1275_RA - margin / cos_dec)
    ax.set_ylim(NGC1275_DEC - margin, NGC1275_DEC + margin)
    ax.set_xlabel("RA (deg, J2000)")
    ax.set_ylabel("Dec (deg, J2000)")
    ax.set_aspect(1.0 / cos_dec)


def _draw_search_circle(ax: plt.Axes, lw: float = 1.4) -> None:
    theta = np.linspace(0.0, 2.0 * math.pi, 360)
    cos_dec = math.cos(math.radians(NGC1275_DEC))
    ra_circle = NGC1275_RA + (SEARCH_RADIUS_DEG / cos_dec) * np.cos(theta)
    dec_circle = NGC1275_DEC + SEARCH_RADIUS_DEG * np.sin(theta)
    ax.plot(ra_circle, dec_circle, color="black", lw=lw, ls="--",
            label=f"r = {SEARCH_RADIUS_DEG:g}°")
    ax.plot([NGC1275_RA], [NGC1275_DEC], marker="*", color="black", ms=12,
            mec="white", mew=0.8, label="NGC 1275")


def _patch_polygon(row: pd.Series) -> Polygon:
    xy = [
        (row.llcra, row.llcdec),
        (row.ulcra, row.ulcdec),
        (row.urcra, row.urcdec),
        (row.lrcra, row.lrcdec),
    ]
    return Polygon(xy, closed=True)


def plot_combined(patches: pd.DataFrame) -> Path:
    """Single-panel sky map with all bands overlaid."""

    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8.0, 8.0))
    bands_in_data = sorted(patches["band"].unique())
    for band in bands_in_data:
        color = BAND_COLORS.get(band, "#444444")
        sub = patches[patches["band"] == band]
        for _, row in sub.iterrows():
            poly = _patch_polygon(row)
            poly.set_facecolor(color)
            poly.set_alpha(0.18)
            poly.set_edgecolor(color)
            poly.set_linewidth(0.4)
            ax.add_patch(poly)
        ax.plot([], [], color=color, lw=6, alpha=0.5, label=band)
    _draw_search_circle(ax)
    _setup_sky_axis(ax)
    ax.set_title(
        f"HSCLA2020 patch footprints — {SEARCH_RADIUS_DEG:g}° around NGC 1275\n"
        f"{len(patches)} patches across {len(bands_in_data)} bands"
    )
    ax.legend(fontsize=8, loc="upper right", framealpha=0.85, ncol=2)
    out = FIG_DIR / "step1_coverage_combined.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def plot_per_band(patches: pd.DataFrame) -> Path:
    """Per-band facets, broadbands first, then narrowbands."""

    bands_in_data = sorted(patches["band"].unique())
    bands_order = [b for b in HSC_BROADBANDS if b in bands_in_data]
    bands_order += [b for b in bands_in_data if b not in HSC_BROADBANDS]
    n_bands = len(bands_order)
    ncols = min(4, n_bands) if n_bands else 1
    nrows = math.ceil(n_bands / ncols) if n_bands else 1
    fig, axes = plt.subplots(
        nrows, ncols, figsize=(3.5 * ncols, 3.6 * nrows), squeeze=False
    )
    for idx, band in enumerate(bands_order):
        ax = axes[idx // ncols][idx % ncols]
        color = BAND_COLORS.get(band, "#444444")
        sub = patches[patches["band"] == band]
        for _, row in sub.iterrows():
            poly = _patch_polygon(row)
            poly.set_facecolor(color)
            poly.set_alpha(0.45)
            poly.set_edgecolor("black")
            poly.set_linewidth(0.3)
            ax.add_patch(poly)
        _draw_search_circle(ax, lw=1.0)
        _setup_sky_axis(ax)
        seeing_med = (
            float(sub["seeing"].median()) if "seeing" in sub.columns and len(sub) else float("nan")
        )
        ax.set_title(f"{band} — n={len(sub)}  seeing≈{seeing_med:.2f}″")
        ax.legend().set_visible(False)
    for j in range(len(bands_order), nrows * ncols):
        axes[j // ncols][j % ncols].axis("off")
    fig.suptitle(
        f"HSCLA2020 per-band coverage — {SEARCH_RADIUS_DEG:g}° around NGC 1275",
        fontsize=13,
    )
    out = FIG_DIR / "step1_coverage_per_band.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


# --------------------------------------------------------------------------- #
# Markdown summary
# --------------------------------------------------------------------------- #


def write_summary(patches: pd.DataFrame, frame_counts: pd.DataFrame,
                  patch_csv: Path, frame_csv: Path,
                  fig_combined: Path, fig_per_band: Path) -> Path:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    out = NOTES_DIR / "step1_coverage.md"

    per_band_rows: list[str] = []
    bands_in_data = sorted(patches["band"].unique())
    for band in bands_in_data:
        sub = patches[patches["band"] == band]
        n_patch = len(sub)
        n_tract = sub["tract"].nunique()
        seeing_med = float(sub["seeing"].median()) if n_patch else float("nan")
        seeing_min = float(sub["seeing"].min()) if n_patch else float("nan")
        seeing_max = float(sub["seeing"].max()) if n_patch else float("nan")
        per_band_rows.append(
            f"| {band} | {n_patch} | {n_tract} | "
            f"{seeing_med:.3f} | {seeing_min:.3f} | {seeing_max:.3f} |"
        )

    frame_rows: list[str] = []
    for _, r in frame_counts.iterrows():
        frame_rows.append(f"| {r.band} | {int(r.n_frames)} | {int(r.n_visits)} |")

    tracts_all = sorted(patches["tract"].unique().tolist())
    fig_combined_rel = fig_combined.relative_to(EXAMPLE_DIR)
    fig_per_band_rel = fig_per_band.relative_to(EXAMPLE_DIR)
    patch_csv_rel = patch_csv.relative_to(EXAMPLE_DIR)
    frame_csv_rel = frame_csv.relative_to(EXAMPLE_DIR)

    text = f"""# Perseus / NGC 1275 — Step 1: HSCLA2020 coverage

## Search

- Center: NGC 1275 (Perseus cluster BCG)
  - RA  = {NGC1275_RA:.7f}° (J2000)
  - Dec = {NGC1275_DEC:.7f}° (J2000)
- Radius: {SEARCH_RADIUS_DEG:g}° (great-circle)
- Source: local Parquet mirror of `la2020.mosaic` + `la2020.frame`
- Method: 4°-square bbox via `coverage.region_coverage(source='local')`,
  post-filtered to patches whose center or any corner falls within
  {SEARCH_RADIUS_DEG:g}° great-circle distance of the target.

## Totals

- Patches inside the circle: **{len(patches)}**
- Bands present: **{len(bands_in_data)}** — {", ".join(bands_in_data)}
- Tracts touched ({len(tracts_all)}): {", ".join(str(t) for t in tracts_all)}

## Coadd patches per band

| Band | n_patches | n_tracts | seeing median (″) | min (″) | max (″) |
|------|-----------|----------|-------------------|---------|---------|
{chr(10).join(per_band_rows)}

## Single-CCD frames per band (la2020.frame, 4°-box proximity)

| Band | n_frames | n_visits |
|------|----------|----------|
{chr(10).join(frame_rows) if frame_rows else "| _none_ | 0 | 0 |"}

## Files

- Patch catalog (CSV): [`{patch_csv_rel}`](../{patch_csv_rel})
- Frame counts (CSV): [`{frame_csv_rel}`](../{frame_csv_rel})
- Combined QA figure: [`{fig_combined_rel}`](../{fig_combined_rel})
- Per-band QA figure: [`{fig_per_band_rel}`](../{fig_per_band_rel})

## Notes

- HSC patches are roughly 12′ on a side; a 2° radius therefore inscribes
  on the order of ~100 patches per broadband if coverage is contiguous.
- The frame proximity test in `la2020.frame` uses a CCD-center margin of
  0.20° + half-box (`coverage.FRAME_HALF_DEG`), so a few rows outside
  the strict circle may sneak into the frame counts. The patch table,
  by contrast, is post-filtered by exact angular distance.
- For Step 2 (metadata collection) the patch CSV is the primary input —
  use `band`, `tract`, `patch_s` as the (band, tract, patch) key.
"""
    out.write_text(text)
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    print(f"Center: NGC 1275 at RA={NGC1275_RA:.7f}, Dec={NGC1275_DEC:.7f}")
    print(f"Radius: {SEARCH_RADIUS_DEG:g}° (4°-square bbox + great-circle post-filter)")

    patches = collect_patches()
    print(f"Patches inside circle: {len(patches)} "
          f"across {patches['band'].nunique()} bands.")

    frame_counts = collect_frame_counts()
    print(f"Frame bands present: {len(frame_counts)}")

    patch_csv, frame_csv = write_tables(patches, frame_counts)
    print(f"Wrote {patch_csv}")
    print(f"Wrote {frame_csv}")

    fig_combined = plot_combined(patches)
    fig_per_band = plot_per_band(patches)
    print(f"Wrote {fig_combined}")
    print(f"Wrote {fig_per_band}")

    summary = write_summary(
        patches, frame_counts, patch_csv, frame_csv, fig_combined, fig_per_band
    )
    print(f"Wrote {summary}")


if __name__ == "__main__":
    main()
