"""`hscla` console script — one command line per user story in `docs/todo.md`.

Eight subcommands, each thin enough to be a wrapper around an existing
module function plus a short human-readable summary:

- ``hscla coverage``   → :func:`hscla_tool.coverage.region_coverage`
- ``hscla frames``     → :func:`hscla_tool.coverage.frame_coverage`
- ``hscla cutout``     → :func:`hscla_tool.cutout.fetch_cutout`
- ``hscla psf``        → :func:`hscla_tool.psf.fetch_psf`
- ``hscla sql``        → :func:`hscla_tool.sql.run_sql` / ``preview_sql``
- ``hscla crossmatch`` → :func:`hscla_tool.crossmatch.match` (with loud warning)
- ``hscla mirror``     → :mod:`hscla_tool.mirror` (build / status)
- ``hscla archive``    → :mod:`hscla_tool.archive` (download / list)

Conventions
-----------

- Friendly progress lines go to **stderr** so stdout can be piped. Use
  ``--quiet`` / ``-q`` to suppress them.
- For subcommands that produce a file, the default destination is an
  auto-named path under ``./outputs/<subkind>/``. Pass ``--out`` to
  override. The cache used by the underlying module is unchanged — the
  CLI hardlinks the cached file to the friendly path when possible.
- Exit codes: 0 ok, 2 no coverage, 3 missing credentials, 4 mirror
  missing, 5 archive / SQL / cutout failure, 6 bad arguments, 130
  keyboard interrupt.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

# Importing the package runs the credential check; that's intentional.
# If credentials are missing, the user gets the package-level message
# pointing at ~/.zshenv before the CLI even starts parsing args.
from hscla_tool import (
    archive as _archive,
)
from hscla_tool import (
    config as _config,
)
from hscla_tool import (
    coverage as _coverage,
)
from hscla_tool import (
    crossmatch as _crossmatch,
)
from hscla_tool import (
    cutout as _cutout,
)
from hscla_tool import (
    mirror as _mirror,
)
from hscla_tool import (
    psf as _psf,
)
from hscla_tool import (
    sql as _sql,
)
from hscla_tool.config import MissingCredentialsError

LOGGER = logging.getLogger("hscla")

EXIT_OK = 0
EXIT_NO_COVERAGE = 2
EXIT_MISSING_CREDS = 3
EXIT_MIRROR_MISSING = 4
EXIT_FETCH_FAILURE = 5
EXIT_BAD_ARGS = 6
EXIT_INTERRUPTED = 130


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _progress(quiet: bool, msg: str) -> None:
    """Print one friendly progress line to stderr unless --quiet is set."""

    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def _outputs_subdir(name: str) -> Path:
    """Resolve ``${HSCLA_TOOL_CACHE-./outputs}/<name>`` and make sure it exists."""

    root = _config.cache_dir() / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _link_or_copy(src: Path, dst: Path) -> Path:
    """Make `dst` point at the same bytes as `src`, hardlink if possible.

    If `dst` already exists we leave it alone — same content by cache
    hash, and overwriting would invalidate any hardlink the user holds.
    """

    src = Path(src)
    dst = Path(dst)
    if dst == src or dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        # Cross-device, unsupported FS, etc. — fall back to copy.
        shutil.copy2(src, dst)
    return dst


def _sanitize_band(band: str) -> str:
    """Make a band name safe for inclusion in a filename."""

    return band.replace("/", "_").replace(" ", "_")


def _fmt_radec(ra: float, dec: float) -> str:
    """RA/Dec in a filename-friendly form, sign included for Dec."""

    sign = "+" if dec >= 0 else "-"
    return f"ra{ra:.4f}_dec{sign}{abs(dec):.4f}"


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #


def _cmd_coverage(args: argparse.Namespace) -> int:
    quiet = args.quiet
    _progress(quiet, f"querying coadd coverage at RA={args.ra} Dec={args.dec} "
                     f"(size={args.size_deg} deg, source={args.source})")
    result = _coverage.region_coverage(
        args.ra, args.dec, size_deg=args.size_deg,
        source=args.source, release=args.release,
    )
    if not result.covered:
        print("no HSCLA coadd coverage at this position", flush=True)
        return EXIT_OK
    print(f"bands: {', '.join(result.filters)}")
    print(f"patches ({len(result.patches)} total):")
    for p in result.patches:
        seeing = f"{p.seeing:.3f}" if p.seeing == p.seeing else "nan"
        print(f"  {p.band:<6} tract={p.tract:<6} patch={p.patch_s:<6} "
              f"seeing={seeing}\"")
    if result.mean_seeing_per_band:
        print("mean seeing per band:")
        for band, val in result.mean_seeing_per_band.items():
            print(f"  {band:<6} {val:.3f}\"")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #


def _cmd_frames(args: argparse.Namespace) -> int:
    quiet = args.quiet
    _progress(quiet, f"querying single-CCD frame coverage at RA={args.ra} "
                     f"Dec={args.dec} (size={args.size_deg} deg, "
                     f"source={args.source})")
    result = _coverage.frame_coverage(
        args.ra, args.dec, size_deg=args.size_deg,
        source=args.source, release=args.release,
        detailed=args.detailed,
    )
    if not result.covered:
        print("no HSCLA single-frame coverage at this position", flush=True)
        return EXIT_OK
    print(f"bands: {', '.join(result.filters)}")
    print(f"{'band':<8}{'n_frames':>10}{'n_visits':>10}")
    for band in result.filters:
        s = result.band_summary[band]
        print(f"{band:<8}{s.n_frames:>10}{s.n_visits:>10}")
    if args.detailed and result.frames is not None:
        print()
        print(f"detailed: {len(result.frames)} frames")
        for row in result.frames[: args.head]:
            print("  " + ", ".join(f"{k}={v}" for k, v in row.items()))
        if len(result.frames) > args.head:
            print(f"  ... ({len(result.frames) - args.head} more rows)")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# cutout
# --------------------------------------------------------------------------- #


def _cmd_cutout(args: argparse.Namespace) -> int:
    quiet = args.quiet
    out: Path | None = Path(args.out).expanduser() if args.out else None
    if out is None:
        name = (
            f"cutout_{_fmt_radec(args.ra, args.dec)}"
            f"_{_sanitize_band(args.band)}"
            f"_{args.size_arcsec:g}as_{args.kind}.fits"
        )
        out = _outputs_subdir("cutouts") / name
    _progress(quiet, f"fetching {args.band} cutout at RA={args.ra} "
                     f"Dec={args.dec} (size={args.size_arcsec}\")")
    try:
        cutout = _cutout.fetch_cutout(
            args.ra, args.dec,
            size_arcsec=args.size_arcsec,
            band=args.band,
            kind=args.kind,
            tract=args.tract,
            with_variance=not args.no_variance,
            with_mask=not args.no_mask,
        )
    except _cutout.NoCoverageError as exc:
        print(f"no coverage: {exc}", file=sys.stderr)
        return EXIT_NO_COVERAGE
    try:
        _link_or_copy(cutout.fits_path, out)
        n_hdus = len(cutout.hdul)
        size_kb = out.stat().st_size / 1024
        has_image = cutout.image is not None
        has_mask = cutout.mask_hdu is not None
        has_var = cutout.variance is not None
        _progress(quiet, f"saved cutout: {n_hdus} HDUs "
                         f"(image={has_image}, mask={has_mask}, variance={has_var}) "
                         f"— {size_kb:.1f} KB")
    finally:
        cutout.close()
    print(out)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# cutouts (batch)
# --------------------------------------------------------------------------- #


def _cmd_cutouts(args: argparse.Namespace) -> int:
    import pandas as pd

    quiet = args.quiet
    src = Path(args.input).expanduser()
    if not src.is_file():
        print(f"hscla cutouts: input not found: {src}", file=sys.stderr)
        return EXIT_BAD_ARGS
    if src.suffix.lower() in (".parquet", ".pq"):
        df = pd.read_parquet(src)
    else:
        df = pd.read_csv(src)

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else _outputs_subdir("cutouts")
    out_dir.mkdir(parents=True, exist_ok=True)

    _progress(quiet, f"fetching {len(df):,} cutouts in one batch")
    try:
        result = _cutout.fetch_cutouts(df)
    except _cutout.CutoutError as exc:
        print(f"hscla cutouts: {exc}", file=sys.stderr)
        return EXIT_FETCH_FAILURE

    saved: list[Path] = []
    try:
        for c in result.cutouts:
            if c is None:
                continue
            name = (
                f"cutout_{_fmt_radec(c.ra, c.dec)}"
                f"_{_sanitize_band(c.band)}"
                f"_{c.size_arcsec:g}as_{c.kind}.fits"
            )
            dest = out_dir / name
            _link_or_copy(c.fits_path, dest)
            saved.append(dest)
    finally:
        result.close()

    _progress(
        quiet,
        f"saved {result.n_success}/{len(result)} cutouts to {out_dir} "
        f"({result.n_failure} with no coverage)",
    )
    for idx, exc in result.failures:
        _progress(quiet, f"  row {idx}: {exc}")
    for path in saved:
        print(path)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# psf
# --------------------------------------------------------------------------- #


def _cmd_psf(args: argparse.Namespace) -> int:
    quiet = args.quiet
    out: Path | None = Path(args.out).expanduser() if args.out else None
    if out is None:
        name = (
            f"psf_{_fmt_radec(args.ra, args.dec)}"
            f"_{_sanitize_band(args.band)}_{args.kind}.fits"
        )
        out = _outputs_subdir("psfs") / name
    _progress(quiet, f"fetching {args.band} PSF at RA={args.ra} Dec={args.dec}")
    try:
        psf = _psf.fetch_psf(
            args.ra, args.dec,
            band=args.band, kind=args.kind,
            tract=args.tract, patch=args.patch,
            centered=not args.no_centered,
        )
    except _cutout.NoCoverageError as exc:
        # Re-exported from psf via the cutout module.
        print(f"no coverage: {exc}", file=sys.stderr)
        return EXIT_NO_COVERAGE
    try:
        _link_or_copy(psf.fits_path, out)
        shape = psf.array.shape
        total = float(psf.array.sum())
        _progress(quiet, f"saved PSF: shape={shape}, sum={total:.6f}")
    finally:
        psf.close()
    print(out)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# sql
# --------------------------------------------------------------------------- #


def _read_sql_source(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).expanduser().read_text(encoding="utf-8")
    if args.query is None:
        raise SystemExit("hscla sql: provide a query string or --file <path>")
    return args.query


def _cmd_sql(args: argparse.Namespace) -> int:
    quiet = args.quiet
    sql_text = _read_sql_source(args).strip()
    if not sql_text:
        print("hscla sql: empty query", file=sys.stderr)
        return EXIT_BAD_ARGS
    if args.preview:
        _progress(quiet, "running preview (fast, ~5s server-side timeout)")
        result = _sql.preview_sql(sql_text)
        fields = result.get("fields", [])
        rows = result.get("rows", [])
        print("\t".join(fields))
        for row in rows[: args.limit]:
            print("\t".join("" if v is None else str(v) for v in row))
        if len(rows) > args.limit:
            print(f"... ({len(rows) - args.limit} more rows)", file=sys.stderr)
        return EXIT_OK

    out: Path | None = Path(args.out).expanduser() if args.out else None
    if out is None:
        name = f"sql_{_utc_stamp()}_{_short_hash(sql_text)}.csv"
        out = _outputs_subdir("sql") / name
    _progress(quiet, "submitting SQL job (this can take a few seconds to many minutes)")
    started = time.monotonic()
    df = _sql.run_sql(sql_text, cache=not args.no_cache)
    elapsed = time.monotonic() - started
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    _progress(quiet, f"got {len(df):,} rows × {df.shape[1]} cols in {elapsed:.1f}s")
    print(out)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# crossmatch
# --------------------------------------------------------------------------- #


_CROSSMATCH_BANNER = (
    "WARNING: HSCLA crossmatch is currently very slow on the server side\n"
    "         (often 30–45 minutes even for a few-row input). The query\n"
    "         is correct but expect to wait. Ctrl-C will not stop the\n"
    "         server-side job; the local poll just stops watching it."
)


def _cmd_crossmatch(args: argparse.Namespace) -> int:
    import pandas as pd  # heavy import, deferred to actual use

    quiet = args.quiet
    print(_CROSSMATCH_BANNER, file=sys.stderr)
    src = Path(args.input).expanduser()
    if not src.is_file():
        print(f"hscla crossmatch: input not found: {src}", file=sys.stderr)
        return EXIT_BAD_ARGS
    if src.suffix.lower() in (".parquet", ".pq"):
        table = pd.read_parquet(src)
    else:
        table = pd.read_csv(src)

    extras: tuple[str, ...] = tuple(
        c.strip() for c in (args.extra_columns or "").split(",") if c.strip()
    )

    out: Path | None = Path(args.out).expanduser() if args.out else None
    if out is None:
        name = f"crossmatch_{src.stem}_r{args.radius_arcsec:g}.csv"
        out = _outputs_subdir("crossmatch") / name

    _progress(quiet, f"crossmatching {len(table):,} rows against HSCLA "
                     f"(radius={args.radius_arcsec}\")")
    started = time.monotonic()
    matched = _crossmatch.match(
        table,
        ra_col=args.ra_col, dec_col=args.dec_col, id_col=args.id_col,
        radius_arcsec=args.radius_arcsec,
        extra_columns=extras,
        nearest_only=args.nearest_only,
    )
    elapsed = time.monotonic() - started
    out.parent.mkdir(parents=True, exist_ok=True)
    matched.to_csv(out, index=False)
    _progress(quiet, f"got {len(matched):,} matches in {elapsed:.1f}s")
    print(out)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# mirror
# --------------------------------------------------------------------------- #


def _cmd_mirror_build(args: argparse.Namespace) -> int:
    path = _mirror.build_mirror(args.table, root=args.root, force=args.force)
    print(path)
    return EXIT_OK


def _cmd_mirror_status(args: argparse.Namespace) -> int:
    root = args.root if args.root is not None else _config.mirror_root()
    print(f"mirror root: {root}")
    for table in _mirror.SUPPORTED_TABLES:
        p = root / f"{table}.parquet"
        marker = "[x]" if p.is_file() else "[ ]"
        size = f"{p.stat().st_size / (1024 * 1024):.1f} MB" if p.is_file() else "—"
        print(f"  {marker} {table:<14} {size:>10}  {p}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# archive
# --------------------------------------------------------------------------- #


def _cmd_archive_download(args: argparse.Namespace) -> int:
    quiet = args.quiet
    dest = Path(args.out).expanduser() if args.out else None
    _progress(quiet, f"fetching {args.kind} for tract={args.tract} "
                     f"patch={args.patch} band={args.band}")
    try:
        result = _archive.download_patch_file(
            args.tract, args.patch, args.band, args.kind,
            dest=dest, force=args.force, resume=not args.no_resume,
        )
    except _archive.ArchiveError as exc:
        print(f"archive error: {exc}", file=sys.stderr)
        return EXIT_FETCH_FAILURE
    _progress(quiet, f"got {result.bytes / (1024 * 1024):.1f} MB")
    print(result.path)
    return EXIT_OK


def _cmd_archive_list(args: argparse.Namespace) -> int:
    client = _archive.HscLaArchiveClient()
    try:
        files = client.list_patch_files(args.tract, args.patch, args.band)
    except _archive.ArchiveError as exc:
        print(f"archive error: {exc}", file=sys.stderr)
        return EXIT_FETCH_FAILURE
    for name in files:
        print(name)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hscla",
        description=(
            "Command-line interface to the HSC Legacy Archive (HSCLA2020). "
            "See README.md for a tour and `docs/SPEC.md` for the design."
        ),
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress friendly progress messages on stderr.",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    # coverage --------------------------------------------------------- #
    p_cov = subs.add_parser(
        "coverage",
        help="List HSC bands and coadd patches overlapping a region.",
    )
    p_cov.add_argument("ra", type=float, help="Region center RA in degrees.")
    p_cov.add_argument("dec", type=float, help="Region center Dec in degrees.")
    p_cov.add_argument("--size-deg", type=float, default=0.0,
                       help="Square box edge in degrees (default: 0, point query).")
    p_cov.add_argument("--source", choices=["server", "local"], default="server",
                       help="Where to query (default: server). "
                            "Use 'local' on machines with the Parquet mirror.")
    p_cov.add_argument("--release", default="la2020",
                       help="HSCLA release short key (only 'la2020' for now).")
    p_cov.set_defaults(func=_cmd_coverage)

    # frames ----------------------------------------------------------- #
    p_fr = subs.add_parser(
        "frames",
        help="Per-band single-CCD frame counts overlapping a region.",
    )
    p_fr.add_argument("ra", type=float)
    p_fr.add_argument("dec", type=float)
    p_fr.add_argument("--size-deg", type=float, default=0.0)
    p_fr.add_argument("--source", choices=["server", "local"], default="server")
    p_fr.add_argument("--release", default="la2020")
    p_fr.add_argument("--detailed", action="store_true",
                      help="Also dump per-frame rows (first --head, default 10).")
    p_fr.add_argument("--head", type=int, default=10,
                      help="With --detailed, how many frame rows to print.")
    p_fr.set_defaults(func=_cmd_frames)

    # cutout ----------------------------------------------------------- #
    p_co = subs.add_parser(
        "cutout",
        help="Download one FITS cutout (image / mask / variance).",
    )
    p_co.add_argument("ra", type=float)
    p_co.add_argument("dec", type=float)
    p_co.add_argument("--size-arcsec", type=float, default=108.0,
                      help="Square box edge in arcseconds (default: 108).")
    p_co.add_argument("--band", default="HSC-I",
                      help="HSC band (e.g., HSC-G/R/I/Z/Y). Default: HSC-I.")
    p_co.add_argument("--kind", default="coadd", choices=["coadd", "warp", "frame"])
    p_co.add_argument("--tract", default="any", help="Tract id or 'any' (default).")
    p_co.add_argument("--no-mask", action="store_true",
                      help="Skip the mask HDU.")
    p_co.add_argument("--no-variance", action="store_true",
                      help="Skip the variance HDU.")
    p_co.add_argument("--out", default=None,
                      help="Destination path (default: ./outputs/cutouts/<auto>.fits).")
    p_co.set_defaults(func=_cmd_cutout)

    # cutouts (batch) -------------------------------------------------- #
    p_cob = subs.add_parser(
        "cutouts",
        help="Bulk: download many cutouts from a CSV/Parquet of (ra,dec,...) rows.",
    )
    p_cob.add_argument("input", help="Input table (.csv or .parquet) with columns "
                                     "ra, dec, size_arcsec, band (plus optional kind, "
                                     "tract, with_mask, with_variance).")
    p_cob.add_argument("--out-dir", default=None,
                       help="Directory for saved files (default: ./outputs/cutouts/).")
    p_cob.set_defaults(func=_cmd_cutouts)

    # psf -------------------------------------------------------------- #
    p_ps = subs.add_parser(
        "psf",
        help="Download one PSF kernel FITS.",
    )
    p_ps.add_argument("ra", type=float)
    p_ps.add_argument("dec", type=float)
    p_ps.add_argument("--band", default="HSC-I")
    p_ps.add_argument("--kind", default="coadd", choices=["coadd", "calexp"])
    p_ps.add_argument("--tract", default="auto")
    p_ps.add_argument("--patch", default="auto")
    p_ps.add_argument("--no-centered", action="store_true",
                      help="Pass centered=false to the picker.")
    p_ps.add_argument("--out", default=None,
                      help="Destination path (default: ./outputs/psfs/<auto>.fits).")
    p_ps.set_defaults(func=_cmd_psf)

    # sql -------------------------------------------------------------- #
    p_sql = subs.add_parser(
        "sql",
        help="Run an HSCLA SQL query (preview or full submit-poll-download).",
    )
    p_sql.add_argument("query", nargs="?", default=None,
                       help="SQL text (or use --file).")
    p_sql.add_argument("--file", default=None, help="Read SQL from a file.")
    p_sql.add_argument("--preview", action="store_true",
                       help="Use the fast preview endpoint (server timeout ~5s).")
    p_sql.add_argument("--limit", type=int, default=50,
                       help="With --preview, max rows to print (default: 50).")
    p_sql.add_argument("--no-cache", action="store_true",
                       help="Bypass the SQL-result content-hash cache.")
    p_sql.add_argument("--out", default=None,
                       help="CSV destination (default: ./outputs/sql/<auto>.csv).")
    p_sql.set_defaults(func=_cmd_sql)

    # crossmatch ------------------------------------------------------- #
    p_xm = subs.add_parser(
        "crossmatch",
        help="Crossmatch a small catalog against HSCLA forced photometry.",
    )
    p_xm.add_argument("input", help="Input catalog (.csv or .parquet).")
    p_xm.add_argument("--ra-col", default="ra")
    p_xm.add_argument("--dec-col", default="dec")
    p_xm.add_argument("--id-col", default=None)
    p_xm.add_argument("--radius-arcsec", type=float, default=1.0)
    p_xm.add_argument("--extra-columns", default="",
                      help="Comma-separated list of additional forced columns.")
    p_xm.add_argument("--nearest-only", action="store_true",
                      help="Keep only the closest match per input row.")
    p_xm.add_argument("--out", default=None,
                      help="CSV destination (default: ./outputs/crossmatch/<auto>.csv).")
    p_xm.set_defaults(func=_cmd_crossmatch)

    # mirror ----------------------------------------------------------- #
    p_mi = subs.add_parser("mirror", help="Manage local Parquet mirrors.")
    mi_subs = p_mi.add_subparsers(dest="mirror_cmd", required=True)
    p_mi_build = mi_subs.add_parser("build", help="Download a table as Parquet.")
    p_mi_build.add_argument("table", choices=_mirror.SUPPORTED_TABLES)
    p_mi_build.add_argument("--force", action="store_true")
    p_mi_build.add_argument("--root", type=Path, default=None,
                            help="Override the mirror root directory.")
    p_mi_build.set_defaults(func=_cmd_mirror_build)
    p_mi_status = mi_subs.add_parser("status", help="Show which mirrors exist.")
    p_mi_status.add_argument("--root", type=Path, default=None)
    p_mi_status.set_defaults(func=_cmd_mirror_status)

    # archive ---------------------------------------------------------- #
    p_ar = subs.add_parser("archive", help="Per-patch file-tree downloads.")
    ar_subs = p_ar.add_subparsers(dest="archive_cmd", required=True)
    p_ar_dl = ar_subs.add_parser("download", help="Download one per-patch FITS.")
    p_ar_dl.add_argument("tract", type=int)
    p_ar_dl.add_argument("patch", help="HSC patch cell as 'x,y' (e.g., 1,6).")
    p_ar_dl.add_argument("band", help="HSC band (e.g., HSC-I).")
    p_ar_dl.add_argument("--kind", default="calexp",
                         choices=list(_archive.SUPPORTED_KINDS))
    p_ar_dl.add_argument("--out", default=None,
                         help="Override the default cache path.")
    p_ar_dl.add_argument("--force", action="store_true",
                         help="Re-download even if the file is cached.")
    p_ar_dl.add_argument("--no-resume", action="store_true",
                         help="Skip Range-based resume of a partial download.")
    p_ar_dl.set_defaults(func=_cmd_archive_download)
    p_ar_ls = ar_subs.add_parser("list", help="List per-patch file names.")
    p_ar_ls.add_argument("tract", type=int)
    p_ar_ls.add_argument("patch")
    p_ar_ls.add_argument("band")
    p_ar_ls.set_defaults(func=_cmd_archive_list)

    return parser


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # `quiet` lives on the top-level parser; propagate it onto the subcommand
    # namespace if the user passed it after the subcommand instead.
    if not getattr(args, "quiet", False) and getattr(args, "cmd", None):
        # argparse already merges; but be defensive for the test harness.
        args.quiet = bool(getattr(args, "quiet", False))
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except MissingCredentialsError as exc:
        print(f"hscla: {exc}", file=sys.stderr)
        return EXIT_MISSING_CREDS
    except _mirror.MirrorError as exc:
        print(f"hscla mirror: {exc}", file=sys.stderr)
        return EXIT_MIRROR_MISSING
    except _config.MirrorRootMissing as exc:
        print(f"hscla: {exc}", file=sys.stderr)
        return EXIT_MIRROR_MISSING
    except _crossmatch.CrossmatchError as exc:
        print(f"hscla crossmatch: {exc}", file=sys.stderr)
        return EXIT_BAD_ARGS
    except _sql.SqlError as exc:
        print(f"hscla sql: {exc}", file=sys.stderr)
        return EXIT_FETCH_FAILURE
    except _cutout.CutoutError as exc:
        # NoCoverageError is a subclass; cover both.
        if isinstance(exc, _cutout.NoCoverageError):
            print(f"hscla: no coverage: {exc}", file=sys.stderr)
            return EXIT_NO_COVERAGE
        print(f"hscla cutout: {exc}", file=sys.stderr)
        return EXIT_FETCH_FAILURE
    except _psf.PsfError as exc:
        print(f"hscla psf: {exc}", file=sys.stderr)
        return EXIT_FETCH_FAILURE
    except _archive.ArchiveError as exc:
        print(f"hscla archive: {exc}", file=sys.stderr)
        return EXIT_FETCH_FAILURE
    except KeyboardInterrupt:
        print("hscla: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
