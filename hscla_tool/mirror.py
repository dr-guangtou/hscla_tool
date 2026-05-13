"""Local Parquet mirrors of the HSCLA metadata catalogs.

This module turns whole HSCLA catalog tables into single Parquet files
on disk so coverage / overlap / lookup queries can run offline without
the network round-trip and the server's preview timeout.

Mirrors are stored under the directory returned by
``config.mirror_root()`` — by default ``/Volumes/galaxy/hsc/la2020/``.
Each table becomes one file named ``<table>.parquet``.

What this module is **not** for: the bulk photometry tables
(`forced` / `meas` and their detail variants) have tens of millions of
rows and dozens of columns; mirroring them belongs to a different
strategy (per-tract files, partition by band, etc.) and is out of
scope here. The catalogs supported by `build_mirror` are the metadata
tables that are small enough to fit in one Parquet file each:
``mosaic`` (~465K rows, ~30 MB), ``frame`` (~4.2M rows, ~300–700 MB),
and optionally ``mosaicframe`` and ``wcs``.

Typical use::

    # One-time build (slow for `frame`, fast for `mosaic`):
    uv run python -m hscla_tool.mirror build mosaic
    uv run python -m hscla_tool.mirror build frame

    # Then in code:
    from hscla_tool import mirror
    df_mosaic = mirror.load_mirror("mosaic")
"""

from __future__ import annotations

import argparse
import gzip
import io
import logging
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from hscla_tool import config
from hscla_tool import sql as _sql

LOGGER = logging.getLogger(__name__)

# Tables this module knows how to mirror as a single Parquet file each.
SUPPORTED_TABLES: tuple[str, ...] = ("mosaic", "frame", "mosaicframe", "wcs")

# SQL schema name. Matches the short release key everywhere else in the toolkit.
SCHEMA_NAME = "la2020"


class MirrorError(RuntimeError):
    """Raised when building or loading a local catalog mirror fails."""


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def mirror_path(table: str, *, root: Path | None = None) -> Path:
    """Return the Parquet path for one mirrored table.

    Does not check whether the file actually exists. The root is taken
    from `config.mirror_root()` unless overridden.
    """

    _check_supported(table)
    base = root if root is not None else config.mirror_root()
    return base / f"{table}.parquet"


def is_mirrored(table: str, *, root: Path | None = None) -> bool:
    """True if the local Parquet mirror for `table` exists."""

    return mirror_path(table, root=root).is_file()


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_mirror(
    table: str,
    *,
    client: _sql.HscLaClient | None = None,
    root: Path | None = None,
    force: bool = False,
    progress: bool = True,
) -> Path:
    """Materialize one HSCLA metadata table as a local Parquet file.

    Submits ``SELECT * FROM la2020.<table>`` as a ``csv.gz`` job, waits
    for it to finish, downloads the result, decompresses, parses with
    `pandas`, and writes a Parquet file to ``mirror_path(table)``.

    If the destination already exists, returns its path immediately
    unless ``force=True``. The destination directory is created on
    demand.
    """

    _check_supported(table)
    base = root if root is not None else config.mirror_root()
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"{table}.parquet"
    if dest.is_file() and not force:
        LOGGER.info("mirror already exists at %s (pass force=True to rebuild)", dest)
        return dest

    cli = client or _sql.HscLaClient()
    sql_text = f"SELECT * FROM {SCHEMA_NAME}.{table}"

    if progress:
        LOGGER.info("submitting full-table SQL: %s", sql_text)
    job = cli.submit_sql(sql_text, out_format="csv.gz", include_metainfo=False)
    started = time.monotonic()
    finished = cli.wait_for_job(job.id)
    elapsed = time.monotonic() - started
    if progress:
        LOGGER.info("job %d done in %.1fs", finished.id, elapsed)

    with tempfile.TemporaryDirectory(prefix="hscla_mirror_") as td:
        gz_path = Path(td) / f"{table}.csv.gz"
        cli.download_job(finished.id, gz_path)
        cli.delete_job(finished.id)
        if progress:
            LOGGER.info(
                "downloaded %.1f MB gzipped CSV", gz_path.stat().st_size / (1024 * 1024)
            )
        df = _read_hscla_csv_gz(gz_path)

    df = _coerce_object_columns_to_string(df)

    # Atomic write: parquet -> tmp -> rename.
    tmp_dest = dest.with_suffix(".parquet.tmp")
    df.to_parquet(tmp_dest, index=False, compression="zstd")
    tmp_dest.replace(dest)
    if progress:
        LOGGER.info(
            "wrote %d rows, %d columns to %s (%.1f MB on disk)",
            df.shape[0],
            df.shape[1],
            dest,
            dest.stat().st_size / (1024 * 1024),
        )
    return dest


def load_mirror(table: str, *, root: Path | None = None) -> pd.DataFrame:
    """Read a previously built mirror back as a `pandas.DataFrame`."""

    path = mirror_path(table, root=root)
    if not path.is_file():
        raise MirrorError(
            f"no local mirror for {table!r} at {path}. "
            f"Run `uv run python -m hscla_tool.mirror build {table}` first."
        )
    return pd.read_parquet(path)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _check_supported(table: str) -> None:
    if table not in SUPPORTED_TABLES:
        raise MirrorError(
            f"table {table!r} is not a supported mirror; pick one of {SUPPORTED_TABLES}."
        )


def _coerce_object_columns_to_string(df: pd.DataFrame) -> pd.DataFrame:
    """Make every `object`-dtype column a clean nullable string column.

    The HSCLA `frame` table includes a literal column named ``object``
    (the target name in the observing log) whose values are sometimes
    numeric and sometimes alphabetic. pandas' CSV reader infers it as
    plain `object` dtype and pyarrow then refuses to write it because
    the cells mix int and bytes. Casting to pandas' nullable string
    dtype gives us a single representation that pyarrow accepts and
    that preserves real nulls (instead of turning them into "None").
    """

    # Check the dtype directly: `select_dtypes(include='object')` would also
    # pick up pandas' StringDtype on pandas >=3 and emit a deprecation
    # warning, so we steer clear.
    object_cols = [c for c in df.columns if df[c].dtype == object]
    if not object_cols:
        return df
    df = df.copy()
    for col in object_cols:
        df[col] = df[col].astype("string")
    return df


def _read_hscla_csv_gz(gz_path: Path) -> pd.DataFrame:
    """Decompress and parse an HSCLA ``csv.gz`` download into a DataFrame.

    The header line still has the ``# `` prefix (a HSCLA quirk; see
    `hscla_tool.sql._read_sql_csv` for the same handling logic).
    """

    with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    header_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("#"):
            header_idx = i
        else:
            break
    if header_idx < 0:
        return pd.read_csv(io.StringIO(text))
    header = lines[header_idx].lstrip("#").lstrip()
    body = "\n".join([header, *lines[header_idx + 1:]])
    return pd.read_csv(io.StringIO(body))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build local Parquet mirrors of HSCLA metadata catalogs.",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    build = subs.add_parser("build", help="Download a table and save it as Parquet.")
    build.add_argument("table", choices=SUPPORTED_TABLES, help="HSCLA table to mirror.")
    build.add_argument("--force", action="store_true", help="Rebuild even if the file exists.")
    build.add_argument("--root", type=Path, default=None,
                       help="Override the mirror root directory.")

    status = subs.add_parser("status", help="Show which mirrors exist on disk.")
    status.add_argument("--root", type=Path, default=None,
                        help="Override the mirror root directory.")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "build":
        path = build_mirror(args.table, root=args.root, force=args.force)
        print(f"mirror ready at {path}")
        return 0
    if args.cmd == "status":
        root = args.root if args.root is not None else config.mirror_root()
        print(f"mirror root: {root}")
        for table in SUPPORTED_TABLES:
            p = root / f"{table}.parquet"
            marker = "[x]" if p.is_file() else "[ ]"
            size = f"{p.stat().st_size / (1024 * 1024):.1f} MB" if p.is_file() else "—"
            print(f"  {marker} {table:<14} {size:>10}  {p}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
