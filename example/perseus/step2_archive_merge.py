"""Step 2c (file-archive flavor): merge per-patch FITS catalogs into a
single per-tract flat parquet.

Strategy (per tract):
  1. List every (band, patch) forced_src file under
     `${root}/archive/<band>/<tract>/<patch>/`.
  2. For each patch, inner-join the 4 band-side forced_src tables on
     `id`. Rename non-shared columns to `<band_letter>_<snake_case>`
     so a single row carries g/r/i/z measurements side-by-side.
  3. If `meas` files are also present for that patch, inner-join the
     reference-band (HSC-I) meas catalog to bring in the
     `detect_isPatchInner` / `detect_isTractInner` / `deblend_nChild`
     flags, and derive `isprimary`. Optionally pass `--primary-only`
     to apply that cut.
  4. Concatenate the per-patch merged tables vertically to form the
     per-tract output, written as parquet.

Output:
  ${root}/catalogs_archive/perseus_tract_<tract>_forced.parquet

Run from the repo root:
  uv run python example/perseus/step2_archive_merge.py --tract 15548
  uv run python example/perseus/step2_archive_merge.py --tract 15548 --primary-only
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from astropy.io import fits
from astropy.table import Table

LOGGER = logging.getLogger("perseus.step2.merge")

BAND_LETTER: dict[str, str] = {
    "HSC-G": "g", "HSC-R": "r", "HSC-I": "i", "HSC-Z": "z", "HSC-Y": "y",
}
REF_BAND = "HSC-I"
DEFAULT_ROOT = Path("/Volumes/galaxy/data/perseus")
DEFAULT_BANDS = ("HSC-G", "HSC-R", "HSC-I", "HSC-Z")

# Columns we keep once (identical or near-identical across bands).
SHARED_COLS: tuple[str, ...] = ("id", "coord_ra", "coord_dec", "parent",
                                 "deblend_nChild")

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def to_snake(name: str) -> str:
    """hscPipe camelCase / mixed -> all-lowercase snake_case."""

    return _CAMEL_RE.sub("_", name).lower()


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PatchKey:
    tract: int
    patch: str       # 'x,y'


def list_patches(root: Path, tract: int, bands: tuple[str, ...]) -> list[PatchKey]:
    """Patches where every requested band has a forced_src file."""

    # Use the first band's patches as the candidate set; filter to those
    # where every band has the file too. Reference-band-first ordering
    # would also work; the first band is fine.
    base_dir = root / "archive" / bands[0] / str(tract)
    if not base_dir.is_dir():
        return []
    candidates = sorted(p.name for p in base_dir.iterdir() if p.is_dir())
    keep: list[PatchKey] = []
    for patch in candidates:
        ok = True
        for band in bands:
            fp = root / "archive" / band / str(tract) / patch \
                / f"forced_src-{band}-{tract}-{patch}.fits"
            if not fp.is_file():
                ok = False
                break
        if ok:
            keep.append(PatchKey(tract=tract, patch=patch))
    return keep


def _flatten(t: Table) -> pd.DataFrame:
    """Drop multidimensional columns (e.g. bit-packed `flags`) before to_pandas."""

    keep = [c for c in t.colnames if t[c].ndim <= 1]
    return t[keep].to_pandas()


def read_forced_src(root: Path, key: PatchKey, band: str) -> pd.DataFrame:
    fp = root / "archive" / band / str(key.tract) / key.patch \
        / f"forced_src-{band}-{key.tract}-{key.patch}.fits"
    return _flatten(Table.read(str(fp), hdu=1, memmap=False))


def read_meas(root: Path, key: PatchKey, band: str) -> pd.DataFrame | None:
    fp = root / "archive" / band / str(key.tract) / key.patch \
        / f"meas-{band}-{key.tract}-{key.patch}.fits"
    if not fp.is_file():
        return None
    return _flatten(Table.read(str(fp), hdu=1, memmap=False))


def read_meas_isprimary(root: Path, key: PatchKey, band: str) -> pd.DataFrame | None:
    """Return a DataFrame of (id, isprimary) extracted from the meas `flags` bits.

    In hscPipe v8 meas catalogs, `detect_isPrimary` lives as a bit in the
    packed `flags` column, with TFLAG<N> header keys naming each bit
    (N is 1-based, so bit index = N - 1).
    """

    fp = root / "archive" / band / str(key.tract) / key.patch \
        / f"meas-{band}-{key.tract}-{key.patch}.fits"
    if not fp.is_file():
        return None
    with fits.open(fp) as hdul:
        h = hdul[1].header
        bit_idx: int | None = None
        for k in h:
            if k.startswith("TFLAG") and h[k] == "detect_isPrimary":
                # k is e.g. "TFLAG246" -> bit index = 246 - 1 = 245
                bit_idx = int(k.removeprefix("TFLAG")) - 1
                break
        if bit_idx is None:
            raise KeyError(f"detect_isPrimary not found in meas TFLAG keys: {fp}")
        data = hdul[1].data
        flags = data["flags"]
        ids = data["id"]
        if flags.ndim == 2:
            isprimary = flags[:, bit_idx].astype(bool)
        else:
            byte_i, bit_in_byte = bit_idx // 8, bit_idx % 8
            isprimary = ((flags[:, byte_i] >> bit_in_byte) & 1).astype(bool)
    return pd.DataFrame({"id": ids, "isprimary": isprimary})


# --------------------------------------------------------------------------- #
# Per-patch merge
# --------------------------------------------------------------------------- #


def merge_patch(
    root: Path,
    key: PatchKey,
    bands: tuple[str, ...],
    *,
    want_isprimary: bool,
) -> pd.DataFrame:
    """Build a single wide row-per-object table for one patch."""

    by_band: dict[str, pd.DataFrame] = {b: read_forced_src(root, key, b) for b in bands}

    # The reference table provides shared cols; per-band tables only
    # contribute band-tagged measurement columns.
    ref_band = bands[0]
    ref = by_band[ref_band]
    shared_present = [c for c in SHARED_COLS if c in ref.columns]
    out = ref[shared_present].copy()
    out["tract"] = key.tract
    out["patch"] = key.patch

    # coord_ra/dec are in radians on disk; convert at the boundary.
    if "coord_ra" in out.columns:
        out["coord_ra"] = np.degrees(out["coord_ra"])
    if "coord_dec" in out.columns:
        out["coord_dec"] = np.degrees(out["coord_dec"])

    # Per-band measurement columns get a band prefix on the snake-cased
    # name. Shared columns get dropped from each band-side table since
    # they're already in `out` from the ref band.
    for band in bands:
        letter = BAND_LETTER[band]
        df = by_band[band]
        meas_cols = [c for c in df.columns if c not in SHARED_COLS]
        per_band = df[["id"] + meas_cols].rename(
            columns={c: f"{letter}_{to_snake(c)}" for c in meas_cols}
        )
        out = out.merge(per_band, on="id", how="inner")

    if want_isprimary:
        prim = read_meas_isprimary(root, key, REF_BAND)
        if prim is None:
            raise FileNotFoundError(
                f"--primary-only requested but {REF_BAND} meas catalog is "
                f"missing for tract {key.tract} patch {key.patch}; "
                f"run step2_archive_fetch.py --kind meas first."
            )
        out = out.merge(prim, on="id", how="inner")

    return out


# --------------------------------------------------------------------------- #
# Per-tract driver
# --------------------------------------------------------------------------- #


def merge_tract(
    root: Path,
    tract: int,
    bands: tuple[str, ...],
    *,
    primary_only: bool,
    out_path: Path,
) -> int:
    want_isprimary = primary_only
    keys = list_patches(root, tract, bands)
    LOGGER.info("tract %d: %d patches with all %d bands", tract, len(keys), len(bands))
    parts: list[pd.DataFrame] = []
    total_rows = 0
    for idx, key in enumerate(keys, 1):
        df = merge_patch(root, key, bands, want_isprimary=want_isprimary)
        if primary_only:
            kept = df["isprimary"]
            df = df.loc[kept].copy()
        parts.append(df)
        total_rows += len(df)
        LOGGER.info("  [%d/%d] %s -> %d rows (running %d)",
                    idx, len(keys), key.patch, len(df), total_rows)
    merged = pd.concat(parts, ignore_index=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(out_path, index=False)
    LOGGER.info("wrote %s (%d rows × %d cols, %.2f MB)",
                out_path, len(merged), merged.shape[1],
                out_path.stat().st_size / 1e6)
    return len(merged)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tract", type=int, required=True,
        help="Tract id to merge.",
    )
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT,
        help=f"Archive root (default: {DEFAULT_ROOT}).",
    )
    parser.add_argument(
        "--band", type=str, action="append",
        help=f"Band; may repeat. Default: {','.join(DEFAULT_BANDS)}.",
    )
    parser.add_argument(
        "--primary-only", action="store_true",
        help="Drop rows where isprimary is False. Requires meas catalogs.",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Output parquet path. Default: "
             "${root}/catalogs_archive/perseus_tract_<tract>_forced[_primary].parquet",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="DEBUG-level logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv) if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bands = tuple(args.band) if args.band else DEFAULT_BANDS
    suffix = "primary" if args.primary_only else "all"
    out_path = args.out or (
        args.root / "catalogs_archive"
        / f"perseus_tract_{args.tract}_forced_{suffix}.parquet"
    )

    merge_tract(
        args.root, args.tract, bands,
        primary_only=args.primary_only,
        out_path=out_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
